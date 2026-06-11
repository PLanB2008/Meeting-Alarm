"""
Google Calendar event source.

Provides get_credentials() and fetch_events(service, window_minutes).
Import GCAL_AVAILABLE to check whether the Google libraries are installed.
"""

import datetime
import pickle
import sys
from pathlib import Path

from typing import Any as _Any
Credentials: _Any = None
InstalledAppFlow: _Any = None
Request: _Any = None
build: _Any = None
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    GCAL_AVAILABLE = True
except ImportError:
    GCAL_AVAILABLE = False

SCOPES     = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_FILE = Path.home() / ".meeting_alarm_token.pickle"
CREDS_FILE = Path(__file__).parent / "credentials.json"


def get_credentials():
    creds = None
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                print(f"\n❌  credentials.json not found at {CREDS_FILE}")
                print("    See SETUP.md for how to create it.\n")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return creds


def fetch_events(service, window_minutes: int = 60,
                 time_min: str | None = None, time_max: str | None = None) -> list[dict]:
    """Return Google Calendar events in the given time range.

    time_min / time_max override the default window (now → now+window_minutes).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    if time_min is None:
        time_min = now.isoformat().replace('+00:00', 'Z')
    if time_max is None:
        time_max = (now + datetime.timedelta(minutes=window_minutes)).isoformat().replace('+00:00', 'Z')

    result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])
