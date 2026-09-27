"""MCP attach mode — expose the hack-house operator verbs as MCP tools.

Phase 6 of docs/spec-multi-tenant-model-hosting.md. Any MCP-capable client
(Claude Desktop, Cursor, Cline, Continue, an Agents-SDK app) can drive a room
as a first-class instance with zero hack-house-specific glue: the client's own
model becomes the instance's brain.

Design:
  * Every tool maps to the existing operator CLI verb (`python -m cmd_chat.operator
    <verb> …`), so there is NO new side-effect surface — the same daemon + bridge
    the human/skill path uses.
  * `hh_join` is the handshake: it starts the operator daemon and records the
    launching member as the instance OWNER. No other tool resolves until join
    succeeds (spec §4).
  * Sandbox drive (`hh_keys`/`hh_exec`/`hh_write`) stays room-host-gated at the
    relay (spec §3 / verified at hh/src/app.rs SbxInput) — these tools are inert
    until the room host runs `/grant <name>`.
  * The MCP transport (FastMCP) is imported lazily in `serve()` so this module
    imports — and unit-tests — without the `mcp` SDK installed. The server is
    also gated behind an explicit enable flag (spec: do not enable by default).

The tool registry + join-gate + argv construction are pure and testable via an
injectable `runner`; the default runner shells out to the operator CLI.
"""
from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable

Runner = Callable[[list[str], str | None], str]


