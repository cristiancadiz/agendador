import os
import json
from uuid import uuid4
from datetime import timedelta

from flask import Flask, request, jsonify, render_template_string
import dateparser

# Google Calendar
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# OpenAI (GPT 3.5 Turbo)
from openai import OpenAI

# =========================
# Configuración / Entorno
# =========================
TIMEZONE = os.getenv("TIMEZONE", "America/Santiago")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-3.5-turbo"  # fijo por tu solicitud

if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
    raise Exception("Falta GOOGLE_SERVICE_ACCOUNT_JSON en variables de entorno.")
if not CALENDAR_ID:
    raise Exception("Falta GOOGLE_CALENDAR_ID en variables de entorno.")
if not OPENAI_API_KEY:
    raise Exception("Falta OPENAI_API_KEY en variables de entorno.")

# Google Calendar client
SCOPES = ["https://www.googleapis.com/auth/calendar"]
info = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
creds = Credentials.from_service_account_info(info, scopes=SCOPES)
gc_service = build("calendar", "v3", credentials=creds, cache_discovery=False)

# OpenAI client
oa_client = OpenAI(api_key=OPENAI_API_KEY)

# Flask app
app = Flask(__name__)

# Memoria simple por sesión (demo). En producción usa Redis/DB.
SESSIONS = {}  # { session_id: [ {"role":"user"/"assistant", "content": "..."} ] }

# ====== Plantillas mínimas (opcional web) ======
FORM_HTML = """
<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Agendar cita</title></head>
<body style="font-family:system-ui;max-width:720px;margin:24px auto">
  <h1>Agendar cita</h1>
  <form method="post" action="/nuevo">
    <label>Nombre</label><br><input name="nombre" placeholder="Pilar" required><br><br>
    <label>Fecha (YYYY-MM-DD)</label><br><input name="fecha" type="date"><br><br>
    <label>Hora (HH:MM)</label><br><input name="hora" type="time"><br><br>
    <label>O texto natural</label><br><input name="datetime_text" placeholder="mañana a las 13:00"><br><br>
    <label>Teléfono (opcional)</label><br><input name="telefono" placeholder="+569..."><br><br>
    <label>Comentario (opcional)</label><br><input name="comentario" placeholder="Asesoría"><br><br>
    <button type="submit">Crear</button>
  </form>
  <p>Zona horaria: {{ tz }} — Calendario: <code>{{ cal }}</code></p>
</body></html>
"""

RESULT_HTML = """
<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Cita creada</title></head>
<body style="font-family:system-ui;max-width:720px;margin:24px auto">
  <h1>✅ Cita creada</h1>
  <p>{{ mensaje }}</p>
  <ul>
    <li><b>Inicio:</b> {{ start_dt }}</li>
    <li><b>Término:</b> {{ end_dt }}</li>
    <li><b>Meet:</b> {% if meet_link %}<a href="{{ meet_link }}" target="_blank">{{ meet_link }}</a>{% else %}no disponible{% endif %}</li>
    <li><b>Evento Calendar:</b> <a href="{{ html_link }}" target="_blank">abrir</a></li>
  </ul>
  <pre style="white-space:pre-wrap;background:#f6f6f6;padding:12px;border-radius:6px">{{ pretty_event }}</pre>
  <p><a href="/nuevo">← Agendar otra</a></p>
</body></html>
"""

# =========================
# Helpers de fechas
# =========================
def parse_datetime_es(payload: dict):
    """Convierte texto o (fecha+hora) a datetime con TZ."""
    settings = {
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": TIMEZONE,
    }
    dt_text = (payload.get("datetime_text") or "").strip()
    if dt_text:
        dt = dateparser.parse(dt_text, languages=["es"], settings=settings)
        if dt:
            return dt

    fecha = (payload.get("fecha") or "").strip()
    hora = (payload.get("hora") or "").strip()
    if fecha and hora:
        dt = dateparser.parse(f"{fecha} {hora}", languages=["es"], settings=settings)
        if dt:
            return dt
    if fecha and not hora:
        dt = dateparser.parse(f"{fecha} 10:00", languages=["es"], settings=settings)
        if dt:
            return dt
    return None


