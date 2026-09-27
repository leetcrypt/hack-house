"""Integrated code-as-action operator brain (native, in-process — no smolagents).

This is the operator's built-in code-as-action harness: the model acts by writing a
Python code block each turn; the operator executes it in a **restricted interpreter**
where the bridge verbs (``exec_shell``/``write_file``/``read_file``/``say``) are the
only functions that touch the shared sandbox — over the same control socket the native
harness and ``hh-bridge`` CLI use. No new side-effect surface, no subprocess, no extra
dependency; it runs in the hh venv alongside the daemon.

Why code-as-action is the default: for local ≤9B models it composes multi-step tool
use where native JSON tool-calling collapses (see docs/harness-integration — smolagent
5/5 vs native 0/5 on the composition fixture, 0.88 vs 0.35 tool-call yield). And because
the model only writes text (a code block), it needs **no function-calling support** — it
works with any provider's plain ``complete``.

Trust model (same as smolagents' LocalPythonExecutor, which the shell-out plugin already
ran): the model's orchestration code runs in the operator process with a curated builtin
set (no ``import``/``open``/``eval``/``exec``/``compile``/``__import__``) and only the
bridge verbs exposed — so it computes + orchestrates, but can only *act* on the sandbox
through the verbs. The real blast radius stays the podman sandbox. This restricted-builtins
executor blocks the common host-touching paths; AST-level hardening against exotic
``__subclasses__`` escapes is a deliberate follow-up increment, not shipped here.
"""

from __future__ import annotations

import builtins as _builtins
import contextlib
import io
import re
from dataclasses import dataclass, field
from typing import Callable

from . import bootstrap as boot
from .cli_client import request as _socket_request
from .harness import _clip, _format_events   # reuse the shared observation helpers

# ── the restricted executor ─────────────────────────────────────────────────
# A whitelist of builtins safe for pure computation. Everything host-touching or
# metaprogramming (import/open/eval/exec/compile/getattr/type/object/super/…) is
# deliberately absent, so the model's code can compute + call the bridge verbs but
# not reach the host.
_SAFE_NAMES = (
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes", "chr", "dict",
    "divmod", "enumerate", "filter", "float", "format", "frozenset", "hex", "int",
    "isinstance", "issubclass", "len", "list", "map", "max", "min", "oct", "ord",
    "pow", "print", "range", "repr", "reversed", "round", "set", "slice", "sorted",
    "str", "sum", "tuple", "zip", "True", "False", "None",
)
SAFE_BUILTINS = {n: getattr(_builtins, n) for n in _SAFE_NAMES if hasattr(_builtins, n)}

# Pull a fenced ```python … ``` block (or a bare ``` … ``` block) out of the model's turn.
_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S | re.I)
_DONE_RE = re.compile(r"^\s*DONE:", re.I)


class _Done(Exception):
    """Raised by the injected ``final_answer`` to end the loop with a summary."""

    def __init__(self, summary: str = ""):
        self.summary = str(summary)


def extract_code(text: str) -> str | None:
    """Return the first fenced code block's body, or None if the turn has none."""
    m = _FENCE_RE.search(text or "")
    return m.group(1).strip() if m else None


def _clean_exec_output(r: dict) -> str:
    """exec verb → clean stdout on success ([exit=N]-prefixed on failure), matching the
    plugin's contract so code that writes a captured value to a file gets a clean value."""
    if r.get("error"):
        return f"error: {r['error']}"
    out = r.get("output", "")
    out = out.rstrip("\n") if isinstance(out, str) else out
    rc = r.get("rc")
    return out if rc == 0 else f"[exit={rc}]\n{out}"


@dataclass
class CodeActResult:
    final: str = ""
    turns: int = 0
    tokens: int = 0          # char-estimate (len/4) — a bound, not exact
    reason: str = ""
    tool_calls: int = 0
    code_errors: int = 0     # code blocks that raised (the code-as-action inert/yield signal)


