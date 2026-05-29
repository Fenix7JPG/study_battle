from flask import Blueprint, request, jsonify
import uuid
from src.models.room import Room

room_bp = Blueprint('rooms', __name__)

# Almacenamiento en memoria
rooms: dict = {}

def get_room(code: str) -> Room:
    return rooms.get(code.upper())

@room_bp.route('/create', methods=['POST'])
def create_room():
    code = str(uuid.uuid4())[:4].upper()
    while code in rooms:
        code = str(uuid.uuid4())[:4].upper()
    rooms[code] = Room(code)
    return jsonify({"room": code})

@room_bp.route('/join', methods=['POST'])
def join_room():
    data = request.json
    code = data.get('room', '').upper()
    name = data.get('name', '').strip()
    
    room = get_room(code)
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    
    if not name:
        return jsonify({"error": "Nombre requerido"}), 400
    
    room.add_player(name)
    return jsonify({"ok": True, "room": code, "phase": room.phase})

@room_bp.route('/poll', methods=['POST'])
def poll():
    data = request.json
    room = get_room(data.get('room', ''))
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    
    return jsonify({
        "phase": room.phase,
        "players_answered": list(room.answers.keys()),
        "all_answered": len(room.answers) >= len(room.players),
        "player_names": room.get_player_names(),
        "question": room.current_question
    })

@room_bp.route('/weakest', methods=['POST'])
def weakest_section():
    data = request.json
    room = get_room(data.get('room', ''))
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    
    return jsonify({"index": room.get_weakest_section()})