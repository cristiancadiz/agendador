import os
import re
import json
from uuid import uuid4
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify, render_template_string, redirect
import dateparser

# Google Calendar
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# OpenAI (GPT 3.5 Turbo)
from openai import OpenAI

# HTTP para WhatsApp Cloud API
import requests

# =========================
# Configuración / Entorno
# =========================
TIMEZONE = os.getenv("TIMEZONE", "America/Santiago")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-3.5-turbo"  # fijo

# Identidad de la empresa y saludo (conversacional)
COMPANY_NAME = os.getenv("COMPANY_NAME", "Departamento de Cobranza")
GREETING_TEXT = os.getenv(
    "GREETING_TEXT",
    f"Hola 👋, somos {COMPANY_NAME}. Te ayudamos a agendar tu cita. ¿Cómo te llamas?"
)

# WhatsApp Cloud API (opcional)
WA_TOKEN = os.getenv("WA_TOKEN")
WA_PHONE_ID = os.getenv("WA_PHONE_ID")
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "verify_me")

# Debug WA (logs de entrada/salida)
DEBUG_WA = os.getenv("DEBUG_WA", "0") == "1"

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

# Estado por sesión:
# { session_id: {
#     "history":[...],                         # turns previos (texto natural)
#     "slots":{"nombre","datetime_text","fecha","hora"},
#     "awaiting_confirm": bool,                # si estamos esperando "sí/no"
#     "candidate": {...}                       # propuesta de fecha/hora a confirmar
# } }
SESSIONS = {}

# =========================
# HTML: Chat Web
# =========================
CHAT_HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Agendar por chat</title>
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
    .meetcard button{padding:6px 10px;border:0;border-radius:8px;cursor:pointer}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="head">
        <h1>Agendar por chat</h1>
        <p>Duración fija: <b>30 minutos</b>. Zona: <b>{{ tz }}</b>. Solo necesito <b>tu nombre</b> y <b>fecha/hora</b>.</p>
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

