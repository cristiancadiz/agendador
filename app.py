import os
import re
import json
import time
import base64
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlparse, parse_qs

from flask import Flask, request, jsonify, render_template_string, redirect, Response
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
# Config / Entornoo
# =========================
TIMEZONE = os.getenv("TIMEZONE", "America/Santiago")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-3.5-turbo"

COMPANY_NAME = os.getenv("COMPANY_NAME", "la Ortiga")
GREETING_TEXT = os.getenv(
    "GREETING_TEXT",
    f"Hola 👋, somos {COMPANY_NAME}. Te ayudamos a agendar una llamada con un ejecutivo. ¿Cómo te llamas?"
)

# WhatsApp Cloud API
WA_TOKEN = os.getenv("WA_TOKEN")
WA_PHONE_ID = os.getenv("WA_PHONE_ID")  # fallback
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "verify_me")
DEBUG_WA = os.getenv("DEBUG_WA", "0") == "1"

# Anti-duplicados (idempotencia webhook)
WA_DEDUP_TTL = int(os.getenv("WA_DEDUP_TTL_SEC", "300"))  # 5 min
_PROCESADOS = {}  # {message_id: expire_ts}

def wa_is_dup(message_id: str) -> bool:
    """True si ya procesamos este message_id dentro del TTL."""
    now = time.time()
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
# { session_id: {
#     "history":[...],
#     "slots":{nombre,datetime_text,fecha,hora,telefono,email},
#     "awaiting_confirm": bool,
#     "candidate": {...},
#     "last_event_id": str|None,
#     "cancel_pending": {"event_id","calendar_id","when"}|None
# } }
SESSIONS = {}

def _get_session(session_id: str):
    s = SESSIONS.get(session_id)
    if not s:
        s = {
            "history": [],
            "slots": {"nombre":"", "datetime_text":"", "fecha":"", "hora":"", "telefono":"", "email":""},
            "awaiting_confirm": False,
            "candidate": None,
            "last_event_id": None,
            "cancel_pending": None,
        }
        SESSIONS[session_id] = s
    return s

# =========================
# HTML: Chat Web
# =========================
CHAT_HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Agendar llamada</title>
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
    .evcard button{padding:6px 10px;border:0;border-radius:8px;cursor:pointer}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="head">
        <h1>Agendar llamada</h1>
        <p>Un ejecutivo te llamará. Duración fija: <b>30 minutos</b>. Zona: <b>{{ tz }}</b>.</p>
        <div class="controls">
          <input id="tel" placeholder="Teléfono (opcional)">
          <input id="mail" placeholder="Correo (opcional)">
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
const mailI= document.getElementById('mail');

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

