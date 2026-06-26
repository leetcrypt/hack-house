"""Offline tests for the operator bridge (no server, no real websocket).

Each test builds the bridge *inside* its own ``asyncio.run`` so the bridge's
``asyncio.Condition`` binds to that test's loop (it is loop-bound on first use).
"""

import sys
import asyncio
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cryptography.fernet import Fernet

from cmd_chat.operator.bridge import OperatorBridge


def _bridge(name="oracle", trigger=None):
    b = OperatorBridge("h", 1, name=name, password="pw", no_tls=True, trigger=trigger)
    b.room_fernet = Fernet(Fernet.generate_key())
    return b


def _enc(b, text):
    return b.room_fernet.encrypt(text.encode()).decode()


def _msg_frame(b, sender, text):
    """A server 'message' envelope carrying an encrypted line from `sender`."""
    return json.dumps({"type": "message",
                       "data": {"username": sender, "text": _enc(b, text)}})


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        pass


# ── addressing ────────────────────────────────────────────────────────────
def test_is_addressed():
    b = _bridge("oracle", trigger="@bot")
    assert b._is_addressed("hey oracle you there")
    assert b._is_addressed("@oracle ping")
    assert b._is_addressed("ORACLE up?")          # case-insensitive
    assert b._is_addressed("ask @bot to help")    # custom trigger
    assert not b._is_addressed("oracleX is a var")  # word boundary
    assert not b._is_addressed("just chatting")


# ── inbox: seq + long-poll wake ───────────────────────────────────────────
def test_emit_seq_monotonic_and_ring():
    async def go():
        b = _bridge()
        for i in range(5):
            await b._emit("message", **{"from": "a"}, text=f"m{i}", addressed=False)
        seqs = [e["seq"] for e in b.events]
        assert seqs == [1, 2, 3, 4, 5]
        assert b.seq == 5
    asyncio.run(go())


def test_read_long_poll_wakes_on_emit():
    async def go():
        b = _bridge()

        async def reader():
            return await b._op_read({"since": 0, "wait": True, "timeout": 5})

        async def writer():
            await asyncio.sleep(0.05)
            await b._emit("message", **{"from": "alice"}, text="hi oracle", addressed=True)

        rd, _ = await asyncio.gather(reader(), writer())
        assert rd["ok"] and len(rd["events"]) == 1
        assert rd["events"][0]["text"] == "hi oracle"
        assert rd["events"][0]["addressed"] is True
    asyncio.run(go())


def test_read_since_filters():
    async def go():
        b = _bridge()
        for i in range(3):
            await b._emit("message", **{"from": "a"}, text=str(i), addressed=False)
        rd = await b._op_read({"since": 2, "wait": False})
        assert [e["seq"] for e in rd["events"]] == [3]
    asyncio.run(go())


# ── frame routing ─────────────────────────────────────────────────────────
def test_message_frame_emits_addressed():
    async def go():
        b = _bridge("oracle")
        await b._handle_frame(None, _msg_frame(b, "alice", "hey oracle help"))
        msgs = [e for e in b.events if e["kind"] == "message"]
        assert len(msgs) == 1
        assert msgs[0]["from"] == "alice" and msgs[0]["addressed"] is True
    asyncio.run(go())


def test_own_message_ignored():
    async def go():
        b = _bridge("oracle")
        await b._handle_frame(None, _msg_frame(b, "oracle", "/say echo"))
        assert [e for e in b.events if e["kind"] == "message"] == []
    asyncio.run(go())


def test_control_acl_recorded():
    async def go():
        b = _bridge("oracle")
        ctrl = json.dumps({"_perm": "acl", "owner": "alice",
                           "drivers": ["alice", "oracle"], "sudoers": ["alice"]})
        await b._handle_frame(None, _msg_frame(b, "alice", ctrl))
        assert b.granted is True
        acl = [e for e in b.events if e["kind"] == "acl"]
        assert acl and acl[0]["granted"] is True
        # an ACL control frame must NOT surface as a chat 'message'
        assert [e for e in b.events if e["kind"] == "message"] == []
    asyncio.run(go())


def test_control_sandbox_status_recorded():
    async def go():
        b = _bridge("oracle")
        ctrl = json.dumps({"_sbx": "status", "state": "ready",
                           "engine": "podman", "name": "hack-house", "backend": "podman"})
        await b._handle_frame(None, _msg_frame(b, "alice", ctrl))
        assert b.sbx_engine == "podman" and b.sbx_name == "hack-house"
        assert any(e["kind"] == "sandbox" for e in b.events)
    asyncio.run(go())


def test_init_backfill_once():
    async def go():
        b = _bridge("oracle")
        init = json.dumps({
            "type": "init",
            "users": [{"username": "alice"}, {"username": "oracle"}],
            "messages": [
                {"username": "alice", "text": _enc(b, "older line")},
                {"username": "oracle", "text": _enc(b, "my own old line")},  # skipped
            ],
        })
        await b._handle_frame(None, init)
        backfilled = [e for e in b.events if e.get("backfill")]
        assert len(backfilled) == 1 and backfilled[0]["from"] == "alice"
        assert backfilled[0]["addressed"] is False
        # a reconnect re-sends init; we must not duplicate history
        await b._handle_frame(None, init)
        assert len([e for e in b.events if e.get("backfill")]) == 1
    asyncio.run(go())


# ── control ops ───────────────────────────────────────────────────────────
def test_say_requires_connection():
    async def go():
        b = _bridge()
        resp = await b._op_say({"text": "hello"})
        assert resp["ok"] is False and "not connected" in resp["error"]
    asyncio.run(go())


def test_say_sends_and_records():
    async def go():
        b = _bridge()
        ws = FakeWS()
        b._ws = ws
        resp = await b._op_say({"text": "yes operator online"})
        assert resp["ok"] is True
        assert len(ws.sent) == 1
        # round-trips through the room key
        assert b.room_fernet.decrypt(ws.sent[0].encode()).decode() == "yes operator online"
        sent = [e for e in b.events if e["kind"] == "sent"]
        assert sent and sent[0]["text"] == "yes operator online"
    asyncio.run(go())


def test_dispatch_status_and_roster():
    async def go():
        b = _bridge("oracle")
        b.users = [{"username": "oracle"}, {"username": "alice"}]
        st = await b._dispatch({"op": "status"})
        assert st["ok"] and st["name"] == "oracle" and st["connected"] is False
        assert set(st["users"]) == {"oracle", "alice"}
        ping = await b._dispatch({"op": "ping"})
        assert ping["pong"] is True
        unknown = await b._dispatch({"op": "frobnicate"})
        assert unknown["ok"] is False
    asyncio.run(go())
