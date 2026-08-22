"""Sandbox primitives for the operator bridge — pure, dependency-light helpers.

Two ways the operator touches a sandbox:

1. **Co-located exec** (this module's `exec_*` / `launch_*`): the daemon owns a
   container it started itself (`podman`/`docker run -d … sleep infinity`) and
   runs commands *out-of-band* with `engine exec -i`. Output is captured and
   returned — clean, scriptable, invisible to the room. Mirrors the native
   harness's exec path (cmd_chat/agent/bridge.py:640-676) argv-for-argv so the
   blast radius and quoting rules are identical.

2. **Relay keystrokes** (`encode_keys`): when the *room's* shared sandbox is
   owned by the Rust broker, the only way in is the same `_sbx:input` keystroke
   frame a human driver's terminal emits. The broker writes our bytes to the
   shared PTY iff we're a granted driver. That path needs a precise byte
   vocabulary — including how to *stop* a running program — which lives here so
   it's one tested table, reused by the bridge and documented once per session.

Nothing here opens a websocket or holds state; the bridge wires these in.
"""

from __future__ import annotations

import asyncio
import os
import re

# CSI / OSC / single-char escape sequences + carriage returns, so relayed PTY
# bytes read as plain text. We strip rather than interpret: the operator wants
# to grep the output, not render a faithful screen.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"   # CSI  …  (colours, cursor moves)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC  …  (title sets) BEL/ST-terminated
    r"|\x1b[@-Z\\-_]"              # two-char escapes
)


def strip_ansi(text: str) -> str:
    """Drop ANSI/control noise from relayed terminal output. Collapses bare
    carriage returns (so progress redraws don't stack) but keeps newlines."""
    clean = _ANSI_RE.sub("", text)
    clean = clean.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in clean if c == "\n" or c == "\t" or c >= " ")

# Match the native harness so behaviour is identical across both drivers.
EXEC_TIMEOUT = 60.0      # NATIVE_TOOL_TIMEOUT
OUTPUT_CAP = 4096        # NATIVE_OUTPUT_CAP (bytes of combined stdout+stderr)


# ── engine / image selection ───────────────────────────────────────────────
def pick_engine() -> str | None:
    """Prefer podman (rootless, daemonless, no sudo) then docker. None if neither
    is on PATH — the caller surfaces that as a clear 'no engine' error."""
    import shutil

    for eng in ("podman", "docker"):
        if shutil.which(eng):
            return eng
    return None


def default_image(engine: str) -> str:
    """Match the Rust/agent defaults: Podman→Kali, Docker→Parrot."""
    return "docker.io/kalilinux/kali-rolling" if engine == "podman" else "docker.io/parrotsec/core"


# ── co-located exec ─────────────────────────────────────────────────────────
def exec_prefix(engine: str | None, name: str | None) -> list[str] | None:
    """argv prefix that runs a program *inside* the named sandbox, or None if we
    can't address it. The real program + args are appended as separate argv
    elements (never a shell-interpolated string), so untrusted room text can't
    inject shell metacharacters outside an explicit `sh -c`. `-i` keeps stdin
    open so `write` can pipe file content in. Identical shape to the native
    harness (bridge.py:640)."""
    if engine in ("docker", "podman"):
        return [engine, "exec", "-i", name] if name else None
    if engine == "multipass":
        return ["multipass", "exec", name, "--"] if name else None
    if engine == "local":
        return []  # host shell — explicit, opt-in exception
    return None