function addEventCard(htmlLink, phone, email, icsUrl, gcalUrl){
  const div = document.createElement('div');
  div.className = 'msg bot';
  const safePhone = phone ? phone : '—';
  const safeMail  = email ? email : '—';
  div.innerHTML = `
    <div class="bubble evcard">
      <div style="display:grid;gap:8px">
        <div><b>Llamada agendada</b></div>
        <div>📞 Teléfono: <b>${safePhone}</b></div>
        <div>✉️ Correo: <b>${safeMail}</b></div>
        ${htmlLink ? `<div>📅 <a href="${htmlLink}" target="_blank" rel="noopener">Ver en mi Google Calendar</a></div>` : ``}
        ${gcalUrl ? `<div>➕ <a href="${gcalUrl}" target="_blank" rel="noopener">Añadir a Google Calendar</a></div>` : ``}
        ${icsUrl ? `<div>📄 <a href="${icsUrl}" target="_blank" rel="noopener">Descargar .ics</a></div>` : ``}
      </div>
    </div>`;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

async function callBot(text){
  const payload = {
    session_id: sid,
    message: text,
    telefono: telI.value || '',
    email: mailI.value || '',
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
    addEventCard(data.evento.htmlLink, data.evento.telefono, data.evento.email, data.evento.icsUrl, data.evento.gcalAddUrl);
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
    - Normaliza '13 horas/hrs', 'a las 13' y '13' (al final) -> '13:00'
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

    if fecha and not hora and payload.get("_allow_date_only"):
        dt = dateparser.parse(f"{fecha} 10:00", languages=["es"], settings=settings)
        if dt:
            return dt

    return None

# =========================
# Links “Añadir a GCal” y .ics
# =========================
def _to_utc_fmt(dt):
    """YYYYMMDDTHHMMSSZ en UTC."""
    dt_utc = dt.astimezone(ZoneInfo("UTC"))
    return dt_utc.strftime("%Y%m%dT%H%M%SZ")

def make_gcal_template_link(summary: str, start_dt, end_dt, description: str = "", location: str = ""):
    qs = {
        "action": "TEMPLATE",
        "text": summary or "Cita",
        "dates": f"{_to_utc_fmt(start_dt)}/{_to_utc_fmt(end_dt)}",
        "details": description or "",
        "location": location or "",
    }
    base = "https://calendar.google.com/calendar/render?"
    return base + "&".join([f"{k}={quote(v)}" for k, v in qs.items() if v])

def build_ics_from_event(ev: dict):
    uid = ev.get("id") + "@bot-citas"
    summary = ev.get("summary", "Cita")
    description = (ev.get("description") or "").replace("\n", "\\n")
    start = ev["start"]["dateTime"]
    end   = ev["end"]["dateTime"]
    start_dt = datetime.fromisoformat(start.replace("Z","+00:00"))
    end_dt   = datetime.fromisoformat(end.replace("Z","+00:00"))
    dtstamp  = _to_utc_fmt(datetime.now(ZoneInfo("UTC")))
    ics = "\r\n".join([
        "BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//Bot Citas//EN","CALSCALE:GREGORIAN","METHOD:PUBLISH",
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
# Helpers Google Calendar
# =========================
def build_event_payload(nombre, start_dt, end_dt, telefono="", email="", comentario=""):
    description_lines = ["Tipo: Llamada saliente", f"Nombre: {nombre}"]
    if telefono:  description_lines.append(f"Teléfono: {telefono}")
    if email:     description_lines.append(f"Email: {email}")
    if comentario:description_lines.append(f"Comentario: {comentario}")
    return {
        "summary": f"Llamada con {nombre}",
        "description": "\n".join(description_lines),
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
    }

def format_confirmation_message(nombre: str, start_dt, telefono: str | None):
    fecha_legible = start_dt.strftime("%d-%m-%Y %H:%M")
    tel_txt = f" al {telefono}" if telefono else ""
    return (f"{COMPANY_NAME} — "
            f"Listo {nombre}, agendé tu llamada para el {fecha_legible} (hora {TIMEZONE}). "
            f"Un ejecutivo te contactará{tel_txt}. Dura 30 minutos. "
            "Si necesitas cambiarla o cancelarla, avísame por aquí.")

def create_event_calendar(nombre, datetime_text=None, fecha=None, hora=None,
                          telefono="", email="", comentario="", allow_date_only=False):
    start_dt = parse_datetime_es({
        "datetime_text": datetime_text, "fecha": fecha, "hora": hora,
        "_allow_date_only": allow_date_only
    })
    if not start_dt:
        return None, "Para agendar necesito la fecha y la hora exactas (ejemplo: 12/08 13:00)."
    telefono = (telefono or "").strip()
    email    = (email or "").strip()
    if not telefono:
        return None, "Me indicas tu número de teléfono para la llamada, por favor."
    if not email:
        return None, "¿Cuál es tu correo electrónico? (lo usamos solo para respaldo de contacto)."

    end_dt = start_dt + timedelta(minutes=30)
    event_body = build_event_payload(nombre or "Cliente", start_dt, end_dt, telefono, email, comentario)
    created = gc_service.events().insert(calendarId=CALENDAR_ID, body=event_body, sendUpdates="none").execute()

    gcal_link = make_gcal_template_link(event_body["summary"], start_dt, end_dt, event_body.get("description",""))
    try:
        base = request.host_url.rstrip("/")
        ics_url = f"{base}/ics/{created.get('id')}.ics"
    except RuntimeError:
        ics_url = ""

    msg = format_confirmation_message(nombre or "Cliente", start_dt, telefono)
    created["telefono"] = telefono
    created["email"] = email
    created["icsUrl"] = ics_url
    created["gcalAddUrl"] = gcal_link
    return created, msg

def update_event_calendar(event_id: str,
                          nombre: str | None = None,
                          datetime_text: str | None = None,
                          fecha: str | None = None,
                          hora: str | None = None,
                          telefono: str | None = None,
                          email: str | None = None,
                          comentario: str | None = None):
    try:
        ev = gc_service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
    except HttpError:
        return None, f"No encontré la cita ({event_id})."

    if any([datetime_text, fecha, hora]):
        start_dt = parse_datetime_es({
            "datetime_text": (datetime_text or ""), "fecha": (fecha or ""), "hora": (hora or "")
        })
        if not start_dt:
            return None, "No entendí la nueva fecha/hora. Ej: 12/08 13:00."
        end_dt = start_dt + timedelta(minutes=30)
        ev["start"] = {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE}
        ev["end"]   = {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE}

    if nombre:
        ev["summary"] = f"Llamada con {nombre}"

    touched_contact = any(v is not None for v in [telefono, email, comentario, nombre])
    if touched_contact:
        desc_prev = ev.get("description") or ""
        def pick(label, nuevo):
            if nuevo is not None and nuevo != "":
                return nuevo
            m = re.search(fr"{label}:\s*(.+)", desc_prev)
            return (m.group(1).strip() if m else "")
        nombre_desc   = nombre or (re.sub(r"^Llamada con\s*", "", ev.get("summary","")) or "Cliente")
        telefono_desc = pick("Teléfono", telefono)
        email_desc    = pick("Email", email)
        coment_desc   = pick("Comentario", comentario)
        lines = ["Tipo: Llamada saliente", f"Nombre: {nombre_desc}"]
        if telefono_desc: lines.append(f"Teléfono: {telefono_desc}")
        if email_desc:    lines.append(f"Email: {email_desc}")
        if coment_desc:   lines.append(f"Comentario: {coment_desc}")
        ev["description"] = "\n".join(lines)

    updated = gc_service.events().update(calendarId=CALENDAR_ID, eventId=event_id, body=ev).execute()
    return updated, "Cita actualizada correctamente."

def delete_event_calendar(event_id: str, calendar_id: str | None = None):
    cal_id = calendar_id or CALENDAR_ID
    try:
        gc_service.events().delete(calendarId=cal_id, eventId=event_id, sendUpdates="none").execute()
        return True, "Cita eliminada."
    except HttpError as e:
        return False, f"No pude eliminar la cita ({event_id}). {e.reason}"

def extract_event_and_cal_from_eid(eid_or_link: str):
    """Acepta el 'eid' o el 'htmlLink' y devuelve (event_id, calendar_id|None)."""
    if not eid_or_link:
        return None, None
    if eid_or_link.startswith("http"):
        try:
            q = parse_qs(urlparse(eid_or_link).query)
            eid = q.get("eid", [None])[0]
        except Exception:
            eid = None
    else:
        eid = eid_or_link
    if not eid:
        return None, None
    try:
        pad = '=' * (-len(eid) % 4)
        decoded = base64.urlsafe_b64decode(eid + pad).decode("utf-8")
        parts = decoded.split(" ")
        event_id = parts[0] if parts else None
        cal_id = parts[1] if len(parts) > 1 else None
        return event_id, cal_id
    except Exception:
        return None, None

# =========================
# Cancelación: helpers
# =========================
CANCEL_RE = re.compile(r"\b(cancelar|anular|eliminar)\b.*\b(cita|llamada)\b", re.I)
YES_RE = re.compile(r"^\s*(sí|si|ok|de acuerdo|confirmo|confirmar|adelante|proceder)\b", re.I)
NO_RE  = re.compile(r"^\s*(no|cancelar no|mejor no|anular no)\b", re.I)

def human_dt(dt_iso: str):
    try:
        dt = datetime.fromisoformat(dt_iso.replace("Z", "+00:00")).astimezone(ZoneInfo(TIMEZONE))
        return dt.strftime("%d-%m-%Y %H:%M")
    except Exception:
        return dt_iso

def find_event_id_by_datetime(dt_target, cal_id=None, tolerance_min=15):
    """Busca un evento que empiece cerca de dt_target ±tolerance_min y tenga summary 'Llamada con ...'."""
    cal_id = cal_id or CALENDAR_ID
    tmin = (dt_target - timedelta(minutes=tolerance_min)).isoformat()
    tmax = (dt_target + timedelta(minutes=tolerance_min)).isoformat()
    resp = gc_service.events().list(
        calendarId=cal_id,
        timeMin=tmin,
        timeMax=tmax,
        singleEvents=True,
        orderBy="startTime",
        maxResults=5
    ).execute()
    for ev in (resp.get("items") or []):
        if (ev.get("summary") or "").lower().startswith("llamada con"):
            return ev.get("id"), ev
    items = resp.get("items") or []
    return (items[0].get("id"), items[0]) if items else (None, None)

# =========================
# Rutas básicas / formulario / .ics
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
<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Agendar llamada</title></head>
<body style="font-family:system-ui;max-width:720px;margin:24px auto">
  <h1>Agendar (formulario)</h1>
  <form method="post" action="/nuevo">
    <label>Nombre</label><br><input name="nombre" placeholder="Juan Pérez" required><br><br>
    <label>Fecha (YYYY-MM-DD)</label><br><input name="fecha" type="date"><br><br>
    <label>Hora (HH:MM)</label><br><input name="hora" type="time"><br><br>
    <label>O texto natural</label><br><input name="datetime_text" placeholder="12/08 a las 13 horas"><br><br>
    <label>Teléfono</label><br><input name="telefono" placeholder="+569..." required><br><br>
    <label>Correo</label><br><input name="email" type="email" placeholder="tu@correo.com" required><br><br>
    <label>Comentario (opcional)</label><br><input name="comentario" placeholder="Motivo de la llamada"><br><br>
    <button type="submit">Crear</button>
  </form>
  <p>Zona: {{ tz }} — Calendario: <code>{{ cal }}</code></p>
</body></html>
"""

RESULT_HTML = """
<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Llamada agendada</title></head>
<body style="font-family:system-ui;max-width:720px;margin:24px auto">
  <h1>✅ Llamada agendada</h1>
  <p>{{ mensaje }}</p>
  <ul>
    <li><b>Inicio:</b> {{ start_dt }}</li>
    <li><b>Término:</b> {{ end_dt }}</li>
    <li><b>Teléfono:</b> {{ telefono or "—" }}</li>
    <li><b>Correo:</b> {{ email or "—" }}</li>
    <li><b>Evento Calendar:</b> <a href="{{ html_link }}" target="_blank">abrir</a></li>
    <li><b>Añadir a Google Calendar:</b> <a href="{{ gcal_add }}" target="_blank">link</a></li>
    <li><b>Descargar .ics:</b> <a href="{{ ics_url }}" target="_blank">archivo</a></li>
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
        email=form.get("email"),
        comentario=form.get("comentario"),
        allow_date_only=True,
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
        telefono=created.get("telefono"),
        email=created.get("email"),
        gcal_add=created.get("gcalAddUrl"),
        ics_url=created.get("icsUrl"),
        pretty_event=pretty
    )

@app.get("/chat")
def chat_ui():
    return render_template_string(CHAT_HTML, tz=TIMEZONE, cal=CALENDAR_ID, greeting=GREETING_TEXT)

@app.get("/ics/<event_id>.ics")
def ics_download(event_id):
    try:
        ev = gc_service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
    except HttpError:
        return "No encontré la cita.", 404
    ics = build_ics_from_event(ev)
    filename = f"cita-{event_id}.ics"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "text/calendar; charset=utf-8",
    }
    return Response(ics, headers=headers)

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
        email=data.get("email"),
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
            "telefono": created.get("telefono"),
            "email": created.get("email"),
            "icsUrl": created.get("icsUrl"),
            "gcalAddUrl": created.get("gcalAddUrl"),
        },
        "mensaje_para_cliente": msg
    }), 201

