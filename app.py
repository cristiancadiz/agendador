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

# OpenAI (GPT 3.5 Turbo)a
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

# ====== Página de chat web ======
from flask import render_template_string

CHAT_HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Chat de Agenda</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root{--bg:#0f172a;--panel:#0b1220;--card:#111827;--txt:#e5e7eb;--muted:#94a3b8;--me:#22c55e;--bot:#60a5fa;}
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--txt);font:16px/1.5 system-ui,Segoe UI,Roboto}
    .wrap{max-width:860px;margin:24px auto;padding:0 12px}
    .card{background:var(--card);border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.35)}
    .head{padding:16px 18px;border-bottom:1px solid #1f2937}
    .head h1{margin:0;font-size:18px}
    .head p{margin:6px 0 0;color:var(--muted);font-size:14px}
    .controls{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
    .controls input{background:var(--panel);border:1px solid #1f2937;border-radius:10px;padding:8px 10px;color:var(--txt)}
    .chat{height:58vh;overflow:auto;padding:16px}
    .msg{display:flex;margin:10px 0}
    .msg.you{justify-content:flex-end}
    .bubble{max-width:70%;padding:10px 12px;border-radius:14px;line-height:1.35}
    .you .bubble{background:rgba(34,197,94,.15);border:1px solid rgba(34,197,94,.4)}
    .bot .bubble{background:rgba(96,165,250,.15);border:1px solid rgba(96,165,250,.4)}
    .foot{display:flex;gap:10px;padding:14px;border-top:1px solid #1f2937}
    .foot textarea{flex:1;min-height:46px;max-height:200px;resize:vertical;background:var(--panel);border:1px solid #1f2937;border-radius:10px;padding:10px;color:var(--txt)}
    .foot button{background:var(--me);border:0;color:#052e16;padding:0 14px;border-radius:10px;font-weight:700;cursor:pointer}
    .hint{color:var(--muted);font-size:13px;margin-top:6px}
    a{color:#93c5fd}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="head">
        <h1>Agendar por chat</h1>
        <p>Duración fija: <b>30 minutos</b>. Zona: <b>{{ tz }}</b>. El bot solo te pedirá <b>nombre</b> y <b>fecha/hora</b>.</p>
        <div class="controls">
          <input id="tel" placeholder="Teléfono (opcional)">
          <input id="com" placeholder="Comentario (opcional)">
        </div>
        <p class="hint">Calendario: <code>{{ cal }}</code></p>
      </div>
      <div id="chat" class="chat"></div>
      <div class="foot">
        <textarea id="box" placeholder="Escribe aquí… (Enter para enviar, Shift+Enter para salto)"></textarea>
        <button id="send">Enviar</button>
      </div>
    </div>
    <p class="hint" style="margin-left:6px">Si prefieres formulario: <a href="/nuevo">agendar con formulario</a></p>
  </div>

<script>
const chat = document.getElementById('chat');
const box  = document.getElementById('box');
const send = document.getElementById('send');
const telI = document.getElementById('tel');
const comI = document.getElementById('com');

const sid = localStorage.getItem('sid') || (() => {
  const v = 'web-' + Date.now() + '-' + Math.random().toString(36).slice(2,8);
  localStorage.setItem('sid', v); return v;
})();

function addMsg(text, who='bot'){
  const div = document.createElement('div');
  div.className = 'msg ' + (who==='you'?'you':'bot');
  const b = document.createElement('div');
  b.className = 'bubble';
  b.textContent = text;
  div.appendChild(b);
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

async function callBot(text){
  const payload = {
    session_id: sid,
    message: text,
    telefono: telI.value || '',
    comentario: comI.value || ''
  };
  const resp = await fetch('/chatbot', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  if (!resp.ok) {
    addMsg('Ups, no pude procesar ahora (' + resp.status + '). Intenta de nuevo.', 'bot');
    return;
  }
  const data = await resp.json();
  addMsg(data.reply || '(sin respuesta)', 'bot');
  if (data.done && data.evento && data.evento.meet_link) {
    addMsg('Link de la reunión: ' + data.evento.meet_link, 'bot');
  }
}

function sendNow(){
  const text = box.value.trim();
  if (!text) return;
  addMsg(text, 'you');
  box.value = '';
  callBot(text);
}

send.onclick = sendNow;
box.addEventListener('keydown', (e)=>{
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendNow(); }
});

// Mensaje inicial
addMsg('¡Hola! ¿Para qué fecha y hora quieres la cita? (p.ej., “mañana a las 13:00”). Dime también tu nombre.');
</script>
</body>
</html>
"""

@app.get("/chat")
def chat_ui():
    # Muestra la caja de chat
    return render_template_string(CHAT_HTML, tz=TIMEZONE, cal=CALENDAR_ID)