async def exec_capture(argv: list[str], stdin: bytes | None = None,
                       text: bool = True) -> tuple[str | bytes, int]:
    """Run an exec argv, capture combined stdout+stderr and the exit code,
    time-bounded. `text=True` decodes + byte-caps for display; `text=False`
    returns raw bytes uncapped (for `get`, where truncation would corrupt the
    file). The container/VM is the blast radius; `local` is the host by choice."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, OSError) as e:
        return (f"[exec failed: {e}]" if text else b""), 127
    try:
        out, _ = await asyncio.wait_for(proc.communicate(input=stdin), timeout=EXEC_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return ("[command timed out]" if text else b""), 124
    rc = proc.returncode if proc.returncode is not None else -1
    if not text:
        return out, rc
    s = out.decode(errors="replace")
    if len(s) > OUTPUT_CAP:
        s = s[:OUTPUT_CAP] + "\n[output truncated]"
    return s, rc


# Ownership labels, matching hh/src/sbx.rs so either side can reclaim the
# other's abandoned sandboxes.
SANDBOX_LABEL = "hh.sandbox"
OWNER_PID_LABEL = "hh.owner-pid"


async def sweep_stale(engine: str) -> int:
    """Remove sandbox containers whose creating process is gone.

    `teardown_container` covers clean shutdown, but a SIGKILLed daemon leaves a
    container running `sleep infinity` forever. Every sandbox is stamped with
    its creator's PID, so one that's absent from /proc is abandoned. Called
    before each launch; best-effort. Returns the count removed."""
    out, rc = await exec_capture(
        [engine, "ps", "-a", "--filter", f"label={SANDBOX_LABEL}=1",
         "--format", '{{.Names}} {{index .Labels "' + OWNER_PID_LABEL + '"}}'])
    if rc != 0:
        return 0
    swept = 0
    for line in str(out).splitlines():
        cname, _, pid = line.strip().partition(" ")
        if not cname or not pid.strip().isdigit():
            continue  # unlabelled or unparseable — can't prove it's abandoned
        if int(pid) == os.getpid() or os.path.exists(f"/proc/{int(pid)}"):
            continue  # owner still alive — hands off
        _, rm_rc = await exec_capture([engine, "rm", "-f", cname])
        swept += rm_rc == 0
    return swept


def _hardening_flags() -> list[str]:
    """Container isolation flags for the sandbox launch (kept argv-identical to
    hh/src/sbx.rs). A safe DoS bound (`--pids-limit`, env-tunable) is ALWAYS on —
    it never affects normal use. `HH_SBX_HARDEN=strict` adds the full escape-
    resistant posture (cap-drop=ALL, no-new-privileges, read-only, network=none);
    that breaks apt/pip/git/su, so it is for EMPTY or locked-down/escape-sensitive
    rooms only, not the general functional sandbox. See the hh-redteam skill.
    Envs: HH_SBX_PIDS (default 4096), HH_SBX_MEMORY (e.g. 8g; off if unset),
    HH_SBX_HARDEN (strict|escape|max → full posture)."""
    flags = ["--pids-limit", os.environ.get("HH_SBX_PIDS", "4096")]
    mem = os.environ.get("HH_SBX_MEMORY", "").strip()
    if mem:
        flags += ["--memory", mem, "--memory-swap", mem]
    if os.environ.get("HH_SBX_HARDEN", "").strip().lower() in ("strict", "escape", "max"):
        flags += ["--cap-drop=ALL", "--security-opt=no-new-privileges",
                  "--read-only", "--tmpfs", "/tmp", "--tmpfs", "/root:rw,exec",
                  "--network=none"]
    return flags


async def launch_container(engine: str, name: str, image: str) -> tuple[bool, str]:
    """Start a persistent throwaway container we can exec into: reclaim any
    abandoned sandboxes, drop a stale same-name one, then `run -d --init --name
    … --hostname … -w /root <image> sleep infinity` (argv-identical to
    hh/src/sbx.rs). Returns (ok, message).

    `--init` is load-bearing: without it PID 1 is `sleep infinity`, which only
    calls nanosleep() and never wait()s, so every process orphaned inside the
    sandbox becomes a permanently unreapable zombie. `--init` puts catatonit at
    PID 1 to reap them."""
    await sweep_stale(engine)
    await exec_capture([engine, "rm", "-f", name])  # ignore "no such container"
    out, rc = await exec_capture(
        [engine, "run", "-d", "--init", "--name", name, "--hostname", name,
         "--label", f"{SANDBOX_LABEL}=1",
         "--label", f"{OWNER_PID_LABEL}={os.getpid()}",
         "-w", "/root", *_hardening_flags(), image, "sleep", "infinity"])
    if rc != 0:
        tail = (out or "").strip().splitlines()[-1:] or [""]
        return False, f"{engine} run failed (exit={rc}): {tail[0]}"
    return True, f"launched {engine} container '{name}' from {image}"


async def teardown_container(engine: str, name: str) -> tuple[bool, str]:
    out, rc = await exec_capture([engine, "rm", "-f", name])
    if rc != 0:
        return False, f"{engine} rm failed (exit={rc}): {(out or '').strip()[:200]}"
    return True, f"removed {engine} container '{name}'"


# ── relay keystrokes (shared-PTY drive) ─────────────────────────────────────
#
# A terminal is just a byte stream. Named keys below are the bytes a real
# keyboard sends; the bridge base64-wraps them into a `{"_sbx":"input"}` frame.
# The control characters are the *how to stop things* vocabulary — being able to
# interrupt a hung program is as essential as starting one, especially in tests.
NAMED_KEYS: dict[str, bytes] = {
    # line endings / whitespace
    "enter": b"\r", "return": b"\r", "newline": b"\n", "tab": b"\t",
    "space": b" ", "backspace": b"\x7f",
    # the stop/exit family — the most important keys for staying unstuck
    "ctrl-c": b"\x03",   # SIGINT  — interrupt the foreground program
    "ctrl-d": b"\x04",   # EOF     — end stdin / exit an empty shell
    "ctrl-z": b"\x1a",   # SIGTSTP — suspend to the background (then `fg`/`bg`)
    "ctrl-\\": b"\x1c",  # SIGQUIT — hard quit + core dump when SIGINT is ignored
    "esc": b"\x1b",      # leave vim insert-mode, dismiss menus, abort readline
    # screen / job control niceties
    "ctrl-l": b"\x0c",   # clear/redraw the screen
    "ctrl-u": b"\x15",   # kill the input line (clear a half-typed command)
    "ctrl-a": b"\x01",   # start-of-line (also tmux/screen prefix)
    "ctrl-e": b"\x05",   # end-of-line
    "ctrl-k": b"\x0b",   # kill to end-of-line
    "ctrl-w": b"\x17",   # delete the previous word
    # arrows / paging — ANSI sequences (history, menus, pagers)
    "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
    "home": b"\x1b[H", "end": b"\x1b[F",
    "pageup": b"\x1b[5~", "pagedown": b"\x1b[6~",
    "delete": b"\x1b[3~",
}

# Token-efficient cheat-sheet the skill/status surfaces once per session, so a
# Claude operator knows the stop-vocabulary without re-deriving it every turn.
KEYS_HELP = (
    "send-keys vocabulary (bytes → shared PTY; granted drivers only):\n"
    "  literal text          types it verbatim (no trailing newline)\n"
    "  enter/tab/space/esc    \\r \\t ' ' \\x1b\n"
    "  ctrl-c   interrupt the running program (SIGINT) — your main 'stop'\n"
    "  ctrl-d   EOF: end stdin / exit an empty shell\n"
    "  ctrl-z   suspend to background (resume with `fg`); ctrl-\\ = SIGQUIT\n"
    "  ctrl-u   clear a half-typed line; ctrl-l clears the screen\n"
    "  up/down/left/right home end pageup pagedown delete backspace\n"
    "  q        quits most pagers (less/man); :q<enter> quits vim\n"
    "End a stuck command with ctrl-c; if that's ignored, ctrl-\\ then ctrl-c again."
)


def encode_keys(spec) -> bytes:
    """Resolve a key spec into the raw bytes to inject.

    Accepts either a list of tokens or a single string:
      - a NAMED_KEYS name (case-insensitive)  → that control sequence
      - 'text:...'                             → the rest, verbatim (preserves a
                                                  literal name like 'enter')
      - 'hex:1b5b41' / '\\x1b'                  → raw bytes from hex
      - anything else                          → typed verbatim as UTF-8

    A bare string with no recognised token is typed as-is, so the common case
    (`send-keys "ls -la" enter`) is one obvious call. Naming is explicit on
    purpose: nothing here guesses, so an operator always knows what it sent."""
    tokens = spec if isinstance(spec, (list, tuple)) else [spec]
    out = bytearray()
    for tok in tokens:
        s = "" if tok is None else str(tok)
        low = s.lower()
        if low in NAMED_KEYS:
            out += NAMED_KEYS[low]
        elif s.startswith("text:"):
            out += s[5:].encode()
        elif low.startswith("hex:"):
            out += bytes.fromhex(s[4:])
        else:
            out += s.encode()
    return bytes(out)