@app.patch("/cita/<event_id>")
def editar_cita(event_id):
    data = request.get_json(silent=True) or {}
    updated, msg = update_event_calendar(
        event_id=event_id,
        nombre=data.get("nombre"),
        datetime_text=data.get("datetime_text"),
        fecha=data.get("fecha"),
        hora=data.get("hora"),
        telefono=data.get("telefono"),
        email=data.get("email"),
        comentario=data.get("comentario"),
    )
    if not updated:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "mensaje": msg, "evento": {
        "id": updated.get("id"),
        "htmlLink": updated.get("htmlLink"),
        "start": updated.get("start"),
        "end": updated.get("end"),
    }}), 200

@app.delete("/cita/<event_id>")
def eliminar_cita(event_id):
    ok, msg = delete_event_calendar(event_id)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "mensaje": msg}), 200

@app.post("/cita/borrar")
def borrar_cita_flexible():
    data = request.get_json(silent=True) or {}
    event_id = (data.get("event_id") or "").strip()
    html_link = (data.get("htmlLink") or data.get("html_link") or "").strip()
    eid = (data.get("eid") or "").strip()
    cal_from_eid = None
    if not event_id:
        event_id, cal_from_eid = extract_event_and_cal_from_eid(html_link or eid)
    if not event_id:
        return jsonify({"ok": False, "error": "Falta event_id o htmlLink/eid válido."}), 400
    ok, msg = delete_event_calendar(event_id, calendar_id=cal_from_eid or CALENDAR_ID)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "mensaje": "Cita eliminada.", "eventId": event_id}), 200

