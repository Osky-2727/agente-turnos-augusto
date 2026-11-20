import json
import os
import re
from typing import List
from datetime import datetime, timedelta
import pytz
from fastapi import FastAPI, HTTPException
from groq import Groq
from pyairtable import Api
from pydantic import BaseModel

app = FastAPI(title="Asistente de Turnos Multi-Cliente - Masajes Augusto")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError("La variable de entorno GROQ_API_KEY no está configurada.")

groq_client = Groq(api_key=GROQ_API_KEY)

CLIENTS_DATABASE = {
    "masajes_augusto": {
        "AIRTABLE_TOKEN": os.getenv("AIRTABLE_TOKEN"),
        "BASE_ID": os.getenv("AIRTABLE_BASE_ID"),
        "TABLA_TURNOS_ID": os.getenv("AIRTABLE_TABLA_TURNOS_ID"),
        "ZONA_HORARIA": "America/Argentina/Buenos_Aires",
        "VENTANA_DIAS": 20,
        "PRIMARY_FIELD_PLACEHOLDER": "-",
        "nombre_empresa": "Masajes Augusto",
        "servicios_ofrecidos": ["Estético", "Terapéutico", "Rehabilitación"],
        "horarios_atencion": (
            "- Lunes a Viernes: Turnos a domicilio (10, 12, 14, 18 y 20 hs).\n"
            "- Sábados: En el local físico (10, 11, 12, 13 y 14 hs)."
        ),
        "reglas_especiales": (
            "- Nombre completo, Teléfono, Dirección y Localidad son obligatorios para reservar.\n"
            "- Si el cliente elige un sábado, aclárale que es en el local. Si es día de semana, aclara que es a domicilio.\n"
            "- NUNCA agendar, cancelar o reprogramar sin que quede claro el ID del turno en cuestión."
        ),
    }
}


class MessageItem(BaseModel):
    role: str
    content: str


class ChatPayload(BaseModel):
    client_key: str
    historial_chat: List[MessageItem]
    mensaje_nuevo: str


def parsear_fecha_airtable(fecha_str: str, zona_ar) -> datetime:
    if fecha_str.endswith("Z"):
        fecha_utc = datetime.strptime(fecha_str, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=pytz.utc)
        return fecha_utc.astimezone(zona_ar)
    fecha_dt = datetime.fromisoformat(fecha_str)
    if fecha_dt.tzinfo is None:
        return zona_ar.localize(fecha_dt)
    return fecha_dt.astimezone(zona_ar)


def obtener_turnos_disponibles(config: dict) -> list:
    api = Api(config["AIRTABLE_TOKEN"])
    tabla_turnos = api.table(config["BASE_ID"], config["TABLA_TURNOS_ID"])
    zona_ar = pytz.timezone(config["ZONA_HORARIA"])
    hoy = datetime.now(zona_ar).date()
    limite = hoy + timedelta(days=config["VENTANA_DIAS"])

    turnos = tabla_turnos.all(formula="{Status} = 'Disponible'")
    turnos_validos = []

    for t in turnos:
        f_str = t.get("fields", {}).get("Fecha de inicio")
        if not f_str:
            continue
        f_ar = parsear_fecha_airtable(f_str, zona_ar)
        if f_ar.date() == hoy:
            continue
        if hoy < f_ar.date() <= limite and f_ar.weekday() <= 5:
            turnos_validos.append({
                "ID": t["id"],
                "Fecha": f_ar.strftime("%d/%m/%Y"),
                "Hora": f_ar.strftime("%H:%M"),
                "Dia": f_ar.strftime("%A"),
            })

    turnos_validos.sort(key=lambda x: (datetime.strptime(x["Fecha"], "%d/%m/%Y"), x["Hora"]))
    return turnos_validos


def generar_prompt_sistema(config: dict, turnos_disp: list, ocupados_disp: list) -> str:
    servicios_str = ", ".join(config["servicios_ofrecidos"])
    return f"""Eres el asistente virtual oficial de '{config["nombre_empresa"]}'. Tu único objetivo es agendar, cancelar y reprogramar turnos de masajes de forma ordenada.

INFORMACIÓN DEL NEGOCIO:
- Ofrecemos: {servicios_str}.
{config["horarios_atencion"]}

REGLAS GENERALES:
{config["reglas_especiales"]}

REGLAS DE REPROGRAMACIÓN Y RESPUESTAS NEGATIVAS:
1. Si el cliente quiere reprogramar, ya identificaste su turno actual y busca otro: NO le vuelvas a pedir sus datos personales (Nombre, Teléfono, Dirección, etc.) si ya constan en el chat.
2. Al cancelar el turno anterior en el proceso de cambio de fecha, responde usando exactamente la frase: "Ya está cancelado, para cuando haces la reserva estas fechas y horas son las disponibles" seguido inmediatamente por las opciones libres.
3. Si la respuesta del cliente a los turnos propuestos es negativa, decide no reprogramar o decide no hacer la reserva en ese momento, debes responder exactamente usando la frase: "Bien, estamos a tu disposición para cuando decidas reservar la próxima. ¡Gracias!".

TURNOS DISPONIBLES EN SISTEMA (PRÓXIMOS {config["VENTANA_DIAS"]} DÍAS):
{str(turnos_disp[:15])}

TURNOS OCUPADOS (Para Cancelaciones/Reprogramaciones):
{str(ocupados_disp[:10])}

--- ACCIONES ESTRUCTURADAS SOPORTADAS ---
1. RESERVA NUEVA -> Devuelve JSON con 'confirmar_reserva'
2. CANCELACIÓN -> Devuelve JSON con 'cancelar_turno'
3. REPROGRAMACIÓN -> Devuelve JSON con 'reprogramar_turno'

Nota: Devuelve ÚNICAMENTE el JSON limpio si decides tomar una acción, sin explicaciones ni bloques de markdown."""