def build_event_payload(nombre, start_dt, end_dt, telefono="", comentario=""):
    description_items = []
    if telefono:
        description_items.append(f"Teléfono: {telefono}")
    if comentario:
        description_items.append(f"Comentario: {comentario}")

    return {
        "summary": f"Cita con {nombre}",
        "description": "\n".join(description_items),
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": endDt.isoformat(), "timeZone": TIMEZONE} if False else {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
        # Sin attendees; se evita 403 y se genera Meet
        "conferenceData": {"createRequest": {"requestId": str(uuid4())}},
    }


def create_event_calendar(nombre, datetime_text=None, fecha=None, hora=None,
                          telefono="", comentario=""):
    """Crea evento (30 min fijos) en Calendar + Meet. Devuelve (evento, mensaje o error)."""
    start_dt = parse_datetime_es({
        "datetime_text": datetime_text, "fecha": fecha, "hora": hora
    })
    if not start_dt:
        return None, "Me falta la fecha y hora exacta (por ej: 'mañana a las 13:00' o fecha+hora)."

    end_dt = start_dt + timedelta(minutes=30)  # fijo 30 min
    event_body = build_event_payload(nombre or "Cliente", start_dt, end_dt, telefono, comentario)

    try:
        created = gc_service.events().insert(
            calendarId=CALENDAR_ID,
            body=event_body,
            sendUpdates="none",
            conferenceDataVersion=1
        ).execute()
    except HttpError:
        # Fallback sin Meet si falla
        event_body.pop("conferenceData", None)
        created = gc_service.events().insert(
            calendarId=CALENDAR_ID,
            body=event_body,
            sendUpdates="none",
        ).execute()

    meet_link = (created.get("hangoutLink")
                 or (created.get("conferenceData", {})
                     .get("entryPoints", [{}])[0]
                     .get("uri")))

    fecha_legible = start_dt.strftime("%d-%m-%Y %H:%M")
    msg = f"Listo {nombre or 'Cliente'}, tu cita quedó para el {fecha_legible} (hora {TIMEZONE})."
    if meet_link:
        msg += f" Enlace Meet: {meet_link}"

    created["meet_link"] = meet_link
    return created, msg


# =========================
# Endpoints básicos
# =========================
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

@app.get("/nuevo")
def nuevo():
    return render_template_string(FORM_HTML, tz=TIMEZONE, cal=CALENDAR_ID)

@app.post("/nuevo")
def crear_cita_web():
    form = request.form
    created, msg = create_event_calendar(
        nombre=form.get("nombre", "Cliente").strip(),
        datetime_text=form.get("datetime_text"),
        fecha=form.get("fecha"),
        hora=form.get("hora"),
        telefono=form.get("telefono"),
        comentario=form.get("comentario"),
    )
    if not created:
        return msg, 400
    pretty = json.dumps(created, ensure_ascii=False, indent=2)
    return render_template_string(
        RESULT_HTML,
        mensaje=msg,
        start_dt=created.get("start", {}).get("dateTime"),
        end_dt=created.get("end", {}).get("dateTime"),
        html_link=created.get("htmlLink"),
        meet_link=created.get("meet_link"),
        pretty_event=pretty
    )

@app.post("/cita")
def crear_cita_api():
    data = request.get_json(silent=True) or {}
    created, msg = create_event_calendar(
        nombre=(data.get("nombre") or "Cliente").strip(),
        datetime_text=data.get("datetime_text"),
        fecha=data.get("fecha"),
        hora=data.get("hora"),
        telefono=data.get("telefono"),
        comentario=data.get("comentario"),
    )
    if not created:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({
        "ok": True,
        "evento": {
            "id": created.get("id"),
            "htmlLink": created.get("htmlLink"),
            "start": created.get("start"),
            "end": created.get("end"),
            "meet_link": created.get("meet_link"),
        },
        "mensaje_para_cliente": msg
    }), 201

