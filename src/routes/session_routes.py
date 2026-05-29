from flask import Blueprint, request, jsonify
import uuid
import time
from src.routes.room_routes import get_room, rooms
from src.models.room import Room

session_bp = Blueprint('session', __name__)

@session_bp.route('/export', methods=['POST'])
def export_session():
    data = request.json
    room = get_room(data.get('room', ''))
    if not room:
        return jsonify({"error": "Sala no encontrada"}), 404
    
    export_data = room.to_export_dict()
    export_data["exported_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    export_data["room_code"] = room.code
    
    return jsonify(export_data)

@session_bp.route('/import', methods=['POST'])
def import_session():
    data = request.json
    session_data = data.get('session')
    host_name = data.get('host_name', '').strip()
    
    if not session_data:
        return jsonify({"error": "No hay datos de sesión"}), 400
    if not host_name:
        return jsonify({"error": "Nombre del host requerido"}), 400
    
    # Crear nueva sala
    code = str(uuid.uuid4())[:4].upper()
    while code in rooms:
        code = str(uuid.uuid4())[:4].upper()
    
    room = Room(code)
    room.import_from_dict(session_data)
    
    # Asegurar que el host está en la sala
    if host_name not in room.players:
        room.add_player(host_name)
    
    rooms[code] = room
    
    return jsonify({
        "room": code,
        "sections": [{"number": s["number"], "title": s["title"]} for s in room.sections],
        "players": {p.name: p.points for p in room.players.values()},
        "restored_questions": len(room.question_history)
    })