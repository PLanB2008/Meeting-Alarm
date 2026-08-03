#!/usr/bin/env python3
"""
Meeting Alarm for macOS
Blacks out your screen and plays a loud alarm until you confirm.
Supports Google Calendar and the macOS Calendar app.
 
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
import re
import queue
from pathlib import Path

import yaml
import rumps

import calendar_google
import calendar_macos
import config

BLACKLIST_FILE = Path(__file__).parent / "blacklist.yaml"


def _tk_btn(parent, text: str, command, bg: str, fg: str = "white", **kw) -> tk.Label:
    """Colored button that works on macOS (tk.Button ignores bg/fg there)."""
    lbl = tk.Label(parent, text=text, bg=bg, fg=fg, cursor="hand2", **kw)
    lbl.bind("<Button-1>", lambda _: command())
    return lbl


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

    sound = next((s for s in config.ALARM_SOUNDS if os.path.exists(s)), None)

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
        _ca_set_volume(_ca_dev_id, config.ALARM_VOLUME)

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
def _alarm_window_direct(event_title: str, event_time: str, meeting_url: str | None) -> None:
    """Tkinter fullscreen alarm. Must run in a process where tkinter owns NSApplication."""
    stop_sound = threading.Event()
    sound_thread = threading.Thread(target=play_alarm, args=(stop_sound,), daemon=True)
    sound_thread.start()
 
    root = tk.Tk()
    root.title("Meeting Approaching")
    try:
        from AppKit import NSApplication  # type: ignore[import-untyped]
        NSApplication.sharedApplication().setActivationPolicy_(1)  # accessory — no Dock icon
    except Exception:
        pass

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

    tk.Label(frame, text="MEETING APPROACHING", font=("SF Pro Display", 36, "bold"),
             bg="#0a0a0a", fg="#ff3b30").pack()

    tk.Label(frame, text=event_title, font=("SF Pro Display", 28),
             bg="#0a0a0a", fg="#ffffff", wraplength=sw - 200).pack(pady=(20, 4))

    tk.Label(frame, text=event_time, font=("SF Pro Rounded", 20),
             bg="#0a0a0a", fg="#aaaaaa").pack(pady=(0, 40))

    def dismiss():
        if after_id[0] is not None:
            root.after_cancel(after_id[0])
        stop_sound.set()
        if meeting_url:
            subprocess.Popen(["open", meeting_url])
        root.destroy()

    def dismiss_no_url():
        if after_id[0] is not None:
            root.after_cancel(after_id[0])
        stop_sound.set()
        root.destroy()

    muted = [False]

    def mute_alarm():
        if not muted[0]:
            muted[0] = True
            stop_sound.set()
            mute_btn.configure(text="🔇  Muted", bg="#2c2c2e", fg="#666666",
                               cursor="arrow")
            mute_btn.unbind("<Button-1>")

    btn_frame = tk.Frame(frame, bg="#0a0a0a")
    btn_frame.pack()

    if meeting_url:
        _tk_btn(btn_frame, "🚀  Join Meeting", dismiss,
                bg="#30d158", fg="#000000",
                font=("SF Pro Display", 22, "bold"),
                padx=40, pady=18).pack(side="left", padx=12)

    mute_btn = _tk_btn(btn_frame, "🔔  Mute Alarm", mute_alarm,
                       bg="#3a3a3c", fg="#ffffff",
                       font=("SF Pro Display", 18),
                       padx=30, pady=18)
    mute_btn.pack(side="left", padx=12)

    _tk_btn(btn_frame,
            "✓  I'm On It" if not meeting_url else "Dismiss",
            dismiss_no_url,
            bg="#1c1c1e", fg="#ffffff",
            font=("SF Pro Display", 18),
            padx=30, pady=18).pack(side="left", padx=12)
 
    # Pulsing border animation
    pulse_colors = ["#ff3b30", "#ff6961", "#ff3b30", "#cc0000"]
    pulse_idx = [0]
    after_id: list[str | None] = [None]

    def pulse():
        try:
            c = pulse_colors[pulse_idx[0] % len(pulse_colors)]
            canvas.configure(highlightbackground=c)
            root.configure(bg=c if pulse_idx[0] % 2 == 0 else "#0a0a0a")
            canvas.configure(bg="#1a0000" if pulse_idx[0] % 2 == 0 else "#0a0a0a")
            frame.configure(bg="#1a0000" if pulse_idx[0] % 2 == 0 else "#0a0a0a")
            pulse_idx[0] += 1
            after_id[0] = root.after(600, pulse)
        except tk.TclError:
            pass

    after_id[0] = root.after(600, pulse)
    # Manual event loop instead of mainloop() — avoids AppKit run-loop conflict
    # when called from a rumps NSTimer callback.
    while True:
        try:
            root.update()
            root.update_idletasks()
        except tk.TclError:
            break
        time.sleep(0.01)
    stop_sound.set()   # ensure sound stops if window closed any other way


def show_alarm_window(event_title: str, event_time: str, meeting_url: str | None) -> None:
    """Launch the alarm in a subprocess so tkinter and rumps don't share NSApplication."""
    payload = json.dumps({'title': event_title, 'time': event_time, 'url': meeting_url or ''})
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), '--alarm', payload],
        close_fds=True,
    )
    _child_procs[:] = [p for p in _child_procs if p.poll() is None]
    _child_procs.append(proc)