@app.post("/cita/reprogramar")
def reprogramar_cita():
    data = request.get_json(silent=True) or {}
    old_event_id = (data.get("event_id") or "").strip()
    html_link = (data.get("htmlLink") or data.get("html_link") or "").strip()
    eid = (data.get("eid") or "").strip()
    cal_from_eid = None
    if not old_event_id:
        old_event_id, cal_from_eid = extract_event_and_cal_from_eid(html_link or eid)
    cal_id = cal_from_eid or CALENDAR_ID

    old = None
    if old_event_id:
        try:
            old = gc_service.events().get(calendarId=cal_id, eventId=old_event_id).execute()
        except HttpError:
            old = None

    def pick_from_desc(label, desc):
        if not desc:
            return ""
        m = re.search(fr"{label}:\s*(.+)", desc)
        return m.group(1).strip() if m else ""

    nombre_old = telefono_old = email_old = ""
    if old:
        desc_prev = old.get("description") or ""
        nombre_old = pick_from_desc("Nombre", desc_prev)
        telefono_old = pick_from_desc("Teléfono", desc_prev)
        email_old = pick_from_desc("Email", desc_prev)
        if not nombre_old:
            nombre_old = re.sub(r"^Llamada con\s*", "", old.get("summary","")).strip() or "Cliente"

    nombre   = (data.get("nombre") or nombre_old or "Cliente").strip()
    telefono = (data.get("telefono") or telefono_old).strip()
    email    = (data.get("email") or email_old).strip()
    comentario = (data.get("comentario") or "").strip()

    created, msg = create_event_calendar(
        nombre=nombre,
        datetime_text=data.get("datetime_text"),
        fecha=data.get("fecha"),
        hora=data.get("hora"),
        telefono=telefono,
        email=email,
        comentario=comentario
    )
    if not created:
        return jsonify({"ok": False, "error": msg}), 400

    deleted = False
    del_error = None
    if old_event_id:
        ok, _msg = delete_event_calendar(old_event_id, calendar_id=cal_id)
        deleted = ok
        if not ok:
            del_error = _msg

    return jsonify({
        "ok": True,
        "mensaje": msg + (" Se eliminó la cita anterior." if deleted else " No pude eliminar la anterior." if old_event_id else ""),
        "evento_nuevo": {
            "id": created.get("id"),
            "htmlLink": created.get("htmlLink"),
            "start": created.get("start"),
            "end": created.get("end"),
            "icsUrl": created.get("icsUrl"),
            "gcalAddUrl": created.get("gcalAddUrl"),
        },
        "eliminacion_anterior": {"hecho": deleted, "calendarId": cal_id, "eventId": old_event_id, "error": del_error}
    }), 200

