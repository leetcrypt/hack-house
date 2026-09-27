"""P0 drive-reconnect verifier — the drive-approval wedge fix (docs/plan-web-relay-fixes.md §1).

Reproduces the reported bug end-to-end on localhost (chat server → _sbx broker →
publisher → relay), then asserts the fix: a viewer that holds drive keeps it across
a socket reconnect, because the browser's persistent `client_id` reclaims the SAME
server viewer_id (identity continuity) and the relay's grace window suppresses the
spurious `viewer_left` a mobile flap would otherwise fire.

  1. DRIVE — grant the publisher's driver token (Gate A) + operator-approve this
     viewer (Gate B); assert the relay roster names THIS viewer_id as driver and its
     encrypted `in` keystrokes reach the PTY.
  2. RECONNECT — close the sub socket and reopen it with the SAME client_id + a
     last_seq. Assert the reclaimed viewer_id is UNCHANGED, the roster STILL names it
     the driver, the operator host_roster still lists it, and it can STILL drive
     without any re-approval.

On the pre-fix code (fresh viewer_id per /sub, instant leave→drive-release) step 2
FAILS: the reconnect gets a new id, the driver clears, and drive is lost.

    python lab/verify_drive_reconnect.py
"""
from __future__ import annotations

import asyncio
import base64
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

import requests
import websockets
from _ports import require_free

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from cmd_chat.web.webkdf import derive_web_keys  # noqa: E402
from _proc import die_with_parent

PW = "recontestpass"
RELAY_PORT = os.environ.get("RECON_RELAY_PORT", "8095")
CHAT_PORT = os.environ.get("RECON_CHAT_PORT", "3000")
RELAY = f"http://127.0.0.1:{RELAY_PORT}"
PUB_NAME = "web-publisher"
# Long enough that our (immediate) reconnect lands inside the grace window; the fix
# under test is exactly this suppression of the flap's spurious leave.
GRACE = "15"
# Room creation is closed by default; pin the secret so the lab is hermetic.
PROVISION = "recon-provision-secret"
ENV = {**os.environ, "CMD_CHAT_PASSWORD": PW, "PYTHONUNBUFFERED": "1",
       "NO_COLOR": "1", "COLUMNS": "1000", "RELAY_VIEWER_GRACE": GRACE,
       "RELAY_PROVISION_SECRET": PROVISION,
       "HH_WEB_PROVISION_SECRET": PROVISION}
procs: list[subprocess.Popen] = []
_URL_RE = re.compile(r"(http://\S+/r/\S+#k=\S+)")
_HOSTTOK_RE = re.compile(r"/host/\S+#t=([A-Za-z0-9_\-]+)")
# The publisher withholds the #k key from a non-terminal stdout (A1) and writes it
# to a 0600 file instead, so this lab reads the file. XDG_RUNTIME_DIR is redirected
# into a lab-owned temp dir so we neither read nor clobber the developer's real one.
SHARE_DIR = tempfile.mkdtemp(prefix="hh-recon-")
ENV["XDG_RUNTIME_DIR"] = SHARE_DIR

b64e = lambda b: base64.b64encode(b).decode()
b64d = base64.b64decode


def spawn(args, cwd=ROOT, capture=False, stdin=False):
    p = subprocess.Popen(
        args, cwd=cwd, env=ENV,
        stdin=(subprocess.PIPE if stdin else subprocess.DEVNULL),
        stdout=(subprocess.PIPE if capture else subprocess.DEVNULL),
        stderr=subprocess.DEVNULL, text=True,
        preexec_fn=die_with_parent())
    procs.append(p)
    return p


def send_cmd(proc, line: str) -> None:
    proc.stdin.write(line + "\n")
    proc.stdin.flush()


def cleanup():
    for p in reversed(procs):
        p.terminate()
    for p in reversed(procs):
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


async def wait_http(url, tries=40):
    for _ in range(tries):
        try:
            requests.get(url, timeout=2)
            return True
        except Exception:
            await asyncio.sleep(0.25)
    return False


def _drain(proc, sink: list):
    for line in proc.stdout:
        sink.append(line)