function addMeetCard(link, htmlLink){
  const div = document.createElement('div');
  div.className = 'msg bot';
  div.innerHTML = `
    <div class="bubble meetcard">
      <div style="display:grid;gap:8px">
        <div><b>Reunión creada</b></div>
        ${link ? `<div>🔗 <a href="${link}" target="_blank" rel="noopener">${link}</a></div>` : `<div>No hay link de Meet</div>`}
        ${htmlLink ? `<div>📅 <a href="${htmlLink}" target="_blank" rel="noopener">Ver en Google Calendar</a></div>` : ``}
        ${link ? `<button id="copyMeet">Copiar enlace</button>` : ``}
      </div>
    </div>`;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  const btn = div.querySelector('#copyMeet');
  if (btn) btn.onclick = async () => {
    try { await navigator.clipboard.writeText(link); btn.textContent = '¡Copiado!'; }
    catch(e){ btn.textContent = 'No se pudo copiar'; }
  };
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
    addMsg('No pude procesarlo ahora (' + resp.status + '). Intenta nuevamente.', 'bot');
    return;
  }
  const data = await resp.json();
  addMsg(data.reply || '(sin respuesta)', 'bot');
  if (data.done && data.evento) {
    addMeetCard(data.evento.meet_link, data.evento.htmlLink);
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

// Mensaje inicial (seguro con tojson)
addMsg({{ greeting|tojson }});
</script>
</body>
</html>
"""

# =========================
# Fechas: parser robusto
# =========================
def _has_time_token(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    if re.search(r"\b(mediod[ií]a|medianoche)\b", t):
        return True
    return bool(re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)?\b", t))

def parse_datetime_es(payload: dict):
    """
    Convierte texto o (fecha+hora) a datetime con tz.
    - DATE_ORDER=DMY (12/08 = 12 de agosto)
    - Normaliza '14 horas/hrs' y 'a las 14' -> '14:00'
    - Requiere hora cuando viene por texto natural
    - Prefiere futuro; RELATIVE_BASE ahora en TZ
    """
    now = datetime.now(ZoneInfo(TIMEZONE))
    settings = {
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": TIMEZONE,
        "RELATIVE_BASE": now,
        "DATE_ORDER": "DMY",
    }

    dt_text = (payload.get("datetime_text") or "").strip()
    if dt_text:
        if not _has_time_token(dt_text):
            return None
        txt = dt_text.lower()
        txt = re.sub(r"\b(a\s*las\s*)?(\d{1,2})\s*(h|hs|hrs|horas)\b", r"\2:00", txt)
        txt = re.sub(r"\b(a\s*las\s*)?(\d{1,2})\b(?=\s*$)", r"\2:00", txt)
        dt = dateparser.parse(txt, languages=["es"], settings=settings)
        if dt:
            return dt

    fecha = (payload.get("fecha") or "").strip()
    hora = (payload.get("hora") or "").strip()
    if fecha and hora:
        if re.fullmatch(r"\d{1,2}", hora):
            hora = f"{hora}:00"
        dt = dateparser.parse(f"{fecha} {hora}", languages=["es"], settings=settings)
        if dt:
            return dt

    if fecha and not hora and payload.get("_allow_date_only"):
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
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
        "conferenceData": {"createRequest": {"requestId": str(uuid4())}},  # Meet
    }

def format_confirmation_message(nombre: str, start_dt, meet_link: str | None) -> str:
    fecha_legible = start_dt.strftime("%d-%m-%Y %H:%M")
    base = (
        f"{COMPANY_NAME} — "
        f"Listo {nombre}, tu cita quedó para el {fecha_legible} (hora {TIMEZONE}). "
        f"Dura 30 minutos."
    )
    if meet_link:
        base += f" Link de videollamada: {meet_link}"
    base += " Si necesitas cambiarla o cancelarla, avísame por aquí."
    return base

def create_event_calendar(nombre, datetime_text=None, fecha=None, hora=None,
                          telefono="", comentario="", allow_date_only=False):
    """Crea evento (30 min) + Meet. Devuelve (evento, mensaje o error)."""
    start_dt = parse_datetime_es({
        "datetime_text": datetime_text, "fecha": fecha, "hora": hora,
        "_allow_date_only": allow_date_only
    })
    if not start_dt:
        return None, "Para agendar necesito la fecha y la hora exactas (ejemplo: 12/08 14:00)."

    end_dt = start_dt + timedelta(minutes=30)
    event_body = build_event_payload(nombre or "Cliente", start_dt, end_dt, telefono, comentario)

    try:
        created = gc_service.events().insert(
            calendarId=CALENDAR_ID,
            body=event_body,
            sendUpdates="none",
            conferenceDataVersion=1
        ).execute()
    except HttpError:
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
    msg = format_confirmation_message(nombre or "Cliente", start_dt, meet_link)
    created["meet_link"] = meet_link
    return created, msg

# =========================
# Rutas básicas / formulario
# =========================
@app.get("/")
def root():
    return redirect("/chat", code=302)

@app.get("/_diag")
def diag():
    return jsonify({
        "ok": True,
        "calendar_id": CALENDAR_ID,
        "timezone": TIMEZONE,
        "service_account_email": info.get("client_email"),
    })

@app.get("/_routes")
def list_routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))

FORM_HTML = """
<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Agendar</title></head>
<body style="font-family:system-ui;max-width:720px;margin:24px auto">
  <h1>Agendar (formulario)</h1>
  <form method="post" action="/nuevo">
    <label>Nombre</label><br><input name="nombre" placeholder="Juan Pérez" required><br><br>
    <label>Fecha (YYYY-MM-DD)</label><br><input name="fecha" type="date"><br><br>
    <label>Hora (HH:MM)</label><br><input name="hora" type="time"><br><br>
    <label>O texto natural</label><br><input name="datetime_text" placeholder="12/08 a las 14:00"><br><br>
    <label>Teléfono (opcional)</label><br><input name="telefono" placeholder="+569..."><br><br>
    <label>Comentario (opcional)</label><br><input name="comentario" placeholder="Motivo de la reunión"><br><br>
    <button type="submit">Crear</button>
  </form>
  <p>Zona: {{ tz }} — Calendario: <code>{{ cal }}</code></p>
</body></html>
"""

RESULT_HTML = """
<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Cita creada</title></head>
<body style="font-family:system-ui;max-width:720px;margin:24px auto">
  <h1>✅ Reunión agendada</h1>
  <p>{{ mensaje }}</p>
  <ul>
    <li><b>Inicio:</b> {{ start_dt }}</li>
    <li><b>Término:</b> {{ end_dt }}</li>
    <li><b>Meet:</b> {% if meet_link %}<a href="{{ meet_link }}" target="_blank">{{ meet_link }}</a>{% else %}no disponible{% endif %}</li>
    <li><b>Evento Calendar:</b> <a href="{{ html_link }}" target="_blank">abrir</a></li>
  </ul>
  <pre style="white-space:pre-wrap;background:#0b1220;color:#e5e7eb;padding:12px;border-radius:6px">{{ pretty_event }}</pre>
  <p><a href="/nuevo">← Agendar otra</a> | <a href="/chat">Ir al chat</a></p>
</body></html>
"""

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
        allow_date_only=True,  # permite solo fecha (default 10:00) en formulario
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

@app.get("/chat")
def chat_ui():
    return render_template_string(CHAT_HTML, tz=TIMEZONE, cal=CALENDAR_ID, greeting=GREETING_TEXT)

# =========================
# API JSON directa
# =========================
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
# Chatbot con GPT 3.5 — Orquestación conversacional
# =========================
SYSTEM_PROMPT = (
    "Eres el asistente de agenda de una empresa de cobranza judicial. "
    "Habla en tono cercano, claro y profesional (de tú). "
    "Objetivo: conseguir NOMBRE y FECHA/HORA para una reunión de 30 minutos y crearla cuando el usuario confirme. "
    "Responde siempre con naturalidad (una o dos frases). No repitas lo que el usuario dice palabra por palabra. "
    "Además de tu respuesta, devuelve una estructura JSON con: "
    "  reply: tu respuesta natural para el usuario, "
    "  slots: {nombre, datetime_text, fecha, hora} con lo que DETECTES EN EL ÚLTIMO MENSAJE, "
    "  next_action: una de [smalltalk, ask_missing, confirm_time, create_event, none], "
    "  candidate: (opcional) {datetime_text | fecha+hora} cuando propongas/confirmes una hora. "
    "Política: "
    "- Si el usuario habla de otra cosa, contesta brevemente y redirige amable a agendar (smalltalk). "
    "- Si falta nombre u hora/fecha, pide SOLO lo que falta (ask_missing). "
    "- Si el usuario dio nombre y hora/fecha, propone y pide confirmación (confirm_time). "
    "- Solo marca create_event cuando el usuario ha confirmado de forma clara. "
    "- No inventes datos."
)

def _get_session(session_id: str):
    s = SESSIONS.get(session_id)
    if not s:
        s = {
            "history": [],
            "slots": {"nombre":"", "datetime_text":"", "fecha":"", "hora":""},
            "awaiting_confirm": False,
            "candidate": None
        }
        SESSIONS[session_id] = s
    return s

def llm_orchestrate(history, slots, awaiting_confirm, candidate, user_message):
    # Contexto breve para el modelo
    state = {
        "slots": slots,
        "awaiting_confirm": awaiting_confirm,
        "candidate": candidate or {}
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Estado actual: {json.dumps(state, ensure_ascii=False)}"},
    ]
    messages += history
    messages.append({"role": "user", "content": user_message})
    messages.append({"role": "system", "content":
        "Devuelve SOLO un JSON con este esquema: "
        "{ \"reply\":\"...\", "
        "  \"slots\": {\"nombre\":\"\", \"datetime_text\":\"\", \"fecha\":\"\", \"hora\":\"\"}, "
        "  \"next_action\":\"smalltalk|ask_missing|confirm_time|create_event|none\", "
        "  \"candidate\": {\"datetime_text\":\"\"} ó {\"fecha\":\"\", \"hora\":\"\"} "
        "} "
        "No agregues texto fuera del JSON."
    })

    resp = oa_client.chat.completions.create(
        model=OPENAI_MODEL, temperature=0.3, messages=messages
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except Exception:
        data = {"reply": "Perdona, no te entendí bien. ¿Me dices tu nombre y una fecha/hora? (ej: 12/08 14:00)",
                "slots": {"nombre":"", "datetime_text":"", "fecha":"", "hora":""},
                "next_action": "ask_missing"}
    # Normaliza
    data.setdefault("reply", "")
    data.setdefault("slots", {"nombre":"", "datetime_text":"", "fecha":"", "hora":""})
    data.setdefault("next_action", "none")
    if "candidate" in data and isinstance(data["candidate"], dict):
        for k, v in list(data["candidate"].items()):
            if isinstance(v, str):
                data["candidate"][k] = v.strip()
    return data

def process_chat(session_id: str, user_msg: str, telefono: str = "", comentario: str = ""):
    session = _get_session(session_id)
    history = session["history"]
    slots = session["slots"]
    awaiting_confirm = session.get("awaiting_confirm", False)
    candidate = session.get("candidate")

    if not user_msg:
        return {"reply": GREETING_TEXT, "done": False}

    # 1) Orquestación por LLM
    plan = llm_orchestrate(history, slots, awaiting_confirm, candidate, user_msg)

    # 2) Fusiona slots con SOLO lo detectado ahora
    new_slots = plan.get("slots", {})
    for k in ["nombre", "datetime_text", "fecha", "hora"]:
        if new_slots.get(k):
            slots[k] = new_slots[k]

    action = plan.get("next_action", "none")
    reply  = plan.get("reply") or GREETING_TEXT
    cand   = plan.get("candidate") or {}

    # 3) Ejecuta acciones mínimas según plan
    if action == "confirm_time":
        session["awaiting_confirm"] = True
        cand_payload = {
            "nombre": slots.get("nombre") or "Cliente",
            "datetime_text": cand.get("datetime_text") or slots.get("datetime_text"),
            "fecha": cand.get("fecha") or slots.get("fecha"),
            "hora":  cand.get("hora")  or slots.get("hora"),
            "telefono": telefono, "comentario": comentario
        }
        session["candidate"] = cand_payload
        history += [{"role":"user","content":user_msg},{"role":"assistant","content":reply}]
        return {"reply": reply, "done": False}

    if action == "create_event":
        nombre = (cand.get("nombre") or slots.get("nombre") or "Cliente").strip()
        created, msg = create_event_calendar(
            nombre=nombre,
            datetime_text=cand.get("datetime_text") or slots.get("datetime_text"),
            fecha=cand.get("fecha") or slots.get("fecha"),
            hora=cand.get("hora") or slots.get("hora"),
            telefono=telefono,
            comentario=comentario,
        )
        session["awaiting_confirm"] = False
        session["candidate"] = None
        if not created:
            reply = "No me quedó clara la fecha/hora. ¿Me la confirmas en DD/MM o YYYY-MM-DD y HH:MM (24 h)?"
            history += [{"role":"user","content":user_msg},{"role":"assistant","content":reply}]
            return {"reply": reply, "done": False}
        session["slots"] = {"nombre":"", "datetime_text":"", "fecha":"", "hora":""}
        history += [{"role":"user","content":user_msg},{"role":"assistant","content":msg}]
        return {"reply": msg, "done": True, "evento": created}

    # smalltalk / ask_missing / none → responde conversacional y seguimos
    history += [{"role":"user","content":user_msg},{"role":"assistant","content":reply}]
    return {"reply": reply, "done": False}

@app.post("/chatbot")
def chatbot():
    data = request.get_json(silent=True) or {}
    res = process_chat(
        session_id=(data.get("session_id") or "default"),
        user_msg=(data.get("message") or "").strip(),
        telefono=(data.get("telefono") or "").strip(),
        comentario=(data.get("comentario") or "").strip(),
    )
    return jsonify(res)

# =========================
# WhatsApp Cloud API
# =========================
@app.get("/whatsapp/webhook")
def wa_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        return challenge, 200
    return "forbidden", 403

@app.post("/whatsapp/webhook")
def wa_incoming():
    if not (WA_TOKEN and WA_PHONE_ID):
        return "whatsapp not configured", 200

    payload = request.get_json(silent=True) or {}
    if DEBUG_WA:
        print("WA IN >>>", json.dumps(payload, ensure_ascii=False))

    try:
        entry = (payload.get("entry") or [])[0]
        changes = (entry.get("changes") or [])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        statuses = value.get("statuses", [])

        if not messages:
            if DEBUG_WA and statuses:
                print("WA STATUS >>>", json.dumps(statuses, ensure_ascii=False))
            return "ok", 200

        msg = messages[0]
        from_id = msg.get("from")
        text = ""
        if msg.get("type") == "text":
            text = (msg.get("text", {}) or {}).get("body", "")

        res = process_chat(session_id=from_id, user_msg=text)

        url = f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}/messages"
        headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
        body = {"messaging_product": "whatsapp", "to": from_id, "text": {"body": res.get("reply") or "..."}} 

        r = requests.post(url, headers=headers, json=body, timeout=30)
        if DEBUG_WA:
            print("WA OUT <<<", r.status_code, r.text)

        return "ok", 200
    except Exception as e:
        if DEBUG_WA:
            print("WA ERROR !!!", repr(e))
        return "ok", 200

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
