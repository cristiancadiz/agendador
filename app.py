import os
import json
from uuid import uuid4
from datetime import timedelta

from flask import Flask, request, jsonify, render_template_string, redirect, url_for
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

# ====== Templates ======
FORM_HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Agendar cita</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root { --bg:#0f172a; --card:#111827; --txt:#e5e7eb; --muted:#94a3b8; --accent:#22c55e; --danger:#ef4444; }
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--txt);font:16px/1.5 system-ui, -apple-system, Segoe UI, Roboto}
    .wrap{max-width:680px;margin:40px auto;padding:0 16px}
    .card{background:var(--card);border-radius:16px;padding:24px;box-shadow:0 10px 30px rgba(0,0,0,.35)}
    h1{margin:0 0 8px} p.muted{color:var(--muted);margin-top:0}
    form{display:grid;gap:14px;margin-top:8px}
    .row{display:grid;gap:12px;grid-template-columns:1fr 1fr}
    label{font-size:14px;color:var(--muted)}
    input, textarea, select{width:100%;padding:12px;border-radius:10px;border:1px solid #1f2937;background:#0b1220;color:var(--txt)}
    button{padding:12px 16px;border-radius:12px;border:0;background:var(--accent);color:#052e16;font-weight:700;cursor:pointer}
    .note{font-size:14px;color:var(--muted)}
    .err{color:var(--danger);margin:0}
    .success{background:#052e16;color:#86efac;padding:12px;border-radius:10px}
    a{color:#93c5fd}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Agendar cita</h1>
      <p class="muted">Zona horaria: <b>{{ tz }}</b>. Completa los datos y crea la reunión (se genera link de Google Meet).</p>

      {% if error %}
        <p class="err">⚠️ {{ error }}</p>
      {% endif %}

      <form method="post" action="{{ url_for('crear_cita_web') }}">
        <div class="row">
          <div>
            <label>Nombre</label>
            <input name="nombre" placeholder="Pilar" value="{{ values.nombre or '' }}" required>
          </div>
          <div>
            <label>Duración (min)</label>
            <input name="duracion_minutos" type="number" min="5" step="5" value="{{ values.duracion_minutos or 30 }}" required>
          </div>
        </div>

        <div class="row">
          <div>
            <label>Fecha (YYYY-MM-DD)</label>
            <input name="fecha" type="date" value="{{ values.fecha or '' }}">
          </div>
          <div>
            <label>Hora (HH:MM 24h)</label>
            <input name="hora" type="time" value="{{ values.hora or '' }}">
          </div>
        </div>

        <div>
          <label>O texto natural (opcional)</label>
          <input name="datetime_text" placeholder="mañana a las 13:00" value="{{ values.datetime_text or '' }}">
          <p class="note">Si rellenas este campo, se ignorarán Fecha y Hora.</p>
        </div>

        <div class="row">
          <div>
            <label>Teléfono (opcional)</label>
            <input name="telefono" placeholder="+56912345678" value="{{ values.telefono or '' }}">
          </div>
          <div>
            <label>Comentario (opcional)</label>
            <input name="comentario" placeholder="Asesoría" value="{{ values.comentario or '' }}">
          </div>
        </div>

        <button type="submit">Crear cita</button>
      </form>

      <p class="note" style="margin-top:10px;">Calendario: <code>{{ cal }}</code></p>
      <p class="note">Si prefieres JSON, usa <code>POST /cita</code>.</p>
    </div>
  </div>
</body>
</html>
"""

RESULT_HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Cita creada</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body{margin:0;background:#0f172a;color:#e5e7eb;font:16px/1.5 system-ui, -apple-system, Segoe UI, Roboto}
    .wrap{max-width:680px;margin:40px auto;padding:0 16px}
    .card{background:#111827;border-radius:16px;padding:24px;box-shadow:0 10px 30px rgba(0,0,0,.35)}
    .ok{background:#052e16;color:#86efac;padding:12px;border-radius:10px}
    .kv{background:#0b1220;padding:12px;border-radius:10px;margin-top:12px;overflow:auto}
    a{color:#93c5fd}
    .row{display:grid;gap:12px;grid-template-columns:1fr 1fr}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>✅ Cita creada</h1>
      <p class="ok">{{ mensaje }}</p>

      <div class="row">
        <div>
          <p><b>Inicio:</b> {{ start_dt }}</p>
          <p><b>Término:</b> {{ end_dt }}</p>
          <p><b>Zona:</b> {{ tz }}</p>
        </div>
        <div>
          <p><b>Evento Calendar:</b> <a href="{{ html_link }}" target="_blank" rel="noopener">abrir</a></p>
          {% if meet_link %}
            <p><b>Google Meet:</b> <a href="{{ meet_link }}" target="_blank" rel="noopener">{{ meet_link }}</a></p>
          {% else %}
            <p><b>Google Meet:</b> no disponible</p>
          {% endif %}
        </div>
      </div>

      <div class="kv">
        <pre>{{ pretty_event }}</pre>
      </div>

      <p style="margin-top:14px;"><a href="{{ url_for('nuevo') }}">← Agendar otra</a></p>
    </div>
  </div>
</body>
</html>
"""

# ====== Helpers ======
def parse_datetime_es(payload: dict):
    """Convierte texto o (fecha+hora) a datetime con tz."""
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

def insert_event_with_meet(event_body: dict):
    """Inserta evento intentando crear Meet; si falla Meet, hace fallback sin él."""
    try:
        created = service.events().insert(
            calendarId=CALENDAR_ID,
            body=event_body,
            sendUpdates="none",
            conferenceDataVersion=1  # requerido para Meet
        ).execute()
        fallback_note = None
    except HttpError as e:
        # Reintenta sin conferenceData (sin Meet)
        event_body.pop("conferenceData", None)
        created = service.events().insert(
            calendarId=CALENDAR_ID,
            body=event_body,
            sendUpdates="none",
        ).execute()
        fallback_note = "No se pudo crear Google Meet automáticamente; la cita se creó igual."
    return created, fallback_note

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
        # Sin attendees; se evita 403 forbiddenForServiceAccounts
        "conferenceData": { "createRequest": {"requestId": str(uuid4())} },
    }

def extract_meet_link(created_event: dict):
    return (created_event.get("hangoutLink")
            or (created_event.get("conferenceData", {})
                 .get("entryPoints", [{}])[0]
                 .get("uri")))

# ====== Rutas API/HTML ======
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

# Página con formulario
@app.get("/nuevo")
def nuevo():
    return render_template_string(FORM_HTML, tz=TIMEZONE, cal=CALENDAR_ID, values={}, error=None)

# Procesa formulario y muestra confirmación
@app.post("/nuevo")
def crear_cita_web():
    form = request.form
    payload = {
        "nombre": form.get("nombre", "Cliente").strip(),
        "duracion_minutos": form.get("duracion_minutos", "30"),
        "telefono": (form.get("telefono") or "").strip(),
        "comentario": (form.get("comentario") or "").strip(),
        "fecha": (form.get("fecha") or "").strip(),
        "hora": (form.get("hora") or "").strip(),
        "datetime_text": (form.get("datetime_text") or "").strip(),
    }
    try:
        duracion = int(payload["duracion_minutos"])
    except ValueError:
        return render_template_string(FORM_HTML, tz=TIMEZONE, cal=CALENDAR_ID, values=payload, error="Duración inválida")

    start_dt = parse_datetime_es(payload)
    if not start_dt:
        return render_template_string(FORM_HTML, tz=TIMEZONE, cal=CALENDAR_ID, values=payload, error="No pude entender la fecha/hora.")

    end_dt = start_dt + timedelta(minutes=duracion)
    event_body = build_event_payload(payload["nombre"], start_dt, end_dt, payload["telefono"], payload["comentario"])
    created, fallback_note = insert_event_with_meet(event_body)
    meet_link = extract_meet_link(created)

    fecha_legible = start_dt.strftime("%d-%m-%Y %H:%M")
    mensaje = f"Listo {payload['nombre']}, tu cita quedó solicitada para el {fecha_legible} (hora {TIMEZONE})."
    if meet_link:
        mensaje += f" Enlace Meet: {meet_link}"
    elif fallback_note:
        mensaje += f" ({fallback_note})"

    pretty_event = json.dumps(created, indent=2, ensure_ascii=False)
    return render_template_string(
        RESULT_HTML,
        mensaje=mensaje,
        start_dt=created.get("start", {}).get("dateTime"),
        end_dt=created.get("end", {}).get("dateTime"),
        tz=TIMEZONE,
        html_link=created.get("htmlLink"),
        meet_link=meet_link,
        pretty_event=pretty_event
    )

# Endpoint JSON (sigue funcionando)
@app.post("/cita")
def crear_cita_api():
    data = request.get_json(silent=True) or {}

    nombre = (data.get("nombre") or "Cliente").strip()
    telefono = (data.get("telefono") or "").strip()
    comentario = (data.get("comentario") or "").strip()
    duracion = int(data.get("duracion_minutos") or 30)

    start_dt = parse_datetime_es(data)
    if not start_dt:
        return jsonify({"ok": False, "error": "No pude entender la fecha/hora."}), 400

    end_dt = start_dt + timedelta(minutes=duracion)
    event_body = build_event_payload(nombre, start_dt, end_dt, telefono, comentario)

    try:
        created, fallback_note = insert_event_with_meet(event_body)
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

    meet_link = extract_meet_link(created)
    fecha_legible = start_dt.strftime("%d-%m-%Y %H:%M")
    mensaje = f"Listo {nombre}, tu cita quedó solicitada para el {fecha_legible} (hora {TIMEZONE})."
    if meet_link:
        mensaje += f" Enlace Meet: {meet_link}"

    if 'fallback_note' in locals() and fallback_note and not meet_link:
        mensaje += f" ({fallback_note})"

    return jsonify({
        "ok": True,
        "evento": {
            "id": created.get("id"),
            "htmlLink": created.get("htmlLink"),
            "start": created.get("start"),
            "end": created.get("end"),
            "meet_link": meet_link
        },
        "mensaje_para_cliente": mensaje
    }), 201

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--runserver", action="store_true")
    args = parser.parse_args()
    if args.runserver:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
