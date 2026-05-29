from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
import cohere
import os
import re
import json
import uuid
import time
from difflib import SequenceMatcher

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
        "current_ideal_answer": "",
        "current_section_text": "",
        "players": {},
        "answers": {},
        "section_stats": {},
        "question_history": [],
        "pending_review": [],
        "phase": "setup",
        "player_names": [],
        "connected": {},
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
    if room["pending_review"]:
        from collections import Counter
        freq = Counter(room["pending_review"])
        return freq.most_common(1)[0][0]
    return best_idx if best_idx is not None else 0

def calculate_backup_score(answer, ideal_answer, question):
    if not answer or answer.strip() == "":
        return 0, "No respondió a tiempo."
    
    answer_lower = answer.lower()
    ideal_lower = ideal_answer.lower()
    question_lower = question.lower()
    
    text_similarity = SequenceMatcher(None, answer_lower, ideal_lower).ratio() * 100
    
    important_words = ["porque", "ya que", "debido a", "principalmente", "además", "también", 
                      "ejemplo", "como", "cuando", "donde", "para", "mediante", "a través de"]
    important_word_score = 0
    for word in important_words:
        if word in answer_lower:
            important_word_score += 5
    important_word_score = min(important_word_score, 30)
    
    length_score = min(len(answer.split()) / 20 * 100, 100)
    if len(answer.split()) < 5:
        length_score = max(length_score, 20)
    
    question_keywords = set(re.findall(r'\b\w{4,}\b', question_lower))
    answer_keywords = set(re.findall(r'\b\w{4,}\b', answer_lower))
    if question_keywords:
        keyword_overlap = len(answer_keywords.intersection(question_keywords)) / len(question_keywords) * 100
    else:
        keyword_overlap = 50
    
    final_score = (text_similarity * 0.4 + length_score * 0.2 + keyword_overlap * 0.25 + important_word_score * 0.15)
    final_score = max(0, min(100, round(final_score)))
    
    if final_score >= 85:
        feedback = "Excelente respuesta. Muy completa y precisa."
    elif final_score >= 70:
        feedback = "Buena respuesta. Tiene los conceptos principales, podría profundizar un poco más."
    elif final_score >= 50:
        feedback = "Respuesta aceptable, pero le faltan detalles importantes."
    elif final_score >= 30:
        feedback = "Respuesta limitada. Revisa el material para complementar."
    else:
        feedback = "Respuesta insuficiente. Te recomendamos repasar esta sección."
    
    return final_score, feedback

