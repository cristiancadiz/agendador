import os
import json
from typing import List, Dict, Any, Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]

class CalendarClient:
    def __init__(self):
        raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not raw:
            raise Exception("Falta GOOGLE_SERVICE_ACCOUNT_JSON")

        info = json.loads(raw)
        self.creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        # cache_discovery=False evita warnings en server sin cache
        self.service = build("calendar", "v3", credentials=self.creds, cache_discovery=False)

    def insert_event(
        self,
        calendar_id: str,
        summary: str,
        start_dt,
        end_dt,
        timezone: str,
        description: str = "",
        attendees: Optional[List[Dict[str, str]]] = None,
        send_updates: str = "none",
    ) -> Dict[str, Any]:
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": timezone},
        }
        if attendees:
            body["attendees"] = attendees

        return self.service.events().insert(
            calendarId=calendar_id,
            body=body,
            sendUpdates=send_updates,
        ).execute()
