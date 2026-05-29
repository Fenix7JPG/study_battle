from typing import Dict, List, Optional
from src.models.player import Player
from src.models.question import QuestionHistory

class Room:
    def __init__(self, code: str):
        self.code = code
        self.sections: List[dict] = []
        self.players: Dict[str, Player] = {}
        self.current_section_index = 0
        self.current_section_text = ""
        self.current_question = ""
        self.answers: Dict[str, str] = {}
        self.question_history: List[QuestionHistory] = []
        self.section_stats: Dict[str, dict] = {}
        self.pending_review: List[int] = []
        self.phase: str = "setup"  # setup, reading, answering, results
        self.last_results_cache: Optional[dict] = None
        
    def add_player(self, name: str) -> Player:
        if name not in self.players:
            self.players[name] = Player(name)
        return self.players[name]
    
    def get_player_names(self) -> List[str]:
        return list(self.players.keys())
    
    def get_scoreboard(self) -> List[tuple]:
        return sorted([(p.name, p.points) for p in self.players.values()], 
                     key=lambda x: -x[1])
    
    def update_section_stats(self, section_number: str, scores: List[int]):
        if section_number not in self.section_stats:
            self.section_stats[section_number] = {"attempts": 0, "wrong": 0}
        
        self.section_stats[section_number]["attempts"] += len(scores)
        self.section_stats[section_number]["wrong"] += sum(1 for s in scores if s < 60)
    
    def get_weakest_section(self) -> int:
        """Devuelve el índice de la sección con más errores"""
        best_idx = 0
        best_ratio = -1
        
        for i, section in enumerate(self.sections):
            key = section.get("number", str(i))
            stats = self.section_stats.get(key, {"attempts": 0, "wrong": 0})
            
            if stats["attempts"] > 0:
                ratio = stats["wrong"] / stats["attempts"]
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_idx = i
        
        # Si hay secciones pendientes de repaso, priorizar la que más veces aparece
        if self.pending_review:
            from collections import Counter
            freq = Counter(self.pending_review)
            return freq.most_common(1)[0][0]
        
        return best_idx
    
    def to_export_dict(self) -> dict:
        return {
            "sections": self.sections,
            "players": [(p.name, p.points) for p in self.players.values()],
            "question_history": [q.to_dict() for q in self.question_history],
            "pending_review": self.pending_review,
            "section_stats": self.section_stats
        }
    
    def import_from_dict(self, data: dict):
        """Restaura estado desde exportación"""
        self.sections = data.get("sections", [])
        self.pending_review = data.get("pending_review", [])
        
        # Restaurar jugadores
        for name, points in data.get("players", []):
            player = self.add_player(name)
            player.points = points
        
        # Restaurar historial y reconstruir stats
        self.section_stats = {}
        self.question_history = []
        
        for h in data.get("question_history", []):
            qh = QuestionHistory(
                h["section_index"],
                h["section_number"],
                h["section_title"],
                h["question"]
            )
            for player, answer_data in h["answers"].items():
                qh.add_answer(
                    player,
                    answer_data["answer"],
                    answer_data["score"],
                    answer_data["feedback"]
                )
            qh.finalize()
            self.question_history.append(qh)
            
            # Reconstruir stats
            scores = [a["score"] for a in h["answers"].values()]
            self.update_section_stats(h["section_number"], scores)