CODEACT_SYSTEM_TAIL = (
    "\n\nYou act by WRITING ONE PYTHON CODE BLOCK per turn, fenced ```python … ```. "
    "Inside it these functions are ALREADY DEFINED (never import or redefine them):\n"
    "  exec_shell(command: str) -> str   # run a shell command in the sandbox; returns its output\n"
    "  write_file(path: str, content: str) -> str   # write a file in the sandbox\n"
    "  read_file(path: str) -> str       # read a file from the sandbox\n"
    "  say(text: str) -> str             # speak one line into the room\n"
    "  final_answer(summary: str)        # call when the objective is fully met, then stop\n"
    "Do ALL arithmetic and logic in Python; print(...) anything you need to see next turn. The "
    "code runs in a RESTRICTED interpreter — no import, open, eval; you touch the sandbox ONLY "
    "through the functions above. A file you write and then run in the sandbox (e.g. `python3 x.py`) "
    "is a SEPARATE program: it must be self-contained (stdlib only, e.g. Python's open()), it does "
    "NOT have exec_shell/read_file/write_file. exec/write/read work only once the room owner has "
    "granted you drive. When the objective is fully met, call final_answer('<one sentence>')."
)


def compose_system(objective: str, *, stop: list[str] | None = None,
                   budget: "boot.Budget | None" = None,
                   role_overlay: str | None = None) -> str:
    """Layer-1 capabilities + objective + stop conditions + the code-as-action rule.
    Mirrors harness.compose_system but with the code-block operating tail."""
    stop = stop or ["objective met", "owner says stop/leave", "idle past remit"]
    parts = [boot.load_capabilities()]
    if role_overlay:
        parts.append(role_overlay.strip())
    parts.append(f"OBJECTIVE: {objective}")
    parts.append("Stop conditions (say a short sign-off, then stop when any holds): "
                 + "; ".join(stop) + ".")
    if budget is not None:
        parts.append(
            f"Recursion budget you inherit: depth={budget.depth}, "
            f"fanout={budget.fanout}, cost≈${budget.cost_usd:.2f}.")
    return "\n\n".join(parts) + CODEACT_SYSTEM_TAIL


