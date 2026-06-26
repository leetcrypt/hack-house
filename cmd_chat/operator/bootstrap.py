"""Bootstrap + recursive-spawn primitives for the operator bridge (Phase 5).

Goal: from inside one room (or a sandbox/VM the operator owns), stand up a
*nested* Claude Code session that runs the `hh-operator` skill against another
room — a tree of operators. Doing that safely needs four things, all here:

1. **Detect** the target system (OS / arch / package manager / what's already
   installed) so install is a decision, not a guess.
2. **Install** Claude Code via a *known* package name (npm `@anthropic-ai/
   claude-code`) or an explicitly-configured installer — never a guessed URL.
3. **Authenticate** by carrying the parent's own credentials into the child —
   but **gated off by default**: a child auths itself unless the operator
   explicitly opts in (`allow_creds`). We never exfiltrate creds to a system we
   don't own, and never enable this implicitly.
4. **Budget** the recursion: hard **depth** and **fan-out** caps plus a soft
   **cost** ceiling, decremented on every descent, refusing at zero. This is the
   guardrail that stops a runaway tree.

This module is pure/decision-making: it builds argv, plans, and budgets. It does
not itself run network installs or launch detached Claude sessions — the bridge
does that behind explicit flags, so unit tests can exercise all the logic with
no side effects.
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

# Documented install package — NOT a guessed URL. Override the whole install
# command with $HH_CLAUDE_INSTALL when a site has its own channel.
CLAUDE_NPM_PACKAGE = "@anthropic-ai/claude-code"


class BudgetExhausted(RuntimeError):
    """A spawn was refused because a recursion budget hit zero."""


@dataclass(frozen=True)
class Budget:
    """Hard caps on a recursion tree.

    - ``depth``  — how many more levels may be spawned below here (0 = leaf).
    - ``fanout`` — how many children *this* node may still spawn.
    - ``cost_usd`` — soft ceiling carried down and surfaced to the child as a
      directive (Claude Code has no native $ cap, so this is advisory; depth and
      fanout are the hard stops).
    """
    depth: int = 1
    fanout: int = 2
    cost_usd: float = 5.0

    def can_spawn(self) -> bool:
        return self.depth > 0 and self.fanout > 0

    def descend(self) -> "Budget":
        """Consume one child slot at this level and return the budget the child
        inherits (one level shallower, its own fresh fanout). Raises when this
        node can't spawn any more."""
        if self.depth <= 0:
            raise BudgetExhausted("depth budget exhausted — cannot nest deeper")
        if self.fanout <= 0:
            raise BudgetExhausted("fan-out budget exhausted at this level")
        # The child gets depth-1 and the same fanout/cost to share among its own
        # children; *this* node records one fewer remaining child via `spent`.
        return Budget(depth=self.depth - 1, fanout=self.fanout, cost_usd=self.cost_usd)

    def spent(self) -> "Budget":
        """Return this node's budget after using one of its child slots."""
        return replace(self, fanout=self.fanout - 1)

    def as_dict(self) -> dict:
        return {"depth": self.depth, "fanout": self.fanout, "cost_usd": self.cost_usd}