def _prefs_window_direct() -> None:
    """Preferences window — runs in its own subprocess so tkinter owns NSApplication."""
    _CONFIG_FILE = Path(__file__).parent / "config.yaml"

    _DEFAULTS = {
        "calendar_source":      "google",
        "macos_calendars":      [],
        "alert_minutes_before": 2,
        "poll_interval_secs":   30,
        "alarm_volume":         0.8,
        "use_24h_time":         True,
        "alarm_sounds":         ["sounds/tunetank.com_notification-warning-alert.wav"],
    }

    try:
        with open(_CONFIG_FILE) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    for k, v in _DEFAULTS.items():
        cfg.setdefault(k, v)

    BG       = "#1e1e1e"
    FG       = "#e0e0e0"
    ACCENT   = "#4a9eff"
    ENTRY_BG = "#2d2d2d"

    root = tk.Tk()
    root.title("Meeting Alarm — Preferences")
    try:
        from AppKit import NSApplication  # type: ignore[import-untyped]
        NSApplication.sharedApplication().setActivationPolicy_(1)  # accessory — no Dock icon
    except Exception:
        pass
    root.configure(bg=BG)
    root.resizable(False, False)

    # Center on screen
    root.update_idletasks()
    root.geometry("+%d+%d" % (
        root.winfo_screenwidth() // 2 - 260,
        root.winfo_screenheight() // 2 - 280,
    ))

    def _lbl(r: int, text: str) -> None:
        tk.Label(root, text=text, bg=BG, fg=FG, font=("Helvetica Neue", 12),
                 justify="right", anchor="e").grid(
                 row=r, column=0, sticky="ne", padx=(20, 8), pady=6)

    tk.Label(root, text="Preferences", bg=BG, fg=ACCENT,
             font=("Helvetica Neue", 17, "bold")).grid(
             row=0, column=0, columnspan=2, padx=20, pady=(18, 14))

    row = 1

    # Calendar source
    _lbl(row, "Calendar source:")
    cal_var = tk.StringVar(value=cfg["calendar_source"])
    cal_f = tk.Frame(root, bg=BG)
    cal_f.grid(row=row, column=1, sticky="w", padx=(0, 20), pady=6)
    for val, label in [("google", "Google Calendar"), ("macos", "macOS Calendar")]:
        tk.Radiobutton(cal_f, text=label, variable=cal_var, value=val,
                       bg=BG, fg=FG, selectcolor=ENTRY_BG, activebackground=BG,
                       font=("Helvetica Neue", 12)).pack(side="left", padx=(0, 14))
    row += 1

    # macOS calendars
    _lbl(row, "macOS calendars\n(one per line):")
    macos_txt = tk.Text(root, width=34, height=4, bg=ENTRY_BG, fg=FG,
                        insertbackground=FG, relief="flat", font=("Menlo", 11),
                        padx=6, pady=4)
    macos_txt.grid(row=row, column=1, sticky="w", padx=(0, 20), pady=6)
    macos_txt.insert("1.0", "\n".join(cfg.get("macos_calendars") or []))
    row += 1

    # Alert minutes before
    _lbl(row, "Alert minutes before:")
    alert_var = tk.IntVar(value=cfg["alert_minutes_before"])
    tk.Spinbox(root, from_=0, to=60, textvariable=alert_var, width=5,
               bg=ENTRY_BG, fg=FG, buttonbackground=ENTRY_BG, insertbackground=FG,
               relief="flat", font=("Helvetica Neue", 12)).grid(
               row=row, column=1, sticky="w", padx=(0, 20), pady=6)
    row += 1

    # Poll interval
    _lbl(row, "Poll interval (secs):")
    poll_var = tk.IntVar(value=cfg["poll_interval_secs"])
    tk.Spinbox(root, from_=10, to=600, textvariable=poll_var, width=5,
               bg=ENTRY_BG, fg=FG, buttonbackground=ENTRY_BG, insertbackground=FG,
               relief="flat", font=("Helvetica Neue", 12)).grid(
               row=row, column=1, sticky="w", padx=(0, 20), pady=6)
    row += 1

    # Alarm volume
    _lbl(row, "Alarm volume:")
    vol_var = tk.DoubleVar(value=cfg["alarm_volume"])
    vol_f = tk.Frame(root, bg=BG)
    vol_f.grid(row=row, column=1, sticky="w", padx=(0, 20), pady=6)
    vol_label = tk.Label(vol_f, text=f"{cfg['alarm_volume']:.0%}", bg=BG, fg=FG,
                         font=("Helvetica Neue", 11), width=5)
    def _update_vol_label(*_):
        vol_label.config(text=f"{vol_var.get():.0%}")
    vol_var.trace_add("write", _update_vol_label)
    tk.Scale(vol_f, variable=vol_var, from_=0.0, to=1.0, resolution=0.05,
             orient="horizontal", length=180, showvalue=False,
             bg=BG, fg=FG, troughcolor=ENTRY_BG, highlightthickness=0).pack(side="left")
    vol_label.pack(side="left", padx=(6, 0))
    row += 1

    # 24-hour time
    _lbl(row, "24-hour time:")
    h24_var = tk.BooleanVar(value=bool(cfg["use_24h_time"]))
    tk.Checkbutton(root, variable=h24_var,
                   bg=BG, fg=FG, selectcolor=ENTRY_BG, activebackground=BG,
                   font=("Helvetica Neue", 12)).grid(
                   row=row, column=1, sticky="w", padx=(0, 20), pady=6)
    row += 1

    # Alarm sounds
    _lbl(row, "Alarm sounds\n(one per line):")
    sounds_txt = tk.Text(root, width=44, height=4, bg=ENTRY_BG, fg=FG,
                         insertbackground=FG, relief="flat", font=("Menlo", 10),
                         padx=6, pady=4)
    sounds_txt.grid(row=row, column=1, sticky="w", padx=(0, 20), pady=6)
    sounds_txt.insert("1.0", "\n".join(cfg.get("alarm_sounds") or []))
    row += 1

    # Status label
    status_var = tk.StringVar(value="")
    tk.Label(root, textvariable=status_var, bg=BG, fg="#88cc88",
             font=("Helvetica Neue", 11)).grid(
             row=row, column=0, columnspan=2, pady=(4, 0))
    row += 1

    def _save() -> None:
        new_cfg = {
            "calendar_source":      cal_var.get(),
            "macos_calendars":      [l.strip() for l in macos_txt.get("1.0", "end").splitlines() if l.strip()],
            "alert_minutes_before": alert_var.get(),
            "poll_interval_secs":   poll_var.get(),
            "alarm_volume":         round(vol_var.get(), 2),
            "use_24h_time":         bool(h24_var.get()),
            "alarm_sounds":         [l.strip() for l in sounds_txt.get("1.0", "end").splitlines() if l.strip()],
        }
        try:
            header = "# Meeting Alarm configuration\n# Changes take effect on the next restart.\n\n"
            with open(_CONFIG_FILE, "w") as f:
                f.write(header)
                yaml.dump(new_cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            status_var.set("Saved — restart to apply")
            restart_btn.pack(side="left", padx=(12, 0))
        except Exception as e:
            status_var.set(f"Error saving: {e}")

    def _restart() -> None:
        import os, signal
        ppid = os.getppid()
        subprocess.Popen([sys.executable, str(Path(__file__).resolve())], close_fds=True)
        try:
            os.kill(ppid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        root.destroy()

    btn_f = tk.Frame(root, bg=BG)
    btn_f.grid(row=row, column=0, columnspan=2, pady=(10, 22))
    _tk_btn(btn_f, "Save", _save, bg=ACCENT,
            font=("Helvetica Neue", 13, "bold"), padx=28, pady=8).pack(side="left")
    restart_btn = _tk_btn(btn_f, "Restart Now", _restart, bg="#cc6644",
                          font=("Helvetica Neue", 13, "bold"), padx=28, pady=8)

    root.mainloop()


def show_prefs_window() -> None:
    """Launch the preferences window in a subprocess."""
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), '--prefs'],
        close_fds=True,
    )
    _child_procs[:] = [p for p in _child_procs if p.poll() is None]
    _child_procs.append(proc)


