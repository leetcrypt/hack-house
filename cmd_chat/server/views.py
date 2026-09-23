import hashlib
import hmac
import json
import base64
import secrets
import socket
from dataclasses import asdict

from sanic import Sanic, Request, response, Websocket
from sanic.response import HTTPResponse, json as json_response

from .models import Message, UserSession
from .helpers import get_client_ip, state_frame, utcnow


def _disable_nagle(ws: Websocket) -> None:
    """Set TCP_NODELAY on a websocket's underlying socket. Small interactive
    frames (PTY echo, keystrokes, chat) must not wait on Nagle coalescing +
    delayed-ACK before reaching a viewer. Best-effort: any transport without a
    raw socket (or where the option can't be set) is simply left as-is."""
    try:
        sock = ws.io_proto.transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass


# Hard cap on a single relayed WS frame. The largest legitimate frame is one
# Fernet-encrypted 64 KB file chunk (~120 KB after base64 + token overhead), so
# 256 KB leaves headroom while bounding per-message memory and the 1000-message
# store. Oversized frames are dropped, not stored or broadcast.
MAX_FRAME_SIZE = 256 * 1024


def generate_ws_token(user_id: str, secret: bytes) -> str:
    return hmac.new(secret, user_id.encode(), hashlib.sha256).hexdigest()


def _roster_frame(app: Sanic) -> str:
    """Authoritative presence snapshot — all clergy members converge on this."""
    users = app.ctx.session_store.get_all()
    host_id = _current_host(app)
    host_name = next((u.username for u in users if u.user_id == host_id), None)
    return json.dumps(
        {
            "type": "roster",
            "users": [{"user_id": u.user_id, "username": u.username} for u in users],
            "capacity": app.ctx.max_users,
            # Host = kick authority, as a username so the client can tell if it's
            # the host and label the roster (None until a member is present).
            "host": host_name,
        }
    )


def _current_host(app: Sanic) -> str | None:
    """The room host = the oldest still-present connection (the "host badge"
    member). Sticky while that member is present; auto-promotes to the next-oldest
    when the host leaves. Only the host may `/kick`."""
    present = list(app.ctx.connection_manager.active_connections.keys())
    hid = getattr(app.ctx, "host_user_id", None)
    if hid not in present:
        hid = present[0] if present else None
        app.ctx.host_user_id = hid
    return hid


_CONTROL_TYPES = ("kick",)


def _parse_control(text: str) -> dict | None:
    """Recognise a cleartext moderation control frame. Room content is E2E
    encrypted (a Fernet token — base64, never starts with '{'), so a frame the
    SERVER must act on rides in the clear as a small JSON object. Gate cheaply on
    that shape, then confirm a known `type` — order-independent (JSON key order
    isn't guaranteed by the sender)."""
    if not (text.startswith("{") and len(text) < 1024 and '"type"' in text):
        return None
    try:
        v = json.loads(text)
    except Exception:
        return None
    if isinstance(v, dict) and v.get("type") in _CONTROL_TYPES:
        return v
    return None


async def _handle_kick(app: Sanic, sender_id: str, sender_name: str, target: str) -> None:
    """Host-only force-kick. Verify the sender is the room host, then ROTATE the
    room password and re-key the room: because the E2E content key is derived from
    the password, every member must reconnect. Remaining members are handed the new
    password (direct sends) and reconnect seamlessly; the kicked member is not, so
    only they can't come back. All sessions are freed so the reconnect wave finds
    its names available."""
    mgr = app.ctx.connection_manager
    if sender_id != _current_host(app):
        await mgr.send_to(sender_id, json.dumps(
            {"type": "kick_denied", "reason": "only the room host can kick"}))
        return
    all_present = [u.user_id for u in app.ctx.session_store.get_all()]
    targets = [u.user_id for u in app.ctx.session_store.get_all()
               if u.username == target and u.user_id != sender_id]
    if not targets:
        await mgr.send_to(sender_id, json.dumps(
            {"type": "kick_denied", "reason": f"no member named {target!r} (can't kick yourself)"}))
        return
    # Rotate the password (E2E key derives from it) + drop the old-key backlog.
    new_password = secrets.token_urlsafe(9)
    app.ctx.srp_manager.rotate(new_password)
    app.ctx.message_store.clear()
    kicked = json.dumps({"type": "kicked", "username": target, "by": sender_name})
    rotated = json.dumps({"type": "password_rotated", "password": new_password})
    remaining = [uid for uid in all_present if uid not in targets]
    # Remaining members: deliver the kick notice + the NEW password, then close so
    # they auto-reconnect (re-key) with it. Targets: notice only, then close — no
    # new password, so their reconnect fails and they stay out.
    for uid in remaining:
        await mgr.deliver_and_close(uid, [kicked, rotated], reason="room re-keyed (kick)")
        app.ctx.session_store.remove(uid)
    for uid in targets:
        await mgr.deliver_and_close(uid, [kicked], reason="kicked by host")
        app.ctx.session_store.remove(uid)
    app.ctx.host_user_id = None  # re-derived when the reconnect wave arrives