# =========================
# Chatbot con GPT 3.5
# =========================
SYSTEM_PROMPT = (
    "Eres un asistente amable y directo para agendar citas (Chile). "
    "Tu objetivo es obtener NOMBRE y FECHA/HORA para crear la cita. "
    "La duración SIEMPRE es 30 minutos (no preguntes por duración). "
    "Acepta expresiones como 'mañana a las 13:00', 'martes 9 a las 10', etc. "
    "Si falta información, haz UNA sola pregunta clara y corta. "
    "Cuando ya tengas todo, responde con solo la palabra: READY."
)

def gpt_extract_fields(history, user_message):
    """Pide a GPT que identifique campos y si está listo."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += history
    messages.append({"role": "user", "content": user_message})

    # Pedimos un JSON simple
    instruction = (
        "Devuelve SOLO un JSON con esta forma:\n"
        "{\n"
        "  \"ready\": true|false,\n"
        "  \"ask\": \"pregunta breve si falta algo o cadena vacía\",\n"
        "  \"nombre\": \"\",\n"
        "  \"datetime_text\": \"\",\n"
        "  \"fecha\": \"\",\n"
        "  \"hora\": \"\"\n"
        "}\n"
        "No incluyas comentarios ni texto extra fuera del JSON."
    )
    messages.append({"role": "system", "content": instruction})

    resp = oa_client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.2,
        messages=messages,
    )
    content = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(content)
    except Exception:
        data = {"ready": False, "ask": "¿Para qué fecha y hora quieres la cita?", "nombre": "", "datetime_text": "", "fecha": "", "hora": ""}

    # Sanitizar mínimos
    data.setdefault("ready", False)
    data.setdefault("ask", "")
    for k in ["nombre", "datetime_text", "fecha", "hora"]:
        data.setdefault(k, "")

    return data

@app.post("/chatbot")
def chatbot():
    """
    Cuerpo esperado:
    {
      "session_id": "abc123",
      "message": "Quiero agendar para mañana a las 13",
      "telefono": "+569...",
      "comentario": "Asesoría"
    }
    Respuesta:
    - Si falta info: { "reply": "<pregunta>", "done": false }
    - Si listo: crea la cita y devuelve { "reply": "<mensaje_para_cliente>", "done": true, "evento": {...} }
    """
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id") or "default"
    user_msg = (data.get("message") or "").strip()
    telefono = (data.get("telefono") or "").strip()
    comentario = (data.get("comentario") or "").strip()

    if not user_msg:
        return jsonify({"reply": "Cuéntame para qué fecha y hora quieres agendar.", "done": False})

    history = SESSIONS.get(session_id, [])
    fields = gpt_extract_fields(history, user_msg)

    if not fields.get("ready"):
        # Falta info → hacemos la pregunta que propone GPT (o una genérica)
        ask = fields.get("ask") or "¿Para qué fecha y hora quieres la cita?"
        # guardamos turno
        history += [{"role": "user", "content": user_msg}, {"role": "assistant", "content": ask}]
        SESSIONS[session_id] = history
        return jsonify({"reply": ask, "done": False})

    # READY → tenemos nombre + fecha/hora (como texto o separados)
    created, msg = create_event_calendar(
        nombre=fields.get("nombre") or "Cliente",
        datetime_text=fields.get("datetime_text"),
        fecha=fields.get("fecha"),
        hora=fields.get("hora"),
        telefono=telefono,
        comentario=comentario,
    )
    if not created:
        # algo no se pudo parsear a pesar de READY → preguntamos explícito
        ask = "No pude entender la fecha/hora exacta. ¿Me confirmas la fecha (YYYY-MM-DD) y la hora (HH:MM)?"
        history += [{"role": "user", "content": user_msg}, {"role": "assistant", "content": ask}]
        SESSIONS[session_id] = history
        return jsonify({"reply": ask, "done": False})

    # guardamos finalización
    history += [{"role": "user", "content": user_msg}, {"role": "assistant", "content": msg}]
    SESSIONS[session_id] = history
    return jsonify({"reply": msg, "done": True, "evento": created})

# =========================
# Main dev
# =========================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--runserver", action="store_true")
    args = parser.parse_args()
    if args.runserver:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
