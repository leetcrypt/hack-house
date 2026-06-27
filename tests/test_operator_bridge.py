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
from cmd_chat.operator import sandbox as sbx
from cmd_chat.operator import bootstrap as boot


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


# ── sandbox: keystroke encoding (the stop-vocabulary) ─────────────────────
def test_encode_keys_named_and_literal():
    assert sbx.encode_keys(["ls -la", "enter"]) == b"ls -la\r"
    assert sbx.encode_keys("ctrl-c") == b"\x03"          # SIGINT
    assert sbx.encode_keys("ctrl-d") == b"\x04"          # EOF
    assert sbx.encode_keys(["CTRL-C"]) == b"\x03"        # case-insensitive
    assert sbx.encode_keys("up") == b"\x1b[A"            # ANSI arrow
    # 'text:' forces verbatim so a literal word like "enter" can be typed
    assert sbx.encode_keys(["text:enter"]) == b"enter"
    assert sbx.encode_keys("hex:1b5b41") == b"\x1b[A"
    # a bare unknown string is typed as-is
    assert sbx.encode_keys("q") == b"q"


def test_exec_prefix_shapes():
    assert sbx.exec_prefix("podman", "box") == ["podman", "exec", "-i", "box"]
    assert sbx.exec_prefix("docker", "box") == ["docker", "exec", "-i", "box"]
    assert sbx.exec_prefix("multipass", "vm") == ["multipass", "exec", "vm", "--"]
    assert sbx.exec_prefix("local", "x") == []
    assert sbx.exec_prefix("podman", "") is None        # unaddressable
    assert sbx.exec_prefix("qemu", "x") is None


# ── sandbox: target gating ────────────────────────────────────────────────
def test_target_prefers_own_then_granted_room():
    async def go():
        b = _bridge("oracle")
        # nothing yet → a clear error, no target
        eng, name, err = b._target()
        assert eng is None and err and "no sandbox" in err
        # room has a sandbox but we're not granted → refused
        b.sbx_engine, b.sbx_name = "podman", "hack-house"
        eng, name, err = b._target()
        assert eng is None and "granted" in err
        # granted → drive the room's container directly (co-located exec)
        b.granted = True
        eng, name, err = b._target()
        assert (eng, name, err) == ("podman", "hack-house", None)
        # our own container always wins
        b._own_engine, b._own_name = "podman", "hh-op-oracle"
        eng, name, err = b._target()
        assert (eng, name, err) == ("podman", "hh-op-oracle", None)
    asyncio.run(go())


def test_keys_requires_connection_and_grant():
    async def go():
        b = _bridge("oracle")
        # not connected
        r = await b._op_keys({"keys": ["enter"]})
        assert r["ok"] is False and "not connected" in r["error"]
        # connected but not granted → inert
        b._ws = FakeWS()
        r = await b._op_keys({"keys": ["enter"]})
        assert r["ok"] is False and "granted" in r["error"]
        # granted → injects an encrypted _sbx:input frame
        b.granted = True
        r = await b._op_keys({"keys": ["ls", "enter"]})
        assert r["ok"] is True and r["bytes"] == 3
        frame = json.loads(b.room_fernet.decrypt(b._ws.sent[0].encode()).decode())
        assert frame["_sbx"] == "input"
        import base64 as _b64
        assert _b64.b64decode(frame["b64"]) == b"ls\r"
    asyncio.run(go())


def test_exec_requires_target():
    async def go():
        b = _bridge("oracle")
        r = await b._op_exec({"cmd": "echo hi"})
        assert r["ok"] is False and "no sandbox" in r["error"]
        r = await b._op_exec({"cmd": ""})
        assert r["ok"] is False and "empty" in r["error"]
    asyncio.run(go())


# ── Phase 5: recursion budget guardrails ──────────────────────────────────
def test_budget_descend_and_exhaust():
    b = boot.Budget(depth=2, fanout=2, cost_usd=5.0)
    assert b.can_spawn()
    child = b.descend()                       # one level down for the child
    assert child.depth == 1 and child.fanout == 2
    spent = b.spent()                         # this node used one child slot
    assert spent.fanout == 1 and spent.depth == 2
    # depth floor: a leaf cannot nest
    leaf = boot.Budget(depth=0, fanout=2)
    assert not leaf.can_spawn()
    try:
        leaf.descend()
        assert False, "expected BudgetExhausted"
    except boot.BudgetExhausted:
        pass
    # fan-out floor
    spread = boot.Budget(depth=3, fanout=0)
    assert not spread.can_spawn()


def test_install_plan_prefers_npm_and_honors_override(monkeypatch=None):
    plan = boot.install_plan({"has_npm": True, "pkg_manager": "apt-get"})
    assert plan and plan[0] == ["npm", "install", "-g", boot.CLAUDE_NPM_PACKAGE]
    # no npm → distro node install then npm global
    plan = boot.install_plan({"has_npm": False, "pkg_manager": "apk"})
    assert ["apk", "add", "nodejs", "npm"] in plan
    # explicit override wins and is the only command
    import os
    os.environ["HH_CLAUDE_INSTALL"] = "echo custom-install"
    try:
        plan = boot.install_plan({"has_npm": True})
        assert plan == [["sh", "-c", "echo custom-install"]]
    finally:
        del os.environ["HH_CLAUDE_INSTALL"]


