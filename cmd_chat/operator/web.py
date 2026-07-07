"""Mobile hack-house console for the operator bridge (`operator web`).

A phone-friendly browser mirror of a live hack-house room. The daemon already
exposes the room as a local API over its AF_UNIX control socket; this module is
a thin, **stdlib-only** HTTP shim in front of it so a browser gets the same
things the Rust TUI shows — the shared sandbox terminal, chat, roster — mobile
optimised:

    GET  /                     the single-page console (self-contained HTML/JS)
    GET  /api/status           proxies {"op":"status"} (connection/roster/driver)
    GET  /api/events?since=N    long-poll: proxies {"op":"read","wait":true}
    POST /api/say   {text}      proxies {"op":"say"}
    GET  /api/screen?ansi=1     proxies {"op":"screen"} — the relayed shared PTY
    POST /api/keys  {tokens}    proxies {"op":"keys"} — drive the shared PTY

Why this exists: on Termux the tmux `read --wait` loop is read-only and shows
none of the sandbox. A browser page gives the terminal view + a real input box
+ live updates with zero extra dependencies (no server stack, no JS build).

Binds 127.0.0.1 by default: the control socket grants room-send/drive rights,
so the listener stays loopback-only unless you pass a routable --bind.
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
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; margin: 0; }
  body {
    font: 14px/1.4 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
    background: #0b0e14; color: #cdd6f4; display: flex; flex-direction: column;
    overflow: hidden;
  }
  header {
    padding: 8px 12px; background: #11151f; border-bottom: 1px solid #1f2430;
    display: flex; align-items: center; gap: 8px; flex: none;
  }
  header .dot { width: 9px; height: 9px; border-radius: 50%; background: #f38ba8; flex: none; }
  header .dot.on { background: #a6e3a1; box-shadow: 0 0 6px #a6e3a1; }
  header .name { font-weight: 700; letter-spacing: .3px; }
  header .badge { font-size: 11px; padding: 1px 7px; border-radius: 10px;
                  background: #313244; color: #9399b2; }
  header .badge.drive { background: #1e3a2a; color: #a6e3a1; }
  header .roster { color: #7f849c; font-size: 12px; margin-left: auto;
                   overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 45%; }
  .tabs { display: flex; flex: none; background: #11151f; border-bottom: 1px solid #1f2430; }
  .tabs button { flex: 1; background: none; border: 0; color: #7f849c; padding: 9px;
                 font: inherit; font-weight: 600; border-bottom: 2px solid transparent; }
  .tabs button.active { color: #89b4fa; border-bottom-color: #89b4fa; }
  .pane { flex: 1; min-height: 0; display: none; flex-direction: column; }
  .pane.active { display: flex; }

  /* terminal */
  #term { flex: 1; margin: 0; padding: 10px; overflow: auto; white-space: pre;
          font: 12px/1.35 "SF Mono", ui-monospace, Menlo, Consolas, monospace;
          background: #05070c; color: #b9c0d4; }
  #term .empty { color: #565a70; font-style: italic; }
  .keys { flex: none; background: #11151f; border-top: 1px solid #1f2430; padding: 6px; }
  .quick { display: flex; gap: 5px; overflow-x: auto; padding-bottom: 6px; }
  .quick button { flex: none; background: #1f2430; color: #cdd6f4; border: 1px solid #313244;
                  border-radius: 7px; padding: 6px 9px; font: 12px monospace; }
  .quick button:active { background: #313244; }
  .keyrow { display: flex; gap: 6px; }
  #kbox { flex: 1; background: #05070c; color: #b9c0d4; border: 1px solid #313244;
          border-radius: 8px; padding: 8px; font: 13px monospace; }
  #kbox:disabled { opacity: .5; }
  #ksend { flex: none; background: #89b4fa; color: #0b0e14; border: 0; border-radius: 8px;
           padding: 0 14px; font: inherit; font-weight: 700; }
  #ksend:disabled { opacity: .4; }
  .hint { color: #f9e2af; font-size: 11px; padding: 4px 2px 0; }

  /* chat */
  #log { flex: 1; overflow-y: auto; padding: 10px; display: flex; flex-direction: column; gap: 7px; }
  .msg { max-width: 82%; padding: 7px 10px; border-radius: 13px; word-wrap: break-word; white-space: pre-wrap; }
  .msg .who { font-size: 10px; color: #7f849c; margin-bottom: 2px; }
  .msg.them { background: #1f2430; align-self: flex-start; border-bottom-left-radius: 4px; }
  .msg.me   { background: #3a4a7a; align-self: flex-end; border-bottom-right-radius: 4px; }
  .msg.addr { box-shadow: 0 0 0 2px #f9e2af inset; }
  .sys { align-self: center; color: #7f849c; font-size: 11px; font-style: italic; }
  footer { flex: none; display: flex; gap: 7px; padding: 8px; background: #11151f; border-top: 1px solid #1f2430; }
  #box { flex: 1; resize: none; background: #05070c; color: #cdd6f4; border: 1px solid #313244;
         border-radius: 9px; padding: 9px; font: inherit; max-height: 110px; }
  #send { flex: none; background: #a6e3a1; color: #0b0e14; border: 0; border-radius: 9px;
          padding: 0 16px; font: inherit; font-weight: 700; }
  #send:disabled { opacity: .5; }
  /* ANSI palette */
  .a30{color:#45475a}.a31{color:#f38ba8}.a32{color:#a6e3a1}.a33{color:#f9e2af}
  .a34{color:#89b4fa}.a35{color:#f5c2e7}.a36{color:#94e2d5}.a37{color:#bac2de}
  .a90{color:#585b70}.a91{color:#f38ba8}.a92{color:#a6e3a1}.a93{color:#f9e2af}
  .a94{color:#89b4fa}.a95{color:#f5c2e7}.a96{color:#94e2d5}.a97{color:#e6edf3}
  .ab{font-weight:700}
</style>
</head>
<body>
  <header>
    <span class="dot" id="dot"></span>
    <span class="name">__SESSION__</span>
    <span class="badge" id="drive">watching</span>
    <span class="roster" id="roster">—</span>
  </header>
  <div class="tabs">
    <button id="tabTerm" class="active">Terminal</button>
    <button id="tabChat">Chat</button>
  </div>

  <div class="pane active" id="paneTerm">
    <pre id="term"><span class="empty">no shared sandbox yet — a room member runs /sbx and /grants this operator to drive it here.</span></pre>
    <div class="keys">
      <div class="quick">
        <button data-k="enter">⏎ Enter</button>
        <button data-k="ctrl-c">^C</button>
        <button data-k="esc">Esc</button>
        <button data-k="tab">Tab</button>
        <button data-k="up">↑</button>
        <button data-k="down">↓</button>
        <button data-k="ctrl-l">clear</button>
        <button data-k="ctrl-d">^D</button>
      </div>
      <div class="keyrow">
        <input id="kbox" placeholder="type a command → Send runs it (adds Enter)" autocomplete="off" autocapitalize="off" spellcheck="false">
        <button id="ksend">Send</button>
      </div>
      <div class="hint" id="khint"></div>
    </div>
  </div>

  <div class="pane" id="paneChat">
    <div id="log"></div>
    <footer>
      <textarea id="box" rows="1" placeholder="Message the room…" autocomplete="off"></textarea>
      <button id="send">Send</button>
    </footer>
  </div>

<script>
const ME = "__SESSION__";
const $ = id => document.getElementById(id);
let since = 0, granted = false, active = "term", termSeq = -1;
const seen = new Set();

/* ── tabs ── */
function show(tab) {
  active = tab;
  $("tabTerm").classList.toggle("active", tab === "term");
  $("tabChat").classList.toggle("active", tab === "chat");
  $("paneTerm").classList.toggle("active", tab === "term");
  $("paneChat").classList.toggle("active", tab === "chat");
}
$("tabTerm").onclick = () => show("term");
$("tabChat").onclick = () => show("chat");

/* ── ANSI (SGR) → HTML for the terminal pane ── */
function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function ansiToHtml(raw) {
  let out = "", open = false, cls = [];
  const re = /\\x1b\\[([0-9;]*)m|\\x1b\\[[0-9;?]*[A-Za-z]|\\x1b\\][^\\x07]*\\x07/g;
  let last = 0, m;
  const flush = t => { if (!t) return; out += esc(t); };
  while ((m = re.exec(raw)) !== null) {
    flush(raw.slice(last, m.index)); last = re.lastIndex;
    if (m[1] === undefined) continue;            // non-SGR CSI / OSC → drop
    const codes = m[1] === "" ? [0] : m[1].split(";").map(Number);
    if (open) { out += "</span>"; open = false; }
    cls = [];
    for (const c of codes) {
      if (c === 0) cls = [];
      else if (c === 1) cls.push("ab");
      else if ((c>=30&&c<=37)||(c>=90&&c<=97)) cls.push("a"+c);
    }
    if (cls.length) { out += '<span class="'+cls.join(" ")+'">'; open = true; }
  }
  flush(raw.slice(last));
  if (open) out += "</span>";
  return out;
}

/* ── terminal poll ── */
async function pollScreen() {
  for (;;) {
    try {
      if (active === "term") {
        const r = await fetch("/api/screen?ansi=1");
        const d = await r.json();
        if (d.ok && d.seq !== termSeq) {
          termSeq = d.seq;
          const t = $("term");
          const bottom = t.scrollHeight - t.scrollTop - t.clientHeight < 40;
          if (d.text && d.text.length) t.innerHTML = ansiToHtml(d.text);
          if (bottom) t.scrollTop = t.scrollHeight;
        }
      }
    } catch (e) {}
    await new Promise(r => setTimeout(r, active === "term" ? 900 : 2000));
  }
}

/* ── keys ── */
async function sendKeys(tokens) {
  if (!granted) return;
  try {
    const r = await fetch("/api/keys", {method:"POST", headers:{"Content-Type":"application/json"},
                                        body: JSON.stringify({tokens})});
    const j = await r.json();
    if (!j.ok) $("khint").textContent = "keys: " + (j.error || "?");
  } catch (e) { $("khint").textContent = "keys: offline"; }
}
document.querySelectorAll(".quick button").forEach(b =>
  b.onclick = () => sendKeys([b.dataset.k]));
function runLine() {
  const v = $("kbox").value;
  if (!granted) return;
  sendKeys(v ? ["text:" + v, "enter"] : ["enter"]);
  $("kbox").value = "";
}
$("ksend").onclick = runLine;
$("kbox").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); runLine(); } });

function setGrant(g) {
  granted = g;
  $("drive").textContent = g ? "driver" : "watching";
  $("drive").classList.toggle("drive", g);
  $("kbox").disabled = !g; $("ksend").disabled = !g;
  document.querySelectorAll(".quick button").forEach(b => b.disabled = !g);
  $("khint").textContent = g ? "" : "read-only — a room owner must /grant " + ME + " to drive the sandbox";
}

/* ── chat ── */
const log = $("log"), box = $("box"), send = $("send"), dot = $("dot"), rosterEl = $("roster");
function atBottom(){ return log.scrollHeight - log.scrollTop - log.clientHeight < 60; }
function addMsg(from, text, mine, addressed) {
  const el = document.createElement("div");
  el.className = "msg " + (mine ? "me" : "them") + (addressed ? " addr" : "");
  if (!mine){ const w=document.createElement("div"); w.className="who"; w.textContent=from; el.appendChild(w); }
  el.appendChild(document.createTextNode(text));
  const stick = atBottom(); log.appendChild(el); if (stick||mine) log.scrollTop = log.scrollHeight;
}
function addSys(text){ const el=document.createElement("div"); el.className="sys"; el.textContent=text;
  const stick=atBottom(); log.appendChild(el); if(stick) log.scrollTop=log.scrollHeight; }

function setRoster(u){ rosterEl.textContent = (u||[]).length + " · " + (u||[]).join(", "); }

function render(ev) {
  if (seen.has(ev.seq)) return; seen.add(ev.seq);
  if (ev.kind === "message") addMsg(ev.from, ev.text, false, ev.addressed);
  else if (ev.kind === "sent") addMsg(ev.from, ev.text, true, false);
  else if (ev.kind === "roster") setRoster(ev.users);
  else if (ev.kind === "system") {
    if (ev.event === "connected") dot.classList.add("on");
    if (ev.event === "reconnecting") dot.classList.remove("on");
    addSys("· " + (ev.event||"") + (ev.reason ? " ("+ev.reason+")" : "") + " ·");
  } else if (ev.kind === "acl") { setGrant(!!ev.granted); addSys("· drive "+(ev.granted?"granted":"revoked")+" ·"); }
  else if (ev.kind === "sandbox") addSys("· sandbox " + (ev.state||"") + " ·");
}

async function refreshStatus() {
  try { const s = await (await fetch("/api/status")).json();
    dot.classList.toggle("on", !!s.connected);
    if (s.users) setRoster(s.users);
    setGrant(!!s.granted);
  } catch (e) {}
}
async function pollEvents() {
  for (;;) {
    try {
      const d = await (await fetch("/api/events?since=" + since)).json();
      for (const ev of (d.events || [])) { since = Math.max(since, ev.seq); render(ev); }
    } catch (e) { dot.classList.remove("on"); await new Promise(r=>setTimeout(r,2000)); }
  }
}
async function doSend() {
  const text = box.value.trim(); if (!text) return;
  send.disabled = true;
  try {
    const j = await (await fetch("/api/say",{method:"POST",headers:{"Content-Type":"application/json"},
                                             body:JSON.stringify({text})})).json();
    if (j.ok){ box.value=""; box.style.height="auto"; } else addSys("· send failed: "+(j.error||"?")+" ·");
  } catch(e){ addSys("· send failed (offline) ·"); }
  send.disabled = false; box.focus();
}
send.onclick = doSend;
box.addEventListener("keydown", e => { if (e.key==="Enter" && !e.shiftKey){ e.preventDefault(); doSend(); } });
box.addEventListener("input", () => { box.style.height="auto"; box.style.height=Math.min(box.scrollHeight,110)+"px"; });

refreshStatus(); pollEvents(); pollScreen();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    session_name = "default"  # set on the class before serving
    protocol_version = "HTTP/1.1"

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
        if url.path in ("/", "/index.html"):
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
        if url.path == "/api/screen":
            q = parse_qs(url.query)
            ansi = (q.get("ansi") or ["0"])[0] not in ("0", "", "false")
            tail = q.get("tail")
            req = {"op": "screen", "ansi": ansi}
            if tail:
                req["tail"] = int(tail[0])
            self._send_json(self._bridge(req, read_timeout=8))
            return
        if url.path == "/api/events":
            q = parse_qs(url.query)
            since = int((q.get("since") or ["0"])[0])
            self._send_json(self._bridge(
                {"op": "read", "since": since, "wait": True, "timeout": _POLL_TIMEOUT},
                read_timeout=_POLL_TIMEOUT + 5.0))
            return
        self._send_json({"ok": False, "error": "not found"}, code=404)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            obj = json.loads(raw.decode())
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            return {}

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path == "/api/say":
            text = str(self._read_body().get("text", "")).strip()
            if not text:
                self._send_json({"ok": False, "error": "empty"}, code=400)
                return
            self._send_json(self._bridge({"op": "say", "text": text}, read_timeout=15))
            return
        if url.path == "/api/keys":
            tokens = self._read_body().get("tokens")
            if not isinstance(tokens, list) or not tokens:
                self._send_json({"ok": False, "error": "no tokens"}, code=400)
                return
            self._send_json(self._bridge({"op": "keys", "keys": tokens}, read_timeout=10))
            return
        self._send_json({"ok": False, "error": "not found"}, code=404)


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
    print(f"operator web console for session '{sess.name}' → http://{shown}:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nweb console stopped")
    finally:
        httpd.server_close()
    return 0