# ── detection ───────────────────────────────────────────────────────────────
def _distro_id() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("ID="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def _pkg_manager() -> str | None:
    for mgr in ("apt-get", "dnf", "yum", "apk", "pacman", "brew"):
        if shutil.which(mgr):
            return mgr
    return None


def detect_system() -> dict:
    """Snapshot what the bootstrap needs to decide install strategy. Pure read —
    no mutation, safe to call anywhere (including inside a sandbox via exec)."""
    return {
        "os": platform.system().lower(),          # linux / darwin
        "arch": platform.machine(),               # x86_64 / aarch64 …
        "distro": _distro_id(),                    # ubuntu / debian / alpine …
        "pkg_manager": _pkg_manager(),
        "has_node": shutil.which("node") is not None,
        "has_npm": shutil.which("npm") is not None,
        "claude": shutil.which("claude"),          # path or None
    }


def claude_present() -> bool:
    return shutil.which("claude") is not None


def install_plan(sysinfo: dict | None = None) -> list[list[str]]:
    """Ordered candidate commands to get the `claude` CLI onto the system.

    Honours $HH_CLAUDE_INSTALL (a full shell command) first so a site can pin
    its own channel. Otherwise prefers the documented npm package; if npm is
    missing, prefix the distro's node install. We never fabricate a download
    URL — if none of these apply the caller surfaces "install claude manually"."""
    override = os.environ.get("HH_CLAUDE_INSTALL")
    if override:
        return [["sh", "-c", override]]
    info = sysinfo or detect_system()
    plans: list[list[str]] = []
    if info.get("has_npm"):
        plans.append(["npm", "install", "-g", CLAUDE_NPM_PACKAGE])
    else:
        mgr = info.get("pkg_manager")
        node_install = {
            "apt-get": ["apt-get", "install", "-y", "nodejs", "npm"],
            "dnf": ["dnf", "install", "-y", "nodejs", "npm"],
            "yum": ["yum", "install", "-y", "nodejs", "npm"],
            "apk": ["apk", "add", "nodejs", "npm"],
            "pacman": ["pacman", "-Sy", "--noconfirm", "nodejs", "npm"],
            "brew": ["brew", "install", "node"],
        }.get(mgr)
        if node_install:
            plans.append(node_install)
            plans.append(["npm", "install", "-g", CLAUDE_NPM_PACKAGE])
    return plans


# ── credentials (gated) ─────────────────────────────────────────────────────
def creds_source() -> Path | None:
    """The parent session's own OAuth/API credentials file, if present."""
    p = Path.home() / ".claude" / ".credentials.json"
    return p if p.exists() else None


def plan_creds(dest_home: str | os.PathLike, *, allow: bool) -> dict:
    """Decide whether to carry credentials into a child's config dir.

    Default (``allow=False``) → carry nothing; the child must authenticate
    itself (`claude` interactive login or its own $ANTHROPIC_API_KEY). Only when
    the operator *explicitly* opts in do we stage a copy of our own creds — and
    only into a path we were handed. This function plans; the bridge performs the
    copy so the side effect stays behind one audited flag."""
    if not allow:
        return {"staged": False, "reason": "creds gating on — child authenticates itself"}
    src = creds_source()
    if src is None:
        return {"staged": False, "reason": "no parent credentials to carry"}
    dest = Path(dest_home) / ".claude" / ".credentials.json"
    return {"staged": True, "src": str(src), "dest": str(dest)}


# ── directive + run argv ────────────────────────────────────────────────────
def compose_directive(objective: str, room: dict, budget: Budget,
                      stop: list[str] | None = None) -> str:
    """The prompt the nested Claude runs under `claude -p`. Tells it to use the
    hh-operator skill, where to join, what to achieve, when to stop, and the
    recursion budget it inherits (so it self-limits its own spawns)."""
    host, port = room.get("host", "?"), room.get("port", "?")
    name = room.get("name", "operator")
    stop = stop or ["objective met", "owner says stop/leave", "idle past remit"]
    lines = [
        "Use the hh-operator skill. You are an autonomous operator joining a "
        "hack-house room.",
        f"Join: `hh-bridge up {host} {port} {name}` "
        f"(password via room config; add --no-tls for plain ws).",
        f"Objective: {objective}",
        "Stop conditions (leave with `hh-bridge down` when any is met): "
        + "; ".join(stop) + ".",
        f"Recursion budget you inherit: depth={budget.depth}, "
        f"fanout={budget.fanout}, cost≈${budget.cost_usd:.2f}. "
        "You may spawn at most `fanout` child operators and only while depth>0; "
        "pass each child a decremented budget. Stay within the cost ceiling.",
        "Drive sandboxes only after the owner grants you. Never touch a system "
        "you weren't asked to.",
    ]
    return "\n".join(lines)


def build_run_argv(directive: str, *, skip_permissions: bool = False,
                   config_dir: str | None = None,
                   claude_bin: str = "claude") -> list[str]:
    """argv to launch a headless nested Claude session. `skip_permissions` maps
    to Claude Code's unattended flag (only for trusted, sandboxed autonomy);
    `config_dir` points the child at an isolated $CLAUDE_CONFIG_DIR so its creds
    and state don't collide with the parent."""
    argv = [claude_bin, "-p", directive]
    if skip_permissions:
        argv.append("--dangerously-skip-permissions")
    return argv


def child_env(config_dir: str | None = None) -> dict:
    """Environment for the child process. Isolates its config dir when given so
    a nested session's auth/state is its own."""
    env = dict(os.environ)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env