async def main() -> int:
    require_free((RELAY_PORT, "relay", "RECON_RELAY_PORT"),
                 (CHAT_PORT, "chat server", "RECON_CHAT_PORT"))

    spawn([sys.executable, "cmd_chat.py", "serve", "127.0.0.1", CHAT_PORT,
           "-p", PW, "--no-tls"])
    spawn([sys.executable, "-m", "relay", "--port", RELAY_PORT, "--no-tls"],
          cwd=os.path.join(ROOT, "web-relay"))
    if not await wait_http(RELAY + "/health"):
        print("FAIL: relay never came up"); return 1
    await asyncio.sleep(1.5)

    emitter = spawn([sys.executable, "-m", "cmd_chat.web.emit_sbx", "127.0.0.1",
                     CHAT_PORT, "-p", PW, "--no-tls", "--interval", "0.3"], stdin=True)
    pub = spawn([sys.executable, "-m", "cmd_chat.web", "127.0.0.1", CHAT_PORT,
                 "-p", PW, "--no-tls", "--relay", RELAY, "--name", PUB_NAME,
                 "--label", "recon-verify"], capture=True, stdin=True)

    lines: list[str] = []
    threading.Thread(target=_drain, args=(pub, lines), daemon=True).start()
    share_url = host_token = None
    for _ in range(60):
        for f in glob.glob(os.path.join(SHARE_DIR, "hack-house", "share-*.url")):
            body = open(f).read()
            m = _URL_RE.search(body)
            if m and not share_url:
                share_url = m.group(1)
            h = _HOSTTOK_RE.search(body)
            if h and not host_token:
                host_token = h.group(1)
        if share_url and host_token:
            break
        await asyncio.sleep(0.25)
    if not share_url or not host_token:
        print(f"FAIL: never captured publisher share URL / host token"
              f" (no share file under {SHARE_DIR})"); return 1
    if any(_URL_RE.search(ln) for ln in lines):
        print("FAIL: publisher printed the #k key to a captured stdout (A1)"); return 1
    slug = share_url.split("/r/")[1].split("#")[0]
    k_b64url = dict(kv.split("=", 1) for kv in share_url.split("#", 1)[1].split("&"))["k"]
    k_web = base64.urlsafe_b64decode(k_b64url + "=" * (-len(k_b64url) % 4))
    keys = derive_web_keys(k_web)
    print(f"ok: captured slug={slug}, K_web=32B, host_token")

    sub_url = f"ws://127.0.0.1:{RELAY_PORT}/sub/{slug}"
    host_url = f"ws://127.0.0.1:{RELAY_PORT}/host/{slug}/ws"
    client_id = "c_recon_test_1"
    # WIRE_PROTO 7: routing metadata rides inside the AEAD, and each direction
    # has its own HKDF sub-key of K_web. `out` frames are bound to
    # their seq, and every `in` frame carries the browser-minted vsid + a monotonic
    # counter ahead of the payload, so the publisher authorizes the identity it can
    # actually authenticate rather than the relay's label.
    aad = lambda kind, seq=0: f"hh1|{slug}|{kind}|{seq}".encode()
    vsid = os.urandom(16)
    in_ctr = int(time.time() * 1000)

    # Shared viewer state, refreshed as we drain frames from whichever socket is live.
    state = {"viewer_id": None, "driver": None, "last_seq": 0}
    seen_pt = bytearray()

    async def drain(ws, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.time()))
            except Exception:
                break
            try:
                m = json.loads(raw)
            except Exception:
                continue
            t = m.get("type")
            if t in ("sync", "resume") and m.get("viewer_id"):
                state["viewer_id"] = m["viewer_id"]
            if t == "sync":
                state["driver"] = m.get("driver")
                for f in (m.get("ring") or []):
                    if isinstance(f.get("seq"), int):
                        state["last_seq"] = max(state["last_seq"], f["seq"])
            if t == "roster":
                state["driver"] = m.get("driver")
            if t == "out" and m.get("ct"):
                if isinstance(m.get("seq"), int):
                    state["last_seq"] = max(state["last_seq"], m["seq"])
                try:
                    seen_pt.extend(keys["out"].decrypt(b64d(m["nonce"]), b64d(m["ct"]),
                                               aad("out", m.get("seq") or 0)))
                except Exception:
                    pass

    async def send_in(ws, payload: bytes) -> None:
        nonlocal in_ctr
        in_ctr = max(in_ctr + 1, int(time.time() * 1000))
        # WIRE_PROTO 7 (B7): explicit length, then zero padding to a 64-byte bucket.
        body = len(payload).to_bytes(2, "big") + payload
        pt = vsid + in_ctr.to_bytes(6, "big") + body
        pt += b"\x00" * (-len(pt) % 64)
        nonce = os.urandom(12)
        ct = keys["in"].encrypt(nonce, pt, aad("in"))
        await ws.send(json.dumps({"type": "in", "ct": b64e(ct), "nonce": b64e(nonce)}))

    async def drove(ws, marker: bytes) -> bool:
        before = len(seen_pt)
        await send_in(ws, marker)
        await drain(ws, 3.0)
        return marker in bytes(seen_pt[before:]) or marker in bytes(seen_pt)

    # The relay does not queue `in` frames, so a vsid announce sent while the
    # publisher is still attaching is simply dropped. Wait for the attach before
    # joining, otherwise this lab grades a race instead of the reconnect.
    for _ in range(60):
        if any("connected to relay" in ln for ln in lines):
            break
        await asyncio.sleep(0.25)
    else:
        print("FAIL: publisher never attached to the relay"); return 1

    # ── (1) DRIVE — approve this viewer + hold the driver token, confirm driving ──
    ws1 = await websockets.connect(sub_url, max_queue=64)
    await ws1.send(json.dumps({"type": "hello", "last_seq": None, "bin": False,
                               "client_id": client_id}))
    await drain(ws1, 2.0)
    v1 = state["viewer_id"]
    if not v1:
        print("FAIL: no viewer_id on first join"); return 1
    print(f"ok: joined viewer_id={v1} client_id={client_id}")

    # Announce the vsid (an `in` frame with an empty payload), exactly as room.html
    # does on every sync. Without it the publisher cannot authenticate this label and
    # `/web allow-input` fails closed.
    await send_in(ws1, b"")
    await ws1.send(json.dumps({"type": "request-drive"}))
    # The stdin REPL and the relay socket are independent paths into the publisher:
    # typing allow-input immediately can beat the announce there and fail closed for
    # a reason that has nothing to do with reconnects. Let the announce land first.
    await drain(ws1, 1.5)
    send_cmd(emitter, f"grant {PUB_NAME}")           # Gate A: driver token
    send_cmd(pub, f"/web allow-input {v1}")          # Gate B: operator approval
    await drain(ws1, 2.5)                            # let acl + approval propagate
    if state["driver"] != v1:
        print(f"FAIL: roster driver {state['driver']!r} != our viewer {v1!r} before reconnect")
        print("--- publisher stdout ---")
        for ln in lines[-25:]:
            print("   " + ln.rstrip())
        return 1
    if not await drove(ws1, b"ZZ-PRE-RECON-ZZ"):
        print("FAIL: approved viewer could not drive before reconnect"); return 1
    print(f"ok: (1) driving before reconnect (roster driver == {v1})")

    # ── (2) RECONNECT — drop + reopen with the SAME client_id; drive must survive ──
    last_seq = state["last_seq"] or None
    await ws1.close()
    await asyncio.sleep(1.0)  # a real flap gap — well inside the grace window
    ws2 = await websockets.connect(sub_url, max_queue=64)
    await ws2.send(json.dumps({"type": "hello", "last_seq": last_seq, "bin": False,
                               "client_id": client_id}))
    await send_in(ws2, b"")      # re-announce on the new socket, as the page does
    await drain(ws2, 2.5)

    if state["viewer_id"] != v1:
        print(f"FAIL: reconnect got a NEW viewer_id {state['viewer_id']!r} (expected {v1!r})")
        return 1
    if state["driver"] != v1:
        print(f"FAIL: after reconnect roster driver {state['driver']!r} != {v1!r} "
              "(drive wedged)"); return 1
    print(f"ok: reconnect reclaimed viewer_id={v1}, roster still driver={v1}")

    # Operator host_roster must still list the viewer as connected + driving.
    async with websockets.connect(host_url, max_queue=64) as hws:
        await hws.send(json.dumps({"type": "auth", "token": host_token}))
        hr = None
        for _ in range(10):
            m = json.loads(await asyncio.wait_for(hws.recv(), timeout=5))
            if m.get("type") == "roster":
                hr = m; break
        if not hr or v1 not in (hr.get("viewers") or []):
            print(f"FAIL: host_roster does not list {v1!r} after reconnect (hr={hr})")
            return 1
        if hr.get("driver") != v1:
            print(f"FAIL: host_roster driver {hr.get('driver')!r} != {v1!r}"); return 1
    print("ok: host console roster still lists the viewer as driver")

    if not await drove(ws2, b"ZZ-POST-RECON-ZZ"):
        print("FAIL: viewer could NOT drive after reconnect (approval lost)"); return 1
    print("ok: (2) still driving after reconnect — no re-approval needed")

    await ws2.close()
    print("\nP0 RECONNECT PASS — persistent client_id + grace window keep the "
          "driver/approval/roster stable across a socket flap; no drive-wedge.")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    finally:
        cleanup()
    sys.exit(rc)
