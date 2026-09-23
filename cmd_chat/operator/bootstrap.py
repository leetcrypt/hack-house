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
import shlex
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


# ── runner registry ──────────────────────────────────────────────────────────
# A *runner* captures everything that varies by which agent CLI we spawn as the
# nested operator. The Claude path stays the default and byte-identical; other
# CLIs are first-class but **best-effort** — their exact flags/creds paths are
# meant to be pinned per-site via the override env vars (or routed through the
# generic ``cmd`` runner). Only ``claude`` is validated end-to-end today.
@dataclass(frozen=True)
class Runner:
    name: str
    bin: str                                    # executable to locate / invoke
    prompt_flags: tuple[str, ...] = ("-p",)     # flag(s) that precede the directive
    unattended_flags: tuple[str, ...] = ()      # appended when skip_permissions
    npm_package: str | None = None              # for npm-installable CLIs
    install_env: str | None = None              # env var holding a full install cmd
    cmd_env: str | None = None                  # generic template env var (the 'cmd' runner)
    creds_path: str | None = None               # ~-relative source credential file
    config_env: str | None = None               # env var for an isolated config dir

    def present(self) -> bool:
        return bool(self.bin) and shutil.which(self.bin) is not None

    def argv(self, directive: str, *, skip_permissions: bool = False) -> list[str]:
        """Launch argv for this runner. The generic ``cmd`` runner reads a full
        command template from ``$cmd_env`` and substitutes ``{directive}``;
        everything else is ``[bin, *prompt_flags, directive] (+ unattended)``."""
        if self.cmd_env:
            tmpl = os.environ.get(self.cmd_env)
            if not tmpl:
                raise ValueError(
                    f"runner '{self.name}' needs ${self.cmd_env} set to a command "
                    "template (use {directive} as the placeholder)"
                )
            if "{directive}" in tmpl:
                return [p.replace("{directive}", directive) for p in shlex.split(tmpl)]
            return [*shlex.split(tmpl), directive]
        a = [self.bin, *self.prompt_flags, directive]
        if skip_permissions:
            a += list(self.unattended_flags)
        return a

    def creds_source(self) -> Path | None:
        if not self.creds_path:
            return None
        p = Path(self.creds_path).expanduser()
        return p if p.exists() else None


RUNNERS: dict[str, Runner] = {
    "claude": Runner(
        "claude", "claude", ("-p",), ("--dangerously-skip-permissions",),
        npm_package=CLAUDE_NPM_PACKAGE, install_env="HH_CLAUDE_INSTALL",
        creds_path="~/.claude/.credentials.json", config_env="CLAUDE_CONFIG_DIR"),
    # Best-effort defaults — override flags/install/creds via the env vars below
    # or use the generic `cmd` runner if a CLI's interface differs.
    "codex": Runner(
        "codex", "codex", ("exec",), ("--dangerously-bypass-approvals-and-sandbox",),
        npm_package="@openai/codex", install_env="HH_CODEX_INSTALL",
        creds_path="~/.codex/auth.json", config_env="CODEX_HOME"),
    "gemini": Runner(
        "gemini", "gemini", ("-p",), ("--yolo",),
        npm_package="@google/gemini-cli", install_env="HH_GEMINI_INSTALL",
        creds_path="~/.gemini/oauth_creds.json", config_env="GEMINI_SYSTEM_MD"),
    # Fully user-controlled escape hatch: $HH_OPERATOR_CMD is the launch template.
    "cmd": Runner("cmd", "", cmd_env="HH_OPERATOR_CMD", install_env="HH_OPERATOR_INSTALL"),
}


def get_runner(runner: "Runner | str | None" = None) -> Runner:
    """Resolve a runner by name (default ``claude``). Pass-through if already a
    :class:`Runner`. Raises ``ValueError`` on an unknown name."""
    if isinstance(runner, Runner):
        return runner
    r = RUNNERS.get(runner or "claude")
    if r is None:
        raise ValueError(
            f"unknown runner '{runner}' (known: {', '.join(RUNNERS)})"
        )
    return r


def runner_present(runner: "Runner | str | None" = None) -> bool:
    return get_runner(runner).present()


