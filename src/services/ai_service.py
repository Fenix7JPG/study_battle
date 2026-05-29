import cohere
import os
import re
import json

class AIService:
    def __init__(self):
        api_key = os.environ.get("COHERE_API_KEY")
        if not api_key:
            raise ValueError("Falta COHERE_API_KEY en .env")
        self.client = cohere.ClientV2(api_key)
    
    def generate_question(self, text: str, previous_questions: list = None) -> str:
        """Genera una pregunta sobre el texto"""
        context = ""
        if previous_questions:
            context = "\n\nPreguntas anteriores (NO las repitas):\n" + \
                      "\n".join(f"- {q}" for q in previous_questions)
        
        response = self.client.chat(
            model="command-r-plus-08-2024",
            messages=[{
                "role": "user",
                "content": f"""Lee este texto y genera UNA sola pregunta de comprensión clara y directa.

Texto:
{text}{context}

Reglas:
- Solo escribe la pregunta, sin numeración ni explicación
- No repitas preguntas anteriores si las hay
- Sé específico y conciso"""
            }]
        )
        
        return response.message.content[0].text.strip()
    
    def evaluate_answer(self, question: str, text: str, answer: str) -> tuple:
        """Evalúa respuesta y devuelve (score, feedback)"""
        prompt = f"""Evalúa esta respuesta del 0 al 100.

Texto de referencia:
{text}

Pregunta:
{question}

Respuesta del estudiante:
{answer}

Responde SOLO en formato JSON:
{{"score": <0-100>, "feedback": "<explicación breve>"}}"""

        response = self.client.chat(
            model="command-r-plus-08-2024",
            messages=[{"role": "user", "content": prompt}]
        )
        
        raw = response.message.content[0].text.strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        
        try:
            result = json.loads(raw)
            return result.get("score", 0), result.get("feedback", "")
        except:
            return 0, "No se pudo evaluar la respuesta."

# Singleton
ai_service = AIService()