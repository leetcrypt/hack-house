#!/usr/bin/env python3
"""Tier-2 code-as-action operator brain (shell-out bridge).

Drives a hack-house room + shared sandbox with a **smolagents CodeAgent** — the
local-ai bench's winning paradigm (code-as-action) — instead of the native JSON
tool-loop in ``cmd_chat/operator/harness.py``. It talks to the SAME operator bridge
daemon over its unix control socket, exposing the bridge verbs as ``@tool`` functions
the model calls from generated Python. **No new side-effect surface:** every tool
sends the exact control-socket request the native harness and ``hh-bridge`` CLI
already send (``say``/``exec``/``write``/``get``), so grant-before-drive, the sandbox
blast radius, and containment are unchanged — the harness only chooses the verb.

Why standalone (not a ``cmd_chat`` module): smolagents+litellm live on the system
``python3``; the daemon runs under the hh venv (3.14). The two never share a process.
``cmd_chat.operator`` imports ``websockets`` (absent on system python3), so this script
inlines the tiny socket protocol + session-path logic instead of importing the package
— the socket is language/version-agnostic (newline-delimited JSON over AF_UNIX).

  hh-smol-operator.py --session <name> --objective "<obj>" --model qwen2.5-coder:7b
  hh-smol-operator.py --sock-path <path> --objective "<obj>" --model <m>
Env: OLLAMA_HOST, SMOLAGENT_CTX (8192), SMOLAGENT_MAX_STEPS (12).
Emits one JSON line {final,reason,turns,tokens,tool_calls,malformed_calls} like `operate`.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent  # the hack-house checkout root
_CAP_FILE = _TOOLS_DIR / "cmd_chat" / "operator" / "CAPABILITIES.md"

# Counters the tool wrappers bump so we can report `operate`-parity telemetry.
_TELEMETRY = {"tool_calls": 0}


# ── inlined control-socket protocol (mirror of cmd_chat/operator/cli_client.py) ──
class BridgeUnreachable(RuntimeError):
    pass


def _sock(sock_path: str, obj: dict, read_timeout: float = 35.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(read_timeout)
    try:
        try:
            s.connect(sock_path)
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise BridgeUnreachable(f"no bridge at {sock_path} ({e.__class__.__name__})") from e
        s.sendall((json.dumps(obj) + "\n").encode())
        buf = bytearray()
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
        line, _, _ = bytes(buf).partition(b"\n")
        if not line:
            raise BridgeUnreachable("bridge closed the connection without replying")
        return json.loads(line.decode())
    finally:
        s.close()


def _runtime_root() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    return Path(base) / "hh-bridge"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name.strip()) or "default"


def _sock_for(session: str) -> str:
    return str(_runtime_root() / _safe(session) / "control.sock")


_OBS_CAP = 4096


def _clip(s: str, cap: int = _OBS_CAP) -> str:
    s = s if isinstance(s, str) else str(s)
    return s if len(s) <= cap else s[:cap] + f"\n…[clipped {len(s) - cap} bytes]"


# Observed failure mode (fixerr fixture, qwen2.5-coder:7b, 2026-09-06): the system
# prompt already warns that these names only exist in the model's OWN code blocks,
# never inside a file that later runs standalone — but a 7B model ignored that under
# error-recovery pressure and wrote `read_file(...)` into a .py it then ran via
# exec_shell, hit NameError, and still called final_answer with a fabricated result.
# A prompt-only nudge isn't a reliable enough lever on its own, so back it with a
# structural guard: reject the write before the mistake ever reaches the sandbox.
_RESERVED_TOOL_NAMES = ("read_file", "write_file", "exec_shell")


def _leaked_tool_calls(content: str) -> list[str]:
    leaked = []
    for name in _RESERVED_TOOL_NAMES:
        if re.search(rf"\bdef\s+{name}\s*\(", content):
            continue  # the file defines its own function with this name — not a leak
        if re.search(rf"\b{name}\s*\(", content):
            leaked.append(name)
    return leaked


# ── the bridge-verb toolset (deliberately small — code-as-action composes for free) ──
def build_tools(sock_path: str, tool):
    """Return the @tool-decorated bridge verbs. Kept to the 4 a filesystem task
    needs (exec/write/read + say to report); code-as-action chains them in one
    program, so a lean surface beats a broad one (literature: 8B reliability drops
    past ~5 tools)."""

    @tool
    def exec_shell(command: str) -> str:
        """Run a shell command in the shared sandbox and return its output.

        On success (exit 0) returns the command's combined stdout+stderr with the
        trailing newline stripped — a clean value you can use directly (e.g. write
        it to a file or parse it). On a non-zero exit the output is prefixed with
        `[exit=N]` so failures are visible.

        Args:
            command: the shell command to run.
        """
        _TELEMETRY["tool_calls"] += 1
        r = _sock(sock_path, {"op": "exec", "cmd": command})
        if r.get("error"):
            return f"error: {r['error']}"
        out = r.get("output", "")
        out = out.rstrip("\n") if isinstance(out, str) else out
        rc = r.get("rc")
        return _clip(out if rc == 0 else f"[exit={rc}]\n{out}")

    @tool
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a file in the sandbox with exact content.

        Args:
            path: destination file path in the sandbox.
            content: the full file content to write.
        """
        _TELEMETRY["tool_calls"] += 1
        leaked = _leaked_tool_calls(content)
        if leaked:
            # Raise (not return) — a returned string can be silently discarded if the
            # model doesn't print/inspect it (confirmed: it didn't, on the original
            # fixerr failure). Raising halts this code block and forces the traceback
            # into the model's next observation, same as any other execution error.
            names = ", ".join(f"{n}()" for n in leaked)
            raise ValueError(
                f"write_file({path!r}) REJECTED, nothing written. {names} appear in this "
                f"content, but they only exist in YOUR code blocks, never inside a file that "
                f"runs standalone (e.g. via exec_shell(\"python3 {path}\")). Rewrite using "
                f"plain stdlib (e.g. Python's open()) instead, then call write_file again.")
        r = _sock(sock_path, {"op": "write", "path": path, "content": content})
        return (f"wrote {r.get('path')} ({r.get('bytes')} bytes)"
                if r.get("ok") else f"error: {r.get('error')}")

    @tool
    def read_file(path: str) -> str:
        """Read and return the contents of a file in the sandbox.

        Args:
            path: the file path in the sandbox to read.
        """
        _TELEMETRY["tool_calls"] += 1
        r = _sock(sock_path, {"op": "get", "path": path})
        if not r.get("ok"):
            return f"error: {r.get('error')}"
        try:
            return _clip(base64.b64decode(r.get("b64", "")).decode(errors="replace"))
        except Exception:  # noqa: BLE001
            return "[binary]"

    @tool
    def say(text: str) -> str:
        """Speak one line into the room to report progress or findings.

        Args:
            text: the chat line to send into the room.
        """
        _TELEMETRY["tool_calls"] += 1
        r = _sock(sock_path, {"op": "say", "text": text})
        return "(said)" if r.get("ok") else f"error: {r.get('error')}"

    return [exec_shell, write_file, read_file, say]