def install_plan(sysinfo: dict | None = None, *,
                 runner: "Runner | str | None" = None) -> list[list[str]]:
    """Ordered candidate commands to get the runner's CLI onto the system.

    Honours the runner's install-override env (``$HH_CLAUDE_INSTALL`` for claude)
    first so a site can pin its own channel. Otherwise prefers the runner's
    documented npm package; if npm is missing, prefix the distro's node install.
    We never fabricate a download URL — a runner with no npm package (e.g. the
    generic ``cmd`` runner) returns ``[]`` and the caller surfaces "install
    manually"."""
    r = get_runner(runner)
    override = os.environ.get(r.install_env) if r.install_env else None
    if override:
        return [["sh", "-c", override]]
    if not r.npm_package:
        return []
    info = sysinfo or detect_system()
    plans: list[list[str]] = []
    if info.get("has_npm"):
        plans.append(["npm", "install", "-g", r.npm_package])
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
            plans.append(["npm", "install", "-g", r.npm_package])
    return plans


# ── credentials (gated) ─────────────────────────────────────────────────────
def creds_source(runner: "Runner | str | None" = None) -> Path | None:
    """The parent session's own credentials file for the runner, if present
    (defaults to Claude's ``~/.claude/.credentials.json``)."""
    return get_runner(runner).creds_source()


def plan_creds(dest_home: str | os.PathLike, *, allow: bool,
               runner: "Runner | str | None" = None) -> dict:
    """Decide whether to carry credentials into a child's config dir.

    Default (``allow=False``) → carry nothing; the child must authenticate
    itself (its own interactive login or API key). Only when the operator
    *explicitly* opts in do we stage a copy of our own creds — and only into a
    path we were handed. The destination mirrors the runner's own creds layout
    under ``dest_home``. This function plans; the bridge performs the copy so the
    side effect stays behind one audited flag."""
    if not allow:
        return {"staged": False, "reason": "creds gating on — child authenticates itself"}
    r = get_runner(runner)
    src = r.creds_source()
    if src is None:
        return {"staged": False, "reason": "no parent credentials to carry"}
    try:
        rel = Path(r.creds_path).expanduser().relative_to(Path.home())
    except ValueError:
        rel = Path(Path(r.creds_path).name)
    dest = Path(dest_home) / rel
    return {"staged": True, "src": str(src), "dest": str(dest)}


# ── directive + run argv ────────────────────────────────────────────────────
def load_capabilities() -> str:
    """The portable Layer-1 capabilities prompt (`CAPABILITIES.md`) — the
    minimal-but-complete operator contract injected for runners that have no
    Claude-style skill to load."""
    return Path(__file__).with_name("CAPABILITIES.md").read_text()


def compose_directive(objective: str, room: dict, budget: Budget,
                      stop: list[str] | None = None,
                      runner: "Runner | str | None" = None) -> str:
    """The prompt the nested operator runs headlessly. Tells it where to join,
    what to achieve, when to stop, and the recursion budget it inherits (so it
    self-limits its own spawns).

    Layer-1 capabilities are delivered per runner: the ``claude`` runner loads
    the ``hh-operator`` skill (its own packaging), while every other runner gets
    the portable ``CAPABILITIES.md`` prepended inline — same contract, no skill
    machinery required."""
    r = get_runner(runner)
    host, port = room.get("host", "?"), room.get("port", "?")
    name = room.get("name", "operator")
    stop = stop or ["objective met", "owner says stop/leave", "idle past remit"]
    if r.name == "claude":
        preamble = ("Use the hh-operator skill. You are an autonomous operator "
                    "joining a hack-house room.")
    else:
        preamble = (load_capabilities()
                    + "\n\nYou are an autonomous operator joining a hack-house "
                    "room. Operate per the capabilities above.")
    lines = [
        preamble,
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
                   runner: "Runner | str | None" = None,
                   claude_bin: str = "claude") -> list[str]:
    """argv to launch a headless nested operator session for the chosen runner
    (default ``claude``). `skip_permissions` maps to the runner's unattended flag
    (only for trusted, sandboxed autonomy); `config_dir` is applied via
    :func:`child_env`, not the argv. `claude_bin` overrides the executable for the
    default claude runner (back-compat)."""
    r = get_runner(runner)
    if r.name == "claude" and claude_bin != "claude":
        argv = [claude_bin, *r.prompt_flags, directive]
        if skip_permissions:
            argv += list(r.unattended_flags)
        return argv
    return r.argv(directive, skip_permissions=skip_permissions)


def child_env(config_dir: str | None = None, *,
              runner: "Runner | str | None" = None) -> dict:
    """Environment for the child process. Isolates its config dir when given (via
    the runner's config-dir env var, e.g. $CLAUDE_CONFIG_DIR) so a nested
    session's auth/state is its own."""
    env = dict(os.environ)
    r = get_runner(runner)
    if config_dir and r.config_env:
        env[r.config_env] = str(config_dir)
    return env
