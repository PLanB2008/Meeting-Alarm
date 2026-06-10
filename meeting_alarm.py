#!/usr/bin/env python3
"""
Meeting Alarm for macOS — Google Calendar Edition
Blacks out your screen and plays a loud alarm until you confirm.
 
Setup: See SETUP.md
Run:  python3 meeting_alarm.py
"""
 
import tkinter as tk
import threading
import datetime
import time
import subprocess
import os
import sys
import json
import pickle
import re
from pathlib import Path

import yaml
 
# ── Google Calendar imports (installed during setup) ──────────────────────────
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    GCAL_AVAILABLE = True
except ImportError:
    GCAL_AVAILABLE = False
 
# ── Config ────────────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_FILE   = Path.home() / ".meeting_alarm_token.pickle"
CREDS_FILE   = Path(__file__).parent / "credentials.json"
 
ALERT_MINUTES_BEFORE = 2   # show alarm this many minutes before meeting start
POLL_INTERVAL_SECS   = 30  # how often to check calendar (seconds)
ALARM_VOLUME         = 1.0 # 0.0 (silent) → 1.0 (full device volume)

BLACKLIST_FILE = Path(__file__).parent / "blacklist.yaml"

# ── Blacklist ─────────────────────────────────────────────────────────────────
def load_blacklist() -> list[re.Pattern]:
    """Read blacklist.yaml and return compiled regex patterns."""
    if not BLACKLIST_FILE.exists():
        return []
    try:
        with open(BLACKLIST_FILE) as f:
            data = yaml.safe_load(f) or {}
        return [re.compile(p, re.IGNORECASE) for p in (data.get("patterns") or [])]
    except Exception as e:
        print(f"⚠️  Could not load blacklist: {e}")
        return []

def is_blacklisted(title: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(title) for p in patterns)

# ── Sound ─────────────────────────────────────────────────────────────────────
from typing import Any
sd: Any
sf: Any
try:
    import sounddevice as sd
    import soundfile as sf
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    sd = None
    sf = None
    SOUNDDEVICE_AVAILABLE = False

# ── CoreAudio device volume (ctypes) ──────────────────────────────────────────
import ctypes
import ctypes.util
import struct as _struct

def _fourcc(s: str) -> int:
    return _struct.unpack('>I', s.encode())[0]

class _PropAddr(ctypes.Structure):
    _fields_ = [('mSelector', ctypes.c_uint32),
                ('mScope',    ctypes.c_uint32),
                ('mElement',  ctypes.c_uint32)]

_CA = ctypes.CDLL(ctypes.util.find_library('CoreAudio'))
_CF = ctypes.CDLL(ctypes.util.find_library('CoreFoundation'))
_CF.CFStringGetCString.restype  = ctypes.c_bool
_CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_CF.CFRelease.argtypes = [ctypes.c_void_p]
_CF.CFRelease.restype  = None

_CA_SYS  = ctypes.c_uint32(1)   # kAudioObjectSystemObject
_CA_UTF8 = 0x08000100

