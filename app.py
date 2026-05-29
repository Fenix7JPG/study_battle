from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
import cohere
import os
import re
import json
import uuid
import time

load_dotenv()

app = Flask(__name__, static_folder="static")
COHERE_API_KEY = os.environ.get("COHERE_API_KEY")
if not COHERE_API_KEY:
    raise ValueError("Falta COHERE_API_KEY en el archivo .env")
co = cohere.ClientV2(COHERE_API_KEY)

# ─── Salas en memoria ──────────────────────────────────────────────────────
rooms = {}

def new_room():
    return {
        "sections": [],
        "current_section_index": 0,
        "current_question": "",
        "current_section_text": "",
        "players": {},          # nombre -> puntos totales
        "answers": {},          # nombre -> respuesta actual
        "section_stats": {},    # número_seccion -> {"attempts": int, "wrong": int}
        "question_history": [], # lista de dicts con pregunta, respuestas, scores
        "pending_review": [],   # índices de secciones que necesitan repaso
        "phase": "setup",       # setup, reading, answering, results
        "player_names": [],     # lista de nombres
        "connected": {},        # nombre -> timestamp
        "last_results_cache": None,
    }

def get_room(code):
    return rooms.get(code.upper())

# ─── Helpers ──────────────────────────────────────────────────────────────
def validate_sections(sections):
    problems = []
    if not sections:
        problems.append("El JSON está vacío. Debe ser una lista con al menos una sección.")
        return problems
    empty = [f"'{s.get('number','?')} {s.get('title','?')}'" for s in sections if not s.get("content", "").strip()]
    if empty:
        problems.append(f"{len(empty)} sección(es) sin contenido: {', '.join(empty[:5])}.")
    return problems

def weakest_section(room):
    """Índice de la sección con mayor ratio de respuestas incorrectas (score<60)"""
    stats = room["section_stats"]
    sections = room["sections"]
    best_idx = None
    best_ratio = -1
    for i, s in enumerate(sections):
        key = s["number"]
        if key in stats and stats[key]["attempts"] > 0:
            ratio = stats[key]["wrong"] / stats[key]["attempts"]
            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = i
    # Si hay secciones pendientes de repaso, priorizar la que más se repite
    if room["pending_review"]:
        from collections import Counter
        freq = Counter(room["pending_review"])
        return freq.most_common(1)[0][0]
    return best_idx if best_idx is not None else 0

# ─── Rutas ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/create_room", methods=["POST"])
def create_room():
    code = str(uuid.uuid4())[:4].upper()
    while code in rooms:
        code = str(uuid.uuid4())[:4].upper()
    rooms[code] = new_room()
    return jsonify({"room": code})

@app.route("/join_room", methods=["POST"])
def join_room():
    data = request.json
    code = data.get("room", "").upper()
    name = data.get("name", "").strip()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    if not name:
        return jsonify({"error": "Falta el nombre"}), 400
    if name not in room["player_names"]:
        room["player_names"].append(name)
        room["players"][name] = 0
    room["connected"][name] = time.time()
    return jsonify({"ok": True, "room": code, "name": name, "phase": room["phase"]})

@app.route("/upload", methods=["POST"])
def upload():
    code = request.form.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file"}), 400
    try:
        sections = json.load(file)
    except Exception as e:
        return jsonify({"error": f"No se pudo leer el JSON: {str(e)}"}), 400
    if not isinstance(sections, list):
        return jsonify({"error": "El JSON debe ser una lista de secciones"}), 400
    for i, s in enumerate(sections):
        if not isinstance(s, dict) or "title" not in s or "content" not in s:
            return jsonify({"error": f"Sección {i+1}: faltan 'title' o 'content'"}), 400
        if "number" not in s:
            s["number"] = str(i + 1)
    problems = validate_sections(sections)
    room["sections"] = sections
    room["current_section_index"] = 0
    room["answers"] = {}
    if problems:
        warning = " | ".join(problems)
        return jsonify({
            "sections": [{"number": s["number"], "title": s["title"]} for s in sections],
            "warning": warning,
            "usable_sections": len([s for s in sections if s.get("content", "").strip()]),
        })
    return jsonify({
        "sections": [{"number": s["number"], "title": s["title"]} for s in sections]
    })

