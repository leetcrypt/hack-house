"""Mobile web chat front-end for the operator bridge (`operator web`).

Turns a running operator daemon into a phone-friendly chat page. The daemon
already exposes the room as a local API over its AF_UNIX control socket
(`read` with long-poll, `say`, `roster`, `status`); this module is a thin,
**stdlib-only** HTTP shim in front of that socket so a browser can drive it:

    GET  /                     the single-page chat UI (self-contained HTML/JS)
    GET  /api/events?since=N   long-poll: proxies {"op":"read","wait":true}
    POST /api/say  {text}      proxies {"op":"say"}
    GET  /api/status           proxies {"op":"status"} (roster + connection)

Why this exists: on Termux the tmux `read --wait` loop is read-only — replying
means a second shell typing `operator say …`, which is painful on a touch
keyboard. A browser page gives a real input box, scrollback and live updates
with zero extra dependencies (no server stack, no JS build — just http.server).

Binds 127.0.0.1 by default: the control socket grants room-send rights, so the
listener stays loopback-only unless you deliberately pass a routable --bind.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .cli_client import BridgeUnreachable, request
from .session import resolve

# Long-poll window the browser holds open per /api/events call. The socket read
# timeout must outlast it (see request(read_timeout=...)).
_POLL_TIMEOUT = 25.0


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>hack-house · __SESSION__</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font: 15px/1.4 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
    background: #0d1117; color: #e6edf3;
    display: flex; flex-direction: column;
  }
  header {
    padding: 10px 14px; background: #161b22; border-bottom: 1px solid #30363d;
    display: flex; align-items: center; gap: 10px; flex: none;
  }
  header .dot { width: 10px; height: 10px; border-radius: 50%; background: #f85149; flex: none; }
  header .dot.on { background: #3fb950; }
  header .name { font-weight: 600; }
  header .roster { color: #8b949e; font-size: 13px; margin-left: auto;
                   overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #log { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
  .msg { max-width: 82%; padding: 8px 11px; border-radius: 14px; word-wrap: break-word; white-space: pre-wrap; }
  .msg .who { font-size: 11px; color: #8b949e; margin-bottom: 2px; }
  .msg.them { background: #21262d; align-self: flex-start; border-bottom-left-radius: 4px; }
  .msg.me   { background: #1f6feb; align-self: flex-end; border-bottom-right-radius: 4px; }
  .msg.addr { box-shadow: 0 0 0 2px #d29922 inset; }
  .sys { align-self: center; color: #8b949e; font-size: 12px; font-style: italic; }
  footer { flex: none; display: flex; gap: 8px; padding: 10px; background: #161b22; border-top: 1px solid #30363d; }
  #box { flex: 1; resize: none; background: #0d1117; color: #e6edf3; border: 1px solid #30363d;
         border-radius: 10px; padding: 10px; font: inherit; max-height: 120px; }
  #send { flex: none; background: #238636; color: #fff; border: 0; border-radius: 10px;
          padding: 0 18px; font: inherit; font-weight: 600; }
  #send:disabled { opacity: .5; }
</style>
</head>
<body>
  <header>
    <span class="dot" id="dot"></span>
    <span class="name">__SESSION__</span>
    <span class="roster" id="roster">—</span>
  </header>
  <div id="log"></div>
  <footer>
    <textarea id="box" rows="1" placeholder="Message the room…" autocomplete="off"></textarea>
    <button id="send">Send</button>
  </footer>
<script>
const ME = "__SESSION__";
const log = document.getElementById("log");
const box = document.getElementById("box");
const send = document.getElementById("send");
const dot = document.getElementById("dot");
const rosterEl = document.getElementById("roster");
let since = 0;
const seen = new Set();

function atBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 60; }
function scroll() { log.scrollTop = log.scrollHeight; }

function addMsg(from, text, mine, addressed) {
  const el = document.createElement("div");
  el.className = "msg " + (mine ? "me" : "them") + (addressed ? " addr" : "");
  if (!mine) { const w = document.createElement("div"); w.className = "who"; w.textContent = from; el.appendChild(w); }
  el.appendChild(document.createTextNode(text));
  const stick = atBottom();
  log.appendChild(el);
  if (stick || mine) scroll();
}
function addSys(text) {
  const el = document.createElement("div");
  el.className = "sys"; el.textContent = text;
  const stick = atBottom();
  log.appendChild(el); if (stick) scroll();
}

function render(ev) {
  if (seen.has(ev.seq)) return; seen.add(ev.seq);
  if (ev.kind === "message") addMsg(ev.from, ev.text, false, ev.addressed);
  else if (ev.kind === "sent") addMsg(ev.from, ev.text, true, false);
  else if (ev.kind === "roster") {
    const u = (ev.users || []); rosterEl.textContent = u.length + " · " + u.join(", ");
  } else if (ev.kind === "system") {
    const on = ev.event === "connected"; dot.classList.toggle("on", on);
    addSys("· " + (ev.event || "") + (ev.reason ? " (" + ev.reason + ")" : "") + " ·");
  } else if (ev.kind === "sandbox") addSys("· sandbox " + (ev.state || "") + " ·");
  else if (ev.kind === "acl") addSys("· drive " + (ev.granted ? "granted" : "revoked") + " ·");
}

async function refreshStatus() {
  try {
    const r = await fetch("/api/status"); const s = await r.json();
    dot.classList.toggle("on", !!s.connected);
    if (s.users) rosterEl.textContent = s.users.length + " · " + s.users.join(", ");
  } catch (e) {}
}

async function poll() {
  for (;;) {
    try {
      const r = await fetch("/api/events?since=" + since);
      const data = await r.json();
      for (const ev of (data.events || [])) { since = Math.max(since, ev.seq); render(ev); }
    } catch (e) {
      dot.classList.remove("on");
      await new Promise(res => setTimeout(res, 2000));
    }
  }
}

async function doSend() {
  const text = box.value.trim();
  if (!text) return;
  send.disabled = true;
  try {
    const r = await fetch("/api/say", {method: "POST", headers: {"Content-Type": "application/json"},
                                       body: JSON.stringify({text})});
    const j = await r.json();
    if (j.ok) { box.value = ""; box.style.height = "auto"; }
    else addSys("· send failed: " + (j.error || "?") + " ·");
  } catch (e) { addSys("· send failed (offline) ·"); }
  send.disabled = false; box.focus();
}

send.addEventListener("click", doSend);
box.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); } });
box.addEventListener("input", () => { box.style.height = "auto"; box.style.height = Math.min(box.scrollHeight, 120) + "px"; });

refreshStatus(); poll();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    session_name = "default"  # set on the class before serving
    protocol_version = "HTTP/1.1"

    # ── plumbing ─────────────────────────────────────────────────────────
    def log_message(self, *a):  # silence per-request stderr spam
        pass

    def _sock(self):
        return resolve(self.session_name).sock_path

    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bridge(self, req: dict, read_timeout: float = 35.0) -> dict:
        try:
            return request(self._sock(), req, read_timeout=read_timeout)
        except BridgeUnreachable as e:
            return {"ok": False, "error": str(e), "offline": True}

    # ── routes ───────────────────────────────────────────────────────────
    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/" or url.path == "/index.html":
            page = _PAGE.replace("__SESSION__", resolve(self.session_name).name)
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/api/status":
            self._send_json(self._bridge({"op": "status"}, read_timeout=8))
            return
        if url.path == "/api/events":
            q = parse_qs(url.query)
            since = int((q.get("since") or ["0"])[0])
            resp = self._bridge(
                {"op": "read", "since": since, "wait": True, "timeout": _POLL_TIMEOUT},
                read_timeout=_POLL_TIMEOUT + 5.0)
            self._send_json(resp)
            return
        self._send_json({"ok": False, "error": "not found"}, code=404)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path != "/api/say":
            self._send_json({"ok": False, "error": "not found"}, code=404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            text = str(json.loads(raw.decode()).get("text", "")).strip()
        except (ValueError, AttributeError):
            self._send_json({"ok": False, "error": "bad json"}, code=400)
            return
        if not text:
            self._send_json({"ok": False, "error": "empty"}, code=400)
            return
        self._send_json(self._bridge({"op": "say", "text": text}, read_timeout=15))


def serve(session_name: str, bind: str, port: int) -> int:
    """Run the web front-end until Ctrl-C. Returns a process exit code."""
    sess = resolve(session_name)
    if not sess.sock_path.exists():
        print(f"no live bridge for session '{sess.name}' (run `up` first)")
        return 2
    _Handler.session_name = session_name
    httpd = ThreadingHTTPServer((bind, port), _Handler)
    httpd.daemon_threads = True
    shown = bind if bind not in ("0.0.0.0", "::") else "127.0.0.1"
    print(f"operator web UI for session '{sess.name}' → http://{shown}:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nweb UI stopped")
    finally:
        httpd.server_close()
    return 0