def _ca_find_device_id(name: str) -> int | None:
    addr = _PropAddr(_fourcc('dev#'), _fourcc('glob'), 0)
    size = ctypes.c_uint32(0)
    _CA.AudioObjectGetPropertyDataSize(_CA_SYS, ctypes.byref(addr), 0, None, ctypes.byref(size))
    ids = (ctypes.c_uint32 * (size.value // 4))()
    _CA.AudioObjectGetPropertyData(_CA_SYS, ctypes.byref(addr), 0, None, ctypes.byref(size), ids)
    for dev_id in ids:
        na  = _PropAddr(_fourcc('lnam'), _fourcc('glob'), 0)
        ns  = ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
        cf  = ctypes.c_void_p()
        err = _CA.AudioObjectGetPropertyData(dev_id, ctypes.byref(na), 0, None, ctypes.byref(ns), ctypes.byref(cf))
        if err or not cf.value:
            continue
        buf = ctypes.create_string_buffer(256)
        ok  = _CF.CFStringGetCString(cf, buf, 256, _CA_UTF8)
        _CF.CFRelease(cf)
        if ok and buf.value.decode() == name:
            return int(dev_id)
    return None

def _ca_get_volume(dev_id: int) -> float | None:
    # Try VirtualMainVolume ('vmvc') first, then per-channel scalar ('volm')
    for sel, elem in [(_fourcc('vmvc'), 0), (_fourcc('volm'), 0), (_fourcc('volm'), 1)]:
        addr = _PropAddr(sel, _fourcc('outp'), elem)
        size = ctypes.c_uint32(4)
        vol  = ctypes.c_float()
        if _CA.AudioObjectGetPropertyData(
                ctypes.c_uint32(dev_id), ctypes.byref(addr), 0, None,
                ctypes.byref(size), ctypes.byref(vol)) == 0:
            return float(vol.value)
    return None

def _ca_set_volume(dev_id: int, volume: float) -> None:
    vol = ctypes.c_float(max(0.0, min(1.0, volume)))
    for sel, elem in [(_fourcc('vmvc'), 0), (_fourcc('volm'), 0), (_fourcc('volm'), 1)]:
        addr = _PropAddr(sel, _fourcc('outp'), elem)
        _CA.AudioObjectSetPropertyData(
            ctypes.c_uint32(dev_id), ctypes.byref(addr), 0, None,
            ctypes.c_uint32(4), ctypes.byref(vol))

# ── Playback helpers ──────────────────────────────────────────────────────────
def find_builtin_speaker_id() -> int | None:
    if not SOUNDDEVICE_AVAILABLE:
        return None
    keywords = ['built-in output', 'macbook pro speakers', 'macbook air speakers',
                'internal speakers', 'built-in speaker', 'lautsprecher',
                'macbook pro-', 'macbook air-']
    for i, dev in enumerate(sd.query_devices()):
        if dev['max_output_channels'] > 0 and any(kw in dev['name'].lower() for kw in keywords):
            return i
    return None

def _ensure_wav(mp3_path: str) -> str | None:
    """Convert an MP3 to WAV using macOS afconvert; cache alongside the original."""
    wav_path = str(Path(mp3_path).with_suffix('.wav'))
    if not Path(wav_path).exists():
        result = subprocess.run(
            ['afconvert', '-f', 'WAVE', '-d', 'LEI16', mp3_path, wav_path],
            capture_output=True
        )
        if result.returncode != 0:
            return None
    return wav_path

_builtin_speaker_id: int | None = None
_ca_dev_id: int | None = None
_ca_dev_queried: bool  = False
_wav_cache: dict[str, str] = {}

def play_alarm(stop_event: threading.Event):
    """Loop a sound through the built-in speaker at ALARM_VOLUME until stop_event is set."""
    global _builtin_speaker_id, _ca_dev_id, _ca_dev_queried

    sounds = [
        "./chrysalyn-loopable-phone-chime-notification-sound-547390.mp3",
        #"/System/Library/Sounds/Hero.aiff",
        #"/System/Library/Sounds/Blow.aiff",
    ]
    sound = next((s for s in sounds if os.path.exists(s)), None)

    if SOUNDDEVICE_AVAILABLE and _builtin_speaker_id is None:
        _builtin_speaker_id = find_builtin_speaker_id()

    # Resolve CoreAudio device ID once so we can control its volume directly
    if not _ca_dev_queried:
        _ca_dev_queried = True
        if SOUNDDEVICE_AVAILABLE and _builtin_speaker_id is not None:
            dev_name = sd.query_devices(_builtin_speaker_id)['name']
            _ca_dev_id = _ca_find_device_id(dev_name)

    # Resolve playable path (convert MP3 → WAV once if needed)
    play_path = None
    if sound and SOUNDDEVICE_AVAILABLE:
        if sound.endswith('.mp3'):
            play_path = _wav_cache.get(sound) or _ensure_wav(sound)
            if play_path:
                _wav_cache[sound] = play_path
        else:
            play_path = sound

    # Save current device volume, apply ALARM_VOLUME, restore on exit
    original_volume: float | None = None
    if _ca_dev_id is not None:
        original_volume = _ca_get_volume(_ca_dev_id)
        _ca_set_volume(_ca_dev_id, ALARM_VOLUME)

    try:
        while not stop_event.is_set():
            if play_path and SOUNDDEVICE_AVAILABLE:
                try:
                    data, samplerate = sf.read(play_path)
                    sd.play(data, samplerate, device=_builtin_speaker_id)
                    while not stop_event.is_set() and sd.get_stream().active:
                        time.sleep(0.05)
                    sd.stop()
                except Exception:
                    subprocess.run(["osascript", "-e", 'beep 3'], check=False)
                    time.sleep(1)
            elif sound:
                # sounddevice not installed — fall back to afplay (uses default device)
                subprocess.Popen(['afplay', sound],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL).wait()
            else:
                subprocess.run(["osascript", "-e", 'beep 3'], check=False)
                time.sleep(1)
            time.sleep(0.2)
    finally:
        if _ca_dev_id is not None and original_volume is not None:
            _ca_set_volume(_ca_dev_id, original_volume)

# ── Fullscreen blackout overlay ───────────────────────────────────────────────
def show_alarm_window(event_title: str, event_time: str, meeting_url: str | None):
    """
    Block the screen with a fullscreen overlay.
    Returns only after the user clicks the confirm button.
    """
    stop_sound = threading.Event()
    sound_thread = threading.Thread(target=play_alarm, args=(stop_sound,), daemon=True)
    sound_thread.start()
 
    root = tk.Tk()
    root.title("⏰ MEETING NOW")
 
    # Cover every screen
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"{sw}x{sh}+0+0")
    root.configure(bg="#0a0a0a")
    root.attributes("-topmost", True)

    # Pulsing red border effect
    canvas = tk.Canvas(root, bg="#0a0a0a", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    # ── Content ──
    frame = tk.Frame(canvas, bg="#0a0a0a")
    canvas.create_window(sw // 2, sh // 2, window=frame)

    tk.Label(frame, text="⏰", font=("SF Pro Display", 80), bg="#0a0a0a",
             fg="#ff3b30").pack(pady=(0, 10))
 
    tk.Label(frame, text="MEETING STARTING NOW", font=("SF Pro Display", 36, "bold"),
             bg="#0a0a0a", fg="#ff3b30").pack()
 
    tk.Label(frame, text=event_title, font=("SF Pro Display", 28),
             bg="#0a0a0a", fg="#ffffff", wraplength=sw - 200).pack(pady=(20, 4))
 
    tk.Label(frame, text=event_time, font=("SF Pro Rounded", 20),
             bg="#0a0a0a", fg="#aaaaaa").pack(pady=(0, 40))
 
    def dismiss():
        stop_sound.set()
        if meeting_url:
            subprocess.Popen(["open", meeting_url])
        root.destroy()
 
    def dismiss_no_url():
        stop_sound.set()
        root.destroy()
 
    btn_frame = tk.Frame(frame, bg="#0a0a0a")
    btn_frame.pack()
 
    if meeting_url:
        tk.Button(
            btn_frame, text="🚀  Join Meeting",
            font=("SF Pro Display", 22, "bold"),
            bg="#30d158", fg="#000000", activebackground="#25a244",
            padx=40, pady=18, bd=0, cursor="hand2",
            command=dismiss
        ).pack(side="left", padx=12)
 
    tk.Button(
        btn_frame,
        text="✓  I'm On It" if not meeting_url else "Dismiss",
        font=("SF Pro Display", 18),
        bg="#1c1c1e", fg="#ffffff", activebackground="#2c2c2e",
        padx=30, pady=18, bd=0, cursor="hand2",
        command=dismiss_no_url
    ).pack(side="left", padx=12)
 
    # Pulsing border animation
    pulse_colors = ["#ff3b30", "#ff6961", "#ff3b30", "#cc0000"]
    pulse_idx = [0]
 
    def pulse():
        if not root.winfo_exists():
            return
        c = pulse_colors[pulse_idx[0] % len(pulse_colors)]
        canvas.configure(highlightbackground=c)
        root.configure(bg=c if pulse_idx[0] % 2 == 0 else "#0a0a0a")
        # flash the canvas background subtly
        canvas.configure(bg="#1a0000" if pulse_idx[0] % 2 == 0 else "#0a0a0a")
        frame.configure(bg="#1a0000" if pulse_idx[0] % 2 == 0 else "#0a0a0a")
        pulse_idx[0] += 1
        root.after(600, pulse)
 
    pulse()
    root.mainloop()
    stop_sound.set()   # ensure sound stops if window closed any other way
 
# ── Google Calendar ───────────────────────────────────────────────────────────
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
 
 
def fetch_upcoming_events(service, window_minutes: int = 60):
    """Return events starting within the next `window_minutes` minutes."""
    now = datetime.datetime.now(datetime.timezone.utc)
    time_min = now.isoformat().replace('+00:00', 'Z')
    time_max = (now + datetime.timedelta(minutes=window_minutes)).isoformat().replace('+00:00', 'Z')
 
    result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])
 
 
def extract_meeting_url(event: dict) -> str | None:
    """Try to find a Google Meet / Zoom / Teams link in the event."""
    # Google Meet
    entry = event.get("hangoutLink")
    if entry:
        return entry
    # Check description for common URLs
    desc = event.get("description", "") or ""
    for line in desc.splitlines():
        for keyword in ["meet.google.com", "zoom.us/j/", "teams.microsoft.com"]:
            if keyword in line:
                # crude URL extraction
                for word in line.split():
                    if keyword in word:
                        return word.strip().strip("<>")
    return None
 
 
def format_event_time(event: dict) -> str:
    start = event["start"].get("dateTime", event["start"].get("date"))
    try:
        dt = datetime.datetime.fromisoformat(start)
        return dt.strftime("%-I:%M %p")
    except Exception:
        return start
 
# ── Already-alerted set (avoid double-triggering same event) ─────────────────
alerted: set[str] = set()
 
def monitor_loop():
    if not GCAL_AVAILABLE:
        print("❌  Google Calendar libraries not installed. Run: pip3 install -r requirements.txt")
        sys.exit(1)
 
    print("🔐  Authenticating with Google Calendar…")
    creds   = get_credentials()
    service = build("calendar", "v3", credentials=creds)
    local_now  = datetime.datetime.now(datetime.timezone.utc).astimezone()
    end_of_day = local_now.replace(hour=23, minute=59, second=59, microsecond=0)
    minutes_until_end = int((end_of_day - local_now).total_seconds() / 60)
    blacklist    = load_blacklist()
    all_today    = [ev for ev in fetch_upcoming_events(service, window_minutes=minutes_until_end)
                    if ev["start"].get("dateTime")]
    active_today = [ev for ev in all_today if not is_blacklisted(ev.get("summary", ""), blacklist)]

    next_ev = next(
        (ev for ev in active_today
         if (datetime.datetime.fromisoformat(ev["start"]["dateTime"]) - local_now).total_seconds() > 0),
        None
    )
    if next_ev:
        title   = next_ev.get("summary", "Untitled Meeting")
        delta_m = int((datetime.datetime.fromisoformat(next_ev["start"]["dateTime"]) - local_now).total_seconds() / 60)
        next_str = f"Next meeting \"{title}\" in {delta_m} min"
    else:
        next_str = "No more meetings today"

    print(f"✅  Connected. Checking every {POLL_INTERVAL_SECS}s for meetings within "
          f"{ALERT_MINUTES_BEFORE} min… {next_str}\n    Press Ctrl+C to stop.\n")

    if all_today:
        print("📅  Today's meetings:")
        for ev in all_today:
            title    = ev.get("summary", "Untitled Meeting")
            t_str    = format_event_time(ev)
            url      = extract_meeting_url(ev)
            link     = f"  🔗 {url}" if url else ""
            delta_m  = int((datetime.datetime.fromisoformat(ev["start"]["dateTime"]) - local_now).total_seconds() / 60)
            if delta_m > 0:
                when = f"in {delta_m} min"
            elif delta_m > -60:
                when = f"{-delta_m} min ago"
            else:
                when = "earlier today"
            if is_blacklisted(title, blacklist):
                print(f"   {t_str:>8}  🔕 {title}  ({when}){link}")
            else:
                print(f"   {t_str:>8}  🔔 {title}  ({when}){link}")
    else:
        print("📅  No meetings today.")
    print()
 
    while True:
        try:
            blacklist = load_blacklist()
            events = fetch_upcoming_events(service, window_minutes=ALERT_MINUTES_BEFORE + 1)
            now = datetime.datetime.now(datetime.timezone.utc)

            for ev in events:
                ev_id  = ev.get("id", "")
                start  = ev["start"].get("dateTime")
                if not start or ev_id in alerted:
                    continue
                if is_blacklisted(ev.get("summary", ""), blacklist):
                    continue
 
                start_dt = datetime.datetime.fromisoformat(start)
                delta    = (start_dt - now).total_seconds() / 60  # minutes until start
 
                if -1 <= delta <= ALERT_MINUTES_BEFORE:
                    alerted.add(ev_id)
                    title   = ev.get("summary", "Untitled Meeting")
                    t_str   = format_event_time(ev)
                    url     = extract_meeting_url(ev)
 
                    print(f"🔔  Alarm triggered: {title!r} at {t_str}")
                    # Run alarm in main thread (tkinter requirement)
                    show_alarm_window(title, t_str, url)
 
        except KeyboardInterrupt:
            print("\n👋  Meeting alarm stopped.")
            sys.exit(0)
        except Exception as e:
            print(f"⚠️  Error: {e}")
 
        time.sleep(POLL_INTERVAL_SECS)
 
 
# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Quick demo mode: python3 meeting_alarm.py --demo
    if "--demo" in sys.argv:
        print("🎬  Demo mode — showing alarm in 1 second…")
        time.sleep(1)
        show_alarm_window(
            "Weekly Team Standup",
            "10:00 AM",
            "https://meet.google.com/abc-defg-hij"
        )
        print("✅  Demo complete.")
    else:
        monitor_loop()
 
