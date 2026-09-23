"""Harness-mode operator (Phase 2) — run *any* function-calling model as a
hack-house operator.

The CLI-subprocess operator (``bootstrap`` runner registry) spawns a vendor
agent CLI and lets *its* own loop drive the room. This module is the other
first-class brain: it runs the native tool-calling loop ourselves, against the
shared Provider core (``cmd_chat.ai``), with the **operator toolset** wired to
the bridge daemon's control verbs. So a raw Ollama / OpenAI-compatible model —
no CLI install, no creds carry — becomes a first-class operator.

It is deliberately a *driver*, not a new side-effect surface: every tool handler
just sends the same control-socket request the `hh-bridge` CLI already sends
(``say``/``exec``/``write``/``get``/``keys``/``screen``/``watch``/``manifest``/
``spawn``). The bridge keeps enforcing grant-before-drive, the sandbox blast
radius, and the recursion budget — the harness only chooses *which* verb to call.

The read→think→act loop owns the **read**: it polls room events between turns and
feeds them to the model as observations, so the model spends its turns deciding
and acting rather than remembering to poll. Capabilities (Layer-1) come straight
from ``CAPABILITIES.md`` via :func:`bootstrap.load_capabilities` — the same
portable contract the Claude skill loads — so harness and CLI operators share one
source of truth.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from cmd_chat.ai.providers import ToolsUnsupported

from . import bootstrap as boot
from .cli_client import request as _socket_request

# The operator toolset — each maps 1:1 to a bridge control verb the daemon
# already implements. Schema is the Ollama/OpenAI `tools` shape (same as the chat
# agent's NATIVE_TOOLS), so any provider that does function calling can drive it.
OPERATOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "say",
            "description": "Speak one line into the room. Report what you are doing "
                           "and what you found — the room is the coordination bus.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The chat line to send."},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exec",
            "description": "Run a shell command in the shared sandbox out-of-band "
                           "(does not disturb the live terminal). Returns combined "
                           "stdout+stderr and the exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the sandbox with exact content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Destination file path."},
                    "content": {"type": "string", "description": "Full file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file",
            "description": "Read and return the contents of a file in the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "keys",
            "description": "Inject keystrokes into the room's shared live terminal "
                           "(drive the PTY). Use for interactive programs; prefer "
                           "exec for one-shot commands.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Literal text to type."},
                    "enter": {"type": "boolean",
                              "description": "Press Enter after the text (default true)."},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screen",
            "description": "Print the relayed shared-sandbox terminal buffer "
                           "(what everyone watching the PTY sees).",
            "parameters": {
                "type": "object",
                "properties": {
                    "tail": {"type": "integer",
                             "description": "Last N bytes only (optional)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "watch",
            "description": "Block until a regex fires in room events or the terminal "
                           "screen — how you wait on a teammate's report or a build "
                           "result instead of sleeping.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex to match."},
                    "source": {"type": "string", "enum": ["events", "screen"],
                               "description": "Where to match (default: events)."},
                    "timeout": {"type": "number",
                                "description": "Give up after this many seconds."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manifest",
            "description": "Stamp the sandbox's .hh-agent handoff record so work and "
                           "memory hand off to the next agent or human.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["push", "update"],
                               "description": "push a fresh record or update the state."},
                    "status": {"type": "string", "description": "Current status line."},
                    "progress": {"type": "string", "description": "Progress note."},
                    "done": {"type": "string", "description": "A completed item."},
                    "todo": {"type": "string", "description": "A next item."},
                },
                "required": ["action"],
            },
        },
    },
]

# Tool names that drive the sandbox/room mutably — used only for messaging; the
# bridge is the real authority on grant-before-drive.
_TOOL_NAMES = {(t["function"]["name"]) for t in OPERATOR_TOOLS}

# DONE marker (same convention as the chat native loop): a text-only turn whose
# first line starts with this means the model considers the objective met.
_DONE_RE = re.compile(r"^\s*DONE:", re.I)

DEFAULT_MAX_TURNS = 12
DEFAULT_TOKEN_CEILING = 20000   # cumulative prompt+completion tokens (hard stop)
DEFAULT_MAX_NUDGES = 2
_OBS_CAP = 4096                 # bytes of any single observation fed back to the model

HARNESS_SYSTEM_TAIL = (
    "\n\nYou act ONLY by calling the provided tools (say/exec/write_file/get_file/"
    "keys/screen/watch/manifest) — never by describing an action in prose. Work in "
    "small steps and read each tool result before the next call. You may read and "
    "`say` immediately, but exec/write_file/get_file/keys only work once the room "
    "owner has granted you drive (the bridge will return an error until then — wait "
    "and watch for the grant). When the objective is fully met, reply with a single "
    "line starting 'DONE:' and a one-sentence summary, and do NOT call another tool."
)


def compose_system(objective: str, *, stop: list[str] | None = None,
                   budget: "boot.Budget | None" = None,
                   role_overlay: str | None = None) -> str:
    """The harness operator's system prompt: portable Layer-1 capabilities +
    objective + stop conditions + (optional) role overlay + recursion budget +
    the tool-only operating rule. One screen of capabilities, behaviour overlaid —
    no Claude-isms (the 'use the skill' line lives only in the Claude path)."""
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
            f"fanout={budget.fanout}, cost≈${budget.cost_usd:.2f}. Spawn at most "
            "`fanout` children and only while depth>0; pass each a decremented budget.")
    return "\n\n".join(parts) + HARNESS_SYSTEM_TAIL


def _clip(s: str, cap: int = _OBS_CAP) -> str:
    if len(s) <= cap:
        return s
    return s[:cap] + f"\n…[clipped {len(s) - cap} bytes]"


def _format_events(events: list[dict], me: str) -> str:
    """Turn raw bridge inbox events into a compact observation block the model
    reads as room state. Drops our own echoes and high-volume noise."""
    lines: list[str] = []
    for ev in events:
        kind = ev.get("kind")
        if kind == "message":
            who = ev.get("from", "?")
            if who == me:
                continue
            mark = " (@you)" if ev.get("addressed") else ""
            lines.append(f"[{who}{mark}] {ev.get('text', '')}")
        elif kind == "acl":
            state = "GRANTED drive" if ev.get("granted") else "drive NOT granted"
            lines.append(f"[room] you are now {state}.")
        elif kind == "sandbox":
            lines.append(f"[room] sandbox {ev.get('state')} "
                         f"({ev.get('engine') or '—'}/{ev.get('name') or '—'}).")
        elif kind == "roster":
            lines.append(f"[room] members: {', '.join(ev.get('users', []))}.")
    return "\n".join(lines)


@dataclass
class HarnessResult:
    final: str = ""
    turns: int = 0
    tokens: int = 0
    reason: str = ""          # why the loop ended
    tool_calls: int = 0


@dataclass
class OperatorHarness:
    """Drives a room with a function-calling Provider over the bridge control
    socket. Side-effect-free to construct; :meth:`run` does the work. ``request_fn``
    is injectable so unit tests can drive the whole loop with no daemon/network."""

    provider: object
    sock_path: str
    objective: str
    stop: list[str] | None = None
    budget: "boot.Budget | None" = None
    role_overlay: str | None = None
    max_turns: int = DEFAULT_MAX_TURNS
    token_ceiling: int = DEFAULT_TOKEN_CEILING
    max_nudges: int = DEFAULT_MAX_NUDGES
    me: str = "operator"
    request_fn: Callable[[str, dict, float], dict] | None = None
    poll_timeout: float = 20.0

    _cursor: int = field(default=0, init=False)

    # ── control-socket plumbing ──────────────────────────────────────────
    def _call(self, obj: dict, read_timeout: float = 35.0) -> dict:
        fn = self.request_fn or _socket_request
        return fn(self.sock_path, obj, read_timeout)

    def _drain_events(self, *, wait: bool, timeout: float) -> list[dict]:
        resp = self._call({"op": "read", "since": self._cursor,
                           "wait": wait, "timeout": timeout},
                          read_timeout=timeout + 5.0)
        events = resp.get("events", []) or []
        if events:
            self._cursor = events[-1].get("seq", self._cursor)
        elif resp.get("seq") is not None:
            self._cursor = max(self._cursor, int(resp["seq"]))
        return events

    # ── tool dispatch (each maps to a bridge verb) ───────────────────────
    def _run_tool(self, name: str, args: dict) -> str:
        if name == "say":
            r = self._call({"op": "say", "text": str(args.get("text", ""))})
            return "(said)" if r.get("ok") else f"error: {r.get('error')}"
        if name == "exec":
            r = self._call({"op": "exec", "cmd": str(args.get("command", ""))})
            out = _clip(r.get("output", ""))
            return f"exit={r.get('rc')}\n{out}" if not r.get("error") else f"error: {r['error']}"
        if name == "write_file":
            r = self._call({"op": "write", "path": str(args.get("path", "")),
                            "content": args.get("content", "")})
            return (f"wrote {r.get('path')} ({r.get('bytes')} bytes)"
                    if r.get("ok") else f"error: {r.get('error')}")
        if name == "get_file":
            import base64
            r = self._call({"op": "get", "path": str(args.get("path", ""))})
            if not r.get("ok"):
                return f"error: {r.get('error')}"
            try:
                raw = base64.b64decode(r.get("b64", "")).decode(errors="replace")
            except Exception:  # noqa: BLE001
                raw = "[binary]"
            return _clip(raw)
        if name == "keys":
            toks = [f"text:{args.get('text', '')}"]
            if args.get("enter", True):
                toks.append("enter")
            r = self._call({"op": "keys", "keys": toks})
            return "(keys sent)" if r.get("ok") else f"error: {r.get('error')}"
        if name == "screen":
            r = self._call({"op": "screen", "tail": args.get("tail"), "ansi": False})
            return _clip(r.get("text", "")) if r.get("ok") else f"error: {r.get('error')}"
        if name == "watch":
            timeout = float(args.get("timeout", 30.0))
            r = self._call({"op": "watch", "for": args.get("pattern"),
                            "in": args.get("source", "events"), "idle": None,
                            "timeout": timeout}, read_timeout=timeout + 5.0)
            return _clip(json.dumps(r))
        if name == "manifest":
            req = {"op": "manifest", "action": args.get("action", "update")}
            for k in ("status", "progress"):
                if args.get(k) is not None:
                    req[k] = args[k]
            for k in ("done", "todo"):
                if args.get(k):
                    req[k] = [args[k]]
            r = self._call(req)
            return _clip(json.dumps(r))
        return f"error: unknown tool '{name}'"

    @staticmethod
    def _calls_to_wire(calls: list[dict]) -> list[dict]:
        """Round-trip the model's tool calls back into the wire `tool_calls` shape
        so the next turn sees its own request alongside the result."""
        return [{"type": "function",
                 "function": {"name": c.get("name", ""),
                              "arguments": c.get("arguments", {})}}
                for c in calls]

    # ── the loop ──────────────────────────────────────────────────────────
    def run(self) -> HarnessResult:
        cwt = getattr(self.provider, "complete_with_tools", None)
        if cwt is None:
            return HarnessResult(
                reason="provider-no-tools",
                final="[refused — this provider cannot do function calling; the "
                      "harness operator needs a tool-capable model]")
        system = compose_system(self.objective, stop=self.stop, budget=self.budget,
                                role_overlay=self.role_overlay)

        # Seed: snapshot current room state (full ring) as the first observation.
        try:
            seed = self._drain_events(wait=False, timeout=0.0)
        except Exception as e:  # noqa: BLE001
            return HarnessResult(reason="bridge-unreachable",
                                 final=f"[bridge unreachable: {e}]")
        obs = _format_events(seed, self.me) or "(room is quiet)"
        messages: list[dict] = [
            {"role": "user",
             "content": f"You have joined the room. Current state:\n{obs}\n\n"
                        f"Begin working toward the objective."}
        ]

        res = HarnessResult()
        nudges = 0
        while res.turns < self.max_turns + self.max_nudges:
            res.turns += 1
            try:
                text, calls, usage = cwt(system, messages, OPERATOR_TOOLS)
            except ToolsUnsupported as e:
                return HarnessResult(
                    turns=res.turns, tokens=res.tokens, reason="tools-unsupported",
                    final=f"[refused — model rejected tool calling: {e}]")
            except Exception as e:  # noqa: BLE001
                res.reason, res.final = "provider-error", f"[ai error: {e}]"
                return res

            res.tokens += int((usage or {}).get("prompt_eval_count", 0) or 0)
            res.tokens += int((usage or {}).get("eval_count", 0) or 0)

            if not calls:
                if _DONE_RE.match(text or ""):
                    res.final = re.sub(_DONE_RE, "", text, count=1).strip()
                    res.reason = "done"
                    return res
                if nudges >= self.max_nudges:
                    res.final = (text or "").strip() or "[stopped — model stalled]"
                    res.reason = "stalled"
                    return res
                nudges += 1
                messages.append({"role": "assistant", "content": text or ""})
                # Poll for any room activity to give the nudge fresh context.
                fresh = []
                try:
                    fresh = self._drain_events(wait=True, timeout=self.poll_timeout)
                except Exception:  # noqa: BLE001
                    pass
                nudge_obs = _format_events(fresh, self.me)
                messages.append({
                    "role": "user",
                    "content": (("Room update:\n" + nudge_obs + "\n\n") if nudge_obs else "")
                    + "If the objective is met, reply with a single 'DONE: <summary>' "
                      "line. Otherwise call the next tool to make progress."})
                continue

            if res.tokens >= self.token_ceiling:
                res.final = ("[stopped — per-agent token ceiling "
                             f"{self.token_ceiling} reached]")
                res.reason = "token-ceiling"
                return res

            # Echo the assistant's tool-call turn, then run each call and feed the
            # results back as tool messages so the next turn sees the outcome.
            messages.append({"role": "assistant", "content": text or "",
                             "tool_calls": self._calls_to_wire(calls)})
            for c in calls:
                name, cargs = c.get("name", ""), c.get("arguments") or {}
                result = self._run_tool(name, cargs)
                res.tool_calls += 1
                messages.append({"role": "tool", "content": _clip(result)})
            # After acting, fold any new room events into the next observation.
            try:
                fresh = self._drain_events(wait=False, timeout=0.0)
            except Exception:  # noqa: BLE001
                fresh = []
            obs = _format_events(fresh, self.me)
            if obs:
                messages.append({"role": "user", "content": "Room update:\n" + obs})

        res.final = res.final or "[stopped — reached turn cap]"
        res.reason = res.reason or "turn-cap"
        return res