@app.route("/section", methods=["POST"])
def get_section():
    data = request.json
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    index = data.get("index", 0)
    if index >= len(room["sections"]):
        return jsonify({"error": "No hay más secciones"}), 400
    section = room["sections"][index]
    room["current_section_index"] = index
    room["current_section_text"] = section["content"]
    room["answers"] = {}
    room["phase"] = "reading"
    return jsonify({
        "number": section["number"],
        "title": section["title"],
        "content": section["content"],
    })

@app.route("/weakest", methods=["POST"])
def get_weakest():
    data = request.json
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    idx = weakest_section(room)
    return jsonify({"index": idx})

@app.route("/question", methods=["POST"])
def generate_question():
    data = request.json
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    text = room.get("current_section_text", "").strip()
    if not text:
        section = room["sections"][room["current_section_index"]] if room["sections"] else {}
        label = f"'{section.get('number','')} {section.get('title','')}'" if section else "desconocida"
        return jsonify({"error": f"La sección {label} no tiene texto legible."}), 400
    # Historial de preguntas de esta misma sección para evitar repetición
    past_qs = [h["question"] for h in room["question_history"] if h.get("section_index") == room["current_section_index"]]
    history_context = ""
    if past_qs:
        history_context = "\n\nPreguntas ya hechas sobre esta sección (NO las repitas):\n" + "\n".join(f"- {q}" for q in past_qs)
    response = co.chat(
        model="command-r-plus-08-2024",
        messages=[{
            "role": "user",
            "content": f"Lee el siguiente fragmento de texto y genera UNA sola pregunta de comprensión clara y directa. Solo escribe la pregunta, sin numeración ni explicación.\n\n{text}{history_context}"
        }]
    )
    question = response.message.content[0].text.strip()
    room["current_question"] = question
    room["answers"] = {}
    room["phase"] = "answering"
    return jsonify({"question": question})

@app.route("/answer", methods=["POST"])
def submit_answer():
    data = request.json
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    player = data.get("player", "").strip()
    answer = data.get("answer", "").strip()
    if not player or not answer:
        return jsonify({"error": "Faltan datos"}), 400
    room["answers"][player] = answer
    room["connected"][player] = time.time()
    total_players = len(room["player_names"])
    answered = len(room["answers"])
    return jsonify({
        "ok": True,
        "players_answered": list(room["answers"].keys()),
        "player_names": room["player_names"],
        "all_answered": answered >= total_players,
    })

@app.route("/poll", methods=["POST"])
def poll():
    data = request.json
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    return jsonify({
        "phase": room["phase"],
        "players_answered": list(room["answers"].keys()),
        "all_answered": len(room["answers"]) >= len(room["player_names"]),
        "player_names": room["player_names"],
        "question": room.get("current_question", ""),
    })

@app.route("/evaluate", methods=["POST"])
def evaluate():
    try:
        return _evaluate_inner()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