def evaluate_answer_with_variation(text, question, ideal_answer, student_answer):
    if not student_answer or student_answer.strip() == "" or student_answer.strip() == "[Tiempo agotado]":
        return 0, "No respondió a tiempo."
    
    prompt = f"""Eres un profesor experto evaluando respuestas de estudiantes.

Texto de referencia:
{text}

Pregunta:
{question}

Respuesta ideal (modelo):
{ideal_answer}

Respuesta del estudiante:
{student_answer}

IMPORTANTE: Usa TODA la escala del 0 al 100. NO uses solo 0, 50 o 100.
- 90-100: Excelente, muy completa y precisa
- 75-89: Buena, con algunos detalles menores faltantes
- 60-74: Aceptable, pero le faltan conceptos clave
- 40-59: Limitada, varios errores o información incompleta
- 20-39: Muy deficiente, apenas toca el tema
- 0-19: Incorrecta o no responde la pregunta

Responde SOLO en formato JSON exacto:
{{"score": <número entre 0 y 100, usa decimales si es necesario>, "feedback": "<explicación breve del puntaje>"}}

Ejemplos de puntuaciones válidas: 45, 62, 78, 83, 91, 37, 55, 68, 74, 88, 94
"""
    try:
        response = co.chat(
            model="command-r-plus-08-2024",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        raw = response.message.content[0].text.strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        result = json.loads(raw)
        score = float(result.get("score", 0))
        feedback = result.get("feedback", "")
        
        if score in [0, 50, 100] and len(student_answer.split()) > 10:
            backup_score, backup_feedback = calculate_backup_score(student_answer, ideal_answer, question)
            score = round((score + backup_score) / 2)
            feedback = f"{feedback} (Evaluación ajustada: {backup_feedback})"
        
        score = max(0, min(100, round(score, 1)))
        if score == 0 and student_answer.strip():
            backup_score, backup_feedback = calculate_backup_score(student_answer, ideal_answer, question)
            if backup_score > 0:
                score = backup_score
                feedback = backup_feedback
        
        return score, feedback
    except Exception as e:
        print(f"Error en evaluación IA: {e}")
        return calculate_backup_score(student_answer, ideal_answer, question)

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
        # Valores por defecto para tipo de sección
        s.setdefault("type", "theory")
        s.setdefault("ideal_answer", "")
    problems = validate_sections(sections)
    room["sections"] = sections
    room["current_section_index"] = 0
    room["answers"] = {}
    if problems:
        warning = " | ".join(problems)
        return jsonify({
            "sections": [{"number": s["number"], "title": s["title"], "type": s.get("type", "theory")} for s in sections],
            "warning": warning,
            "usable_sections": len([s for s in sections if s.get("content", "").strip()]),
        })
    return jsonify({
        "sections": [{"number": s["number"], "title": s["title"], "type": s.get("type", "theory")} for s in sections]
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
        "type": section.get("type", "theory"),
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
    
    current_index = room["current_section_index"]
    if current_index >= len(room["sections"]):
        return jsonify({"error": "No hay sección activa"}), 400
    
    current_section = room["sections"][current_index]
    text = room.get("current_section_text", "").strip()
    
    # Si es tipo "exercise", usar directamente el contenido como pregunta
    if current_section.get("type") == "exercise":
        question = text
        ideal_answer = current_section.get("ideal_answer", "")
        if not ideal_answer:
            ideal_answer = "Respuesta modelo no proporcionada."
        room["current_question"] = question
        room["current_ideal_answer"] = ideal_answer
        room["answers"] = {}
        room["phase"] = "answering"
        return jsonify({"question": question, "ideal_answer": ideal_answer})
    
    # Si es tipo "theory" (default), generar pregunta con IA
    if not text:
        label = f"'{current_section.get('number','')} {current_section.get('title','')}'"
        return jsonify({"error": f"La sección {label} no tiene texto legible."}), 400

    past_qs = [h["question"] for h in room["question_history"] if h.get("section_index") == current_index]
    history_context = ""
    if past_qs:
        history_context = "\n\nPreguntas ya hechas sobre esta sección (NO las repitas):\n" + "\n".join(f"- {q}" for q in past_qs)

    prompt = f"""Lee el siguiente fragmento de texto y genera:
1. Una pregunta de comprensión clara y directa.
2. Una respuesta ideal (modelo) para esa pregunta.

Devuelve SOLO un objeto JSON con dos campos: "question" y "ideal_answer".

Texto:
{text}
{history_context}
"""
    response = co.chat(
        model="command-r-plus-08-2024",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )
    raw = response.message.content[0].text.strip()
    try:
        raw = re.sub(r"```json|```", "", raw).strip()
        result = json.loads(raw)
        question = result.get("question", "")
        ideal_answer = result.get("ideal_answer", "")
    except Exception:
        question = "No se pudo generar la pregunta."
        ideal_answer = "No disponible."

    room["current_question"] = question
    room["current_ideal_answer"] = ideal_answer
    room["answers"] = {}
    room["phase"] = "answering"
    return jsonify({"question": question, "ideal_answer": ideal_answer})

@app.route("/answer", methods=["POST"])
def submit_answer():
    data = request.json
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    player = data.get("player", "").strip()
    answer = data.get("answer", "").strip()
    if not player:
        return jsonify({"error": "Falta el nombre del jugador"}), 400
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
        "ideal_answer": room.get("current_ideal_answer", ""),
    })

@app.route("/extend_time", methods=["POST"])
def extend_time():
    data = request.json
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    if room["phase"] != "answering":
        return jsonify({"error": "Solo se puede extender el tiempo durante la fase de respuesta"}), 400
    return jsonify({"ok": True, "extra_seconds": 30})

@app.route("/evaluate", methods=["POST"])
def evaluate():
    try:
        return _evaluate_inner()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/force_evaluate", methods=["POST"])
def force_evaluate():
    data = request.json or {}
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404

    question = room.get("current_question", "")
    ideal_answer = room.get("current_ideal_answer", "")
    text = room.get("current_section_text", "")
    answers = room.get("answers", {})

    for player in room["player_names"]:
        if player not in answers:
            answers[player] = ""

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
            score = 0
            feedback = "No respondió a tiempo."
            points = 0
        else:
            score, feedback = evaluate_answer_with_variation(text, question, ideal_answer, answer)
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

    history_entry = {
        "section_index": section_idx,
        "section_number": section_key,
        "section_title": section["title"],
        "question": question,
        "ideal_answer": ideal_answer,
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
        "question": question,
        "ideal_answer": ideal_answer,
    }
    room["phase"] = "results"

    return jsonify({
        "results": results,
        "scoreboard": room["players"],
        "has_pending_review": len(room["pending_review"]) > 0,
        "question": question,
        "ideal_answer": ideal_answer,
    })

def _evaluate_inner():
    data = request.json or {}
    code = data.get("room", "").upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    question = room.get("current_question", "")
    ideal_answer = room.get("current_ideal_answer", "")
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
        score, feedback = evaluate_answer_with_variation(text, question, ideal_answer, answer)
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

    history_entry = {
        "section_index": section_idx,
        "section_number": section_key,
        "section_title": section["title"],
        "question": question,
        "ideal_answer": ideal_answer,
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
        "question": question,
        "ideal_answer": ideal_answer,
    }
    room["phase"] = "results"
    return jsonify({
        "results": results,
        "scoreboard": room["players"],
        "has_pending_review": len(room["pending_review"]) > 0,
        "question": question,
        "ideal_answer": ideal_answer,
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
    for name, pts in session.get("players", []):
        room["players"][name] = pts
        if name not in room["player_names"]:
            room["player_names"].append(name)
    room["question_history"] = session.get("question_history", [])
    pending_set = set()
    for h in room["question_history"]:
        if h.get("needs_review", False):
            pending_set.add(h.get("section_index", 0))
    room["pending_review"] = list(pending_set)
    room["section_stats"] = {}
    for h in room["question_history"]:
        key = h["section_number"]
        if key not in room["section_stats"]:
            room["section_stats"][key] = {"attempts": 0, "wrong": 0}
        scores = [a["score"] for a in h["answers"].values()]
        room["section_stats"][key]["attempts"] += len(scores)
        room["section_stats"][key]["wrong"] += sum(1 for s in scores if s < 60)
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