@dataclass
class CodeActHarness:
    """Drives a room with a code-as-action loop over the bridge control socket. The
    model writes a Python block; we execute it with the bridge verbs bound. Side-effect
    free to construct; :meth:`run` does the work. ``request_fn`` is injectable so unit
    tests drive the whole loop with no daemon/network/model."""

    provider: object
    sock_path: str
    objective: str
    stop: list[str] | None = None
    budget: "boot.Budget | None" = None
    role_overlay: str | None = None
    max_turns: int = 12
    token_ceiling: int = 20000
    max_nudges: int = 2
    me: str = "operator"
    request_fn: Callable[[str, dict, float], dict] | None = None
    poll_timeout: float = 20.0

    _cursor: int = field(default=0, init=False)

    def _call(self, obj: dict, read_timeout: float = 35.0) -> dict:
        fn = self.request_fn or _socket_request
        return fn(self.sock_path, obj, read_timeout)

    def _drain_events(self, *, wait: bool, timeout: float) -> list[dict]:
        resp = self._call({"op": "read", "since": self._cursor, "wait": wait,
                           "timeout": timeout}, read_timeout=timeout + 5.0)
        events = resp.get("events", []) or []
        if events:
            self._cursor = events[-1].get("seq", self._cursor)
        elif resp.get("seq") is not None:
            self._cursor = max(self._cursor, int(resp["seq"]))
        return events

    # ── bridge verbs the model's code can call (each 1:1 with a control-socket op) ──
    def _verbs(self, counter: list[int]) -> dict:
        import base64

        def exec_shell(command: str) -> str:
            counter[0] += 1
            return _clip(_clean_exec_output(self._call({"op": "exec", "cmd": str(command)})))

        def write_file(path: str, content: str) -> str:
            counter[0] += 1
            r = self._call({"op": "write", "path": str(path), "content": content})
            return (f"wrote {r.get('path')} ({r.get('bytes')} bytes)"
                    if r.get("ok") else f"error: {r.get('error')}")

        def read_file(path: str) -> str:
            counter[0] += 1
            r = self._call({"op": "get", "path": str(path)})
            if not r.get("ok"):
                return f"error: {r.get('error')}"
            try:
                return _clip(base64.b64decode(r.get("b64", "")).decode(errors="replace"))
            except Exception:  # noqa: BLE001
                return "[binary]"

        def say(text: str) -> str:
            counter[0] += 1
            r = self._call({"op": "say", "text": str(text)})
            return "(said)" if r.get("ok") else f"error: {r.get('error')}"

        def final_answer(summary: str = "") -> None:
            raise _Done(summary)

        return {"exec_shell": exec_shell, "write_file": write_file,
                "read_file": read_file, "say": say, "final_answer": final_answer}

    def _run_code(self, code: str) -> tuple[str, bool, str, int]:
        """Execute one model code block in the restricted namespace. Returns
        (captured_output, done, final_summary, verb_calls)."""
        counter = [0]
        ns = {"__builtins__": SAFE_BUILTINS, **self._verbs(counter)}
        buf = io.StringIO()
        done, final = False, ""
        try:
            with contextlib.redirect_stdout(buf):
                exec(compile(code, "<codeact>", "exec"), ns)  # noqa: S102 — restricted ns
        except _Done as d:
            done, final = True, d.summary
        except Exception as e:  # noqa: BLE001
            buf.write(f"\n[code error: {type(e).__name__}: {e}]")
            return buf.getvalue(), False, "", counter[0]  # signalled to caller via reason
        return buf.getvalue(), done, final, counter[0]

    # ── the loop ─────────────────────────────────────────────────────────────
    def run(self) -> CodeActResult:
        from .harness import HARNESS_SYSTEM_TAIL  # noqa: F401  (kept for parity import)
        from cmd_chat.ai.providers import Msg

        system = compose_system(self.objective, stop=self.stop, budget=self.budget,
                                role_overlay=self.role_overlay)
        try:
            seed = self._drain_events(wait=False, timeout=0.0)
        except Exception as e:  # noqa: BLE001
            return CodeActResult(reason="bridge-unreachable", final=f"[bridge unreachable: {e}]")
        obs = _format_events(seed, self.me) or "(room is quiet)"
        messages = [Msg("user", f"You have joined the room. Current state:\n{obs}\n\n"
                                 "Begin working toward the objective.")]

        res = CodeActResult()
        nudges = 0
        did_action = False
        while res.turns < self.max_turns + self.max_nudges:
            res.turns += 1
            try:
                text = self.provider.complete(system, messages)
            except Exception as e:  # noqa: BLE001
                res.reason, res.final = "provider-error", f"[ai error: {e}]"
                return res
            res.tokens += max(1, len(text or "") // 4)

            code = extract_code(text)
            if code is None:
                done_claimed = bool(_DONE_RE.match(text or ""))
                if done_claimed and (did_action or res.turns > 1 or nudges >= self.max_nudges):
                    res.final = re.sub(_DONE_RE, "", text, count=1).strip()
                    res.reason = "done"
                    return res
                if nudges >= self.max_nudges:
                    res.final = ((text or "").strip() or "[stopped — model wrote no code]") if did_action \
                        else "[stopped — model never ran code; task likely not performed]"
                    res.reason = "stalled" if did_action else "stalled-no-action"
                    return res
                nudges += 1
                messages.append(Msg("assistant", text or ""))
                ask = ("You wrote no ```python code block. Write ONE code block that calls the "
                       "tools to make progress, or call final_answer(...) only if the objective "
                       "is truly done.")
                messages.append(Msg("user", ask))
                continue

            if res.tokens >= self.token_ceiling:
                res.final = f"[stopped — token ceiling {self.token_ceiling} reached]"
                res.reason = "token-ceiling"
                return res

            messages.append(Msg("assistant", text))
            output, done, final, ncalls = self._run_code(code)
            res.tool_calls += ncalls
            if ncalls:
                did_action = True
            if "[code error:" in output:
                res.code_errors += 1
            if done:
                res.final = final or "(done)"
                res.reason = "done"
                return res
            # feed execution output + any new room events back
            try:
                fresh = self._drain_events(wait=False, timeout=0.0)
            except Exception:  # noqa: BLE001
                fresh = []
            room = _format_events(fresh, self.me)
            fb = "Execution output:\n" + (_clip(output) if output.strip() else "(no output)")
            if room:
                fb += "\n\nRoom update:\n" + room
            messages.append(Msg("user", fb))

        res.final = res.final or "[stopped — reached turn cap]"
        res.reason = res.reason or "turn-cap"
        return res