def extract_meeting_url(event: dict) -> str | None:
    """Try to find a Google Meet / Zoom / Teams link in the event."""
    import html as _html
    if entry := event.get("hangoutLink"):
        return entry
    desc = _html.unescape(event.get("description", "") or "")
    keywords = ["meet.google.com", "zoom.us/j/", "teams.microsoft.com"]
    # href="..." attributes (Google Calendar wraps URLs in HTML)
    for m in re.finditer(r'href=["\']([^"\']+)["\']', desc):
        if any(kw in m.group(1) for kw in keywords):
            return m.group(1)
    # Plain text fallback
    for line in desc.splitlines():
        for kw in keywords:
            if kw in line:
                for word in line.split():
                    word = word.strip().strip("<>").rstrip(".,;)")
                    if kw in word:
                        return word
    return None
 
 
def format_delta(delta_m: int) -> str:
    """Return a human-readable relative time string for a delta in minutes."""
    if delta_m > 0:
        h, m = divmod(delta_m, 60)
        if h and m:
            return f"in {h}h {m} min"
        elif h:
            return f"in {h}h"
        else:
            return f"in {m} min"
    elif delta_m == 0:
        return "now"
    elif delta_m > -60:
        return f"{-delta_m} min ago"
    else:
        return "earlier today"


