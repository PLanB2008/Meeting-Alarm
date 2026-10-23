"""
Phone remote — a tiny web server on the local network that shows today's
schedule and lets you mute or dismiss a ringing alarm from another device.

Access is protected by a random token stored in .remote_token (created on first
run), which is part of the URL shown in the menu bar.
"""

import ipaddress
import json
import re
import secrets
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

_TOKEN_FILE = Path(__file__).parent / ".remote_token"

# Interfaces that are tunnels (VPN etc.), not the local network a phone is on
_TUNNEL_PREFIXES = ("utun", "ipsec", "ppp", "tun", "tap", "gif", "stf", "lo")


def _load_token() -> str:
    try:
        token = _TOKEN_FILE.read_text().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(12)
    _TOKEN_FILE.write_text(token + "\n")
    _TOKEN_FILE.chmod(0o600)
    return token


def lan_ip() -> str:
    """LAN IP of this Mac, skipping VPN tunnels.

    `scutil --nwi` lists interfaces in macOS's priority order (e.g. VPN, then
    Ethernet, then Wi-Fi); take the first non-tunnel one with a private address.
    """
    try:
        out = subprocess.run(["scutil", "--nwi"], capture_output=True, text=True, timeout=3).stdout
        for name, addr in re.findall(r"^\s*(\w+) : flags.*\n\s*address\s*:\s*([\d.]+)", out, re.M):
            if not name.startswith(_TUNNEL_PREFIXES) and ipaddress.ip_address(addr).is_private:
                return addr
    except Exception:
        pass
    # Fallback: address of the default route (may be the VPN)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Meeting Alarm</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: 16px; background: #0a0a0a; color: #fff;
         font: 17px -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif; }
  h1 { font-size: 22px; margin: 8px 0 4px; }
  h2 { font-size: 15px; color: #888; text-transform: uppercase; letter-spacing: .05em;
       margin: 22px 0 8px; }
  #status { color: #888; font-size: 14px; }
  .alarm { background: #3a0a08; border: 2px solid #ff3b30; border-radius: 14px;
           padding: 16px; margin-bottom: 14px; animation: pulse 1.2s infinite alternate; }
  @keyframes pulse { to { border-color: #661510; } }
  .alarm .title { font-size: 20px; font-weight: 600; }
  .alarm .time  { color: #ddd; margin: 4px 0 6px; }
  button, a.btn { display: block; width: 100%; box-sizing: border-box; border: 0;
         border-radius: 10px; padding: 16px; margin-top: 10px; font-size: 18px;
         font-weight: 600; text-align: center; text-decoration: none; }
  .dismiss { background: #ff3b30; color: #fff; }
  .mute    { background: #3a3a3c; color: #fff; }
  .mute[disabled] { color: #777; }
  a.btn    { background: #30d158; color: #000; }
  .ev { display: flex; align-items: center; gap: 12px; padding: 12px 14px;
        background: #1c1c1e; border-radius: 10px; margin-bottom: 6px; }
  .ev .t { font-variant-numeric: tabular-nums; color: #aaa; min-width: 96px; font-size: 15px; }
  .ev .n { flex: 1; }
  .ev a  { color: #30d158; text-decoration: none; font-size: 15px; white-space: nowrap; }
  .ev.past { opacity: .4; }
  .ev.blacklisted .n { color: #777; text-decoration: line-through; }
  .ev.current { background: #0f2a17; border-left: 4px solid #30d158; }
  .empty { color: #888; }
  .err   { color: #ff6961; }
  #qr    { text-align: center; color: #aaa; }
  #qr svg { background: #fff; border-radius: 8px; }
  #qr .link { font: 12px Menlo, monospace; word-break: break-all; }
</style>
</head><body>
<h1>⏰ Meeting Alarm</h1>
<div id="status">Connecting…</div>
<div id="qr"></div>
<div id="alarms"></div>
<h2>Today</h2>
<div id="schedule"></div>
<script>
const token = new URLSearchParams(location.search).get("t") || "";
const q = s => `?t=${encodeURIComponent(token)}`;
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function act(id, action) {
  await fetch(`/api/alarms/${id}/${action}${q()}`, {method: "POST"});
  refresh();
}

function renderAlarms(alarms) {
  $("alarms").innerHTML = alarms.map(a => `
    <div class="alarm">
      <div class="title">🔔 ${esc(a.title)}</div>
      <div class="time">${esc(a.time)}</div>
      <button class="dismiss" onclick="act(${a.id}, 'dismiss')">Dismiss</button>
      <button class="mute" onclick="act(${a.id}, 'mute')" ${a.muted ? "disabled" : ""}>
        ${a.muted ? "🔇 Muted" : "🔇 Mute"}</button>
      ${a.url ? `<a class="btn" href="${esc(a.url)}" onclick="act(${a.id}, 'dismiss')">🚀 Join on phone</a>` : ""}
    </div>`).join("");
}

function renderSchedule(events) {
  if (!events.length) { $("schedule").innerHTML = `<p class="empty">No meetings today</p>`; return; }
  $("schedule").innerHTML = events.map(e => `
    <div class="ev ${esc(e.style)}">
      <span class="t">${esc(e.time)}</span>
      <span class="n">${e.style === "blacklisted" ? "🔕 " : ""}${esc(e.title)}</span>
      ${e.url && e.style !== "past" ? `<a href="${esc(e.url)}">Join</a>` : ""}
    </div>`).join("");
}

// Poll fast only while an alarm is ringing; pause while the page is hidden
const FAST_MS = 2000, SLOW_MS = 15000;
let timer = null, ringing = false;

function schedule() {
  clearTimeout(timer);
  if (!document.hidden) timer = setTimeout(refresh, ringing ? FAST_MS : SLOW_MS);
}
document.addEventListener("visibilitychange", () => document.hidden ? clearTimeout(timer) : refresh());

async function refresh() {
  try {
    await load();
  } finally {
    schedule();
  }
}

async function load() {
  try {
    const r = await fetch(`/api/state${q()}`);
    if (!r.ok) { $("status").innerHTML = `<span class="err">Access denied — scan the QR code from the menu bar again.</span>`; return; }
    const st = await r.json();
    const n = st.alarms.length;
    ringing = n > 0;
    $("status").textContent = n ? `${n} alarm${n > 1 ? "s" : ""} ringing`
                                : `Connected · updated ${new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"})}`;
    renderAlarms(st.alarms);
    renderSchedule(st.schedule);
  } catch (e) {
    $("status").innerHTML = `<span class="err">Can't reach the Mac — same Wi-Fi? App running?</span>`;
  }
}
refresh();

// Opened from the menu bar with &qr=1: show a QR code of this link for the phone
if (new URLSearchParams(location.search).get("qr")) {
  const s = document.createElement("script");
  s.src = "https://cdnjs.cloudflare.com/ajax/libs/qrcode-generator/1.4.4/qrcode.min.js";
  s.onload = () => {
    const link = `${location.origin}/${q()}`;
    const qr = qrcode(0, "M"); qr.addData(link); qr.make();
    $("qr").innerHTML = `<p>Scan with your phone (same Wi-Fi):</p>${qr.createSvgTag({cellSize: 6, margin: 4})}
                         <p class="link">${esc(link)}</p>`;
  };
  document.head.appendChild(s);
}
</script>
</body></html>
"""


def start(port: int,
          get_state: Callable[[], dict],
          mute: Callable[[int], bool],
          dismiss: Callable[[int], bool]) -> str:
    """Start the server in a daemon thread and return the URL to open on the phone."""
    token = _load_token()

    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            given = parse_qs(urlparse(self.path).query).get("t", [""])[0]
            return secrets.compare_digest(given, token)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data) -> None:
            self._send(code, json.dumps(data).encode(), "application/json")

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                # Page itself is harmless; the API calls it makes need the token
                self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/state":
                if not self._authorized():
                    return self._json(403, {"error": "forbidden"})
                self._json(200, get_state())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._authorized():
                return self._json(403, {"error": "forbidden"})
            parts = urlparse(self.path).path.strip("/").split("/")
            # /api/alarms/<id>/<action>
            if len(parts) == 4 and parts[:2] == ["api", "alarms"] and parts[2].isdigit():
                action = {"mute": mute, "dismiss": dismiss}.get(parts[3])
                if action:
                    ok = action(int(parts[2]))
                    return self._json(200 if ok else 404, {"ok": ok})
            self._json(404, {"error": "not found"})

        def log_message(self, *_args) -> None:
            pass  # keep the app log quiet

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://{lan_ip()}:{port}/?t={token}"