# =========================
# Chatbot con GPT 3.5 — Orquestación + Cancelación
# =========================
SYSTEM_PROMPT = (
    "Eres el asistente de agenda de una empresa de cobranza judicial. "
    "Habla en tono cercano, claro y profesional (de tú). "
    "La cita es una LLAMADA telefónica de 30 minutos que realizará un ejecutivo. "
    "Objetivo: conseguir NOMBRE, FECHA/HORA, TELÉFONO y CORREO; luego confirmar y crear la cita. "
    "Responde siempre con naturalidad (una o dos frases). No repitas lo que el usuario dice palabra por palabra. "
    "Además de tu respuesta, devuelve una estructura JSON con: "
    "  reply: tu respuesta natural para el usuario, "
    "  slots: {nombre, datetime_text, fecha, hora, telefono, email} con lo que DETECTES EN EL ÚLTIMO MENSAJE, "
    "  next_action: una de [smalltalk, ask_missing, confirm_time, create_event, none], "
    "  candidate: (opcional) {datetime_text | fecha+hora} cuando propongas/confirmes una hora. "
    "Reglas: "
    "- Si el usuario habla de otra cosa, contesta breve y redirige amable a agendar (smalltalk). "
    "- Si falta alguno de [nombre, fecha/hora, teléfono, correo], pide SOLO lo que falte (ask_missing). "
    "- Si ya hay [nombre, fecha/hora, teléfono y correo], propone y pide confirmación (confirm_time). "
    "- Marca create_event SOLO cuando el usuario haya confirmado de forma clara. "
    "- No inventes datos."
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
        "Devuelve SOLO un JSON con este esquema: "
        "{ \"reply\":\"...\", "
        "  \"slots\": {\"nombre\":\"\", \"datetime_text\":\"\", \"fecha\":\"\", \"hora\":\"\", \"telefono\":\"\", \"email\":\"\"}, "
        "  \"next_action\":\"smalltalk|ask_missing|confirm_time|create_event|none\", "
        "  \"candidate\": {\"datetime_text\":\"\"} ó {\"fecha\":\"\", \"hora\":\"\"} "
        "} "
        "No agregues texto fuera del JSON."
    })
    resp = oa_client.chat.completions.create(model=OPENAI_MODEL, temperature=0.3, messages=messages)
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except Exception:
        data = {"reply": "¿Me indicas tu nombre, tu teléfono y una fecha/hora? (ej: 12/08 13:00). También tu correo, por favor.",
                "slots": {"nombre":"", "datetime_text":"", "fecha":"", "hora":"", "telefono":"", "email":""},
                "next_action": "ask_missing"}
    data.setdefault("reply", "")
    data.setdefault("slots", {"nombre":"", "datetime_text":"", "fecha":"", "hora":"", "telefono":"", "email":""})
    data.setdefault("next_action", "none")
    if "candidate" in data and isinstance(data["candidate"], dict):
        for k, v in list(data["candidate"].items()):
            if isinstance(v, str):
                data["candidate"][k] = v.strip()
    return data