# Sections of CAPABILITIES.md written for the CLI-driving operator (native harness /
# Claude via the hh-operator skill) — real `hh-bridge <verb> --flag value` syntax that
# does NOT exist in the code-as-action tool surface (exec_shell/write_file/read_file/say
# as plain Python calls, no `up`/`screen`/`keys`/`watch`/`manifest`/`spawn`/`down`, no
# `--for`/`--in`/`--tail` flags). Confirmed root cause of a real failure (fixerr-fix
# regression check, 2026-09-21): a model literally wrote `watch --for '...' --in events`
# into its Python code block -> SyntaxError. Drop these sections for the smol prompt
# rather than caveat them — the tool docs right after already cover the real surface.
_CODEACT_DROP_SECTIONS = ("## Verbs", "## Recursion budget", "## Coordination is event-gated")


def _capabilities(for_codeact: bool = False) -> str:
    try:
        text = _CAP_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ("You are a hack-house operator driving a shared sandbox. Act only "
                "through the provided tools.")
    if not for_codeact:
        return text
    parts = re.split(r"(?m)^(?=## )", text)
    kept = [p for p in parts if not p.startswith(_CODEACT_DROP_SECTIONS)]
    return "".join(kept).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="smolagents code-as-action operator brain")
    ap.add_argument("--session", default=None, help="bridge session name (to locate the socket)")
    ap.add_argument("--sock-path", default=None, help="explicit control socket path")
    ap.add_argument("--objective", required=True)
    ap.add_argument("--model", default=os.environ.get("AI_MODEL", "qwen2.5-coder:7b"))
    ap.add_argument("--json-out", default=None,
                    help="write the result JSON here instead of stdout (keeps stdout for the "
                         "smolagents trace, so telemetry parses cleanly)")
    args = ap.parse_args()

    def _emit(obj: dict) -> None:
        line = json.dumps(obj)
        if args.json_out:
            Path(args.json_out).write_text(line + "\n", encoding="utf-8")
        else:
            print(line)

    sock_path = args.sock_path or (_sock_for(args.session) if args.session else None)
    if not sock_path:
        _emit({"final": "[need --session or --sock-path]", "reason": "config",
               "turns": 0, "tokens": 0, "tool_calls": 0, "malformed_calls": 0})
        return 2
    # Confirm the daemon is live + in-room before spending a model turn.
    try:
        st = _sock(sock_path, {"op": "status"}, 5.0)
    except BridgeUnreachable as e:
        _emit({"final": f"[bridge unreachable: {e}]", "reason": "bridge-unreachable",
               "turns": 0, "tokens": 0, "tool_calls": 0, "malformed_calls": 0})
        return 2
    if not st.get("connected"):
        _emit({"final": "[bridge up but not in-room]", "reason": "not-connected",
               "turns": 0, "tokens": 0, "tool_calls": 0, "malformed_calls": 0})
        return 2

    api_base = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    ctx = int(os.environ.get("SMOLAGENT_CTX", "8192"))
    max_steps = int(os.environ.get("SMOLAGENT_MAX_STEPS", "12"))
    # Token ceiling — a weak model (e.g. llama3.1:8b) can loop for 80k+ tokens on a
    # task a code model nails in ~11k, holding the shared GPU lease for minutes and
    # starving the live fleet. Cap cumulative tokens and stop cleanly. Capable models
    # (qwen2.5-coder ~11k on compose, ~3.5k on toolrun) never approach it.
    token_ceiling = int(os.environ.get("HH_SMOL_TOKEN_CEILING", "40000"))
    try:
        from smolagents import CodeAgent, LiteLLMModel, tool
        from smolagents.agents import AgentMaxStepsError, FinalAnswerStep
    except ImportError:
        _emit({"final": "[smolagents not installed]", "reason": "no-smolagents",
               "turns": 0, "tokens": 0, "tool_calls": 0, "malformed_calls": 0})
        return 3

    model = LiteLLMModel(model_id=f"ollama_chat/{args.model}", api_base=api_base,
                         num_ctx=ctx, temperature=0.0)
    # markdown fencing (```python ... ```) instead of smolagents' default <code></code>:
    # the model has seen the former millions of times in training and closes it far more
    # reliably. Root cause of a real failure (fixerr-fix regression check, 2026-09-21):
    # a model wrote "Final answer:\nDONE: ..." as bare prose instead of calling
    # final_answer() inside a <code> block, tripping the parser into a multi-turn retry
    # loop (3x the normal tokens/turns in one traced case, an outright fail in another).
    agent = CodeAgent(tools=build_tools(sock_path, tool), model=model,
                      additional_authorized_imports=[], code_block_tags="markdown")

    # Layer-1 capabilities (same portable contract the native harness/Claude skill load,
    # minus the CLI-verb sections that don't apply to code-as-action — see _capabilities)
    # + the objective + a code-as-action operating rule.
    task = (
        _capabilities(for_codeact=True)
        + "\n\nYou drive the sandbox by WRITING PYTHON that calls the provided tools "
          "(exec_shell/write_file/read_file/say) — chain the whole task in as few code "
          "blocks as possible. Do ALL arithmetic and logic in PYTHON (e.g. total = a+b+c; "
          "odds = sum(1 for x in vals if x % 2)) — do NOT shell out to calculators like bc "
          "or awk, and do NOT assume optional tools are installed. Use exec_shell only to run "
          "programs or inspect the system, and read_file/write_file for file contents. "
          "IMPORTANT: exec_shell/read_file/write_file/say are YOUR tools — they exist ONLY in the "
          "code blocks YOU write here, never inside a program you save to a file. When you write a "
          "script that the sandbox will run on its own (e.g. `python3 /root/x.py` or `./x.sh`), it "
          "must be SELF-CONTAINED using only what that runtime provides (in Python: `open()`, "
          "stdlib) — calling read_file/write_file/exec_shell from inside such a file will fail with "
          "NameError. If a command fails, read the error and change approach — never re-run the same "
          "failing block. You already have drive on a ready sandbox; act immediately. To finish, call "
          "final_answer(\"...\") as a normal function call INSIDE a code block — never write "
          "'Final answer:' as plain text outside a code block, that will not parse."
        + f"\n\nOBJECTIVE: {args.objective}"
    )

    def _tokens() -> int:
        try:
            return int(agent.monitor.get_total_token_counts().total_tokens)
        except Exception:  # noqa: BLE001
            return 0

    # Stream the steps so we can enforce the token ceiling between them (this version of
    # smolagents has no step-callback hook). max_steps is a run() arg, NOT an __init__ one
    # — passing it to the constructor silently does nothing, so enforce it here.
    reason, final = "done", ""
    try:
        for step in agent.run(task, stream=True, max_steps=max_steps):
            if isinstance(step, FinalAnswerStep):
                final = str(getattr(step, "output", None)
                            or getattr(step, "final_answer", None) or step)
            if _tokens() >= token_ceiling:
                reason = "token-ceiling"
                final = final or f"[stopped — token ceiling {token_ceiling} reached]"
                break
    except AgentMaxStepsError as e:  # noqa: BLE001
        reason, final = "max-steps", f"[stopped — max steps {max_steps} reached: {e}]"
    except Exception as e:  # noqa: BLE001
        reason, final = "error", f"[smol error: {type(e).__name__}: {e}]"

    tokens, turns = _tokens(), 0
    try:
        turns = len(agent.memory.steps)
    except Exception:  # noqa: BLE001
        pass

    _emit({"final": final, "reason": reason, "turns": turns, "tokens": tokens,
           "tool_calls": _TELEMETRY["tool_calls"], "malformed_calls": 0})
    return 0 if reason == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
