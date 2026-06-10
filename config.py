"""
Loads config.yaml from the project directory.
Falls back to built-in defaults if the file is missing or a key is absent.
"""

from pathlib import Path
import yaml

_CONFIG_FILE = Path(__file__).parent / "config.yaml"

_DEFAULTS = {
    "calendar_source":      "google",
    "macos_calendars":      [],
    "alert_minutes_before": 2,
    "poll_interval_secs":   30,
    "alarm_volume":         0.8,
}


def _load() -> dict:
    if not _CONFIG_FILE.exists():
        return _DEFAULTS.copy()
    try:
        with open(_CONFIG_FILE) as f:
            data = yaml.safe_load(f) or {}
        return {**_DEFAULTS, **data}
    except Exception as e:
        print(f"⚠️  Could not read config.yaml: {e} — using defaults")
        return _DEFAULTS.copy()


_cfg = _load()

CALENDAR_SOURCE:      str       = str(_cfg["calendar_source"]).lower()
MACOS_CALENDARS:      list[str] = list(_cfg.get("macos_calendars") or [])
ALERT_MINUTES_BEFORE: int       = int(_cfg["alert_minutes_before"])
POLL_INTERVAL_SECS:   int       = int(_cfg["poll_interval_secs"])
ALARM_VOLUME:         float     = float(_cfg["alarm_volume"])
