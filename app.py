import os
import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
import dateparser

from calendar_client import CalendarClient

# ====== Config ======
TIMEZONE = os.getenv("TIMEZONE", "America/Santiago")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")  # e.g. tu_calendario@group.calendar.google.com

if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
    raise Exception("Falta GOOGLE_SERVICE_ACCOUNT_JSON en variables de entorno.")
if not CALENDAR_ID:
    raise Exception("Falta GOOGLE_CALENDAR_ID en variables de entorno.")

# ====== App ======
app = Flask(__name__)
calendar_client = CalendarClient()

@app.get("/")
def health():
    return "OK - Bot de Citas ejecutándose", 200

# Helper: parsear fecha/hora en español hacia datetime con zona horaria

def parse_datetime_es(payload: dict):
    tz = ZoneInfo(TIMEZONE)
    settings = {
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": TIMEZONE,
    }

    # 1) Texto natural: "mañana a las 13:00", "lunes 10 a las 9"
    dt_text = payload.get("datetime_text")
    if dt_text:
        dt = dateparser.parse(dt_text, languages=["es"], settings=settings)
        if dt:
            return dt

    # 2) Fecha + hora separadas (YYYY-MM-DD y HH:MM)
    fecha = payload.get("fecha")
    hora = payload.get("hora")
    if fecha and hora:
        dt = dateparser.parse(f"{fecha} {hora}", languages=["es"], settings=settings)
        if dt:
            return dt

    # 3) Solo fecha (por defecto 10:00)
    if fecha and not hora:
        dt = dateparser.parse(f"{fecha} 10:00", languages=["es"], settings=settings)
        if dt:
            return dt

    return None

@app.post("/cita")
def crear_cita():
    """Crea una cita en Google Calendar.

    Body JSON (ejemplos):
    {
      "nombre": "Pilar",
      "datetime_text": "mañana a las 13:00",
      "duracion_minutos": 45,
      "email": "pilar@example.com",
      "telefono": "+56912345678",
      "comentario": "Asesoría"
    }

    o bien

    {
      "nombre": "Juan",
      "fecha": "2025-08-12",
      "hora": "09:30",
      "duracion_minutos": 30
    }
    """
    data = request.get_json(silent=True) or {}

    # Campos
    nombre = (data.get("nombre") or "Cliente").strip()
    duracion = int(data.get("duracion_minutos") or 30)
    email = (data.get("email") or "").strip()
    telefono = (data.get("telefono") or "").strip()
    comentario = (data.get("comentario") or "").strip()

    # Fecha/hora
    start_dt = parse_datetime_es(data)
    if not start_dt:
        return jsonify({
            "ok": False,
            "error": "No pude entender la fecha/hora. Envíame algo como 'mañana a las 13:00' o usa campos 'fecha' y 'hora'."
        }), 400

    end_dt = start_dt + timedelta(minutes=duracion)

    # Crear evento
    summary = f"Cita con {nombre}".strip()

    description_items = []
    if telefono:
        description_items.append(f"Teléfono: {telefono}")
    if comentario:
        description_items.append(f"Comentario: {comentario}")
    description = "\n".join(description_items) if description_items else ""

    attendees = []
    if email:
        attendees.append({"email": email})

    created = calendar_client.insert_event(
        calendar_id=CALENDAR_ID,
        summary=summary,
        start_dt=start_dt,
        end_dt=end_dt,
        timezone=TIMEZONE,
        description=description,
        attendees=attendees,
        send_updates="all"  # manda invitación por correo si hay 'email'
    )

    # Respuesta para que muestres al cliente
    fecha_legible = start_dt.strftime("%d-%m-%Y %H:%M")
    mensaje_cliente = (
        f"Listo {nombre}, tu cita quedó solicitada para el {fecha_legible} (hora {TIMEZONE}). "
        f"Si dejaste correo, te llegará confirmación."
    )

    return jsonify({
        "ok": True,
        "evento": {
            "id": created.get("id"),
            "htmlLink": created.get("htmlLink"),
            "start": created.get("start"),
            "end": created.get("end"),
        },
        "mensaje_para_cliente": mensaje_cliente
    }), 201

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
