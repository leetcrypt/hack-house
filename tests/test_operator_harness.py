"""Offline tests for the harness-mode operator (Phase 2).

The loop is exercised end-to-end with a scripted fake Provider (no network/model)
and a fake control socket (no daemon), so every tool dispatch and stop condition
is asserted deterministically.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from cmd_chat.ai.providers import ToolsUnsupported
from cmd_chat.operator import bootstrap as boot
from cmd_chat.operator.harness import OPERATOR_TOOLS, OperatorHarness, compose_system


class FakeProvider:
    """Returns a scripted (text, calls, usage) per turn. A script entry is either a
    list of {"name","arguments"} tool calls, or a string (a text-only turn)."""

    def __init__(self, script, usage=None, raise_tools=False):
        self.script = list(script)
        self.usage = usage or {"prompt_eval_count": 10, "eval_count": 5}
        self.raise_tools = raise_tools
        self.calls_seen = []

    def complete_with_tools(self, system, messages, tools):
        self.calls_seen.append((system, list(messages), tools))
        if self.raise_tools:
            raise ToolsUnsupported("nope")
        step = self.script.pop(0) if self.script else "DONE: ran out of script"
        if isinstance(step, str):
            return step, [], self.usage
        return "", step, self.usage


class NoToolsProvider:
    pass


class FakeBridge:
    """Records every request and answers by op with canned responses."""

    def __init__(self, seed_events=None):
        self.requests = []
        self._seq = 0
        self._seed = seed_events or []
        self._seeded = False

    def __call__(self, sock_path, obj, read_timeout=35.0):
        self.requests.append(obj)
        op = obj.get("op")
        if op == "read":
            if not self._seeded:
                self._seeded = True
                evs = []
                for e in self._seed:
                    self._seq += 1
                    evs.append({"seq": self._seq, **e})
                return {"ok": True, "events": evs, "seq": self._seq}
            return {"ok": True, "events": [], "seq": self._seq}
        if op == "say":
            return {"ok": True}
        if op == "exec":
            return {"ok": True, "rc": 0, "output": "hello\n"}
        if op == "write":
            return {"ok": True, "path": obj.get("path"), "bytes": len(obj.get("content", ""))}
        if op == "get":
            import base64
            return {"ok": True, "b64": base64.b64encode(b"file body").decode()}
        if op == "keys":
            return {"ok": True}
        if op == "screen":
            return {"ok": True, "text": "screen buffer"}
        if op == "watch":
            return {"ok": True, "matched": True}
        if op == "manifest":
            return {"ok": True}
        if op == "down":
            return {"ok": True, "stopping": True}
        return {"ok": False, "error": f"unknown op {op}"}

    def ops(self):
        return [r.get("op") for r in self.requests]


def _harness(provider, bridge, **kw):
    return OperatorHarness(
        provider=provider, sock_path="/tmp/fake.sock",
        objective="build the thing", request_fn=bridge, me="builder", **kw)


def test_compose_system_is_portable_no_claude_isms():
    sysmsg = compose_system("do x", stop=["done"],
                            budget=boot.Budget(depth=1, fanout=2, cost_usd=3.0))
    assert "hh-operator skill" not in sysmsg          # no Claude-only delivery line
    assert "do x" in sysmsg
    assert "depth=1" in sysmsg and "fanout=2" in sysmsg
    # Layer-1 capabilities are inlined.
    assert boot.load_capabilities().split("\n", 1)[0] in sysmsg
    # The tool-only operating rule is present.
    assert "DONE:" in sysmsg


def test_full_flow_drives_bridge_then_done():
    provider = FakeProvider(script=[
        [{"name": "say", "arguments": {"text": "starting"}}],
        [{"name": "exec", "arguments": {"command": "echo hi"}}],
        [{"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}],
        "DONE: built and verified the thing",
    ])
    bridge = FakeBridge(seed_events=[
        {"kind": "message", "from": "andre", "text": "go build it", "addressed": True},
    ])
    res = _harness(provider, bridge).run()
    assert res.reason == "done"
    assert res.final == "built and verified the thing"
    assert res.tool_calls == 3
    # Each tool was dispatched to the matching bridge verb.
    ops = bridge.ops()
    assert "say" in ops and "exec" in ops and "write" in ops
    # The loop read the room (seed + post-action polls).
    assert ops.count("read") >= 1


def test_seed_observation_includes_room_state():
    provider = FakeProvider(script=["DONE: nothing to do"])
    bridge = FakeBridge(seed_events=[
        {"kind": "message", "from": "andre", "text": "hello operator", "addressed": True},
    ])
    _harness(provider, bridge).run()
    first_system, first_messages, first_tools = provider.calls_seen[0]
    seed_obs = first_messages[0]["content"]
    assert "hello operator" in seed_obs
    assert first_tools is OPERATOR_TOOLS


def test_own_messages_filtered_from_observation():
    provider = FakeProvider(script=["DONE: ok"])
    bridge = FakeBridge(seed_events=[
        {"kind": "message", "from": "builder", "text": "my own echo", "addressed": False},
        {"kind": "message", "from": "andre", "text": "real input", "addressed": False},
    ])
    _harness(provider, bridge).run()
    seed_obs = provider.calls_seen[0][1][0]["content"]
    assert "real input" in seed_obs
    assert "my own echo" not in seed_obs


def test_get_file_tool_decodes_content():
    provider = FakeProvider(script=[
        [{"name": "get_file", "arguments": {"path": "a.txt"}}],
        "DONE: read it",
    ])
    bridge = FakeBridge()
    res = _harness(provider, bridge).run()
    assert res.reason == "done"
    # The tool result (decoded file body) is fed back as a tool message.
    _, msgs, _ = provider.calls_seen[-1]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert any("file body" in m["content"] for m in tool_msgs)


def test_keys_tool_maps_to_text_token_and_enter():
    provider = FakeProvider(script=[
        [{"name": "keys", "arguments": {"text": "make build", "enter": True}}],
        "DONE: drove the pty",
    ])
    bridge = FakeBridge()
    _harness(provider, bridge).run()
    keys_req = next(r for r in bridge.requests if r.get("op") == "keys")
    assert keys_req["keys"] == ["text:make build", "enter"]


def test_token_ceiling_stops_the_loop():
    provider = FakeProvider(
        script=[[{"name": "exec", "arguments": {"command": "sleep 1"}}]] * 50,
        usage={"prompt_eval_count": 9000, "eval_count": 2000})
    bridge = FakeBridge()
    res = _harness(provider, bridge, token_ceiling=10000).run()
    assert res.reason == "token-ceiling"
    assert res.tokens >= 10000


def test_tools_unsupported_refuses_clearly():
    provider = FakeProvider(script=[], raise_tools=True)
    bridge = FakeBridge()
    res = _harness(provider, bridge).run()
    assert res.reason == "tools-unsupported"
    assert "refused" in res.final.lower()


def test_provider_without_tool_support_refuses():
    res = _harness(NoToolsProvider(), FakeBridge()).run()
    assert res.reason == "provider-no-tools"
    assert "function calling" in res.final.lower()


def test_stall_then_nudge_then_done():
    # A text-only non-DONE turn nudges; the next turn finishes.
    provider = FakeProvider(script=[
        "let me think about this",      # stall → nudge
        "DONE: finished after a nudge",
    ])
    bridge = FakeBridge()
    res = _harness(provider, bridge).run()
    assert res.reason == "done"
    assert res.final == "finished after a nudge"
    assert res.turns == 2


def test_stall_exhausts_nudges_and_stops():
    provider = FakeProvider(script=["thinking"] * 10)
    bridge = FakeBridge()
    res = _harness(provider, bridge, max_nudges=2).run()
    assert res.reason == "stalled"