def format_event_time(event: dict) -> str:
    start = event["start"].get("dateTime", event["start"].get("date"))
    try:
        dt = datetime.datetime.fromisoformat(start)
        return dt.strftime("%H:%M") if config.USE_24H_TIME else dt.strftime("%-I:%M %p")
    except Exception:
        return start


def format_event_time_range(event: dict) -> str:
    """Return a start–end time string, e.g. '09:30–10:00'."""
    start_str = event["start"].get("dateTime")
    end_str   = (event.get("end") or {}).get("dateTime")
    fmt = "%H:%M" if config.USE_24H_TIME else "%-I:%M"
    try:
        s = datetime.datetime.fromisoformat(start_str).strftime(fmt)
        if end_str:
            e = datetime.datetime.fromisoformat(end_str).strftime(fmt)
            return f"{s}–{e}"
        return s
    except Exception:
        return start_str or ""


def _event_style(ev: dict, local_now: datetime.datetime, blacklist: list) -> str:
    """Return a display style key for the event: past | current | future | blacklisted."""
    start_dt = datetime.datetime.fromisoformat(ev["start"]["dateTime"])
    end_str  = (ev.get("end") or {}).get("dateTime")
    end_dt   = datetime.datetime.fromisoformat(end_str) if end_str else start_dt + datetime.timedelta(hours=1)
    if end_dt <= local_now:
        return "past"
    if is_blacklisted(ev.get("summary", ""), blacklist):
        return "blacklisted"
    if start_dt <= local_now:
        return "current"
    return "future"


def _menu_tab_location(lines: list, padding: float = 24.0) -> float:
    """X-coordinate (points) just past the widest 'time  title' prefix.

    Used as the icon column's tab stop so icons line up regardless of title
    length. Measured with the bold menu font (the widest case — 'current'
    events) so no prefix ever overruns the column.
    """
    try:
        from AppKit import NSAttributedString, NSFont  # type: ignore[import-untyped]
        size = NSFont.menuFontOfSize_(0).pointSize()
        attrs = {"NSFont": NSFont.boldSystemFontOfSize_(size)}
        max_w = 0.0
        for label, _url, _style in lines:
            prefix = label.split("\t", 1)[0]
            w = NSAttributedString.alloc().initWithString_attributes_(prefix, attrs).size().width
            if w > max_w:
                max_w = w
        return max_w + padding
    except Exception:
        return 240.0


