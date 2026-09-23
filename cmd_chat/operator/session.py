"""Session filesystem layout for the operator bridge.

Each running daemon owns a session directory under a per-user runtime root:

    ${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/hh-bridge/<session>/
        control.sock   unix socket the CLI verbs talk to
        inbox.jsonl    append-only event log (durability/debug; truth is in-RAM)
        meta.json      {host, port, user, pid, trigger, no_tls, started}
        daemon.log     daemon stdout/stderr
        cursor         last seq the CLI `read` consumed (client-side bookmark)

The session name defaults to the room display name (the `user` we join as), so
one operator-per-name maps to one session dir. `--session` overrides it when a
single box runs several bridges.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


def runtime_root() -> Path:
    # Prefer a real per-user runtime dir; else $TMPDIR (Termux/Android sets this
    # to $PREFIX/tmp — there is no /tmp on Android); else /tmp on a normal box.
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    return Path(base) / "hh-bridge"


def _safe(name: str) -> str:
    """Reduce a session label to a filesystem-safe slug (no path escapes)."""
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", name.strip())
    return slug or "default"


class Session:
    """Resolves the paths for one bridge session; never opens anything itself."""

    def __init__(self, name: str):
        self.name = _safe(name)
        self.dir = runtime_root() / self.name

    # ── paths ────────────────────────────────────────────────────────────
    @property
    def sock_path(self) -> Path:
        return self.dir / "control.sock"

    @property
    def inbox_path(self) -> Path:
        return self.dir / "inbox.jsonl"

    @property
    def meta_path(self) -> Path:
        return self.dir / "meta.json"

    @property
    def log_path(self) -> Path:
        return self.dir / "daemon.log"

    @property
    def cursor_path(self) -> Path:
        return self.dir / "cursor"

    # ── lifecycle helpers ────────────────────────────────────────────────
    def ensure_dir(self) -> None:
        # 0700: the socket grants room-send rights, so keep it user-private.
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    def write_meta(self, **fields) -> None:
        self.ensure_dir()
        self.meta_path.write_text(json.dumps(fields, indent=2))

    def read_meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def read_cursor(self) -> int:
        try:
            return int(self.cursor_path.read_text().strip() or "0")
        except (OSError, ValueError):
            return 0

    def write_cursor(self, seq: int) -> None:
        self.ensure_dir()
        self.cursor_path.write_text(str(seq))

    def cleanup(self) -> None:
        """Remove the socket so a stale path never confuses the next `up`."""
        for p in (self.sock_path,):
            try:
                p.unlink()
            except OSError:
                pass


def resolve(name: str | None) -> Session:
    """Pick the session to act on.

    Explicit name → that session. Otherwise, if exactly one live session dir
    exists (has a control.sock), use it; if none/many, fall back to "default"
    (so a clear error surfaces when the CLI can't reach a socket).
    """
    if name:
        return Session(name)
    root = runtime_root()
    if root.is_dir():
        live = [d for d in root.iterdir() if (d / "control.sock").exists()]
        if len(live) == 1:
            return Session(live[0].name)
    return Session("default")


def list_sessions() -> list[Session]:
    root = runtime_root()
    if not root.is_dir():
        return []
    return [Session(d.name) for d in sorted(root.iterdir()) if d.is_dir()]
