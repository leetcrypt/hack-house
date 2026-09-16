"""Web relay — Sanic app (P0 skeleton).

A **separate, opt-in trust domain** from the zero-knowledge chat server
(`cmd_chat/server/`), and itself zero-knowledge (spec decision A): it brokers
opaque ciphertext + routing metadata only. Two WSS roles — **publish**
(host→relay) and **subscribe** (browser→relay) — plus an HTTPS control plane.

Invariants enforced here (spec §7, GOAL rails):
- **RAM-only.** Nothing is persisted. `DELETE`/process exit drops everything.
- **No content logged.** We log slug / seq counts / viewer counts / dims / label
  only. The `data`/`ct` payload is forwarded verbatim and never printed.
- **No key.** The relay cannot decrypt; there is no room key or `K_web` here.
- **Localhost only** in P0 — bind 127.0.0.1, `--no-tls`. Public exposure
  (Tailscale Funnel / port-forward) is a human-gated later phase.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sanic import Sanic, Request, Websocket, response

from .provisioning import resolve as resolve_provisioning
from .limits import Lockout, SlidingWindow
from .rooms import ROOM_IDLE_TIMEOUT, RoomRegistry, VIEWER_QUEUE_MAX

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MAX_FRAME_SIZE = 512 * 1024  # opaque cap; oversized frames dropped, not brokered
_HEX_RE = re.compile(r"[0-9a-f]{16,64}")
# Raw Ed25519 public key / signature, hex. Exact lengths — these are fixed-size
# primitives, so anything else is malformed and refused before it reaches verify.
_PIN_PUB_RE = re.compile(r"[0-9a-f]{64}")
_PIN_SIG_RE = re.compile(r"[0-9a-f]{128}")
# How long a viewer may sit on an unanswered PIN challenge. The socket holds no
# viewer slot until the PIN passes (see subscribe_ws), so this bounds nothing but
# an idle fd — still, an unauthenticated socket should not live indefinitely.
PIN_CHALLENGE_TIMEOUT = 120.0

# ── Headers for every page we serve ──────────────────────────────────────────
# The room page holds K_web — in JS memory and in `location.hash` — and it is the
# only part of this project with a script surface at all (the native client has
# none). README's claim is "encrypted client-side before anything leaves your
# machine", so that deserves to be enforced by the browser rather than resting
# solely on our own code being careful.
#
# The pages use inline <script>/<style>, so `script-src` must allow
# 'unsafe-inline' and this CSP does NOT stop script execution. That is fine: what
# it stops is EXFILTRATION. `connect-src 'self'` pins the websocket and every
# fetch to this origin, `img-src` forbids a pixel to a third party, and
# `form-action`/`base-uri 'none'` close the two classic no-JS-needed escapes. A
# key that cannot leave the origin cannot be stolen by anything short of a
# compromise of the relay itself — which already holds the traffic.
#
# jsdelivr is listed because xterm and qrcode-generator load from it; both are
# pinned with SRI + crossorigin, so tampering is caught independently of this.
# `frame-ancestors 'none'` keeps the room and operator pages out of an iframe,
# which is what a clickjack against the drive-approval buttons would need.
PAGE_CSP = "; ".join((
    "default-src 'none'",
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    "font-src 'self' data:",
    "img-src 'self' data:",
    "connect-src 'self'",
    "form-action 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
))
# Referrer-Policy is the older half of this: it keeps the `#k` / `#t` fragment and
# the slug out of any onward Referer (spec §7 fragment hygiene).
PAGE_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": PAGE_CSP,
    "X-Content-Type-Options": "nosniff",
}

# ── Build identity (operational hygiene) ─────────────────────────────────────
# A relay answers /health identically whether it started ten seconds or ten days
# ago, so a long-lived process silently serves code that predates the tree it was
# launched from. That has already cost a live session (2026-07-26): a relay two
# days stale could not parse the browser-upload frames its publisher was sending,
# and the only symptom was an endless reconnect loop with rooms expiring under it.
# /health therefore carries a build stamp, and both `scripts/hh-up.sh` and the
# publisher check it before adopting a relay they did not start themselves.
#
# WIRE_PROTO is bumped BY HAND whenever the pub/sub frame contract changes such
# that an older peer cannot parse it. It is deliberately duplicated (not imported)
# in `cmd_chat/web/publisher.py`: the relay is a separate deployable that may be
# public, remote, and running a checkout nobody here controls, so there is no
# shared module to import — and detecting the two having drifted apart is the
# entire point.
#   5 = P4 binary out/in frames + U0–U4 browser upload frames
#   6 = /pub + /host authenticate from frame one (no `?token=`); `out` frames bind
#       slug|dir|seq|epoch as AEAD additional data; `in` frames carry a
#       browser-minted vsid + monotonic ctr inside the ciphertext
#   7 = room PIN is salted + PBKDF2-stretched: create takes {pin_salt, pin_hash},
#       /sub hello takes `pin_digest` (not `pin`), 4401 returns the parameters.
#       Frames are encrypted under HKDF sub-keys of K_web, not K_web itself — the
#       relay cannot see that, but it serves the page that has to agree with the
#       publisher about it, so the two must move together
#   8 = room PIN is proved by SIGNATURE, not by equality. Create takes `pin_pub`
#       (raw Ed25519 public key) and NO salt; /sub answers a hello without proof
#       with an in-band {"type":"pin_challenge","nonce"} frame and expects
#       {"pin_sig"} back over that nonce. Replaces the v7 scheme in which the
#       stored `pin_hash` was itself the credential — readable from relay memory
#       and replayable verbatim — and in which the 4401 refusal handed the KDF
#       salt to unauthenticated callers. The salt now rides the share link's
#       #fragment, so the relay never holds it (current)
WIRE_PROTO = 8


def source_stamp() -> dict:
    """Identify the CODE this process runs: git rev of the checkout plus the newest
    source mtime.

    Both halves are needed. The rev alone is blind to uncommitted edits — the
    normal state of this tree during a dev loop — while mtime alone cannot tell a
    rebuild from a branch switch. Resolved once at import: it describes the code
    the process started with, so it must NOT be recomputed while it lives (that
    would make a stale relay report itself fresh the moment someone saved a file).
    """
    root = Path(__file__).resolve().parent.parent.parent
    rev = "unknown"
    try:
        # `git -C dir` does not confine git to dir — it still walks up until it
        # finds a repository. Unpack the release tarball anywhere beneath one
        # (a $HOME kept in git is the common case) and this reported that
        # unrelated repo's HEAD as the relay's own revision: /health answered
        # `f44862f+dirty` for a tree built from 561656f. A wrong rev is worse
        # than none here, because the rev is read as evidence of what is
        # deployed. Demand that the repo we found *is* this checkout.
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5)
        in_own_checkout = (
            top.returncode == 0
            and top.stdout.strip()
            and Path(top.stdout.strip()).resolve() == root)
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5) if in_own_checkout else None
        if out is not None and out.returncode == 0:
            rev = out.stdout.strip() or "unknown"
            dirty = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                capture_output=True, text=True, timeout=5)
            if dirty.returncode == 0 and dirty.stdout.strip():
                rev += "+dirty"
    except Exception:
        pass  # no git, no checkout, or a slow disk — the mtime half still works
    srcs = list(Path(__file__).resolve().parent.glob("*.py")) + list(STATIC_DIR.glob("*.html"))
    newest = max((p.stat().st_mtime for p in srcs), default=0.0)
    return {"rev": rev, "src_mtime": round(newest, 3)}


BUILD = source_stamp()

# Reconnect grace (P0 drive-wedge fix). When a viewer socket drops we do NOT tear
# the viewer down immediately: a mobile socket flaps constantly (backgrounding,
# network switch), and an instant `viewer_left` would drop the driver/approval a
# reconnect is about to reclaim. We keep the (now-disconnected) viewer as a
# placeholder for this many seconds; a reconnect with the same client_id supersedes
# it seamlessly, otherwise the reaper below fires the real leave. Env-overridable so
# a test can drive it fast.
VIEWER_GRACE = float(os.environ.get("RELAY_VIEWER_GRACE", "10"))

# Sentinel queued to a viewer whose backlog we dropped: the sender re-`sync`s it to
# the latest snapshot instead of replaying a flood it can't keep up with (spec §5.3).
_RESYNC = object()

# ── P4 binary WS frames (spec §5 optimization; JSON+base64 stays the fallback). ──
# A browser opts in with `bin:true` in its subscribe `hello`; the relay then fans
# out `out` as a compact binary frame (no base64 bloat, no JSON parse on the hot
# path) and accepts binary `in` keystrokes. The payload is still opaque ciphertext
# — the relay only slices fixed-width headers, never the encrypted body.
#   out : 0x01 | seq(4, big-endian) | nonce(12) | ct(...)
#   in  : 0x03 | nonce(12) | ct(...)
FRAME_OUT = 0x01
FRAME_IN = 0x03


def encode_out_binary(item: dict) -> bytes:
    """Render a queued `out` dict as its binary wire form (opaque ct/nonce)."""
    seq = int(item.get("seq") or 0) & 0xFFFFFFFF
    nonce = base64.b64decode(item.get("nonce", "") or "")
    ct = base64.b64decode(item.get("ct", "") or "")
    return bytes([FRAME_OUT]) + seq.to_bytes(4, "big") + nonce + ct


def decode_in_binary(raw: bytes) -> tuple[str, str] | None:
    """Parse a binary `in` frame → (ct_b64, nonce_b64) for the publisher interface,
    which stays JSON. Returns None on a malformed/short frame."""
    if len(raw) < 1 + 12 or raw[0] != FRAME_IN:
        return None
    nonce = raw[1:13]
    ct = raw[13:]
    return base64.b64encode(ct).decode(), base64.b64encode(nonce).decode()


def _nodelay(ws: Websocket) -> None:
    """TCP_NODELAY so small interactive frames aren't held by Nagle. Best-effort;
    mirrors cmd_chat/server/views.py::_disable_nagle."""
    try:
        sock = ws.io_proto.transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass


async def _ws_auth(ws: Websocket, expected: str | None) -> bool:
    """Authenticate a websocket from its FIRST FRAME rather than the query string.

    A `?token=` lands in browser history, in every reverse-proxy access log on the
    path, and in a Referer if the page ever links out — for a bearer credential
    that grants publish or host-console rights that is a durable leak in places
    nobody thinks to scrub. `/sub/<slug>` already proves the pattern works, so the
    other two sockets use it too: connect unauthenticated, prove it in frame one,
    2s or you're closed.

    Uses Sanic's native recv timeout deliberately — wrapping recv() in
    asyncio.wait_for() cancels mid-frame and wedges the assembler (see the long
    note in subscribe_ws).
    """
    if not expected:
        return False
    try:
        first = await ws.recv(timeout=2.0)
        if first is None:
            return False
        msg = json.loads(first)
    except (asyncio.TimeoutError, json.JSONDecodeError, TypeError, ValueError):
        return False
    except Exception:
        return False
    if msg.get("type") != "auth":
        return False
    tok = msg.get("token")
    return isinstance(tok, str) and hmac.compare_digest(tok, expected)


async def _viewer_sender(room, viewer_id: str, v) -> None:
    """Sole writer for one viewer: drains its queue to the socket. Because each
    viewer has its own task + queue, a browser that stops reading only stalls
    *itself* — the fan-out to healthy viewers never blocks on it. On a dropped
    backlog (`_RESYNC`) it sends a fresh full `sync` (drop-to-latest-snapshot)."""
    try:
        while True:
            item = await v.queue.get()
            if item is _RESYNC:
                v.behind = False
                await v.ws.send(json.dumps(room.sync_payload(viewer_id)))
            elif v.binary and isinstance(item, dict) and item.get("type") == "out":
                # Binary fast-path for the high-volume `out` fan-out. Control frames
                # (sync/resume/roster/resize/end) stay JSON for both viewer kinds.
                await v.ws.send(encode_out_binary(item))
            else:
                await v.ws.send(json.dumps(item))
    except Exception:
        return  # socket dead; the subscribe handler's finally removes the viewer


async def _fanout(room, frame: dict) -> None:
    """Enqueue one JSON frame for every viewer (non-blocking). `frame` may carry
    opaque `ct`/`nonce` ciphertext — we serialize it but never inspect/log it. A
    viewer whose queue is full is *slow*: we drop its backlog and queue a `_RESYNC`
    so it catches up to the latest snapshot rather than to a stale flood."""
    for v in list(room.viewers.values()):
        try:
            v.queue.put_nowait(frame)
        except asyncio.QueueFull:
            while True:
                try:
                    v.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            v.behind = True
            try:
                v.queue.put_nowait(_RESYNC)
            except asyncio.QueueFull:
                pass


async def _send_viewer(room, viewer_id: str, frame: dict) -> None:
    """Enqueue one frame for a single viewer via its own queue — never write the
    socket directly (`_viewer_sender` is the sole writer, so a direct send would
    interleave and corrupt frames). On overflow we mark the viewer behind and drop;
    for a file chunk that surfaces as a failed sha256 the browser can retry, which
    is preferable to unbounded relay memory."""
    v = room.viewers.get(viewer_id)
    if v is None or not v.connected:
        return
    try:
        v.queue.put_nowait(frame)
    except asyncio.QueueFull:
        v.behind = True


def _clamp_seq(v) -> int:
    """Chunk sequence number, coerced to a sane non-negative int. It rides in the
    clear purely so the publisher can order and de-duplicate chunks, so a
    browser-supplied bool/string/float/huge value must degrade to 0 rather than
    reach the publisher as something it has to defend against."""
    if isinstance(v, bool) or not isinstance(v, int):
        return 0
    return v if 0 <= v <= 1_000_000 else 0


async def _notify_publisher(room, frame: dict) -> bool:
    """Push one frame to the room's publisher. Returns False when it did not land.

    A publisher socket can be absent (reconnecting, host restarting) and this is
    a fire-and-forget path for most frame types — a lost drive request is retried
    by a human. But an upload offer is not: the guest waits on a reply that will
    now never come, and the host never learns anyone tried. Callers that need to
    tell the guest check the return value.
    """
    if room.publisher is None:
        return False
    try:
        await room.publisher.send(json.dumps(frame))
        return True
    except Exception:
        return False


async def _notify_hosts(room, frame: dict) -> None:
    """Push one JSON frame to every connected operator console. Carries only
    routing metadata (viewer_ids, driver, gate state) — never room content."""
    for hws in list(room.hosts):
        try:
            await hws.send(json.dumps(frame))
        except Exception:
            pass


async def _grace_evict(room, viewer_id: str, v, client_id: str) -> None:
    """Fire the REAL viewer teardown, but only if the viewer is still gone after the
    grace window (P0 drive-wedge fix). If a reconnect superseded this Viewer object
    (`viewers[viewer_id]` is a different Viewer) or it came back (`v.connected`), we
    do nothing — no spurious `viewer_left`, so the driver/approval keyed on this id
    survives the flap. On a genuine leave we drop it, release drive if it was the
    driver, forget the client mapping, and re-fan the roster."""
    await asyncio.sleep(VIEWER_GRACE)
    if room.viewers.get(viewer_id) is not v or v.connected:
        return  # reconnected or superseded — the live viewer stays
    was_driver = room.driver == viewer_id
    room.remove_viewer(viewer_id)
    room.forget_client(client_id)
    if v.task is not None:
        v.task.cancel()
    if was_driver:
        room.driver = None
        await _notify_publisher(room, {"type": "drive_release", "viewer_id": viewer_id})
    print(f"[relay] viewer left slug={room.slug} viewers={room.viewer_count}")
    await _notify_publisher(
        room, {"type": "viewer_left", "viewer_id": viewer_id, "count": room.viewer_count})
    await _fanout(room, room.roster_payload())
    await _notify_hosts(room, room.host_roster())


# ── Real client IP behind a trusted reverse proxy / tunnel (hardening). ──
# Behind `cloudflared` the socket peer is loopback for EVERY client, which would
# collapse the per-IP connect limiter and PIN lockout onto a single shared key
# (one abuser trips 4429 / a PIN lockout for everyone). We therefore honor a
# forwarded real-IP header — but ONLY when the socket peer is itself a trusted
# proxy origin (default: loopback, where cloudflared terminates). We never trust
# the header from an arbitrary client, which could otherwise spoof its source IP.
_TRUSTED_PROXIES = {
    p.strip() for p in os.environ.get(
        "RELAY_TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if p.strip()
}
_REAL_IP_HEADER = os.environ.get("RELAY_REAL_IP_HEADER", "CF-Connecting-IP")


def _client_ip(request: Request) -> str:
    """The real client IP for rate-limiting/lockout keys. Falls back to the socket
    peer unless that peer is a trusted proxy AND presents the real-IP header."""
    peer = request.ip or request.remote_addr or "?"
    if peer in _TRUSTED_PROXIES:
        fwd = request.headers.get(_REAL_IP_HEADER)
        if fwd:
            return fwd.split(",")[0].strip()  # left-most = original client
    return peer




def _announce_provisioning(p) -> None:
    """Say where the provisioning secret came from.

    The secret itself is printed only when it could not be persisted, because
    that is the one case where the operator must copy it somewhere by hand —
    otherwise the publisher reads the file and nobody needs to see it. Printing
    it every time would scatter a long-lived credential through terminal
    scrollback and any log that captures stdout.
    """
    if p.origin == "env":
        print("[relay] provisioning secret: from RELAY_PROVISION_SECRET")
    elif p.origin == "state":
        print(f"[relay] provisioning secret: reusing {p.path}")
    elif p.origin == "generated":
        print(f"[relay] provisioning secret: generated → {p.path} (0600)")
    else:
        print(f"[relay] provisioning secret: could NOT persist ({p.error}) — "
              "room creation is still closed, but this secret dies with the "
              "process and the publisher cannot discover it.\n"
              f"[relay]   export HH_WEB_PROVISION_SECRET={p.secret}")


def create_app(name: str = "hh-web-relay") -> Sanic:
    app = Sanic(name)
    app.ctx.rooms = RoomRegistry()
    app.ctx.started_at = time.time()

    # ── P4 DoS caps (spec §7), env-overridable so a load test can drive small
    # values. Per-IP connect rate + per-(slug,IP) PIN-attempt lockout. ──
    connect_limiter = SlidingWindow(
        int(os.environ.get("RELAY_CONNECT_RATE", "120")),
        float(os.environ.get("RELAY_CONNECT_WINDOW", "10")))
    pin_locker = Lockout(
        int(os.environ.get("RELAY_PIN_MAX_FAILS", "5")),
        float(os.environ.get("RELAY_PIN_WINDOW", "60")),
        float(os.environ.get("RELAY_PIN_LOCKOUT", "30")))
    # Per-SLUG global PIN backstop (defence-in-depth). The per-(slug,ip) locker
    # above is defeated by an attacker who can influence the trusted real-IP key:
    # from a trusted-proxy peer (loopback, where cloudflared/funnel terminate) a
    # rotated CF-Connecting-IP mints a fresh bucket per attempt, so the per-IP cap
    # never trips (confirmed red-team, 2026-08-24). This slug-keyed locker caps PIN
    # FAILURES across ALL ips for one room, so IP rotation cannot buy fresh guesses.
    # Threshold is set well above normal fat-finger retries by a handful of real
    # viewers; a sustained brute-force trips it and the room's PIN gate cools down.
    # The bounded self-DoS (an attacker can lock a room's gate) is the deliberate
    # trade for closing an otherwise-unbounded online brute-force — env-tunable, and
    # 0 disables it (restoring the pre-fix per-IP-only behaviour for a deployment
    # that fronts with a proxy which provably sanitises the real-IP header).
    pin_slug_locker = Lockout(
        int(os.environ.get("RELAY_PIN_SLUG_MAX_FAILS", "20")),
        float(os.environ.get("RELAY_PIN_SLUG_WINDOW", "300")),
        float(os.environ.get("RELAY_PIN_SLUG_LOCKOUT", "300")))
    _pin_slug_cap = int(os.environ.get("RELAY_PIN_SLUG_MAX_FAILS", "20"))
    # ── Room-creation caps (hardening — create-flood / RAM exhaustion). Per-IP
    # create rate + a global concurrent-room ceiling; an optional shared
    # provisioning secret gates creation entirely when set. ──
    create_limiter = SlidingWindow(
        int(os.environ.get("RELAY_CREATE_RATE", "30")),
        float(os.environ.get("RELAY_CREATE_WINDOW", "60")))
    max_rooms = int(os.environ.get("RELAY_MAX_ROOMS", "256"))
    # Standing rooms one source may hold. The create *rate* limit does not bound
    # this: idle reaping only collects rooms with no publisher and no viewers, so
    # a caller that attaches a publish socket keeps its room forever. Without this
    # cap, 30 rooms/min — comfortably inside the rate limit — accumulates the full
    # 256-room ceiling in nine minutes and every later room gets 503. 0 disables.
    max_rooms_per_ip = int(os.environ.get("RELAY_MAX_ROOMS_PER_IP", "8"))
    require_pin = os.environ.get("RELAY_REQUIRE_PIN", "").lower() in (
        "1", "true", "yes", "on")
    # Resolved, never merely read: unset now means "generate one and keep it"
    # rather than "let anyone in". See relay/provisioning.py, in particular for
    # why this cannot instead key off the caller looking local.
    provisioning = resolve_provisioning()
    provision_secret = provisioning.secret if provisioning else ""
    if provisioning:
        _announce_provisioning(provisioning)
    # ── Operator plane (hardening — visibility + a targeted kill). ──
    # `DELETE /api/rooms/<slug>` needs that room's publish_token, which only its
    # publisher holds, and `GET /api/rooms` hides unlisted rooms. Correct for
    # tenants, but between them they leave the person running the box unable to
    # see or stop anything on it — the only lever being a restart, which drops
    # every unrelated session too. Unset token → the routes 404 like the download
    # ones, so a relay nobody configured grows no new surface.
    admin_token = os.environ.get("RELAY_ADMIN_TOKEN", "")

    def _admin_ok(request: Request) -> bool:
        """Header only, never a query parameter: request paths land in access
        logs and shell history, and this credential outlives any one room."""
        if not admin_token:
            return False
        supplied = request.headers.get("X-Admin-Token", "")
        return bool(supplied) and hmac.compare_digest(supplied, admin_token)
    async def _idle_reaper() -> None:
        """Background sweep: purge idle (publisher-less, viewer-less, silent) rooms
        from RAM (spec §7). Runs often enough that a short test timeout is caught."""
        interval = max(0.5, min(5.0, ROOM_IDLE_TIMEOUT / 2))
        while True:
            await asyncio.sleep(interval)
            try:
                for room in app.ctx.rooms.purge_idle():
                    for v in list(room.viewers.values()):
                        if v.task is not None:
                            v.task.cancel()
                        try:
                            await v.ws.close()
                        except Exception:
                            pass
                    print(f"[relay] room reaped (idle) slug={room.slug}")
                connect_limiter.sweep()
            except Exception:
                pass

    @app.before_server_start
    async def _launch_reaper(app_, loop):
        app_.ctx._reaper = loop.create_task(_idle_reaper())

    @app.before_server_stop
    async def _drain_rooms(app_, loop):
        """Tell viewers the relay is going away, before their socket just dies.

        A restart is the one ending that is nobody's decision, and an unannounced
        one is indistinguishable from the guest's own network dropping — so the
        page reconnects forever against a room that no longer exists, and whose
        K_web would not have survived the restart anyway. `reason` lets it stop
        and say so instead.

        Deliberately not paired with any persistence: keeping rooms across a
        restart would mean writing publish/host tokens to disk, which is a worse
        trade than a clear ending. The host already recovers on its own — the
        publisher reprovisions on 4401 — so this is only about the guests.

        Notifies without emptying the registry. The registry is RAM-only and
        dies with the process, so clearing it buys nothing, and a shutdown hook
        that mutates shared state is the kind of thing that quietly changes
        behaviour anywhere the server is stopped and started more than once.
        """
        reaper = getattr(app_.ctx, "_reaper", None)
        if reaper is not None:
            reaper.cancel()
        n = 0
        for room in app_.ctx.rooms:
            if room.viewers:
                n += 1
            await _teardown_room(room, reason="relay_restart")
        if n:
            print(f"[relay] told viewers in {n} room(s) that the relay is stopping")

    # ── Control plane (HTTPS REST) ───────────────────────────────────────
    @app.post("/api/rooms")
    async def create_room(request: Request):
        """Publisher registers a room. Body: {label, cols, rows, ttl?, pin_pub?,
        listed?}. `label` is operator-chosen public text — never room content.
        `pin_pub` is a PUBLIC key, so this body carries no PIN-equivalent secret."""
        # Optional shared provisioning secret (hardening): when set, only a caller
        # that presents it may create rooms. The publisher runs server-side, so it
        # can safely hold one; unset keeps P0/local use frictionless.
        if provision_secret:
            supplied = request.headers.get("X-Provision-Secret", "")
            if not supplied or not hmac.compare_digest(supplied, provision_secret):
                return response.json({"error": "unauthorized"}, status=401)
        ip = _client_ip(request)
        # Per-IP create-rate cap + global concurrent-room ceiling: a create-flood
        # can't exhaust the RAM-only registry before the idle reaper catches up.
        if not create_limiter.allow(ip):
            return response.json({"error": "rate limited"}, status=429)
        if len(app.ctx.rooms) >= max_rooms:
            return response.json({"error": "at capacity"}, status=503)
        if max_rooms_per_ip and app.ctx.rooms.count_for_ip(ip) >= max_rooms_per_ip:
            return response.json({"error": "too many rooms"}, status=429)
        body = request.json or {}
        label = str(body.get("label") or "hack-house room")[:120]
        cols = int(body.get("cols") or 80)
        rows = int(body.get("rows") or 24)
        # The PIN verifier: a raw Ed25519 PUBLIC key, hex (optional; P0 has none).
        # Strictly 64 hex chars — a fixed-size primitive, so anything else is
        # malformed. Note what is NOT accepted here any more: the KDF salt. The
        # relay cannot derive and never needs it, and holding it would give anyone
        # who reads this process the missing half of an offline PIN search.
        pin_pub = str(body.get("pin_pub") or "")[:64]
        if pin_pub and not _PIN_PUB_RE.fullmatch(pin_pub):
            return response.json({"error": "pin_pub must be 64 hex chars"},
                                 status=400)
        # Refuse the v7 fields outright instead of ignoring them. A publisher still
        # speaking v7 asks for a PIN with `pin_hash`; silently dropping it would hand
        # the operator an UNGATED room while their console said a PIN was set. The
        # proto check in `preflight_relay` should already have stopped them — this is
        # the fail-closed backstop for when it did not.
        if body.get("pin_hash") or body.get("pin_salt"):
            return response.json(
                {"error": "pin_hash/pin_salt are wire-proto 7 and no longer accepted; "
                          f"send pin_pub (relay speaks proto {WIRE_PROTO})"},
                status=400)
        if require_pin and not pin_pub:
            return response.json({"error": "pin required"}, status=400)
        listed = bool(body.get("listed"))  # unlisted by default (no enumeration)
        room = app.ctx.rooms.create(label, cols, rows, pin_pub=pin_pub or None,
                                    listed=listed, created_ip=ip)
        print(f"[relay] room created slug={room.slug} label={label!r} {cols}x{rows}")
        return response.json({
            "slug": room.slug,
            "publish_token": room.publish_token,
            "host_token": room.host_token,
        })

    @app.delete("/api/rooms/<slug>")
    async def end_room(request: Request, slug: str):
        # Auth teardown (hardening): every viewer knows the slug (it is in the share
        # URL), so an unauthenticated DELETE would let anyone end any room — a
        # one-request DoS. Require the room's publish_token (constant-time compare),
        # the same credential /pub/<slug> already uses.
        room = app.ctx.rooms.get(slug)
        if room is None:
            return response.json({"error": "no such room"}, status=404)
        # Header only — a `?token=` here lands in every access log on the path, and
        # _admin_ok already reasons this way for the admin credential.
        token = request.headers.get("X-Publish-Token")
        if not token or not hmac.compare_digest(token, room.publish_token):
            return response.json({"error": "unauthorized"}, status=401)
        room = app.ctx.rooms.delete(slug)
        if room is None:
            return response.json({"error": "no such room"}, status=404)
        await _teardown_room(room)
        print(f"[relay] room ended slug={slug}")
        return response.json({"ok": True})

    async def _teardown_room(room, reason: str = "") -> None:
        # Send `end` directly, stop each sender task, close sockets, drop the
        # (RAM-only) ring. Direct send (not via queue) since we close right after.
        # `reason` lets a viewer distinguish "the host ended this" from "the relay
        # went away", which are the same dead socket without it.
        end = {"type": "end"}
        if reason:
            end["reason"] = reason
        for v in list(room.viewers.values()):
            try:
                await v.ws.send(json.dumps(end))
            except Exception:
                pass
            if v.task is not None:
                v.task.cancel()
            try:
                await v.ws.close()
            except Exception:
                pass

    # ── Operator plane ───────────────────────────────────────────────────
    @app.get("/api/admin/rooms")
    async def admin_list_rooms(request: Request):
        """Every room on this relay, listed or not. Metadata only — see
        `Room.admin_meta` for what is deliberately absent."""
        if not _admin_ok(request):
            return response.json({"error": "not found"}, status=404)
        rooms = app.ctx.rooms.admin_list()
        return response.json({
            "rooms": rooms,
            "count": len(rooms),
            "max_rooms": max_rooms,
            "max_rooms_per_ip": max_rooms_per_ip,
            "uptime_s": round(time.time() - app.ctx.started_at, 1),
        })

    @app.delete("/api/admin/rooms/<slug>")
    async def admin_end_room(request: Request, slug: str):
        """End one room without holding its publish_token.

        This is the whole point of the operator plane: previously the only way to
        stop an abusive room was restarting the relay, which drops every
        unrelated session with it."""
        if not _admin_ok(request):
            return response.json({"error": "not found"}, status=404)
        room = app.ctx.rooms.delete(slug)
        if room is None:
            return response.json({"error": "no such room"}, status=404)
        await _teardown_room(room, reason="ended_by_operator")
        print(f"[relay] room ended by operator slug={slug}")
        return response.json({"ok": True})

    @app.get("/r/<slug>")
    async def room_page(request: Request, slug: str):
        """Serve the static room page. The `#k` fragment is applied client-side
        only and never reaches us. Referrer-Policy: no-referrer keeps the fragment
        (and slug) out of any onward Referer (spec §7 fragment hygiene)."""
        html = (STATIC_DIR / "room.html").read_text()
        return response.html(html, headers=PAGE_HEADERS)

    @app.get("/host/<slug>")
    async def host_page(request: Request, slug: str):
        """Serve the operator console page. The `#t` host-token fragment is applied
        client-side only and never reaches us in a Referer (spec §7 hygiene)."""
        html = (STATIC_DIR / "host.html").read_text()
        return response.html(html, headers=PAGE_HEADERS)

    @app.get("/health")
    async def health(request: Request):
        body = {"ok": True, "rooms": len(app.ctx.rooms.list_meta()),
                "proto": WIRE_PROTO}
        # `proto` is public: a remote publisher must be able to refuse an
        # incompatible relay before it starts looping. The rest of the stamp
        # (rev, mtime, pid, uptime) fingerprints the deployment and is answered
        # only to loopback — the callers that need it (`hh-up.sh`, a local
        # publisher) are local by definition, and a public relay has no reason to
        # hand its commit hash to every passer-by.
        #
        # "Arrived from 127.0.0.1" is NOT the same as "is local", and the gap is
        # not theoretical: this relay sits behind a cloudflared tunnel on the same
        # host, so every request off the public internet also arrives from
        # 127.0.0.1 — the check below, alone, answered all of them. A forwarding
        # header is proof the request was proxied, so its mere presence closes the
        # gate. A genuine loopback caller (hh-up.sh, a local publisher) sets none.
        proxied = any(h in request.headers for h in
                      ("x-forwarded-for", "x-forwarded-host", "x-real-ip",
                       "forwarded", "cf-connecting-ip"))
        if not proxied and (request.ip or request.remote_addr or "") in ("127.0.0.1", "::1"):
            body |= BUILD
            body["pid"] = os.getpid()
            body["uptime"] = round(time.time() - app.ctx.started_at, 1)
        return response.json(body)

    # ── Publish WSS (host ↔ relay) ───────────────────────────────────────
    @app.websocket("/pub/<slug>")
    async def publish_ws(request: Request, ws: Websocket, slug: str):
        room = app.ctx.rooms.get(slug)
        if room is None:
            await ws.close(code=4401)  # unauthorized / unknown room
            return
        _nodelay(ws)
        # Token arrives in frame one, not `?token=` — see _ws_auth.
        if not await _ws_auth(ws, room.publish_token):
            await ws.close(code=4401)
            return
        room.publisher = ws
        room.touch()
        print(f"[relay] publisher connected slug={slug}")
        try:
            async for raw in ws:
                if len(raw) > MAX_FRAME_SIZE:
                    continue  # drop oversized; never broker/store
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                mtype = msg.get("type")
                if mtype == "hello":
                    room.cols = int(msg.get("cols") or room.cols)
                    room.rows = int(msg.get("rows") or room.rows)
                    print(f"[relay] hello slug={slug} {room.cols}x{room.rows}")
                elif mtype == "out":
                    # Opaque ciphertext: record + fan out, never inspected/logged.
                    seq = int(msg.get("seq") or 0)
                    ct = msg.get("ct", "")
                    nonce = msg.get("nonce", "")
                    room.record_out(seq, ct, nonce)
                    await _fanout(room, {"type": "out", "seq": seq, "ct": ct, "nonce": nonce})
                elif mtype == "snapshot":
                    # Opaque current-screen replay blob {seq, ct, nonce}; relay
                    # cannot decrypt it. `seq` marks how far it has caught up so a
                    # joiner applies only ring frames after it (no double-render).
                    room.set_snapshot(
                        int(msg.get("seq") or room.last_seq),
                        msg.get("ct", ""), msg.get("nonce", ""))
                elif mtype == "chat":
                    # Opaque chat frame {seq, ct, nonce} (plaintext {from,text,ts}
                    # lives under K_web — the relay cannot read it). Buffer for late
                    # joiners + fan out; authorship/text are never inspected/logged.
                    seq = int(msg.get("seq") or 0)
                    ct = msg.get("ct", "")
                    nonce = msg.get("nonce", "")
                    room.record_chat(seq, ct, nonce)
                    await _fanout(room, {"type": "chat", "seq": seq, "ct": ct, "nonce": nonce})
                elif mtype == "file_offer":
                    # Opaque file offer {seq, ct, nonce} → all viewers. Never stored
                    # (offers are ephemeral) or inspected; the relay only fans it out.
                    await _fanout(room, {
                        "type": "file_offer", "seq": int(msg.get("seq") or 0),
                        "ct": msg.get("ct", ""), "nonce": msg.get("nonce", ""),
                    })
                elif mtype in ("file_chunk", "file_done", "file_error",
                               "upload_ack", "upload_reject", "upload_error",
                               "upload_progress"):
                    # Targeted file frame for a single viewer: the one who accepted a
                    # download, or the one whose upload this answers. Opaque
                    # ciphertext; the relay routes by viewer_id (via that viewer's
                    # queue) and never inspects, stores, or logs the bytes — so it
                    # cannot tell an upload the host accepted from one they refused.
                    vid = msg.get("viewer_id")
                    if vid:
                        await _send_viewer(room, vid, {
                            "type": mtype, "seq": int(msg.get("seq") or 0),
                            "ct": msg.get("ct", ""), "nonce": msg.get("nonce", ""),
                        })
                elif mtype == "resize":
                    room.cols = int(msg.get("cols") or room.cols)
                    room.rows = int(msg.get("rows") or room.rows)
                    await _fanout(room, {"type": "resize", "cols": room.cols, "rows": room.rows})
                elif mtype == "driver":
                    # Publisher declares the effective driver (viewer_id) or clears
                    # it (null). The relay does NOT decide drive rights — the
                    # publisher gates on the broker's driver-token ACL *and* its own
                    # per-viewer approval, then tells us who is effective so we can
                    # mirror it into the roster badge. Opaque viewer_id, no content.
                    vid = msg.get("viewer_id")
                    room.driver = vid if (vid and vid in room.viewers) else None
                    await _fanout(room, room.roster_payload())
                    await _notify_hosts(room, room.host_roster())
                elif mtype == "host_event":
                    # Operator-console event from the publisher (drive request /
                    # gate state). Routing metadata only — fan to the host sockets.
                    await _notify_hosts(room, msg)
                elif mtype == "end":
                    break
        finally:
            room.publisher = None
            print(f"[relay] publisher disconnected slug={slug}")

    # ── Host console WSS (operator ↔ relay, spec §6) ─────────────────────
    # A SEPARATE capability from publish/subscribe: authed by host_token, it lets
    # the operator approve/revoke drive from any browser (incl. mobile) and get a
    # notification when a viewer requests drive. It carries only routing metadata
    # (viewer_ids, gate state) — never room content, never a decryption key. It
    # CANNOT bypass the driver-token ACL: it only sets the publisher's per-viewer
    # approval (Gate B); the room's driver token (Gate A) still gates the PTY.
    @app.websocket("/host/<slug>/ws")
    async def host_ws(request: Request, ws: Websocket, slug: str):
        room = app.ctx.rooms.get(slug)
        if room is None:
            await ws.close(code=4401)
            return
        _nodelay(ws)
        # Token arrives in frame one, not `?token=`. host.html used to lift `#t` out
        # of the fragment and paste it into the WS URL — six lines below a comment
        # saying it must never leak in a Referer.
        if not await _ws_auth(ws, room.host_token):
            await ws.close(code=4401)
            return
        room.hosts.add(ws)
        print(f"[relay] host console connected slug={slug}")
        try:
            # Initial roster from the relay's own state; then ask the publisher to
            # push current gate state (granted / approved / pending requests).
            await ws.send(json.dumps(room.host_roster()))
            await _notify_publisher(room, {"type": "host_cmd", "cmd": "sync"})
            async for raw in ws:
                if len(raw) > MAX_FRAME_SIZE:
                    continue
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                t = msg.get("type")
                if t == "allow":
                    vid = msg.get("viewer_id")
                    if vid:
                        await _notify_publisher(
                            room, {"type": "host_cmd", "cmd": "allow", "viewer_id": vid})
                elif t == "revoke":
                    await _notify_publisher(room, {"type": "host_cmd", "cmd": "revoke"})
                elif t == "sync":
                    await ws.send(json.dumps(room.host_roster()))
                    await _notify_publisher(room, {"type": "host_cmd", "cmd": "sync"})
        finally:
            room.hosts.discard(ws)
            print(f"[relay] host console disconnected slug={slug}")

    # ── Subscribe WSS (browser ↔ relay) ──────────────────────────────────
    # Close codes: 4404 unknown room · 4401 PIN proof missing/malformed ·
    #              4403 PIN wrong · 4429 rate-limited / PIN-locked · 4503 room full.
    @app.websocket("/sub/<slug>")
    async def subscribe_ws(request: Request, ws: Websocket, slug: str):
        ip = _client_ip(request)  # real client IP behind the tunnel (hardening)
        # Connect-rate cap (spec §7): blunt a connect flood per source before we do
        # any work. Localhost load tests share one IP, so a burst trips this.
        if not connect_limiter.allow(ip):
            await ws.close(code=4429)
            return
        room = app.ctx.rooms.get(slug)
        if room is None:
            await ws.close(code=4404)
            return
        # PIN-attempt lockout (spec §7): a locked (slug, ip) is refused outright so
        # the socket can't be used to keep guessing.
        pin_key = f"{slug}:{ip}"
        if room.pin_pub and (pin_locker.locked(pin_key)
                             or (_pin_slug_cap and pin_slug_locker.locked(slug))):
            await ws.close(code=4429)
            return

        _nodelay(ws)
        # ONE first frame carries everything: the PIN (defence-in-depth), the
        # reconnect `last_seq` (spec §5.3, read before registering so no live `out`
        # races the replay), and the binary-frame opt-in. A missing/late hello
        # times out to a full sync — but a PIN-gated room refuses without proof.
        fm: dict = {}
        got_hello = False
        try:
            # Use Sanic's NATIVE recv timeout — it returns None on timeout without
            # cancelling the recv. Wrapping recv() in asyncio.wait_for() cancels it
            # mid-frame, leaving the assembler's get_in_progress flag stuck True, so
            # the next recv() in the `async for raw in ws` loop below throws
            # ("get() ... while asynchronous get is already in progress") and the
            # socket dies — dropping any viewer whose hello is slower than 2s.
            first = await ws.recv(timeout=2.0)
            if first is not None:
                fm = json.loads(first)
                got_hello = True
        except (asyncio.TimeoutError, json.JSONDecodeError, TypeError, ValueError):
            pass
        except Exception:
            await ws.close(code=4400)
            return

        if room.pin_pub:
            # v8: the viewer SIGNS a nonce we mint; we only verify. Nothing here is
            # a shared secret, so there is nothing in this process to steal and
            # replay — the v7 scheme compared the browser's digest against a stored
            # copy of that same digest, which meant reading relay memory was
            # equivalent to knowing the PIN.
            #
            # The challenge carries the nonce and NOTHING else. It used to hand back
            # the KDF salt and iteration count to any unauthenticated caller; the
            # browser now gets those from the share link's #fragment, which never
            # reaches us. So an attacker who cannot already open the link learns
            # only that the room is PIN-gated.
            #
            # Nonce is per-connection and single-use by construction: it lives in
            # this coroutine's frame, is never stored, and the socket closes on any
            # failure. There is no cross-connection replay window to manage.
            nonce = os.urandom(32)
            try:
                await ws.send(json.dumps({"type": "pin_challenge",
                                          "nonce": nonce.hex()}))
                proof_raw = await ws.recv(timeout=PIN_CHALLENGE_TIMEOUT)
                proof = json.loads(proof_raw) if proof_raw else {}
                sig = bytes.fromhex(_PIN_SIG_RE.fullmatch(
                    str(proof.get("pin_sig", ""))).group())
            except (asyncio.TimeoutError, json.JSONDecodeError, AttributeError,
                    TypeError, ValueError):
                # No answer, or one that is not a well-formed signature. Not a
                # wrong PIN — it never got as far as being a guess — so this does
                # not burn a lockout attempt.
                await ws.close(code=4401)
                return
            try:
                Ed25519PublicKey.from_public_bytes(
                    bytes.fromhex(room.pin_pub)).verify(sig, nonce)
            except InvalidSignature:
                locked = pin_locker.fail(pin_key)
                locked_slug = pin_slug_locker.fail(slug) if _pin_slug_cap else False
                await ws.close(code=4429 if (locked or locked_slug) else 4403)
                return
            pin_locker.clear(pin_key)    # correct PIN forgives prior misses
            pin_slug_locker.clear(slug)  # …and the whole-room backstop too

        last_seq = None
        if fm.get("last_seq") is not None:
            try:
                last_seq = int(fm["last_seq"])
            except (TypeError, ValueError):
                last_seq = None
        binary = bool(fm.get("bin"))
        # Persistent browser identity (P0 fix). When present and already known, this
        # is a RECONNECT reclaiming its old viewer_id — it does not occupy a NEW slot,
        # so it is exempt from the full-room cap.
        client_id = str(fm.get("client_id") or "")
        is_reconnect = bool(client_id and client_id in room.clients)

        # Viewer cap (spec §7): a full room refuses new subscribers. Checked after
        # the PIN so a wrong-PIN attempt can't probe the count. A reconnecting client
        # reclaiming an existing viewer_id is not a new subscriber.
        if room.is_full() and not is_reconnect:
            await ws.close(code=4503)
            return

        viewer_id, v, is_reconnect = room.new_viewer(ws, client_id)
        v.binary = binary
        # Queue the initial replay first (the sender task is the sole socket writer,
        # so nothing else can interleave). Resume with a delta when the ring still
        # holds the next frame; otherwise a full sync (snapshot + ring tail). The
        # resume also echoes viewer_id so a reconnecting browser re-pins its identity
        # (belt-and-braces with the client_id reuse above).
        resume = room.ring_after(last_seq) if room.can_resume(last_seq) else None
        if resume is not None and len(resume) < VIEWER_QUEUE_MAX - 1:
            v.queue.put_nowait({"type": "resume", "from_seq": last_seq, "viewer_id": viewer_id})
            for f in resume:
                v.queue.put_nowait({"type": "out", **f})
        else:
            v.queue.put_nowait(room.sync_payload(viewer_id))
        # Supersede any prior Viewer under this id (a reconnect reclaiming the slot):
        # cancel its dead sender and close its socket so only one writer remains.
        old = room.viewers.get(viewer_id)
        room.attach_viewer(viewer_id, v)
        if old is not None and old is not v:
            old.connected = False
            if old.task is not None:
                old.task.cancel()
            try:
                await old.ws.close()
            except Exception:
                pass
        v.task = asyncio.create_task(_viewer_sender(room, viewer_id, v))
        print(f"[relay] viewer {'rejoined' if is_reconnect else 'joined'} "
              f"slug={slug} viewers={room.viewer_count}")
        # A reconnect is flagged, not suppressed. It kept its slot (and its
        # driver/approval), so the publisher must not announce it as a new arrival —
        # but it DOES need to know the viewer is back: if the grace window expired it
        # already dropped this id from the roster, and staying silent left a live
        # viewer invisible to the host with no way to recover its chosen name.
        await _notify_publisher(
            room, {"type": "viewer_joined", "viewer_id": viewer_id,
                   "count": room.viewer_count, "reconnect": is_reconnect})
        try:
            await _fanout(room, room.roster_payload())
            await _notify_hosts(room, room.host_roster())
            async for raw in ws:
                # P2 input path (spec §6): browser drive requests + encrypted `in`
                # keystrokes are routed back to the publisher, tagged with the
                # server-side viewer_id (never a browser-asserted identity). The
                # relay stays blind — `in` carries opaque {ct, nonce} it cannot
                # decrypt; the publisher enforces the driver-token ACL + approval.
                if len(raw) > MAX_FRAME_SIZE:
                    continue
                # Binary `in` fast-path: a compact keystroke frame. Decode the fixed
                # header to opaque ct/nonce and forward to the publisher over its
                # (JSON) interface — the relay never decrypts.
                if isinstance(raw, (bytes, bytearray)):
                    parsed = decode_in_binary(bytes(raw))
                    if parsed is not None:
                        ct_b64, nonce_b64 = parsed
                        await _notify_publisher(room, {
                            "type": "in", "viewer_id": viewer_id,
                            "ct": ct_b64, "nonce": nonce_b64,
                        })
                    continue
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                mtype = msg.get("type")
                if mtype == "request-drive":
                    await _notify_publisher(
                        room, {"type": "drive_request", "viewer_id": viewer_id})
                elif mtype == "release-drive":
                    await _notify_publisher(
                        room, {"type": "drive_release", "viewer_id": viewer_id})
                elif mtype == "savevm":
                    # Browser asked to save the LIVE VM to its own machine → publisher,
                    # tagged with the viewer_id so the resulting offer routes back to
                    # THIS viewer only. The host still approves it (/accept-save).
                    await _notify_publisher(
                        room, {"type": "savevm", "viewer_id": viewer_id})
                elif mtype == "in":
                    await _notify_publisher(room, {
                        "type": "in", "viewer_id": viewer_id,
                        "ct": msg.get("ct", ""), "nonce": msg.get("nonce", ""),
                    })
                elif mtype == "chat":
                    # Browser chat message → publisher, tagged with the server-side
                    # viewer_id. Opaque {ct, nonce} ({from,text} under K_web); the
                    # relay never decrypts. The publisher posts it into the room.
                    await _notify_publisher(room, {
                        "type": "chat_in", "viewer_id": viewer_id,
                        "ct": msg.get("ct", ""), "nonce": msg.get("nonce", ""),
                    })
                elif mtype == "name":
                    # Browser set/changed its display name → publisher, tagged with
                    # the server-side viewer_id. Opaque {ct, nonce} ({name} under
                    # K_web); the relay never decrypts. The publisher updates the
                    # roster handle it broadcasts into the room.
                    await _notify_publisher(room, {
                        "type": "name_in", "viewer_id": viewer_id,
                        "ct": msg.get("ct", ""), "nonce": msg.get("nonce", ""),
                    })
                elif mtype in ("file-accept", "file-reject"):
                    # Browser accepted/declined a file offer → publisher, tagged with
                    # the viewer_id. `tok` is the random per-offer web token (never
                    # the real room id), so the relay learns nothing about the file.
                    await _notify_publisher(room, {
                        "type": mtype.replace("-", "_"), "viewer_id": viewer_id,
                        "tok": str(msg.get("tok", ""))[:64],
                    })
                elif mtype in ("upload-offer", "upload-chunk",
                               "upload-done", "upload-cancel"):
                    # Browser → host file upload. Same posture as every other
                    # inbound path: the relay forwards opaque {ct, nonce} under
                    # K_web and never learns the filename, the bytes, or who the
                    # guest is in the room. Only routing metadata travels in the
                    # clear — `tok` (browser-minted, per upload) and `seq`
                    # (chunk ordering). The publisher owns every decision that
                    # matters: whether uploads are allowed at all, the caps, and
                    # synthesising the room offer the host must /accept.
                    landed = await _notify_publisher(room, {
                        "type": mtype.replace("-", "_"), "viewer_id": viewer_id,
                        "tok": str(msg.get("tok", ""))[:64],
                        "seq": _clamp_seq(msg.get("seq")),
                        "ct": msg.get("ct", ""), "nonce": msg.get("nonce", ""),
                    })
                    if not landed and mtype == "upload-offer":
                        # No publisher to hear it. Every other reply to an upload is
                        # ciphertext the publisher mints, and we hold no key — so
                        # this cleartext nack is the only way to stop the guest
                        # waiting on a host that cannot answer. It leaks nothing:
                        # "the host is not connected" is already visible in the
                        # roster.
                        await _send_viewer(room, viewer_id,
                                           {"type": "upload_unavailable",
                                            "tok": str(msg.get("tok", ""))[:64]})
                # everything else ignored
        finally:
            # This socket dropped — but a mobile flap reconnects in seconds and would
            # reclaim this viewer_id (P0 fix). Mark disconnected and defer the real
            # teardown to the grace reaper; if this is still the live Viewer and still
            # gone after the window, it fires `viewer_left` / drive-release then. If a
            # reconnect already superseded us, do nothing (our task is already dead).
            v.connected = False
            if v.task is not None:
                v.task.cancel()
            if room.viewers.get(viewer_id) is v:
                asyncio.create_task(_grace_evict(room, viewer_id, v, client_id))

    return app