def _apply_menu_style(item: rumps.MenuItem, style: str, tab_location: float = 240.0) -> None:
    """Set NSAttributedString on the underlying NSMenuItem for colored/bold text."""
    try:
        from AppKit import NSAttributedString, NSColor, NSFont  # type: ignore[import-untyped]
        FG   = "NSForegroundColor"
        FONT = "NSFont"
        size = NSFont.menuFontOfSize_(0).pointSize()

        # Tab stop for icon column alignment — isolated so a failure here
        # doesn't break colours/fonts.
        para_attrs: dict = {}
        try:
            from Foundation import NSMutableParagraphStyle  # type: ignore
            from AppKit import NSTextTab  # type: ignore[import-untyped]
            para = NSMutableParagraphStyle.alloc().init()
            tab = NSTextTab.alloc().initWithTextAlignment_location_options_(0, tab_location, {})
            para.setTabStops_([tab])
            para_attrs = {"NSParagraphStyle": para}
        except Exception:
            pass

        if style == "past":
            attrs = {FG: NSColor.secondaryLabelColor(),
                     FONT: NSFont.menuFontOfSize_(size), **para_attrs}
        elif style == "blacklisted":
            attrs = {FG: NSColor.tertiaryLabelColor(),
                     FONT: NSFont.menuFontOfSize_(size), **para_attrs}
        elif style == "current":
            attrs = {FG: NSColor.labelColor(),
                     FONT: NSFont.boldSystemFontOfSize_(size), **para_attrs}
        elif style == "header":
            attrs = {FG: NSColor.secondaryLabelColor(),
                     FONT: NSFont.boldSystemFontOfSize_(size - 1)}
        else:  # future
            attrs = {FG: NSColor.labelColor(),
                     FONT: NSFont.menuFontOfSize_(size), **para_attrs}
        item._menuitem.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(item.title, attrs)
        )
    except Exception as e:
        print(f"⚠️  _apply_menu_style({style}): {e}")


# ── Already-alerted set (avoid double-triggering same event) ─────────────────
alerted: set[str] = set()

# ── Spawned alarm/prefs windows (so we can clean them up on quit/restart) ────
_child_procs: list[subprocess.Popen] = []


def _cleanup_child_procs() -> None:
    """Terminate any alarm/prefs window subprocesses spawned by this process."""
    for p in _child_procs:
        if p.poll() is None:
            try:
                p.terminate()
            except ProcessLookupError:
                pass
    _child_procs.clear()


# ── Tray app shared state ─────────────────────────────────────────────────────
_alarm_queue: queue.Queue = queue.Queue()
_menu_lock  = threading.Lock()
_menu_state: dict = {
    'lines':        [],
    'next_dt':      None,  # ISO datetime str of the next upcoming event
    'next_summary': '',    # title of the next upcoming event
    'dirty':        False,
}


class MeetingAlarmApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("🔔 Loading…", quit_button=None)  # type: ignore[arg-type]
        self._build_menu()
        rumps.Timer(self._tick, 1).start()

    def _build_menu(self) -> None:
        self.menu.clear()
        with _menu_lock:
            lines = list(_menu_state['lines'])

        # "Today — 11. Jun" header
        today_hdr = rumps.MenuItem(datetime.datetime.now().strftime("Today  —  %-d. %b"))
        today_hdr.set_callback(None)
        _apply_menu_style(today_hdr, "header")
        self.menu.add(today_hdr)
        self.menu.add(None)

        if lines:
            tab_loc = _menu_tab_location(lines)
            for label, url, style in lines:
                if url and style not in ("past", "blacklisted"):
                    item = rumps.MenuItem(label, callback=lambda _, u=url: subprocess.Popen(["open", u]))
                else:
                    item = rumps.MenuItem(label)
                    item.set_callback(None)
                _apply_menu_style(item, style, tab_loc)
                self.menu.add(item)
        else:
            placeholder = rumps.MenuItem("No meetings today")
            placeholder.set_callback(None)
            self.menu.add(placeholder)

        self.menu.add(None)
        self.menu.add(rumps.MenuItem("Preferences…", callback=lambda _: show_prefs_window()))
        self.menu.add(rumps.MenuItem("Quit", callback=self._quit))

    def _quit(self, _) -> None:
        _cleanup_child_procs()
        rumps.quit_application()

    def _tick(self, _) -> None:
        alarm: tuple | None = None
        try:
            alarm = _alarm_queue.get_nowait()
        except queue.Empty:
            pass
        if alarm is not None:
            try:
                show_alarm_window(*alarm)
            except Exception as e:
                print(f"⚠️  Alarm window error: {e}", flush=True)

        with _menu_lock:
            dirty        = _menu_state['dirty']
            next_dt      = _menu_state['next_dt']
            next_summary = _menu_state['next_summary']
            if dirty:
                _menu_state['dirty'] = False

        # Rebuild menu items only when the event list has changed
        if dirty:
            self._build_menu()

        # Recalculate title every tick so the countdown stays in sync
        if next_dt:
            local_now = datetime.datetime.now(datetime.timezone.utc).astimezone()
            delta_m   = int((datetime.datetime.fromisoformat(next_dt) - local_now).total_seconds() / 60)
            self.title = f"{next_summary} {format_delta(delta_m)}"
        else:
            self.title = "No more meetings today"


def _fetch_all_events(service) -> list[dict]:
    """Return all of today's events (past and future) from the configured calendar source."""
    if config.CALENDAR_SOURCE == "macos":
        events = calendar_macos.fetch_events(calendars=config.MACOS_CALENDARS)
    else:
        now       = datetime.datetime.now(datetime.timezone.utc).astimezone()
        start_utc = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(datetime.timezone.utc)
        end_utc   = now.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(datetime.timezone.utc)
        events = calendar_google.fetch_events(
            service,
            time_min=start_utc.isoformat().replace('+00:00', 'Z'),
            time_max=end_utc.isoformat().replace('+00:00', 'Z'),
        )
    return [ev for ev in events if ev["start"].get("dateTime")]


def _poll(service) -> tuple[list[dict], list[dict]]:
    """Fetch today's events, update _menu_state, return (all_today, active_today)."""
    local_now = datetime.datetime.now(datetime.timezone.utc).astimezone()

    blacklist = load_blacklist()
    all_today = _fetch_all_events(service)
    active_today = [ev for ev in all_today if not is_blacklisted(ev.get("summary", ""), blacklist)]

    next_ev      = next(
        (ev for ev in active_today
         if (datetime.datetime.fromisoformat(ev["start"]["dateTime"]) - local_now).total_seconds() > 0),
        None
    )
    next_dt      = next_ev["start"]["dateTime"] if next_ev else None
    next_summary = next_ev.get("summary", "Untitled Meeting") if next_ev else ""

    menu_lines = []
    for ev in all_today:
        title  = ev.get("summary", "Untitled Meeting")
        t_str  = format_event_time_range(ev)
        url    = extract_meeting_url(ev)
        style  = _event_style(ev, local_now, blacklist)
        icon   = "🔕" if is_blacklisted(title, blacklist) else ("🔗" if url else "")
        menu_lines.append((f"{t_str}  {title}\t{icon}", url, style))

    with _menu_lock:
        _menu_state['lines']        = menu_lines
        _menu_state['next_dt']      = next_dt
        _menu_state['next_summary'] = next_summary
        _menu_state['dirty']        = True

    return all_today, active_today


def _build_day_state(service: object) -> None:
    """Initial fetch: populate menu and print today's schedule to the console."""
    all_today, _ = _poll(service)

    with _menu_lock:
        next_dt      = _menu_state['next_dt']
        next_summary = _menu_state['next_summary']
        menu_lines   = list(_menu_state['lines'])

    if next_dt:
        local_now = datetime.datetime.now(datetime.timezone.utc).astimezone()
        delta_m   = int((datetime.datetime.fromisoformat(next_dt) - local_now).total_seconds() / 60)
        next_str  = f"{next_summary} {format_delta(delta_m)}"
    else:
        next_str = "No more meetings today"

    print(f"✅  Connected. Checking every {config.POLL_INTERVAL_SECS}s for meetings within "
          f"{config.ALERT_MINUTES_BEFORE} min… {next_str}\n    Press Ctrl+C to stop.\n")
    if all_today:
        print("📅  Today's meetings:")
        for line in menu_lines:
            print(f"   {line}")
    else:
        print("📅  No meetings today.")
    print()