@app.get("/")
def healthcheck():
    return {"status": "ok", "servicio": "agente-turnos-augusto"}


@app.post("/webhook/chat")
def chat_webhook(payload: ChatPayload):
    client_key = payload.client_key
    if client_key not in CLIENTS_DATABASE:
        raise HTTPException(status_code=404, detail="Cliente no registrado")

    config = CLIENTS_DATABASE[client_key]
    if not config["AIRTABLE_TOKEN"] or not config["BASE_ID"] or not config["TABLA_TURNOS_ID"]:
        raise HTTPException(status_code=500, detail="Credenciales de Airtable no configuradas.")

    api = Api(config["AIRTABLE_TOKEN"])
    tabla_turnos = api.table(config["BASE_ID"], config["TABLA_TURNOS_ID"])
    zona_horaria = pytz.timezone(config["ZONA_HORARIA"])

    try:
        turnos_disponibles = obtener_turnos_disponibles(config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error obteniendo turnos: {e}")

    try:
        turnos_ocupados_raw = tabla_turnos.all(formula="{Status} = 'Ocupado'")
        turnos_ocupados = []
        for to in turnos_ocupados_raw:
            f_str = to.get("fields", {}).get("Fecha de inicio")
            if f_str:
                f_ar = parsear_fecha_airtable(f_str, zona_horaria)
                turnos_ocupados.append({
                    "ID": to["id"],
                    "Fecha": f_ar.strftime("%d/%m/%Y"),
                    "Hora": f_ar.strftime("%H:%M"),
                    "Cliente": to.get("fields", {}).get("Nombre Completo", ""),
                })
    except Exception:
        turnos_ocupados = []

    system_prompt = generar_prompt_sistema(config, turnos_disponibles, turnos_ocupados)
    mensajes = [{"role": "system", "content": system_prompt}]
    for item in payload.historial_chat:
        mensajes.append({"role": item.role, "content": item.content})
    mensajes.append({"role": "user", "content": payload.mensaje_nuevo})

    completion = groq_client.chat.completions.create(
        model="qwen/qwen3.8-27b",
        messages=mensajes,
        temperature=0.1,
        max_tokens=400,
    )
    respuesta_raw = completion.choices[0].message.content.strip()

    texto_procesable = re.sub(r"<think>.*?</think>", "", respuesta_raw, flags=re.DOTALL)
    texto_procesable = re.sub(r"```json\s*|```\s*", "", texto_procesable).strip()

    try:
        datos = json.loads(texto_procesable)
        if isinstance(datos, dict):
            accion = datos.get("accion")
            if accion == "confirmar_reserva":
                record_id = datos.get("id_turno")
                fecha_dt = datetime.strptime(datos.get("fecha"), "%d/%m/%Y")
                fecha_iso = fecha_dt.strftime("%Y-%m-%d")
                fecha_inicio_unificada = f"{fecha_iso}T{datos.get('hora')}:00.000Z"
                tabla_turnos.update(record_id, {
                    "Nombre Completo": datos.get("nombre_completo"),
                    "Teléfono": str(datos.get("telefono")),
                    "Domicilio": datos.get("direccion") or datos.get("domicilio"),
                    "Localidad": datos.get("localidad"),
                    "Tipo de Masaje": datos.get("tipo_masaje"),
                    "Status": "Ocupado",
                    "Fecha de inicio": fecha_inicio_unificada,
                })
                return {
                    "respuesta": (
                        f"Turno agendado para {datos.get('nombre_completo')} "
                        f"el {datos.get('fecha')} a las {datos.get('hora')} hs."
                    )
                }
            if accion == "cancelar_turno":
                record_id = datos.get("id_turno")
                tabla_turnos.update(record_id, {
                    "Nombre Completo": config["PRIMARY_FIELD_PLACEHOLDER"],
                    "Teléfono": None,
                    "Domicilio": None,
                    "Localidad": None,
                    "Tipo de Masaje": None,
                    "Status": "Disponible",
                })
                return {
                    "respuesta": f"Turno cancelado exitosamente para {datos.get('nombre_completo')}."
                }
    except json.JSONDecodeError:
        pass
    except Exception:
        return {"respuesta": "Hubo un error de base de datos. Por favor reintente."}

    return {"respuesta": respuesta_raw}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
