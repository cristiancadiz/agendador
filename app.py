import os
import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
import dateparser

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ====== Config ======
TIMEZONE = os.getenv("TIMEZONE", "America/Santiago")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")

if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
    raise Exception("Falta GOOGLE_SERVICE_ACCOUNT_JSON en variables de entorno.")
if not CALENDAR_ID:
    raise Exception("Falta GOOGLE_CALENDAR_ID en variables de entorno.")

# ====== Calendar Client ======
SCOPES = ["https://www.googleapis.com/auth/calendar"]
raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
info = json.loads(raw)
creds = Credentials.from_service_account_info(info, scopes=SCOPES)
service = build("calendar", "v3", credentials=creds, cache_discovery=False)

# ====== App ======
app = Flask(__name__)


def parse_datetime_es(payload: dict):
    """Parsea fecha/hora en español a datetime consciente de zona."""
    settings = {
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": TIMEZONE,
    }
    dt_text = payload.get("datetime_text")
    if dt_text:
        dt = dateparser.parse(dt_text, languages=["es"], settings=settings)
        if dt:
            return dt
    fecha = payload.get("fecha")
    hora = payload.get("hora")
    if fecha and hora:
        dt = dateparser.parse(f"{fecha} {hora}", languages=["es"], settings=settings)
        if dt:
            return dt
    if fecha and not hora:
        dt = dateparser.parse(f"{fecha} 10:00", languages=["es"], settings=settings)
        if dt:
            return dt
    return None


@app.get("/")
def health():
    return "OK - Bot de Citas ejecutándose", 200


@app.post("/cita")
def crear_cita():
    """Crea una cita en Google Calendar.

    Ejemplos de body JSON:
    1) Texto natural:
       {"nombre":"Pilar","datetime_text":"mañana a las 13:00","duracion_minutos":45,
        "email":"pilar@example.com","telefono":"+56912345678","comentario":"Asesoría"}

    2) Fecha/hora separadas:
       {"nombre":"Juan","fecha":"2025-08-12","hora":"09:30","duracion_minutos":30}
    """
    data = request.get_json(silent=True) or {}

    nombre = (data.get("nombre") or "Cliente").strip()
    duracion = int(data.get("duracion_minutos") or 30)
    email = (data.get("email") or "").strip()
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

    attendees = []
    if email:
        attendees.append({"email": email})

    event_body = {
        "summary": f"Cita con {nombre}",
        "description": "\n".join(description_items),
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
    }
    if attendees:
        event_body["attendees"] = attendees

    created = service.events().insert(
        calendarId=CALENDAR_ID,
        body=event_body,
        sendUpdates="all",  # enviará invitación si hay email
    ).execute()

    fecha_legible = start_dt.strftime("%d-%m-%Y %H:%M")
    return jsonify({
        "ok": True,
        "evento": {
            "id": created.get("id"),
            "htmlLink": created.get("htmlLink"),
            "start": created.get("start"),
            "end": created.get("end"),
        },
        "mensaje_para_cliente": (
            f"Listo {nombre}, tu cita quedó solicitada para el {fecha_legible} (hora {TIMEZONE})."
            + (" Revisa tu correo para la invitación." if email else "")
        )
    }), 201


# =====================
# Template de PRUEBAS
# =====================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Bot de Citas - servidor y pruebas")
    parser.add_argument("--runserver", action="store_true", help="Levanta el servidor Flask localmente")
    parser.add_argument("--test", action="store_true", help="Ejecuta una prueba contra el endpoint /cita")
    parser.add_argument("--url", default=os.getenv("BOT_URL", "http://localhost:8000/cita"),
                        help="URL del endpoint /cita (por defecto BOT_URL o http://localhost:8000/cita)")
    parser.add_argument("--modo", choices=["natural", "separado"], default="natural",
                        help="Modo de envío de fecha/hora para la prueba")
    parser.add_argument("--nombre", default="Cliente", help="Nombre del cliente para la prueba")
    parser.add_argument("--texto", help="Texto natural (ej. 'mañana a las 13:00')")
    parser.add_argument("--fecha", help="Fecha YYYY-MM-DD (modo 'separado')")
    parser.add_argument("--hora", help="Hora HH:MM 24h (modo 'separado')")
    parser.add_argument("--duracion", type=int, default=30, help="Duración en minutos (default 30)")
    parser.add_argument("--email", help="Email del cliente (opcional)")
    parser.add_argument("--telefono", help="Teléfono del cliente (opcional)")
    parser.add_argument("--comentario", help="Comentario/notas (opcional)")

    args = parser.parse_args()

    if args.runserver:
        # Ejecutar servidor local
        port = int(os.getenv("PORT", 8000))
        app.run(host="0.0.0.0", port=port)

    elif args.test:
        # Enviar prueba al endpoint /cita
        try:
            import requests
        except ImportError:
            raise SystemExit("Para usar --test instala requests: pip install requests")

        payload = {
            "nombre": args.nombre,
            "duracion_minutos": args.duracion,
        }
        if args.email:
            payload["email"] = args.email
        if args.telefono:
            payload["telefono"] = args.telefono
        if args.comentario:
            payload["comentario"] = args.comentario

        if args.modo == "natural":
            if not args.texto:
                raise SystemExit("En modo 'natural' debes pasar --texto (ej. 'mañana a las 13:00')")
            payload["datetime_text"] = args.texto
        else:
            if not (args.fecha and args.hora):
                raise SystemExit("En modo 'separado' debes pasar --fecha YYYY-MM-DD y --hora HH:MM")
            payload["fecha"] = args.fecha
            payload["hora"] = args.hora

        print("→ POST", args.url)
        print("→ Payload:", json.dumps(payload, ensure_ascii=False))
        try:
            resp = requests.post(args.url, json=payload, timeout=30)
        except Exception as e:
            raise SystemExit(f"Error de red: {e}")

        print("← Status:", resp.status_code)
        try:
            print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
        except ValueError:
            print(resp.text)

    else:
        parser.print_help()
