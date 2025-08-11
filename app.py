import os
import json
from datetime import timedelta

from flask import Flask, request, jsonify
import dateparser

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ====== Config ======
TIMEZONE = os.getenv("TIMEZONE", "America/Santiago")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")

if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
    raise Exception("Falta GOOGLE_SERVICE_ACCOUNT_JSON en variables de entorno.")
if not CALENDAR_ID:
    raise Exception("Falta GOOGLE_CALENDAR_ID en variables de entorno.")

# ====== Google Calendar client ======
SCOPES = ["https://www.googleapis.com/auth/calendar"]
info = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
creds = Credentials.from_service_account_info(info, scopes=SCOPES)
service = build("calendar", "v3", credentials=creds, cache_discovery=False)

# ====== App ======
app = Flask(__name__)

def parse_datetime_es(payload: dict):
    settings = {
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": TIMEZONE,
    }
    # Texto natural: "mañana a las 13:00"
    dt_text = payload.get("datetime_text")
    if dt_text:
        dt = dateparser.parse(dt_text, languages=["es"], settings=settings)
        if dt:
            return dt
    # Fecha + hora separadas
    fecha = payload.get("fecha")
    hora = payload.get("hora")
    if fecha and hora:
        dt = dateparser.parse(f"{fecha} {hora}", languages=["es"], settings=settings)
        if dt:
            return dt
    # Solo fecha (default 10:00)
    if fecha and not hora:
        dt = dateparser.parse(f"{fecha} 10:00", languages=["es"], settings=settings)
        if dt:
            return dt
    return None

@app.get("/")
def health():
    return "OK - Bot de Citas ejecutándose", 200

@app.get("/_diag")
def diag():
    return jsonify({
        "ok": True,
        "calendar_id": CALENDAR_ID,
        "timezone": TIMEZONE,
        "service_account_email": info.get("client_email"),
    })

@app.post("/cita")
def crear_cita():
    """Crea una cita en Google Calendar (sin invitados/correos)."""
    data = request.get_json(silent=True) or {}

    nombre = (data.get("nombre") or "Cliente").strip()
    duracion = int(data.get("duracion_minutos") or 30)
    telefono = (data.get("telefono") or "").strip()
    comentario = (data.get("comentario") or "").strip()

    start_dt = parse_datetime_es(data)
    if not start_dt:
        return jsonify({"ok": False, "error": "No pude entender la fecha/hora."}), 400

    end_dt = start_dt + timedelta(minutes=duracion)

    description_items = []
    if telefono:
        description_items.append(f"Teléfono: {telefono}")
    if comentario:
        description_items.append(f"Comentario: {comentario}")

    event_body = {
        "summary": f"Cita con {nombre}",
        "description": "\n".join(description_items),
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
        # Sin 'attendees' para evitar 403 forbiddenForServiceAccounts
    }

    try:
        created = service.events().insert(
            calendarId=CALENDAR_ID,
            body=event_body,
            sendUpdates="none",   # no envía correos
        ).execute()
    except HttpError as e:
        status = getattr(e, "status_code", None) or (getattr(e, "resp", None).status if getattr(e, "resp", None) else 500)
        try:
            detail = e.content.decode() if hasattr(e, "content") and isinstance(e.content, (bytes, bytearray)) else str(e)
        except Exception:
            detail = str(e)
        return jsonify({
            "ok": False,
            "where": "google_insert",
            "status": int(status),
            "error": "Google API error",
            "details": detail
        }), 502
    except Exception as e:
        return jsonify({"ok": False, "where": "unexpected", "error": f"{type(e).__name__}: {e}"}), 500

    fecha_legible = start_dt.strftime("%d-%m-%Y %H:%M")
    return jsonify({
        "ok": True,
        "evento": {
            "id": created.get("id"),
            "htmlLink": created.get("htmlLink"),
            "start": created.get("start"),
            "end": created.get("end"),
        },
        "mensaje_para_cliente": f"Listo {nombre}, tu cita quedó solicitada para el {fecha_legible} (hora {TIMEZONE})."
    }), 201

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--runserver", action="store_true")
    args = parser.parse_args()
    if args.runserver:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
