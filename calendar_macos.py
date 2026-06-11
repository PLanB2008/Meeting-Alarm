"""
macOS Calendar event source (EventKit via PyObjC).

Provides fetch_events(window_minutes) — returns today's events normalized to
the same dict format used by calendar_google.fetch_events(), so the rest of
the app can treat both sources identically:

    {
        "id":          str,              # unique, prefixed with "macos_"
        "summary":     str,              # event title
        "start":       {"dateTime": str},# ISO-8601 UTC
        "description": str,              # notes (scanned for meeting URLs)
        "hangoutLink": str | absent,     # set when EventKit URL is a meeting link
    }

MACOS_CAL_AVAILABLE is True once EventKit has been imported successfully.
"""

import datetime
import threading
from typing import Any

MACOS_CAL_AVAILABLE: bool = False

_ek_store:   Any  = None
_authorized: bool = False

_MEETING_KEYWORDS = [
    "meet.google.com",
    "zoom.us",
    "teams.microsoft.com",
    "webex.com",
    "gotomeeting.com",
    "whereby.com",
    "chime.aws",
]


def _is_meeting_url(url: str) -> bool:
    return any(kw in url for kw in _MEETING_KEYWORDS)


def _ensure_authorized() -> bool:
    """
    Import EventKit, create the store, and request calendar access if needed.
    Returns True when the app has read access.
    Called lazily on the first fetch so the permission prompt only appears
    when the monitor loop is already running.
    """
    global _ek_store, _authorized, MACOS_CAL_AVAILABLE

    if _authorized:
        return True

    try:
        import EventKit  # part of PyObjC — available because rumps is installed
        from Foundation import NSDate as _  # noqa: verify Foundation is loadable
    except ImportError:
        print("ℹ️  EventKit not available — macOS Calendar integration disabled.")
        return False

    MACOS_CAL_AVAILABLE = True

    if _ek_store is None:
        _ek_store = EventKit.EKEventStore.alloc().init()

    # EKAuthorizationStatus: 0=NotDetermined, 1=Restricted, 2=Denied,
    #                         3=Authorized/FullAccess, 4=WriteOnly (macOS 14+)
    status = EventKit.EKEventStore.authorizationStatusForEntityType_(0)

    if status == 3:
        _authorized = True
        return True
    if status in (1, 2):
        print("⚠️  macOS Calendar: access denied. "
              "Grant access in System Settings → Privacy & Security → Calendars.")
        return False
    if status == 4:
        print("⚠️  macOS Calendar: only write access granted — need full access.")
        return False

    # status == 0 (NotDetermined): ask the user
    sema = threading.Semaphore(0)
    granted_ref = [False]

    def _handler(granted, _error):
        granted_ref[0] = bool(granted)
        sema.release()

    try:
        # macOS 14+
        _ek_store.requestFullAccessToEventsWithCompletion_(_handler)
    except AttributeError:
        # macOS < 14
        _ek_store.requestAccessToEntityType_completion_(0, _handler)

    sema.acquire(timeout=30)
    _authorized = granted_ref[0]

    if not _authorized:
        print("⚠️  macOS Calendar: access not granted.")

    return _authorized


def list_calendars() -> list[str]:
    """Return the titles of all calendars visible in the macOS Calendar app."""
    if not _ensure_authorized():
        return []
    return sorted(str(cal.title()) for cal in (_ek_store.calendarsForEntityType_(0) or []))


def _resolve_calendars(names: list[str]):
    """Return EKCalendar objects matching the given names, or None to fetch all."""
    if not names:
        return None
    all_cals = _ek_store.calendarsForEntityType_(0) or []
    matched  = [cal for cal in all_cals if str(cal.title()) in names]
    if not matched:
        print(f"⚠️  macOS Calendar: none of {names!r} matched any calendar — fetching all.")
        return None
    return matched


def fetch_events(window_minutes: int = 1440, calendars: list[str] | None = None) -> list[dict]:
    """Return today's events from macOS Calendar app.

    calendars: list of calendar names to include; None/[] means fetch all.
    """
    if not _ensure_authorized():
        return []

    import EventKit  # noqa: already imported in _ensure_authorized
    from Foundation import NSDate

    now_local = datetime.datetime.now().astimezone()
    start_ts  = now_local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    end_ts    = start_ts + 86_400.0

    start_ns = NSDate.dateWithTimeIntervalSince1970_(start_ts)
    end_ns   = NSDate.dateWithTimeIntervalSince1970_(end_ts)

    cal_filter = _resolve_calendars(calendars or [])
    predicate  = _ek_store.predicateForEventsWithStartDate_endDate_calendars_(
        start_ns, end_ns, cal_filter
    )
    ek_events = _ek_store.eventsMatchingPredicate_(predicate) or []

    results = []
    for ev in ek_events:
        title = str(ev.title()) if ev.title() else "Untitled"

        start_ns_ev = ev.startDate()
        if not start_ns_ev:
            continue

        ts       = start_ns_ev.timeIntervalSince1970()
        start_dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)

        # Notes become the description so extract_meeting_url can scan for links
        notes       = ev.notes()
        description = str(notes) if notes else ""

        # EventKit URL field → hangoutLink when it's a known meeting URL,
        # otherwise prepend to description so it's still findable
        hangout_link = None
        ek_url = ev.URL()
        if ek_url:
            raw = str(ek_url.absoluteString()) if hasattr(ek_url, "absoluteString") else str(ek_url)
            if raw and raw not in ("None", ""):
                if _is_meeting_url(raw):
                    hangout_link = raw
                else:
                    description = raw + "\n" + description

        ev_id = str(ev.eventIdentifier()) if ev.eventIdentifier() else f"{title}_{ts}"

        entry: dict = {
            "id":          f"macos_{ev_id}",
            "summary":     title,
            "start":       {"dateTime": start_dt.isoformat()},
            "description": description,
        }
        end_ns_ev = ev.endDate()
        if end_ns_ev:
            end_ts = end_ns_ev.timeIntervalSince1970()
            end_dt = datetime.datetime.fromtimestamp(end_ts, tz=datetime.timezone.utc)
            entry["end"] = {"dateTime": end_dt.isoformat()}
        if hangout_link:
            entry["hangoutLink"] = hangout_link

        results.append(entry)

    return results