def _default_runner(argv: list[str], stdin: str | None = None) -> str:
    """Invoke `python -m cmd_chat.operator <argv>` and return combined output."""
    proc = subprocess.run(
        [sys.executable, "-m", "cmd_chat.operator", *argv],
        input=stdin, capture_output=True, text=True, check=False,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return out.strip() or f"(exit {proc.returncode})"


class NotJoinedError(RuntimeError):
    """A drive/read/say tool was called before `hh_join` succeeded."""


class HackHouseMCP:
    """Session state + tool handlers for one MCP-hosted operator instance.

    One MCP client session drives one room seat. `owner` is the member named at
    join; the instance's query ACL (managed in the room, spec §2) is enforced by
    the room, not here. This object just brokers verbs to the operator daemon.
    """

    # Sandbox-drive verbs — inert until the room host grants drive.
    DRIVE_TOOLS = ("hh_keys", "hh_exec", "hh_write")

    def __init__(self, runner: Runner | None = None):
        self._run = runner or _default_runner
        self.joined = False
        self.owner: str | None = None
        self.session: str | None = None
        self.room: tuple[str, int, str] | None = None

    # -- handshake ---------------------------------------------------------- #
    def hh_join(self, host: str, port: int, name: str, password: str,
                owner: str, no_tls: bool = True, insecure: bool = False) -> str:
        """Start the operator daemon and register this instance. MUST be the
        first tool called; `owner` is the room member who launched this client."""
        self.session = f"mcp-{name}"
        argv = ["up", host, str(port), name, "--session", self.session]
        if password:
            argv += ["--password", password]
        if no_tls:
            argv += ["--no-tls"]
        if insecure:
            argv += ["--insecure"]
        out = self._run(argv, None)
        self.joined = True
        self.owner = owner
        self.room = (host, port, name)
        return f"joined {host}:{port} as {name} (owner {owner}). {out}"

    def _require_join(self, tool: str) -> None:
        if not self.joined:
            raise NotJoinedError(f"{tool}: call hh_join(host, port, name, password, owner) first")

    def _session_args(self) -> list[str]:
        return ["--session", self.session] if self.session else []

    # -- room + sandbox verbs ---------------------------------------------- #
    def hh_say(self, text: str) -> str:
        self._require_join("hh_say")
        return self._run(["say", text, *self._session_args()], None)

    def hh_read(self, timeout: int = 30, wait: bool = True) -> str:
        self._require_join("hh_read")
        argv = ["read", *self._session_args()]
        if wait:
            argv += ["--wait", "--timeout", str(timeout)]
        return self._run(argv, None)

    def hh_screen(self, tail: int | None = None) -> str:
        self._require_join("hh_screen")
        argv = ["screen", *self._session_args()]
        if tail is not None:
            argv += ["--tail", str(tail)]
        return self._run(argv, None)

    def hh_keys(self, text: str, enter: bool = False) -> str:
        self._require_join("hh_keys")
        argv = ["keys", text]
        if enter:
            argv += ["enter"]
        return self._run([*argv, *self._session_args()], None)

    def hh_exec(self, command: str) -> str:
        self._require_join("hh_exec")
        return self._run(["exec", command, *self._session_args()], None)

    def hh_write(self, path: str, content: str) -> str:
        self._require_join("hh_write")
        return self._run(["write", path, "--text", content, *self._session_args()], None)

    def hh_get(self, path: str) -> str:
        self._require_join("hh_get")
        return self._run(["get", path, *self._session_args()], None)

    def hh_watch(self, pattern: str, in_: str = "screen", timeout: int = 30) -> str:
        self._require_join("hh_watch")
        return self._run(
            ["watch", "--for", pattern, "--in", in_, "--timeout", str(timeout),
             *self._session_args()], None)

    def hh_manifest(self, root: str, name: str, purpose: str = "",
                    objective: str = "") -> str:
        self._require_join("hh_manifest")
        argv = ["manifest", "push", "--root", root, "--name", name]
        if purpose:
            argv += ["--purpose", purpose]
        if objective:
            argv += ["--objective", objective]
        return self._run([*argv, *self._session_args()], None)

    def hh_spawn(self, objective: str, go: bool = True) -> str:
        self._require_join("hh_spawn")
        argv = ["spawn", objective]
        if go:
            argv += ["--go"]
        return self._run([*argv, *self._session_args()], None)

    # -- introspection ------------------------------------------------------ #
    def tool_specs(self) -> list[dict]:
        """Name + one-line description for each exposed tool, in call order.
        The single source of truth for what an MCP client sees."""
        return [
            {"name": "hh_join", "handler": self.hh_join,
             "description": "Join a hack-house room and register this instance "
                            "(owner = the launching member). Call this first."},
            {"name": "hh_say", "handler": self.hh_say,
             "description": "Speak a line into the room."},
            {"name": "hh_read", "handler": self.hh_read,
             "description": "Long-poll for new room activity (chat, joins, grants)."},
            {"name": "hh_screen", "handler": self.hh_screen,
             "description": "Print the shared sandbox terminal buffer."},
            {"name": "hh_keys", "handler": self.hh_keys,
             "description": "Type into the shared PTY. Requires the room host to "
                            "/grant this instance drive first."},
            {"name": "hh_exec", "handler": self.hh_exec,
             "description": "Run a shell command in the sandbox (needs drive)."},
            {"name": "hh_write", "handler": self.hh_write,
             "description": "Write a file in the sandbox (needs drive)."},
            {"name": "hh_get", "handler": self.hh_get,
             "description": "Read a file out of the sandbox."},
            {"name": "hh_watch", "handler": self.hh_watch,
             "description": "Block until a regex fires in the screen or events."},
            {"name": "hh_manifest", "handler": self.hh_manifest,
             "description": "Stamp the sandbox's .hh-agent handoff record."},
            {"name": "hh_spawn", "handler": self.hh_spawn,
             "description": "Spawn a nested operator (budgeted)."},
        ]


def serve(transport: str = "stdio", runner: Runner | None = None) -> None:
    """Run the MCP server over `transport` (stdio | sse). Imports FastMCP lazily
    so this module loads without the `mcp` SDK; raises a clear error if serving
    is requested without it installed."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover - depends on optional dep
        raise SystemExit(
            "MCP attach mode needs the `mcp` SDK: pip install mcp\n"
            f"(import failed: {e})"
        )
    hh = HackHouseMCP(runner=runner)
    server = FastMCP("hack-house")
    for spec in hh.tool_specs():  # pragma: no cover - exercised only under a live client
        server.add_tool(spec["handler"], name=spec["name"], description=spec["description"])
    server.run(transport=transport)


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - thin CLI
    import argparse
    import os

    ap = argparse.ArgumentParser(
        prog="cmd_chat.operator.mcp_server",
        description="Expose hack-house operator verbs as an MCP server (spec §6). "
                    "Disabled unless explicitly enabled.")
    ap.add_argument("--enable", action="store_true",
                    help="required to actually serve (also honors HH_MCP_ENABLE=1)")
    ap.add_argument("--transport", choices=("stdio", "sse"), default="stdio")
    args = ap.parse_args(argv)
    if not (args.enable or os.environ.get("HH_MCP_ENABLE") == "1"):
        raise SystemExit("MCP attach mode is off by default — pass --enable "
                         "(or set HH_MCP_ENABLE=1) to serve.")
    serve(transport=args.transport)


if __name__ == "__main__":  # pragma: no cover
    main()
