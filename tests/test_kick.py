"""Host-only force-kick (server-enforced) + room-password rotation.

`/kick` is authorised strictly to the room host (oldest present connection), it
disconnects the target and rotates the SRP password so the removed member can't
rejoin with the shared secret. Cleartext control frames are distinguished from
E2E-encrypted room content by shape. See views.py `_handle_kick`/`_current_host`.
"""
import asyncio

from cmd_chat.server.factory import create_app
from cmd_chat.server.models import UserSession
from cmd_chat.server.views import _current_host, _parse_control, _handle_kick


class FakeWs:
    def __init__(self):
        self.sent: list[str] = []
        self.closed = None

    async def send(self, m):
        self.sent.append(m)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


async def _seat(app, uid, name):
    ws = FakeWs()
    await app.ctx.connection_manager.connect(uid, ws)
    app.ctx.session_store.add(UserSession(user_id=uid, ip="127.0.0.1", username=name))
    _current_host(app)
    return ws


def test_parse_control_distinguishes_kick_from_ciphertext():
    assert _parse_control("gAAAAABm-someFernetCiphertextTokenNotJson==") is None
    assert _parse_control('{"type":"message"}') is None
    got = _parse_control('{"type":"kick","target":"baddie"}')
    assert got and got["target"] == "baddie"


def test_current_host_promotes_when_host_leaves():
    async def scenario():
        app = create_app(password="x", name="t-host-promote")
        mgr = app.ctx.connection_manager
        await _seat(app, "a", "alice")
        await _seat(app, "b", "bob")
        assert _current_host(app) == "a"        # first joiner is host
        await mgr.disconnect("a")
        assert _current_host(app) == "b"         # auto-promoted
    asyncio.run(scenario())


def test_non_host_kick_is_denied_and_password_unchanged():
    async def scenario():
        app = create_app(password="orig", name="t-kick-deny")
        await _seat(app, "h1", "host")
        bad = await _seat(app, "t1", "baddie")
        old_salt = app.ctx.srp_manager.salt
        await _handle_kick(app, "t1", "baddie", "host")   # non-host tries to kick
        await asyncio.sleep(0.05)                          # let writer drain
        assert any("kick_denied" in s for s in bad.sent)
        assert app.ctx.srp_manager.salt == old_salt        # NOT rotated
        assert app.ctx.session_store.get("h1") is not None  # host still present
    asyncio.run(scenario())


def test_host_kick_disconnects_target_and_rotates_password():
    async def scenario():
        app = create_app(password="orig", name="t-kick-do")
        host_ws = await _seat(app, "h1", "host")
        bad = await _seat(app, "t1", "baddie")
        old_salt = app.ctx.srp_manager.salt
        await _handle_kick(app, "h1", "host", "baddie")    # host kicks
        await asyncio.sleep(0.05)
        assert app.ctx.srp_manager.salt != old_salt         # password rotated
        assert bad.closed and bad.closed[0] == 4009          # target socket closed
        assert app.ctx.session_store.get("t1") is None       # session removed
        assert "t1" not in app.ctx.connection_manager.active_connections
        # remaining members are handed the new password
        assert any('"password_rotated"' in s for s in host_ws.sent)
    asyncio.run(scenario())