def process_chat(session_id: str, user_msg: str, telefono: str = "", email: str = "", comentario: str = ""):
    session = _get_session(session_id)
    history = session["history"]
    slots = session["slots"]
    awaiting_confirm = session.get("awaiting_confirm", False)

    if not user_msg:
        return {"reply": GREETING_TEXT, "done": False}

    # --- CANCELACIÓN: confirmación y ejecución (antes del LLM) ---
    cp = session.get("cancel_pending")
    if cp:
        if YES_RE.search(user_msg):
            ok, msg_del = delete_event_calendar(cp["event_id"], calendar_id=cp.get("calendar_id"))
            session["cancel_pending"] = None
            if ok:
                if session.get("last_event_id") == cp["event_id"]:
                    session["last_event_id"] = None
                reply = f"Listo, cancelé tu cita del {cp['when']}."
                return {"reply": reply, "done": False}
            else:
                return {"reply": f"No pude cancelar la cita ({cp['event_id']}). {msg_del}", "done": False}
        elif NO_RE.search(user_msg):
            session["cancel_pending"] = None
            return {"reply": "Perfecto, dejamos la cita tal como está.", "done": False}
        return {"reply": f"¿Confirmas que deseas cancelar la cita del {cp['when']}? Responde “sí cancelar” o “no”.", "done": False}

    if CANCEL_RE.search(user_msg):
        # 1) última cita de la sesión
        last_id = session.get("last_event_id")
        if last_id:
            try:
                ev = gc_service.events().get(calendarId=CALENDAR_ID, eventId=last_id).execute()
                when = human_dt((ev.get("start") or {}).get("dateTime", ""))
                session["cancel_pending"] = {"event_id": last_id, "calendar_id": CALENDAR_ID, "when": when}
                return {"reply": f"¿Confirmas que quieres cancelar la cita del {when}? Responde “sí cancelar” o “no”.", "done": False}
            except HttpError:
                pass

        # 2) link o eid
        m = re.search(r"(https?://www\.google\.com/calendar/event\?eid=[^\s]+)", user_msg)
        eid = None
        if m:
            eid = m.group(1)
        else:
            m2 = re.search(r"\beid=([A-Za-z0-9_\-]+)", user_msg)
            if m2:
                eid = m2.group(1)
        if eid:
            ev_id, cal_id = extract_event_and_cal_from_eid(eid)
            if ev_id:
                try:
                    ev = gc_service.events().get(calendarId=(cal_id or CALENDAR_ID), eventId=ev_id).execute()
                    when = human_dt((ev.get("start") or {}).get("dateTime", ""))
                    session["cancel_pending"] = {"event_id": ev_id, "calendar_id": cal_id or CALENDAR_ID, "when": when}
                    return {"reply": f"¿Confirmas cancelar la cita del {when}? Responde “sí cancelar” o “no”.", "done": False}
                except HttpError:
                    return {"reply": "No pude localizar esa cita con el enlace. ¿Puedes darme la fecha y hora exactas (ej: 12/08 13:00)?", "done": False}

        # 3) fecha/hora “cancela la del 12/08 13:00”
        dt = parse_datetime_es({"datetime_text": user_msg})
        if dt:
            ev_id, ev = find_event_id_by_datetime(dt, CALENDAR_ID, tolerance_min=15)
            if ev_id:
                when = human_dt((ev.get("start") or {}).get("dateTime", ""))
                session["cancel_pending"] = {"event_id": ev_id, "calendar_id": CALENDAR_ID, "when": when}
                return {"reply": f"Voy a cancelar la cita del {when}. ¿Lo confirmas? (responde “sí cancelar” o “no”)", "done": False}
            else:
                return {"reply": "No encontré una cita en ese horario. ¿Puedes confirmar fecha y hora exactas (ej: 12/08 13:00) o pegar el link del evento?", "done": False}

        # 4) pedir datos
        return {"reply": "Para cancelar, indícame la fecha y hora de la cita (ej: 12/08 13:00) o pégame el link del evento.", "done": False}

    # --- Orquestación normal con LLM ---
    candidate = session.get("candidate")
    plan = llm_orchestrate(history, slots, awaiting_confirm, candidate, user_msg)

    # fusionar slots con lo detectado ahora
    new_slots = plan.get("slots", {})
    for k in ["nombre", "datetime_text", "fecha", "hora", "telefono", "email"]:
        if new_slots.get(k):
            slots[k] = new_slots[k]

    action = plan.get("next_action", "none")
    reply  = plan.get("reply") or GREETING_TEXT
    cand   = plan.get("candidate") or {}

    if action == "confirm_time":
        session["awaiting_confirm"] = True
        cand_payload = {
            "nombre": (slots.get("nombre") or "Cliente").strip(),
            "datetime_text": cand.get("datetime_text") or slots.get("datetime_text"),
            "fecha": cand.get("fecha") or slots.get("fecha"),
            "hora":  cand.get("hora")  or slots.get("hora"),
            "telefono": (slots.get("telefono") or telefono or "").strip(),
            "email": (slots.get("email") or email or "").strip(),
            "comentario": comentario
        }
        session["candidate"] = cand_payload
        history += [{"role":"user","content":user_msg},{"role":"assistant","content":reply}]
        return {"reply": reply, "done": False}

    if action == "create_event":
        cand_or_slots = {
            "nombre": (cand.get("nombre") or slots.get("nombre") or "Cliente").strip(),
            "datetime_text": cand.get("datetime_text") or slots.get("datetime_text"),
            "fecha": cand.get("fecha") or slots.get("fecha"),
            "hora": cand.get("hora") or slots.get("hora"),
            "telefono": (cand.get("telefono") or slots.get("telefono") or telefono or "").strip(),
            "email": (cand.get("email") or slots.get("email") or email or "").strip(),
        }
        created, msg = create_event_calendar(
            nombre=cand_or_slots["nombre"],
            datetime_text=cand_or_slots["datetime_text"],
            fecha=cand_or_slots["fecha"],
            hora=cand_or_slots["hora"],
            telefono=cand_or_slots["telefono"],
            email=cand_or_slots["email"],
            comentario=comentario,
        )
        session["awaiting_confirm"] = False
        session["candidate"] = None
        if not created:
            history += [{"role":"user","content":user_msg},{"role":"assistant","content":msg}]
            return {"reply": msg, "done": False}

        # auto-borrar último evento de la sesión (reprogramación)
        last_event_id = session.get("last_event_id")
        if last_event_id:
            try:
                gc_service.events().delete(calendarId=CALENDAR_ID, eventId=last_event_id, sendUpdates="none").execute()
            except HttpError:
                pass
        session["last_event_id"] = created.get("id")

        # limpiar slots para próxima cita
        session["slots"] = {"nombre":"", "datetime_text":"", "fecha":"", "hora":"", "telefono":"", "email":""}
        history += [{"role":"user","content":user_msg},{"role":"assistant","content":msg}]
        return {"reply": msg, "done": True, "evento": created}

    history += [{"role":"user","content":user_msg},{"role":"assistant","content":reply}]
    return {"reply": reply, "done": False}

