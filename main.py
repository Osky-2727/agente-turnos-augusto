import os
import re
import json
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List
from datetime import datetime, timedelta
import pytz
from pyairtable import Api
from groq import Groq

app = FastAPI(title="Asistente de Turnos Masajes Augusto")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError("La variable de entorno GROQ_API_KEY no está configurada.")

groq_client = Groq(api_key=GROQ_API_KEY)

AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN")
BASE_ID = os.getenv("AIRTABLE_BASE_ID")
TABLA_TURNOS_ID = os.getenv("AIRTABLE_TABLA_TURNOS_ID")

api = Api(AIRTABLE_TOKEN) if AIRTABLE_TOKEN else None
tabla_turnos = api.table(BASE_ID, TABLA_TURNOS_ID) if api else None

class MessageItem(BaseModel):
    role: str
    content: str

class ChatPayload(BaseModel):
    historial_chat: List[MessageItem]
    mensaje_nuevo: str

@app.get("/")
def home():
    return {"estado": "¡El agente de Masajes Augusto está activo y funcionando!"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