# Añadir esta función después de /evaluate (o dentro del mismo)
@app.route("/force_evaluate", methods=["POST"])
def force_evaluate():
    """Evalúa la pregunta actual aunque no hayan respondido todos los jugadores.
       Los que no respondieron obtienen score 0 y feedback 'No respondió'.
    """
    data = request.json or {}
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    
    # Verificar que el que pide es el host (no hay autenticación, pero podemos asumir que solo el host llama)
    # Lo dejamos sin restricción extra por simplicidad.
    
    question = room.get("current_question", "")
    text = room.get("current_section_text", "")
    answers = room.get("answers", {})
    
    # Completar respuestas faltantes con vacío para todos los jugadores
    for player in room["player_names"]:
        if player not in answers:
            answers[player] = ""  # respuesta vacía
    
    # Ahora evaluamos igual que en /evaluate, pero con todas las respuestas
    if not answers:
        return jsonify({"error": "No hay respuestas"}), 400
    
    section = room["sections"][room["current_section_index"]]
    section_key = section["number"]
    section_idx = room["current_section_index"]
    
    if section_key not in room["section_stats"]:
        room["section_stats"][section_key] = {"attempts": 0, "wrong": 0}
    
    results = {}
    any_wrong = False
    
    for player, answer in answers.items():
        if answer.strip() == "":
            # No respondió
            score = 0
            feedback = "No respondió a tiempo."
            points = 0
        else:
            # Evaluar con IA
            prompt = f"Texto de referencia:\n{text}\n\nPregunta: {question}\n\nRespuesta del estudiante: {answer}\n\nEvalúa la respuesta del 0 al 100 según qué tan correcta y completa es. Responde SOLO en este formato JSON exacto:\n{{ \"score\": <número 0-100>, \"feedback\": \"<una oración explicando el puntaje>\" }}"
            response = co.chat(
                model="command-r-plus-08-2024",
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.message.content[0].text.strip()
            try:
                raw = re.sub(r"```json|```", "", raw).strip()
                result = json.loads(raw)
            except Exception:
                result = {"score": 0, "feedback": "Error en evaluación."}
            score = result.get("score", 0)
            feedback = result.get("feedback", "")
            points = round(score / 100 * 3, 1)
        
        room["players"][player] = round(room["players"].get(player, 0) + points, 1)
        results[player] = {
            "answer": answer if answer else "(no respondió)",
            "score": score,
            "feedback": feedback,
            "points_earned": points,
            "total_points": room["players"][player],
        }
        room["section_stats"][section_key]["attempts"] += 1
        if score < 60:
            room["section_stats"][section_key]["wrong"] += 1
            any_wrong = True
    
    # Guardar en historial
    history_entry = {
        "section_index": section_idx,
        "section_number": section_key,
        "section_title": section["title"],
        "question": question,
        "needs_review": any_wrong,
        "answers": {
            p: {
                "answer": results[p]["answer"],
                "score": results[p]["score"],
                "feedback": results[p]["feedback"],
                "points_earned": results[p]["points_earned"],
                "total_points": results[p]["total_points"],
            }
            for p in results
        },
    }
    room["question_history"].append(history_entry)
    
    # Actualizar pending_review
    if any_wrong:
        if section_idx not in room["pending_review"]:
            room["pending_review"].append(section_idx)
    else:
        if section_idx in room["pending_review"]:
            room["pending_review"].remove(section_idx)
    
    room["last_results_cache"] = {
        "results": results,
        "scoreboard": room["players"],
        "has_pending_review": len(room["pending_review"]) > 0,
    }
    room["phase"] = "results"
    
    return jsonify({
        "results": results,
        "scoreboard": room["players"],
        "has_pending_review": len(room["pending_review"]) > 0,
    })
def _evaluate_inner():
    data = request.json or {}
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    question = room.get("current_question", "")
    text = room.get("current_section_text", "")
    answers = room.get("answers", {})
    if not answers:
        return jsonify({"error": "No hay respuestas guardadas"}), 400
    section = room["sections"][room["current_section_index"]]
    section_key = section["number"]
    section_idx = room["current_section_index"]
    if section_key not in room["section_stats"]:
        room["section_stats"][section_key] = {"attempts": 0, "wrong": 0}
    results = {}
    any_wrong = False
    for player, answer in answers.items():
        prompt = f"Texto de referencia:\n{text}\n\nPregunta: {question}\n\nRespuesta del estudiante: {answer}\n\nEvalúa la respuesta del 0 al 100 según qué tan correcta y completa es. Responde SOLO en este formato JSON exacto:\n{{ \"score\": <número 0-100>, \"feedback\": \"<una oración explicando el puntaje>\" }}"
        response = co.chat(
            model="command-r-plus-08-2024",
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.message.content[0].text.strip()
        try:
            raw = re.sub(r"```json|```", "", raw).strip()
            result = json.loads(raw)
        except Exception:
            result = {"score": 0, "feedback": "No se pudo evaluar la respuesta."}
        score = result.get("score", 0)
        feedback = result.get("feedback", "")
        points = round(score / 100 * 3, 1)
        room["players"][player] = round(room["players"].get(player, 0) + points, 1)
        results[player] = {
            "answer": answer,
            "score": score,
            "feedback": feedback,
            "points_earned": points,
            "total_points": room["players"][player],
        }
        room["section_stats"][section_key]["attempts"] += 1
        if score < 60:
            room["section_stats"][section_key]["wrong"] += 1
            any_wrong = True
    # Guardar en historial
    history_entry = {
        "section_index": section_idx,
        "section_number": section_key,
        "section_title": section["title"],
        "question": question,
        "needs_review": any_wrong,
        "answers": {
            p: {
                "answer": answers[p],
                "score": results[p]["score"],
                "feedback": results[p]["feedback"],
                "points_earned": results[p]["points_earned"],
                "total_points": results[p]["total_points"],
            }
            for p in answers
        },
    }
    room["question_history"].append(history_entry)
    # Actualizar pending_review
    if any_wrong:
        if section_idx not in room["pending_review"]:
            room["pending_review"].append(section_idx)
    else:
        if section_idx in room["pending_review"]:
            room["pending_review"].remove(section_idx)
    room["last_results_cache"] = {
        "results": results,
        "scoreboard": room["players"],
        "has_pending_review": len(room["pending_review"]) > 0,
    }
    room["phase"] = "results"
    return jsonify({
        "results": results,
        "scoreboard": room["players"],
        "has_pending_review": len(room["pending_review"]) > 0,
    })

@app.route("/history", methods=["POST"])
def get_history():
    data = request.json
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    return jsonify({"history": room["question_history"]})

@app.route("/current_section", methods=["POST"])
def current_section():
    data = request.json
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    idx = room["current_section_index"]
    if idx >= len(room["sections"]):
        return jsonify({"error": "No hay sección activa"}), 400
    section = room["sections"][idx]
    return jsonify({
        "number": section["number"],
        "title": section["title"],
        "content": section["content"],
    })

@app.route("/last_results", methods=["POST"])
def last_results():
    data = request.json
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    cache = room.get("last_results_cache")
    if not cache:
        return jsonify({"error": "Sin resultados aún"}), 400
    return jsonify(cache)

@app.route("/scoreboard", methods=["POST"])
def scoreboard():
    data = request.json
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    return jsonify({"scoreboard": room["players"]})

@app.route("/export_session", methods=["POST"])
def export_session():
    data = request.json
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    export = {
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "room_code": code,
        "players": [[name, pts] for name, pts in room["players"].items()],
        "question_history": room["question_history"],
        "pending_review": room["pending_review"],
        "sections": room["sections"],
    }
    return jsonify(export)

@app.route("/import_session", methods=["POST"])
def import_session():
    data = request.json
    session = data.get("session")
    host_name = data.get("host_name", "").strip()
    if not session:
        return jsonify({"error": "No hay datos de sesión"}), 400
    if not host_name:
        return jsonify({"error": "Falta el nombre del host"}), 400
    code = str(uuid.uuid4())[:4].upper()
    while code in rooms:
        code = str(uuid.uuid4())[:4].upper()
    room = new_room()
    room["sections"] = session.get("sections", [])
    # Restaurar jugadores
    for name, pts in session.get("players", []):
        room["players"][name] = pts
        if name not in room["player_names"]:
            room["player_names"].append(name)
    # Restaurar historial y reconstruir pending_review
    room["question_history"] = session.get("question_history", [])
    pending_set = set()
    for h in room["question_history"]:
        if h.get("needs_review", False):
            pending_set.add(h.get("section_index", 0))
    room["pending_review"] = list(pending_set)
    # Reconstruir section_stats
    room["section_stats"] = {}
    for h in room["question_history"]:
        key = h["section_number"]
        if key not in room["section_stats"]:
            room["section_stats"][key] = {"attempts": 0, "wrong": 0}
        scores = [a["score"] for a in h["answers"].values()]
        room["section_stats"][key]["attempts"] += len(scores)
        room["section_stats"][key]["wrong"] += sum(1 for s in scores if s < 60)
    # Añadir host si no estaba
    if host_name not in room["player_names"]:
        room["player_names"].append(host_name)
        room["players"].setdefault(host_name, 0)
    room["connected"][host_name] = time.time()
    rooms[code] = room
    return jsonify({
        "room": code,
        "sections": [{"number": s["number"], "title": s["title"]} for s in room["sections"]],
        "players": room["players"],
        "restored_questions": len(room["question_history"]),
    })

@app.route("/static/style.css")
def serve_css():
    return send_from_directory("static", "style.css")

if __name__ == "__main__":
    app.run(debug=True, port=5000)