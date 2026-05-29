from flask import Blueprint, request, jsonify
from src.routes.room_routes import get_room, rooms
from src.services.ai_service import ai_service
from src.models.question import QuestionHistory
import time
game_bp = Blueprint('game', __name__)

@game_bp.route('/upload', methods=['POST'])
def upload_sections():
    code = request.form.get('room', '').upper()
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    
    file = request.files.get('file')
    if not file:
        return jsonify({"error": "No file"}), 400
    
    import json
    try:
        sections = json.load(file)
    except Exception as e:
        return jsonify({"error": f"JSON inválido: {e}"}), 400
    
    # Validar formato
    for i, s in enumerate(sections):
        if "title" not in s or "content" not in s:
            return jsonify({"error": f"Sección {i+1}: faltan 'title' o 'content'"}), 400
        if "number" not in s:
            s["number"] = str(i + 1)
    
    room.sections = sections
    room.current_section_index = 0
    
    return jsonify({
        "sections": [{"number": s["number"], "title": s["title"]} for s in sections]
    })

@game_bp.route('/section/load', methods=['POST'])
def load_section():
    data = request.json
    room = get_room(data.get('room', ''))
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    
    index = data.get('index', 0)
    if index >= len(room.sections):
        return jsonify({"error": "Sección inválida"}), 400
    
    section = room.sections[index]
    room.current_section_index = index
    room.current_section_text = section["content"]
    room.answers = {}
    room.phase = "reading"
    
    return jsonify({
        "number": section["number"],
        "title": section["title"],
        "content": section["content"]
    })

@game_bp.route('/question/generate', methods=['POST'])
def generate_question():
    data = request.json
    room = get_room(data.get('room', ''))
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    
    text = room.current_section_text
    if not text:
        return jsonify({"error": "No hay texto cargado"}), 400
    
    # Obtener preguntas previas de esta sección
    past_questions = [
        h.question for h in room.question_history
        if h.section_index == room.current_section_index
    ]
    
    try:
        question = ai_service.generate_question(text, past_questions)
        room.current_question = question
        room.answers = {}
        room.phase = "answering"
        return jsonify({"question": question})
    except Exception as e:
        return jsonify({"error": f"Error generando pregunta: {e}"}), 500
# En main.py, modifica la ruta /answer para incluir player_names en la respuesta
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
        "player_names": room["player_names"],   # <- añadido
        "all_answered": answered >= total_players,
    })

@game_bp.route('/question/evaluate', methods=['POST'])
def evaluate_question():
    data = request.json
    room = get_room(data.get('room', ''))
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    
    if not room.answers:
        return jsonify({"error": "No hay respuestas"}), 400
    
    section = room.sections[room.current_section_index]
    qh = QuestionHistory(
        room.current_section_index,
        section["number"],
        section["title"],
        room.current_question
    )
    
    scores = []
    results = {}
    
    for player, answer in room.answers.items():
        score, feedback = ai_service.evaluate_answer(
            room.current_question,
            room.current_section_text,
            answer
        )
        
        points = round(score / 100 * 3, 1)
        room.players[player].add_points(score)
        
        qh.add_answer(player, answer, score, feedback)
        scores.append(score)
        
        results[player] = {
            "answer": answer,
            "score": score,
            "feedback": feedback,
            "points_earned": points,
            "total_points": room.players[player].points
        }
    
    qh.finalize()
    room.question_history.append(qh)
    room.update_section_stats(section["number"], scores)
    
    # Actualizar pending_review
    if qh.needs_review:
        if room.current_section_index not in room.pending_review:
            room.pending_review.append(room.current_section_index)
    else:
        if room.current_section_index in room.pending_review:
            room.pending_review.remove(room.current_section_index)
    
    room.last_results_cache = {
        "results": results,
        "scoreboard": {p.name: p.points for p in room.players.values()},
        "has_pending_review": len(room.pending_review) > 0
    }
    
    room.phase = "results"
    
    return jsonify({
        "results": results,
        "scoreboard": {p.name: p.points for p in room.players.values()},
        "has_pending_review": len(room.pending_review) > 0
    })

@game_bp.route('/history', methods=['POST'])
def get_history():
    data = request.json
    room = get_room(data.get('room', ''))
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    
    return jsonify({"history": [h.to_dict() for h in room.question_history]})

@game_bp.route('/results/last', methods=['POST'])
def last_results():
    data = request.json
    room = get_room(data.get('room', ''))
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    
    if not room.last_results_cache:
        return jsonify({"error": "Sin resultados"}), 400
    
    return jsonify(room.last_results_cache)