def test_compose_directive_carries_objective_stop_budget():
    d = boot.compose_directive(
        "map the room and report", {"host": "h", "port": 9, "name": "scout"},
        boot.Budget(depth=1, fanout=2, cost_usd=3.0), stop=["task done"])
    assert "hh-operator skill" in d
    assert "map the room and report" in d
    assert "task done" in d
    assert "depth=1" in d and "fanout=2" in d


def test_plan_creds_gated_off_by_default():
    off = boot.plan_creds("/tmp/child", allow=False)
    assert off["staged"] is False and "gating on" in off["reason"]


def test_build_run_argv_flags():
    argv = boot.build_run_argv("do x", skip_permissions=True)
    assert argv[:3] == ["claude", "-p", "do x"]
    assert "--dangerously-skip-permissions" in argv
    argv = boot.build_run_argv("do x")
    assert "--dangerously-skip-permissions" not in argv


# ── spawn op: gating + dry-run ────────────────────────────────────────────
def test_spawn_dry_run_returns_plan_without_launch():
    async def go():
        b = _bridge("oracle")
        b.budget = boot.Budget(depth=1, fanout=1, cost_usd=2.0)
        r = await b._op_spawn({"objective": "scout the room",
                               "room": {"host": "h", "port": 7, "name": "kid"},
                               "dry_run": True})
        assert r["ok"] and r["dry_run"] is True
        assert r["plan"]["budget"] == {"depth": 0, "fanout": 1, "cost_usd": 2.0}
        assert r["plan"]["creds"]["staged"] is False     # gated off
        # dry-run must not consume the budget
        assert b.budget.fanout == 1
        # no spawn event emitted on a dry run
        assert [e for e in b.events if e["kind"] == "spawn"] == []
    asyncio.run(go())


def test_spawn_refuses_without_objective_or_room_or_budget():
    async def go():
        b = _bridge("oracle")
        assert (await b._op_spawn({"objective": ""}))["ok"] is False
        assert (await b._op_spawn({"objective": "x"}))["ok"] is False  # no room
        b.budget = boot.Budget(depth=0, fanout=0)
        r = await b._op_spawn({"objective": "x",
                               "room": {"host": "h", "port": 7}})
        assert r["ok"] is False and "budget exhausted" in r["error"]
    asyncio.run(go())


# ── Phase 6: relay read (screen) + stop-condition engine (watch) ──────────
def test_strip_ansi():
    assert sbx.strip_ansi("\x1b[31mred\x1b[0m text") == "red text"
    assert sbx.strip_ansi("a\rb") == "a\nb"            # bare CR → newline
    assert sbx.strip_ansi("\x1b]0;title\x07ok") == "ok"  # OSC title strip


def _sbx_data(b, raw: bytes):
    """A broker PTY-relay frame carrying raw terminal bytes."""
    import base64 as _b64
    payload = json.dumps({"_sbx": "data", "b64": _b64.b64encode(raw).decode()})
    return _msg_frame(b, "broker", payload)


def test_sbx_data_absorbed_into_screen():
    async def go():
        b = _bridge("oracle")
        await b._handle_frame(None, _sbx_data(b, b"\x1b[32mroot@box\x1b[0m:~# ls\r\n"))
        # PTY relay must NOT surface as a chat message
        assert [e for e in b.events if e["kind"] == "message"] == []
        r = await b._op_screen({})
        assert "root@box:~# ls" in r["text"]            # ansi-stripped
        raw = await b._op_screen({"ansi": True})
        assert "\x1b[32m" in raw["text"]                 # raw keeps codes
    asyncio.run(go())


def test_watch_matches_screen():
    async def go():
        b = _bridge("oracle")

        async def driver():
            await asyncio.sleep(0.05)
            await b._handle_frame(None, _sbx_data(b, b"...building...\n"))
            await asyncio.sleep(0.05)
            await b._handle_frame(None, _sbx_data(b, b"BUILD SUCCESS\n"))

        watcher = b._op_watch({"for": "BUILD (SUCCESS|FAIL)", "in": "screen",
                               "timeout": 5})
        res, _ = await asyncio.gather(watcher, driver())
        assert res["reason"] == "match" and res["match"] == "BUILD SUCCESS"
    asyncio.run(go())


def test_watch_matches_event_and_idle_and_timeout():
    async def go():
        b = _bridge("oracle")

        async def speak():
            await asyncio.sleep(0.05)
            await b._emit("message", **{"from": "alice"}, text="please stop now",
                          addressed=True)
        res, _ = await asyncio.gather(
            b._op_watch({"for": r"\bstop\b", "in": "events", "timeout": 5}), speak())
        assert res["reason"] == "match" and res["event"]["from"] == "alice"
        # idle: nothing happens → quiescence fires fast
        res = await b._op_watch({"idle": 0.2, "timeout": 5})
        assert res["reason"] == "idle"
        # timeout: a pattern that never appears
        res = await b._op_watch({"for": "never-ever", "timeout": 0.3})
        assert res["reason"] == "timeout" and res["matched"] is False
    asyncio.run(go())