def monitor_loop(service: object) -> None:
    _build_day_state(service)

    while True:
        try:
            all_today, active_today = _poll(service)

            # Prune IDs from previous days so the set doesn't grow forever
            today_ids = {ev.get("id", "") for ev in all_today}
            alerted.intersection_update(today_ids)

            # Alarm detection
            now = datetime.datetime.now(datetime.timezone.utc)
            for ev in active_today:
                ev_id    = ev.get("id", "")
                start_dt = datetime.datetime.fromisoformat(ev["start"]["dateTime"])
                delta    = (start_dt - now).total_seconds() / 60
                if ev_id not in alerted and -1 <= delta <= config.ALERT_MINUTES_BEFORE:
                    alerted.add(ev_id)
                    title = ev.get("summary", "Untitled Meeting")
                    t_str = format_event_time(ev)
                    url   = extract_meeting_url(ev)
                    print(f"🔔  Alarm triggered: {title!r} at {t_str}")
                    _alarm_queue.put((title, t_str, url))

        except Exception as e:
            print(f"⚠️  Error: {e}")

        time.sleep(config.POLL_INTERVAL_SECS)
 
 
# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Internal: spawned by show_alarm_window() to display the alarm in a clean process
    if "--alarm" in sys.argv:
        import json as _json
        _idx = sys.argv.index("--alarm")
        if _idx + 1 >= len(sys.argv):
            print("Usage: meeting_alarm.py --alarm '<json>'")
            sys.exit(1)
        _data = _json.loads(sys.argv[_idx + 1])
        _alarm_window_direct(_data.get("title", ""), _data.get("time", ""), _data.get("url") or None)
        sys.exit(0)

    # Preferences GUI: spawned by show_prefs_window()
    if "--prefs" in sys.argv:
        _prefs_window_direct()
        sys.exit(0)

    # List macOS calendars: python3 meeting_alarm.py --list-calendars
    if "--list-calendars" in sys.argv:
        names = calendar_macos.list_calendars()
        if names:
            print("📅  Available macOS calendars:")
            for name in names:
                print(f"   - {name!r}")
            print()
            print("Add the ones you want to config.yaml under 'macos_calendars'.")
        else:
            print("⚠️  No calendars found (or access not granted).")
        sys.exit(0)

    # Quick demo mode: python3 meeting_alarm.py --demo
    if "--demo" in sys.argv:
        print("🎬  Demo mode — showing alarm in 1 second…")
        time.sleep(1)
        _alarm_window_direct(
            "Weekly Team Standup",
            "10:00 AM",
            "https://meet.google.com/abc-defg-hij"
        )
        print("✅  Demo complete.")
    else:
        if config.CALENDAR_SOURCE == "macos":
            print("📅  Using macOS Calendar app as event source.")
            _service = None
        else:
            if not calendar_google.GCAL_AVAILABLE:
                print("❌  Google Calendar libraries not installed. Run: pip3 install -r requirements.txt")
                sys.exit(1)
            print("🔐  Authenticating with Google Calendar…")
            _creds   = calendar_google.get_credentials()
            _service = calendar_google.build("calendar", "v3", credentials=_creds)

        _monitor_thread = threading.Thread(target=monitor_loop, args=(_service,), daemon=True)
        _monitor_thread.start()

        # Clean up spawned alarm/prefs windows if this process is terminated
        # (e.g. by "Restart Now" in Preferences, which SIGTERMs the old instance).
        import signal as _signal

        def _handle_sigterm(_signum, _frame):
            _cleanup_child_procs()
            sys.exit(0)

        _signal.signal(_signal.SIGTERM, _handle_sigterm)

        # Hide Python from the Dock — menu bar only, alarm windows still work fine
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory  # type: ignore[import-untyped]
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        MeetingAlarmApp().run()
 
