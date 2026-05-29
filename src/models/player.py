class Player:
    def __init__(self, name: str):
        self.name = name
        self.points = 0.0
        self.joined_at = None
        
    def add_points(self, score: int):
        """Agrega puntos basado en score (0-100)"""
        earned = round(score / 100 * 3, 1)
        self.points += earned
        return earned
        
    def to_dict(self):
        return {"name": self.name, "points": self.points}