async def srp_init(request: Request, app: Sanic) -> HTTPResponse:
    try:
        client_ip = get_client_ip(request)
        if not app.ctx.rate_limiter.is_allowed(client_ip):
            return response.json({"error": "Rate limited"}, status=429)

        data = request.json or {}
        username = data.get("username", "unknown")
        client_public_b64 = data.get("A")

        if not client_public_b64:
            return response.json({"error": "Missing A"}, status=400)

        client_public = base64.b64decode(client_public_b64)

        if app.ctx.session_store.username_exists(username):
            return response.json({"error": "Username taken"}, status=409)

        if app.ctx.session_store.count() >= app.ctx.max_users:
            return response.json({"error": "Clergy full"}, status=409)

        user_id, B, salt = app.ctx.srp_manager.init_auth(username, client_public)

        return response.json(
            {
                "user_id": user_id,
                "B": base64.b64encode(B).decode(),
                "salt": base64.b64encode(salt).decode(),
                "room_salt": base64.b64encode(app.ctx.room_salt).decode(),
            }
        )

    except Exception:
        return response.json({"error": "SRP init failed"}, status=500)


async def srp_verify(request: Request, app: Sanic) -> HTTPResponse:
    try:
        client_ip = get_client_ip(request)
        if not app.ctx.rate_limiter.is_allowed(client_ip):
            return response.json({"error": "Rate limited"}, status=429)

        data = request.json or {}
        user_id = data.get("user_id")
        client_proof_b64 = data.get("M")
        username = data.get("username", "unknown")

        if not user_id or not client_proof_b64:
            return response.json({"error": "Missing user_id or M"}, status=400)

        client_proof = base64.b64decode(client_proof_b64)

        # Authoritative capacity gate — the slot is only consumed once a session
        # is actually added here (init is best-effort / racy).
        if app.ctx.session_store.count() >= app.ctx.max_users:
            return response.json({"error": "Clergy full"}, status=409)

        H_AMK, session_key = app.ctx.srp_manager.verify_auth(user_id, client_proof)

        fernet_key = base64.urlsafe_b64encode(session_key[:32])

        session = UserSession(
            user_id=user_id,
            ip=get_client_ip(request),
            username=username,
            fernet_key=fernet_key,
        )
        app.ctx.session_store.add(session)

        ws_token = generate_ws_token(user_id, app.ctx.ws_secret)

        return response.json(
            {
                "H_AMK": base64.b64encode(H_AMK).decode(),
                "ws_token": ws_token,
            }
        )

    except ValueError as e:
        return response.json({"error": str(e)}, status=401)
    except Exception:
        return response.json({"error": "SRP verify failed"}, status=500)


async def chat_ws(request: Request, ws: Websocket, app: Sanic) -> None:
    user_id = request.args.get("user_id")
    ws_token = request.args.get("ws_token")

    if not user_id or not ws_token:
        await ws.close(code=4002, reason="user_id and ws_token required")
        return

    expected_token = generate_ws_token(user_id, app.ctx.ws_secret)
    if not hmac.compare_digest(ws_token, expected_token):
        await ws.close(code=4003, reason="Invalid token")
        return

    session = app.ctx.session_store.get(user_id)
    if not session:
        await ws.close(code=4002, reason="Invalid session")
        return

    manager = app.ctx.connection_manager
    _disable_nagle(ws)
    # Enqueue this client's init snapshot as its first outbound frame, then
    # register it so broadcasts can target it — guaranteeing init arrives first.
    await manager.connect(user_id, ws, initial=state_frame(app))
    _current_host(app)  # first joiner becomes the room host (kick authority)

    try:
        # Announce arrival to everyone already present, then a fresh roster.
        await manager.broadcast(
            json.dumps(
                {
                    "type": "user_joined",
                    "user_id": user_id,
                    "username": session.username,
                }
            ),
            exclude_user=user_id,
        )
        await manager.broadcast(_roster_frame(app))

        async for data in ws:
            if data is None:
                break

            text = str(data)
            # Drop oversized frames before they reach the store/broadcast: this
            # bounds memory and stops a single client from flooding the room.
            if len(text) > MAX_FRAME_SIZE:
                continue

            app.ctx.session_store.update_activity(user_id)

            # Cleartext moderation control frames are handled server-side, never
            # relayed as chat. Everything else is an opaque E2E-encrypted blob.
            ctl = _parse_control(text)
            if ctl is not None:
                if ctl.get("type") == "kick":
                    await _handle_kick(app, user_id, session.username,
                                       str(ctl.get("target", "")))
                continue

            message = Message(
                text=text,
                username=session.username,
            )
            app.ctx.message_store.add(message)

            await manager.broadcast(
                json.dumps(
                    {
                        "type": "message",
                        "data": asdict(message),
                    }
                )
            )

    except Exception:
        pass
    finally:
        await manager.disconnect(user_id)
        # Free the slot + username so the clergy can be rejoined (was previously
        # held until the 1h stale sweep, which also blocked the name).
        app.ctx.session_store.remove(user_id)
        await manager.broadcast(
            json.dumps(
                {
                    "type": "user_left",
                    "user_id": user_id,
                }
            )
        )
        await manager.broadcast(_roster_frame(app))


async def health(request: Request, app: Sanic) -> HTTPResponse:
    return json_response(
        {
            "status": "ok",
            "messages": app.ctx.message_store.count(),
            "users": app.ctx.session_store.count(),
            "timestamp": utcnow().isoformat(),
        }
    )


async def clear_messages(request: Request, app: Sanic) -> HTTPResponse:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return response.json({"error": "Unauthorized"}, status=401)

    token = auth_header[7:]
    if not hmac.compare_digest(token, app.ctx.admin_token):
        return response.json({"error": "Unauthorized"}, status=401)

    app.ctx.message_store.clear()
    return json_response({"status": "cleared"})
