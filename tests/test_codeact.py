"""Offline tests for the integrated code-as-action operator brain (cmd_chat/operator/codeact.py).

Drives the whole loop with a scripted fake provider (returns code-block text per turn)
and a fake control socket (no daemon/model), and unit-tests the restricted executor.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cmd_chat.operator.codeact import (  # noqa: E402
    CodeActHarness, SAFE_BUILTINS, extract_code, compose_system,
)


class FakeProvider:
    """Scripted plain-completion provider: returns the next string per complete() call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls_seen = []

    def complete(self, system, messages):
        self.calls_seen.append((system, list(messages)))
        return self.script.pop(0) if self.script else "final: ```python\nfinal_answer('ran out')\n```"


class FakeBridge:
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
        if op == "exec":
            return {"ok": True, "rc": 0, "output": "hello\n"}
        if op == "write":
            return {"ok": True, "path": obj.get("path"), "bytes": len(obj.get("content", ""))}
        if op == "get":
            import base64
            return {"ok": True, "b64": base64.b64encode(b"file body").decode()}
        if op == "say":
            return {"ok": True}
        return {"ok": True}

    def ops(self):
        return [r.get("op") for r in self.requests]


def _h(provider, bridge, **kw):
    return CodeActHarness(provider=provider, sock_path="/tmp/fake.sock",
                          objective="build the thing", request_fn=bridge, me="builder", **kw)


def _block(code):
    return f"here you go:\n```python\n{code}\n```"


# ── the restricted executor ──────────────────────────────────────────────────
def test_executor_blocks_import_and_open():
    h = _h(FakeProvider([]), FakeBridge())
    out, done, final, n = h._run_code("import os\nos.system('echo pwned')")
    assert not done and "[code error:" in out          # import blocked
    out2, *_ = h._run_code("open('/etc/passwd')")
    assert "[code error:" in out2                       # open blocked
    assert "open" not in SAFE_BUILTINS and "eval" not in SAFE_BUILTINS


def test_executor_runs_pure_python_and_prints():
    h = _h(FakeProvider([]), FakeBridge())
    out, done, final, n = h._run_code("print(sum(1 for x in [3,1,4,1,5] if x % 2))")
    assert out.strip() == "4" and not done and n == 0


def test_executor_verbs_hit_the_bridge():
    bridge = FakeBridge()
    h = _h(FakeProvider([]), bridge)
    out, done, final, n = h._run_code(
        "print(write_file('/root/a.txt', 'x'))\nprint(exec_shell('ls'))\nprint(read_file('/root/a.txt'))")
    assert n == 3                                       # three verb calls
    assert "wrote /root/a.txt" in out and "hello" in out and "file body" in out
    assert "write" in bridge.ops() and "exec" in bridge.ops() and "get" in bridge.ops()


def test_final_answer_ends_the_code():
    h = _h(FakeProvider([]), FakeBridge())
    out, done, final, n = h._run_code("x = 1 + 1\nfinal_answer('all done')")
    assert done and final == "all done"


# ── the loop ─────────────────────────────────────────────────────────────────
def test_full_flow_drives_bridge_then_done():
    provider = FakeProvider([
        _block("say('starting')\nwrite_file('a.txt', 'x')"),
        _block("out = exec_shell('echo hi')\nprint(out)\nfinal_answer('built it')"),
    ])
    bridge = FakeBridge(seed_events=[{"kind": "message", "from": "andre",
                                      "text": "go", "addressed": True}])
    res = _h(provider, bridge).run()
    assert res.reason == "done" and res.final == "built it"
    assert res.tool_calls == 3          # say + write + exec
    ops = bridge.ops()
    assert "say" in ops and "write" in ops and "exec" in ops


def test_no_code_block_is_nudged_then_finishes():
    # A prose turn (no code block) on turn 1 is nudged, not accepted; next turn acts+done.
    provider = FakeProvider([
        "I will think about this first.",
        _block("final_answer('done after nudge')"),
    ])
    res = _h(provider, FakeBridge()).run()
    assert res.reason == "done" and res.turns == 2


def test_pure_prose_never_acts_stalls_no_action():
    provider = FakeProvider(["thinking"] * 10)
    res = _h(provider, FakeBridge(), max_nudges=2).run()
    assert res.reason == "stalled-no-action"


def test_code_error_is_counted():
    provider = FakeProvider([
        _block("x = 1 / 0"),                      # raises → code_errors += 1, keeps looping
        _block("final_answer('recovered')"),
    ])
    res = _h(provider, FakeBridge()).run()
    assert res.code_errors == 1 and res.reason == "done"


def test_token_ceiling_stops():
    big = _block("print('x' * 100)\n" + "# pad " + "y" * 5000)
    provider = FakeProvider([big] * 50)
    res = _h(provider, FakeBridge(), token_ceiling=2000).run()
    assert res.reason == "token-ceiling"


def test_extract_code_and_compose_system():
    assert extract_code("no code here") is None
    assert extract_code("```python\nprint(1)\n```") == "print(1)"
    sysmsg = compose_system("do x")
    assert "do x" in sysmsg and "final_answer" in sysmsg and "code block" in sysmsg.lower()