@app.post("/chatbot")
def chatbot():
    data = request.get_json(silent=True) or {}
    res = process_chat(
        session_id=(data.get("session_id") or "default"),
        user_msg=(data.get("message") or "").strip(),
        telefono=(data.get("telefono") or "").strip(),
        email=(data.get("email") or "").strip(),
        comentario=(data.get("comentario") or "").strip(),
    )
    return jsonify(res)

# =========================
# WhatsApp Cloud API (de-dup + phone_id dinámico + .ics/link)
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

            from_id = msg.get("from")
            text = ""
            if msg.get("type") == "text":
                text = (msg.get("text", {}) or {}).get("body", "")

            res = process_chat(session_id=from_id, user_msg=text, telefono=from_id)

            url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
            headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}

            # 1) Mensaje de texto
            body = {"messaging_product": "whatsapp", "to": from_id, "text": {"body": res.get("reply") or "..."}}
            r = requests.post(url, headers=headers, json=body, timeout=30)
            if DEBUG_WA:
                print("WA OUT <<<", r.status_code, r.text)

            # 2) Si se creó la cita, enviar .ics y link “Añadir a Google Calendar”
            if res.get("done") and res.get("evento"):
                ev = res["evento"]
                if ev.get("icsUrl"):
                    body_doc = {
                      "messaging_product": "whatsapp",
                      "to": from_id,
                      "type": "document",
                      "document": {"link": ev["icsUrl"], "filename": "cita.ics"}
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
# Main dev
# =========================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--runserver", action="store_true")
    args = parser.parse_args()
    if args.runserver:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
