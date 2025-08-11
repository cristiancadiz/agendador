import os
import re
import json
import time
from datetime import timedelta, datetime, date
from zoneinfo import ZoneInfo
from urllib.parse import quote

from flask import Flask, request, jsonify, render_template_string, redirect, Response
import dateparser

# Google Calendar
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# OpenAI (GPT-3.5)
from openai import OpenAI

# WhatsApp Cloud API (opcional)
import requests


# =========================
# Config / Entorno
# =========================
TIMEZONE = os.getenv("TIMEZONE", "America/Santiago")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-3.5-turbo"

COMPANY_NAME = os.getenv("COMPANY_NAME", "CrossFit Box")
GREETING_TEXT = os.getenv(
    "GREETING_TEXT",
    f"Hola 👋, somos {COMPANY_NAME}. Te inscribimos en clases. ¿Cuál es tu nombre?"
)

# Horario de clases: Lunes–Viernes 09:00–18:00, duración 60 min
BUSINESS_START_HOUR = 9
BUSINESS_END_HOUR   = 18
CLASS_DURATION_MIN = int(os.getenv("CLASS_DURATION_MIN", "60"))
CLASS_CAPACITY_DEFAULT = int(os.getenv("CLASS_CAPACITY_DEFAULT", "12"))
CLASS_TITLE_BASE = os.getenv("CLASS_TITLE_BASE", "Clase CrossFit")

# WhatsApp Cloud API (opcional)
WA_TOKEN = os.getenv("WA_TOKEN")
WA_PHONE_ID = os.getenv("WA_PHONE_ID")  # fallback si el webhook no trae metadata
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "verify_me")
DEBUG_WA = os.getenv("DEBUG_WA", "0") == "1"

# Anti-duplicados (idempotencia webhook)
WA_DEDUP_TTL = int(os.getenv("WA_DEDUP_TTL_SEC", "300"))  # 5 min
_PROCESADOS = {}  # {message_id: expire_ts}

def wa_is_dup(message_id: str) -> bool:
    now = time.time()
    # limpia expirados
    for k, exp in list(_PROCESADOS.items()):
        if exp < now:
            _PROCESADOS.pop(k, None)
    if not message_id:
        return False
    if message_id in _PROCESADOS and _PROCESADOS[message_id] > now:
        return True
    _PROCESADOS[message_id] = now + WA_DEDUP_TTL
    return False


# Validaciones iniciales
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


# =========================
# Estado por sesión
# =========================
SESSIONS = {}  # { session_id: {...} }

def _get_session(session_id: str):
    s = SESSIONS.get(session_id)
    if not s:
        s = {
            "history": [],
            "slots": {"nombre":"", "datetime_text":"", "fecha":"", "hora":""},
            "awaiting_confirm": False,
            "candidate": None,
            "last_event_id": None,
        }
        SESSIONS[session_id] = s
    return s


