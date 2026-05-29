from typing import Dict, List

class QuestionHistory:
    def __init__(self, section_index: int, section_number: str, section_title: str, question: str):
        self.section_index = section_index
        self.section_number = section_number
        self.section_title = section_title
        self.question = question
        self.answers: Dict[str, dict] = {}
        self.needs_review = False
        self.wrong_players: List[str] = []
        
    def add_answer(self, player: str, answer: str, score: int, feedback: str):
        self.answers[player] = {
            "answer": answer,
            "score": score,
            "feedback": feedback
        }
        if score < 60:
            self.wrong_players.append(player)
            
    def finalize(self):
        """Calcula si la pregunta necesita repaso"""
        self.needs_review = len(self.wrong_players) > 0
        
    def to_dict(self):
        return {
            "section_index": self.section_index,
            "section_number": self.section_number,
            "section_title": self.section_title,
            "question": self.question,
            "needs_review": self.needs_review,
            "wrong_players": self.wrong_players,
            "answers": self.answers
        }