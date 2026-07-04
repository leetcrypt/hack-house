import hashlib
import hmac
import json
import base64
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
    return json.dumps(
        {
            "type": "roster",
            "users": [{"user_id": u.user_id, "username": u.username} for u in users],
            "capacity": app.ctx.max_users,
        }
    )


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