# =========================
# HTML: Chat Web (solo nombre + fecha + hora)
# =========================
CHAT_HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Inscripción a clases</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root{--bg:#0f172a;--panel:#0b1220;--card:#111827;--txt:#e5e7eb;--muted:#94a3b8;--me:#22c55e;--bot:#60a5fa;}
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--txt);font:16px/1.5 system-ui,Segoe UI,Roboto}
    .wrap{max-width:860px;margin:24px auto;padding:0 12px}
    .card{background:var(--card);border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.35)}
    .head{padding:16px 18px;border-bottom:1px solid #1f2937}
    .head h1{margin:0;font-size:18px}
    .head p{margin:6px 0 0;color:var(--muted);font-size:14px}
    .chat{height:62vh;overflow:auto;padding:16px}
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
        <h1>Inscripción a clases</h1>
        <p>Clases de <b>lunes a viernes</b>, entre <b>09:00–18:00</b>, duración <b>60 min</b>. Zona: <b>{{ tz }}</b>.</p>
      </div>
      <div id="chat" class="chat"></div>
      <div class="foot">
        <textarea id="box" placeholder="Escribe aquí… (Enter para enviar, Shift+Enter para salto)"></textarea>
        <button id="send">Enviar</button>
      </div>
    </div>
    <p class="hint" style="margin-left:6px">¿Prefieres formulario? <a href="/nuevo">abrir</a></p>
  </div>

<script>
const chat = document.getElementById('chat');
const box  = document.getElementById('box');
const send = document.getElementById('send');

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

function addEventCard(htmlLink, icsUrl, gcalUrl){
  const div = document.createElement('div');
  div.className = 'msg bot';
  div.innerHTML = `
    <div class="bubble">
      <div style="display:grid;gap:8px">
        <div><b>Inscripción registrada</b></div>
        ${htmlLink ? `<div>📅 <a href="${htmlLink}" target="_blank" rel="noopener">Ver evento</a></div>` : ``}
        ${gcalUrl ? `<div>➕ <a href="${gcalUrl}" target="_blank" rel="noopener">Añadir a Google Calendar</a></div>` : ``}
        ${icsUrl ? `<div>📄 <a href="${icsUrl}" target="_blank" rel="noopener">Descargar .ics</a></div>` : ``}
      </div>
    </div>`;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

async function callBot(text){
  const payload = { session_id: sid, message: text };
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
    addEventCard(data.evento.htmlLink, data.evento.icsUrl, data.evento.gcalAddUrl);
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
addMsg({{ greeting|tojson }});
</script>
</body>
</html>
"""


# =========================
# Fechas: parser robusto (13 horas -> 13:00, etc.)
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
    - DATE_ORDER=DMY
    - Normaliza '13 horas/hrs', 'a las 13' y '13' (al final) -> '13:00'
    - Requiere hora explícita en texto natural
    - Prefiere futuro; usa TZ local
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
        txt = dt_text.lower()
        txt = re.sub(r"\b(a\s*las\s*)?(\d{1,2})\s*(h|hs|hrs|horas)\b", r"\2:00", txt)
        txt = re.sub(r"\b(a\s*las\s*)?(\d{1,2})\b(?=\s*$)", r"\2:00", txt)
        if not _has_time_token(txt):
            return None
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
    return None


# =========================
# Business rules: L-V 09–18
# =========================
def within_class_hours(start_dt, end_dt):
    local_start = start_dt.astimezone(ZoneInfo(TIMEZONE))
    local_end   = end_dt.astimezone(ZoneInfo(TIMEZONE))

    # Lunes=0 ... Domingo=6
    if local_start.weekday() >= 5 or local_end.weekday() >= 5:
        return False, "Las clases son de lunes a viernes."
    # mismo día
    if local_start.date() != local_end.date():
        return False, "La clase debe quedar dentro del mismo día."
    # 09:00–18:00
    sm = local_start.hour * 60 + local_start.minute
    em = local_end.hour * 60 + local_end.minute
    if sm < BUSINESS_START_HOUR*60 or em > BUSINESS_END_HOUR*60:
        return False, f"El horario de clases es de {BUSINESS_START_HOUR:02d}:00 a {BUSINESS_END_HOUR:02d}:00."
    return True, ""


# =========================
# Links “Añadir a GCal” y .ics
# =========================
def _to_utc_fmt(dt):
    dt_utc = dt.astimezone(ZoneInfo("UTC"))
    return dt_utc.strftime("%Y%m%dT%H%M%SZ")

def make_gcal_template_link(summary: str, start_dt, end_dt, description: str = "", location: str = ""):
    qs = {
        "action": "TEMPLATE",
        "text": summary or "Clase",
        "dates": f"{_to_utc_fmt(start_dt)}/{_to_utc_fmt(end_dt)}",
        "details": description or "",
        "location": location or "",
    }
    base = "https://calendar.google.com/calendar/render?"
    return base + "&".join([f"{k}={quote(v)}" for k, v in qs.items() if v])

def build_ics_from_event(ev: dict):
    uid = ev.get("id") + "@clase-bot"
    summary = ev.get("summary", "Clase")
    description = (ev.get("description") or "").replace("\n", "\\n")
    start = ev["start"]["dateTime"]
    end   = ev["end"]["dateTime"]
    start_dt = datetime.fromisoformat(start.replace("Z","+00:00"))
    end_dt   = datetime.fromisoformat(end.replace("Z","+00:00"))
    dtstamp  = _to_utc_fmt(datetime.now(ZoneInfo("UTC")))
    ics = "\r\n".join([
        "BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//Clases//EN","CALSCALE:GREGORIAN","METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{_to_utc_fmt(start_dt)}",
        f"DTEND:{_to_utc_fmt(end_dt)}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
        "END:VEVENT",
        "END:VCALENDAR",""
    ])
    return ics


# =========================
# Helpers de “clase con múltiples participantes” (con wa_id)
# =========================
def _load_participants(ev):
    extp = (ev.get("extendedProperties") or {}).get("private") or {}
    arr = []
    try:
        arr = json.loads(extp.get("participants", "[]"))
    except Exception:
        arr = []
    return arr, extp

def _save_participants(ev, participants, capacity, title=None):
    extp_priv = (ev.get("extendedProperties") or {}).get("private") or {}
    extp_priv["type"] = "class"
    extp_priv["capacity"] = str(capacity)
    extp_priv["participants"] = json.dumps(participants, ensure_ascii=False)
    ev.setdefault("extendedProperties", {})["private"] = extp_priv

    # Actualiza título con contador
    if title:
        ev["summary"] = f"{title} ({len(participants)}/{capacity})"

    # Description legible
    lines = [ev.get("description") or "Tipo: Clase de CrossFit", "Participantes:"]
    for p in participants:
        pname = p.get('nombre','')
        lines.append(f"- {pname}" if pname else "- (sin nombre)")
    ev["description"] = "\n".join(lines)

def _find_class_event(start_dt, end_dt, title_prefix=None):
    resp = gc_service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=10
    ).execute()
    for ev in (resp.get("items") or []):
        extp = (ev.get("extendedProperties") or {}).get("private") or {}
        if (extp.get("type") == "class") or (title_prefix and (ev.get("summary","").startswith(title_prefix))):
            return ev
    return None

def _create_class_event(start_dt, end_dt, title_base, capacity):
    body = {
        "summary": f"{title_base} (0/{capacity})",
        "description": "Tipo: Clase de CrossFit\nParticipantes:",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE},
        "extendedProperties": {"private": {"type": "class", "capacity": str(capacity), "participants": "[]"}}
    }
    return gc_service.events().insert(calendarId=CALENDAR_ID, body=body, sendUpdates="none").execute()

def enroll_in_class(nombre, start_dt, end_dt, capacidad=None, titulo=None, wa_id=None):
    cap = int(capacidad or CLASS_CAPACITY_DEFAULT)
    title_base = titulo or f"{CLASS_TITLE_BASE} {start_dt.astimezone(ZoneInfo(TIMEZONE)).strftime('%H:%M')}"

    ev = _find_class_event(start_dt, end_dt, title_prefix=title_base)
    if not ev:
        ev = _create_class_event(start_dt, end_dt, title_base, cap)

    participants, extp = _load_participants(ev)
    curr_cap = int(extp.get("capacity", cap))

    def _norm(n): 
        return re.sub(r"\s+", " ", (n or "").strip().lower())

    # Dedupe por wa_id primero; si no hay, por nombre
    for p in participants:
        if wa_id and p.get("wa_id") == wa_id:
            hora_txt = start_dt.astimezone(ZoneInfo(TIMEZONE)).strftime("%H:%M")
            msg = f"Ya estabas inscrito/a en {title_base} a las {hora_txt}. ¡Te esperamos!"
            updated = ev
            gcal_link = make_gcal_template_link(updated.get("summary","Clase"), start_dt, end_dt, updated.get("description",""))
            base = request.host_url.rstrip("/")
            ics_url = f"{base}/ics/{updated.get('id')}.ics"
            updated["gcalAddUrl"] = gcal_link
            updated["icsUrl"] = ics_url
            return updated, msg
        if not wa_id and _norm(p.get("nombre")) == _norm(nombre):
            hora_txt = start_dt.astimezone(ZoneInfo(TIMEZONE)).strftime("%H:%M")
            msg = f"Ya estabas inscrito/a en {title_base} a las {hora_txt}. ¡Te esperamos!"
            updated = ev
            gcal_link = make_gcal_template_link(updated.get("summary","Clase"), start_dt, end_dt, updated.get("description",""))
            base = request.host_url.rstrip("/")
            ics_url = f"{base}/ics/{updated.get('id')}.ics"
            updated["gcalAddUrl"] = gcal_link
            updated["icsUrl"] = ics_url
            return updated, msg

    if len(participants) >= curr_cap:
        return None, f"La clase ya está completa ({len(participants)}/{curr_cap}). ¿Quieres otra hora?"

    # Agregar participante
    participants.append({
        "nombre": nombre,
        "wa_id": wa_id or "",
        "ts": datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    })

    _save_participants(ev, participants, curr_cap, title=title_base)
    updated = gc_service.events().update(calendarId=CALENDAR_ID, eventId=ev["id"], body=ev, sendUpdates="none").execute()

    gcal_link = make_gcal_template_link(updated.get("summary","Clase"), start_dt, end_dt, updated.get("description",""))
    base = request.host_url.rstrip("/")
    ics_url = f"{base}/ics/{updated.get('id')}.ics"
    updated["gcalAddUrl"] = gcal_link
    updated["icsUrl"] = ics_url

    hora_txt = start_dt.astimezone(ZoneInfo(TIMEZONE)).strftime("%H:%M")
    msg = f"¡Listo {nombre}! Te inscribí en {title_base} a las {hora_txt}. Cupos: {len(participants)}/{curr_cap}."
    return updated, msg


# =========================
# Chatbot con GPT 3.5 — SOLO nombre + fecha/hora
# =========================
SYSTEM_PROMPT = (
    "Eres el asistente de inscripciones de un box de CrossFit. "
    "Objetivo: pedir SOLO NOMBRE y FECHA/HORA de la clase. "
    "Las clases duran 60 minutos y son de lunes a viernes, entre 09:00 y 18:00 (hora local). "
    "Cuando tengas nombre y una fecha/hora válida, propone confirmación y crea la inscripción. "
    "Habla cercano y profesional, breve (1–2 frases). "
    "Si el usuario habla de otra cosa, responde breve y redirige a agendar. "
    "Devuelve SOLO un JSON con: "
    "{ "
    "  \"reply\": \"...\", "
    "  \"slots\": {\"nombre\":\"\", \"datetime_text\":\"\", \"fecha\":\"\", \"hora\":\"\"}, "
    "  \"next_action\": \"smalltalk|ask_missing|confirm_time|create_event|none\", "
    "  \"candidate\": {\"datetime_text\":\"\"} ó {\"fecha\":\"\", \"hora\":\"\"} "
    "}"
)

def llm_orchestrate(history, slots, awaiting_confirm, candidate, user_message):
    state = {"slots": slots, "awaiting_confirm": awaiting_confirm, "candidate": candidate or {}}
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Estado actual: {json.dumps(state, ensure_ascii=False)}"},
    ]
    messages += history
    messages.append({"role": "user", "content": user_message})
    messages.append({"role": "system", "content":
        "Devuelve SOLO el JSON indicado, sin texto adicional."
    })
    resp = oa_client.chat.completions.create(model=OPENAI_MODEL, temperature=0.3, messages=messages)
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except Exception:
        data = {
            "reply": "¿Me indicas tu nombre y la fecha/hora? (ej: 12/08 18:00).",
            "slots": {"nombre":"", "datetime_text":"", "fecha":"", "hora":""},
            "next_action": "ask_missing"
        }
    data.setdefault("reply", "")
    data.setdefault("slots", {"nombre":"", "datetime_text":"", "fecha":"", "hora":""})
    data.setdefault("next_action", "none")
    if "candidate" in data and isinstance(data["candidate"], dict):
        for k, v in list(data["candidate"].items()):
            if isinstance(v, str):
                data["candidate"][k] = v.strip()
    return data

def process_chat(session_id: str, user_msg: str, wa_id: str | None = None):
    session = _get_session(session_id)
    history = session["history"]
    slots = session["slots"]
    awaiting_confirm = session.get("awaiting_confirm", False)

    if not user_msg:
        return {"reply": GREETING_TEXT, "done": False}

    plan = llm_orchestrate(history, slots, awaiting_confirm, session.get("candidate"), user_msg)

    new_slots = plan.get("slots", {})
    for k in ["nombre", "datetime_text", "fecha", "hora"]:
        if new_slots.get(k):
            slots[k] = new_slots[k]

    action = plan.get("next_action", "none")
    reply  = plan.get("reply") or GREETING_TEXT
    cand   = plan.get("candidate") or {}

    if action == "confirm_time":
        session["awaiting_confirm"] = True
        cand_payload = {
            "nombre": (slots.get("nombre") or "Alumno/a").strip(),
            "datetime_text": cand.get("datetime_text") or slots.get("datetime_text"),
            "fecha": cand.get("fecha") or slots.get("fecha"),
            "hora":  cand.get("hora")  or slots.get("hora"),
        }
        session["candidate"] = cand_payload
        history += [{"role":"user","content":user_msg},{"role":"assistant","content":reply}]
        return {"reply": reply, "done": False}

    if action == "create_event":
        cand_or_slots = {
            "nombre": (cand.get("nombre") or slots.get("nombre") or "Alumno/a").strip(),
            "datetime_text": cand.get("datetime_text") or slots.get("datetime_text"),
            "fecha": cand.get("fecha") or slots.get("fecha"),
            "hora": cand.get("hora") or slots.get("hora"),
        }
        # Parse fecha/hora
        start_dt = parse_datetime_es({
            "datetime_text": cand_or_slots["datetime_text"],
            "fecha": cand_or_slots["fecha"],
            "hora": cand_or_slots["hora"],
        })
        if not start_dt:
            msg = "Para inscribirte necesito la fecha y la hora exactas (ej: 12/08 18:00)."
            history += [{"role":"user","content":user_msg},{"role":"assistant","content":msg}]
            return {"reply": msg, "done": False}

        end_dt = start_dt + timedelta(minutes=CLASS_DURATION_MIN)
        ok_hours, msg_hours = within_class_hours(start_dt, end_dt)
        if not ok_hours:
            history += [{"role":"user","content":user_msg},{"role":"assistant","content":msg_hours}]
            return {"reply": msg_hours, "done": False}

        # Inscribir (múltiples por evento) — pasamos wa_id para dedupe
        ev, msg = enroll_in_class(
            nombre=cand_or_slots["nombre"],
            start_dt=start_dt,
            end_dt=end_dt,
            capacidad=None,
            titulo=None,
            wa_id=wa_id
        )
        if not ev:
            history += [{"role":"user","content":user_msg},{"role":"assistant","content":msg}]
            return {"reply": msg, "done": False}

        session["last_event_id"] = ev.get("id")

        # Reset de slots
        session["slots"] = {"nombre":"", "datetime_text":"", "fecha":"", "hora":""}
        session["awaiting_confirm"] = False
        session["candidate"] = None

        history += [{"role":"user","content":user_msg},{"role":"assistant","content":msg}]
        return {"reply": msg, "done": True, "evento": {
            "id": ev.get("id"),
            "htmlLink": ev.get("htmlLink"),
            "start": ev.get("start"),
            "end": ev.get("end"),
            "icsUrl": ev.get("icsUrl"),
            "gcalAddUrl": ev.get("gcalAddUrl"),
        }}

    history += [{"role":"user","content":user_msg},{"role":"assistant","content":reply}]
    return {"reply": reply, "done": False}


# =========================
# Vistas básicas
# =========================
@app.get("/")
def root():
    return redirect("/chat", code=302)

@app.get("/chat")
def chat_ui():
    subtitle = "Indícame tu <b>nombre</b> y la <b>fecha/hora</b> de la clase (ej: <code>12/08 a las 18 horas</code>)."
    return render_template_string(
        CHAT_HTML,
        tz=TIMEZONE,
        greeting=GREETING_TEXT,
        subtitle=subtitle,
        header="Inscripción a clases",
        title="Clases",
        cal=CALENDAR_ID
    )

@app.post("/chatbot")
def chatbot():
    data = request.get_json(silent=True) or {}
    res = process_chat(
        session_id=(data.get("session_id") or "default"),
        user_msg=(data.get("message") or "").strip(),
        wa_id=None  # en chat web no tenemos wa_id
    )
    return jsonify(res)

# Formulario opcional
FORM_HTML = """
<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Inscripción</title></head>
<body style="font-family:system-ui;max-width:720px;margin:24px auto">
  <h1>Inscripción a clases</h1>
  <form method="post" action="/nuevo">
    <label>Nombre</label><br><input name="nombre" placeholder="Ana" required><br><br>
    <label>Fecha (YYYY-MM-DD)</label><br><input name="fecha" type="date" required><br><br>
    <label>Hora (HH:MM 24h)</label><br><input name="hora" type="time" required><br><br>
    <button type="submit">Inscribirme</button>
  </form>
  <p>Horario: lunes a viernes, 09:00–18:00 (60 min) — Zona: {{ tz }}</p>
</body></html>
"""

RESULT_HTML = """
<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Inscripción registrada</title></head>
<body style="font-family:system-ui;max-width:720px;margin:24px auto">
  <h1>✅ Inscripción registrada</h1>
  <p>{{ mensaje }}</p>
  <ul>
    <li><b>Inicio:</b> {{ start_dt }}</li>
    <li><b>Término:</b> {{ end_dt }}</li>
    <li><b>Evento:</b> <a href="{{ html_link }}" target="_blank">abrir</a></li>
    <li><b>Añadir a Google Calendar:</b> <a href="{{ gcal_add }}" target="_blank">link</a></li>
    <li><b>Descargar .ics:</b> <a href="{{ ics_url }}" target="_blank">archivo</a></li>
  </ul>
  <pre style="white-space:pre-wrap;background:#0b1220;color:#e5e7eb;padding:12px;border-radius:6px">{{ pretty_event }}</pre>
  <p><a href="/nuevo">← Inscribir otra persona</a> | <a href="/chat">Ir al chat</a></p>
</body></html>
"""

@app.get("/nuevo")
def nuevo():
    return render_template_string(FORM_HTML, tz=TIMEZONE)

@app.post("/nuevo")
def crear_inscripcion_web():
    form = request.form
    nombre = form.get("nombre", "Alumno/a").strip()
    fecha = form.get("fecha", "").strip()
    hora  = form.get("hora", "").strip()

    dt = parse_datetime_es({"fecha": fecha, "hora": hora})
    if not dt:
        return "Fecha u hora inválida.", 400
    end_dt = dt + timedelta(minutes=CLASS_DURATION_MIN)

    ok_hours, msg_hours = within_class_hours(dt, end_dt)
    if not ok_hours:
        return msg_hours, 400

    ev, msg = enroll_in_class(nombre=nombre, start_dt=dt, end_dt=end_dt)
    if not ev:
        return msg, 409

    pretty = json.dumps(ev, ensure_ascii=False, indent=2)
    return render_template_string(
        RESULT_HTML,
        mensaje=msg,
        start_dt=ev.get("start", {}).get("dateTime"),
        end_dt=ev.get("end", {}).get("dateTime"),
        html_link=ev.get("htmlLink"),
        gcal_add=ev.get("gcalAddUrl"),
        ics_url=ev.get("icsUrl"),
        pretty_event=pretty
    )

# .ics download
@app.get("/ics/<event_id>.ics")
def ics_download(event_id):
    try:
        ev = gc_service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
    except HttpError:
        return "No encontré la clase.", 404
    ics = build_ics_from_event(ev)
    filename = f"clase-{event_id}.ics"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "text/calendar; charset=utf-8",
    }
    return Response(ics, headers=headers)


# =========================
# WhatsApp Cloud API (opcional)
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
    if not WA_TOKEN:
        return "whatsapp not configured", 200

    payload = request.get_json(silent=True) or {}
    if DEBUG_WA:
        print("WA IN >>>", json.dumps(payload, ensure_ascii=False))

    try:
        entry = (payload.get("entry") or [])[0]
        changes = (entry.get("changes") or [])[0]
        value = changes.get("value", {})
        phone_id = (value.get("metadata") or {}).get("phone_number_id") or WA_PHONE_ID
        messages = value.get("messages", [])
        statuses = value.get("statuses", [])

        if not messages:
            if DEBUG_WA and statuses:
                print("WA STATUS >>>", json.dumps(statuses, ensure_ascii=False))
            return "ok", 200

        for msg in messages:
            message_id = msg.get("id") or msg.get("wamid")
            if wa_is_dup(message_id):
                if DEBUG_WA:
                    print("WA DUP >>>", message_id)
                continue

            from_id = msg.get("from")  # este es el wa_id del usuario
            text = ""
            if msg.get("type") == "text":
                text = (msg.get("text", {}) or {}).get("body", "")

            # Pasamos wa_id para dedupe y estadística
            res = process_chat(session_id=from_id, user_msg=text, wa_id=from_id)

            url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
            headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
            body = {"messaging_product": "whatsapp", "to": from_id, "text": {"body": res.get("reply") or "..."}} 
            r = requests.post(url, headers=headers, json=body, timeout=30)
            if DEBUG_WA:
                print("WA OUT <<<", r.status_code, r.text)

            if res.get("done") and res.get("evento"):
                ev = res["evento"]
                if ev.get("icsUrl"):
                    body_doc = {
                      "messaging_product": "whatsapp",
                      "to": from_id,
                      "type": "document",
                      "document": {"link": ev["icsUrl"], "filename": "clase.ics"}
                    }
                    rd = requests.post(url, headers=headers, json=body_doc, timeout=30)
                    if DEBUG_WA:
                        print("WA OUT DOC <<<", rd.status_code, rd.text)
                if ev.get("gcalAddUrl"):
                    body_link = {
                      "messaging_product": "whatsapp",
                      "to": from_id,
                      "text": {"body": f"Para agregarla en tu Google Calendar: {ev['gcalAddUrl']}"}
                    }
                    rl = requests.post(url, headers=headers, json=body_link, timeout=30)
                    if DEBUG_WA:
                        print("WA OUT LINK <<<", rl.status_code, rl.text)

        return "ok", 200
    except Exception as e:
        if DEBUG_WA:
            print("WA ERROR !!!", repr(e))
        return "ok", 200


# =========================
# STATS: clases por persona (nombre o wa_id)
# =========================
def _normalize_name(n: str) -> str:
    return re.sub(r"\s+", " ", (n or "").strip().lower())

def _parse_date(s: str):
    try:
        y, m, d = map(int, s.split("-"))
        return date(y, m, d)
    except Exception:
        return None

def _iter_class_events(tmin_dt: datetime, tmax_dt: datetime):
    page = None
    while True:
        resp = gc_service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=tmin_dt.isoformat(),
            timeMax=tmax_dt.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=2500,
            pageToken=page
        ).execute()
        for ev in (resp.get("items") or []):
            extp = (ev.get("extendedProperties") or {}).get("private") or {}
            if extp.get("type") == "class":
                yield ev
        page = resp.get("nextPageToken")
        if not page:
            break

def _class_stats(tmin_dt: datetime, tmax_dt: datetime, group_by="name"):
    tz = ZoneInfo(TIMEZONE)
    counts = {}
    first_seen = {}
    last_seen = {}
    display_name = {}

    def push(key, shown_name, when_dt):
        counts[key] = counts.get(key, 0) + 1
        display_name.setdefault(key, shown_name)
        if when_dt:
            if key not in first_seen or when_dt < first_seen[key]:
                first_seen[key] = when_dt
            if key not in last_seen or when_dt > last_seen[key]:
                last_seen[key] = when_dt

    for ev in _iter_class_events(tmin_dt, tmax_dt):
        try:
            start_iso = (ev.get("start") or {}).get("dateTime")
            start_dt = datetime.fromisoformat(start_iso.replace("Z","+00:00")).astimezone(tz) if start_iso else None
        except Exception:
            start_dt = None

        participants, _ = _load_participants(ev)
        # dedupe por (evento, key)
        seen_keys_in_event = set()
        for p in participants:
            name = (p.get("nombre") or "").strip()
            wa = (p.get("wa_id") or "").strip()
            if group_by == "wa" and wa:
                key = f"wa:{wa}"
                shown = name or wa
            else:
                key = f"nm:{_normalize_name(name)}"
                shown = name or "(sin nombre)"
            ek = (ev.get("id"), key)
            if ek in seen_keys_in_event:
                continue
            seen_keys_in_event.add(ek)
            push(key, shown, start_dt)

    rows = []
    for key, c in sorted(counts.items(), key=lambda kv: (-kv[1], display_name[kv[0]].lower())):
        rows.append({
            "key": key,
            "nombre": display_name[key],
            "clases": c,
            "primera": first_seen.get(key).strftime("%Y-%m-%d %H:%M") if first_seen.get(key) else "",
            "ultima":  last_seen.get(key).strftime("%Y-%m-%d %H:%M") if last_seen.get(key) else "",
        })
    return rows

@app.get("/clases/stats")
def clases_stats_json():
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start_s = request.args.get("start")  # YYYY-MM-DD
    end_s   = request.args.get("end")    # YYYY-MM-DD
    group_by = (request.args.get("group_by") or "name").lower()

    if start_s:
        d0 = _parse_date(start_s)
        if not d0: return jsonify({"ok": False, "error": "start inválido (YYYY-MM-DD)"}), 400
        tmin = datetime(d0.year, d0.month, d0.day, 0, 0, tzinfo=tz)
    else:
        tmin = now - timedelta(days=90)

    if end_s:
        d1 = _parse_date(end_s)
        if not d1: return jsonify({"ok": False, "error": "end inválido (YYYY-MM-DD)"}), 400
        tmax = datetime(d1.year, d1.month, d1.day, 23, 59, 59, tzinfo=tz)
    else:
        tmax = now

    rows = _class_stats(tmin, tmax, group_by=group_by)
    return jsonify({"ok": True, "desde": tmin.isoformat(), "hasta": tmax.isoformat(), "group_by": group_by, "total_personas": len(rows), "detalle": rows})

@app.get("/clases/stats.csv")
def clases_stats_csv():
    from io import StringIO
    import csv
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start_s = request.args.get("start")
    end_s   = request.args.get("end")
    group_by = (request.args.get("group_by") or "name").lower()

    if start_s:
        d0 = _parse_date(start_s)
        if not d0: return "start inválido", 400
        tmin = datetime(d0.year, d0.month, d0.day, 0, 0, tzinfo=tz)
    else:
        tmin = now - timedelta(days=90)

    if end_s:
        d1 = _parse_date(end_s)
        if not d1: return "end inválido", 400
        tmax = datetime(d1.year, d1.month, d1.day, 23, 59, 59, tzinfo=tz)
    else:
        tmax = now

    rows = _class_stats(tmin, tmax, group_by=group_by)
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["clave","nombre","clases","primera","ultima"])
    for r in rows:
        w.writerow([r["key"], r["nombre"], r["clases"], r["primera"], r["ultima"]])
    out = buf.getvalue()
    return Response(out, headers={
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": "attachment; filename=clases_stats.csv"
    })

@app.get("/clases/stats.html")
def clases_stats_html():
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start_s = request.args.get("start")
    end_s   = request.args.get("end")
    group_by = (request.args.get("group_by") or "name").lower()

    if start_s:
        d0 = _parse_date(start_s)
        if not d0: return "start inválido", 400
        tmin = datetime(d0.year, d0.month, d0.day, 0, 0, tzinfo=tz)
    else:
        tmin = now - timedelta(days=90)

    if end_s:
        d1 = _parse_date(end_s)
        if not d1: return "end inválido", 400
        tmax = datetime(d1.year, d1.month, d1.day, 23, 59, 59, tzinfo=tz)
    else:
        tmax = now

    rows = _class_stats(tmin, tmax, group_by=group_by)

    html = [
        "<!doctype html><meta charset='utf-8'><title>Stats clases</title>",
        "<style>body{font-family:system-ui;margin:24px} table{border-collapse:collapse} th,td{padding:8px 10px;border:1px solid #ddd} ",
        ".btn{display:inline-block;padding:6px 10px;border-radius:8px;background:#0ea5e9;color:#fff;text-decoration:none;font-weight:600} ",
        ".btn:hover{filter:brightness(0.95)}",
        "</style>",
        f"<h1>Estadísticas de clases</h1><p>Desde {tmin.strftime('%Y-%m-%d')} hasta {tmax.strftime('%Y-%m-%d')} (agrupado por: <b>{group_by}</b>)</p>",
        "<table><tr><th>Clave</th><th>Nombre</th><th>Clases</th><th>Primera</th><th>Última</th><th>Historial</th></tr>"
    ]

    for r in rows:
        # Construir link al historial según agrupación:
        if group_by == "wa" and r["key"].startswith("wa:"):
            ident = r["key"][3:]  # quita "wa:"
            hist_q = f"wa_id={quote(ident)}"
        else:
            # agrupado por nombre
            hist_q = f"name={quote(r['nombre'])}"

        if start_s:
            hist_q += f"&start={quote(start_s)}"
        if end_s:
            hist_q += f"&end={quote(end_s)}"

        hist_url = f"/clases/historial.html?{hist_q}"

        html.append(
            f"<tr>"
            f"<td>{r['key']}</td>"
            f"<td>{r['nombre']}</td>"
            f"<td>{r['clases']}</td>"
            f"<td>{r['primera']}</td>"
            f"<td>{r['ultima']}</td>"
            f"<td><a class='btn' target='_blank' href='{hist_url}'>Ver historial</a></td>"
            f"</tr>"
        )

    html.append("</table>")
    return Response("\n".join(html), headers={"Content-Type":"text/html; charset=utf-8"})


# ======== HISTORIAL: clases por persona (wa_id o nombre) ========
def _class_history(tmin_dt: datetime, tmax_dt: datetime, wa_id: str | None = None, name: str | None = None):
    if not wa_id and not name:
        return []

    tz = ZoneInfo(TIMEZONE)
    rows = []

    for ev in _iter_class_events(tmin_dt, tmax_dt):
        try:
            s_iso = (ev.get("start") or {}).get("dateTime")
            e_iso = (ev.get("end") or {}).get("dateTime")
            s_dt = datetime.fromisoformat(s_iso.replace("Z","+00:00")).astimezone(tz) if s_iso else None
            e_dt = datetime.fromisoformat(e_iso.replace("Z","+00:00")).astimezone(tz) if e_iso else None
        except Exception:
            s_dt, e_dt = None, None

        participants, _ = _load_participants(ev)
        for p in participants:
            p_name = (p.get("nombre") or "").strip()
            p_wa   = (p.get("wa_id") or "").strip()

            match = False
            if wa_id:
                match = (p_wa == wa_id)
            elif name:
                match = (_normalize_name(p_name) == _normalize_name(name))

            if match:
                rows.append({
                    "event_id": ev.get("id"),
                    "nombre": p_name or "(sin nombre)",
                    "wa_id": p_wa,
                    "inicio": s_dt.isoformat() if s_dt else "",
                    "termino": e_dt.isoformat() if e_dt else "",
                    "summary": ev.get("summary", ""),
                    "htmlLink": ev.get("htmlLink"),
                })

    rows.sort(key=lambda r: r["inicio"])
    return rows

def _parse_date_qs(param_value: str | None, default_dt: datetime):
    if not param_value:
        return default_dt
    try:
        y, m, d = map(int, param_value.split("-"))
        return default_dt.replace(year=y, month=m, day=d, hour=default_dt.hour, minute=default_dt.minute, second=default_dt.second)
    except Exception:
        return None

@app.get("/clases/historial")
def clases_historial_json():
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    # rango por defecto: últimos 90 días
    d0 = request.args.get("start")
    d1 = request.args.get("end")
    wa  = request.args.get("wa_id")
    nm  = request.args.get("name")

    tmin = _parse_date_qs(d0, now - timedelta(days=90))
    tmax = _parse_date_qs(d1, now)
    if not tmin or not tmax:
        return jsonify({"ok": False, "error": "start/end inválidos (YYYY-MM-DD)"}), 400

    # normaliza a 00:00 y 23:59
    tmin = tmin.replace(hour=0, minute=0, second=0)
    tmax = tmax.replace(hour=23, minute=59, second=59)

    rows = _class_history(tmin, tmax, wa_id=wa, name=nm)
    return jsonify({
        "ok": True,
        "desde": tmin.isoformat(),
        "hasta": tmax.isoformat(),
        "filtro": {"wa_id": wa, "name": nm},
        "total_clases": len(rows),
        "detalle": rows
    })

@app.get("/clases/historial.html")
def clases_historial_html():
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    d0 = request.args.get("start")
    d1 = request.args.get("end")
    wa  = request.args.get("wa_id")
    nm  = request.args.get("name")

    tmin = _parse_date_qs(d0, now - timedelta(days=90))
    tmax = _parse_date_qs(d1, now)
    if not tmin or not tmax:
        return "start/end inválidos (YYYY-MM-DD)", 400

    tmin = tmin.replace(hour=0, minute=0, second=0)
    tmax = tmax.replace(hour=23, minute=59, second=59)

    rows = _class_history(tmin, tmax, wa_id=wa, name=nm)

    html = [
        "<!doctype html><meta charset='utf-8'><title>Historial de clases</title>",
        "<style>body{font-family:system-ui;margin:24px} table{border-collapse:collapse} th,td{padding:8px 10px;border:1px solid #ddd}</style>",
        f"<h1>Historial de clases</h1>",
        f"<p>Desde {tmin.strftime('%Y-%m-%d')} hasta {tmax.strftime('%Y-%m-%d')} — Filtro: "
        f"{'wa_id='+wa if wa else ''}{' / ' if wa and nm else ''}{'name='+nm if nm else ''}</p>",
        "<table><tr><th>Fecha</th><th>Inicio</th><th>Término</th><th>Nombre</th><th>Evento</th></tr>"
    ]
    for r in rows:
        # fecha legible
        try:
            ini = datetime.fromisoformat(r["inicio"]).astimezone(tz)
            fin = datetime.fromisoformat(r["termino"]).astimezone(tz)
            fstr = ini.strftime("%Y-%m-%d")
            istr = ini.strftime("%H:%M")
            estr = fin.strftime("%H:%M")
        except Exception:
            fstr, istr, estr = "", "", ""

        link = r["htmlLink"] or "#"
        html.append(f"<tr><td>{fstr}</td><td>{istr}</td><td>{estr}</td><td>{r['nombre']}</td>"
                    f"<td><a target='_blank' href='{link}'>ver</a></td></tr>")
    html.append("</table>")
    return Response("\n".join(html), headers={"Content-Type":"text/html; charset=utf-8"})


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
