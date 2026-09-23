#!/usr/bin/env python3
"""On-device Phase-0 preflight for a hack-house operator (Termux / aarch64).

Run this ON THE PHONE after installing the operator deps. It cannot be run
authoritatively from an x86_64/glibc dev box — the whole point is to exercise
the aarch64 + Android-bionic runtime (wheel availability, Termux package
versions, the $TMPDIR socket path). See docs/termux-operator.md §2.

Usage (in Termux, from the repo root):

    pkg install python                     # 3.11+ recommended
    pip install -r requirements-operator.txt   # srp is best-effort (shim covers it)
    python scripts/termux-preflight.py

It prints a PASS/FAIL line per check and exits non-zero if any hard check fails.
Copy the whole report back to the operator/dev session to decide next steps.
"""

from __future__ import annotations

import importlib
import os
import platform
import socket
import sys
import tempfile
from pathlib import Path

# Repo root on sys.path so `cmd_chat.*` imports resolve when run in-tree.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_hard_failures = 0
_soft_notes = 0


def _line(status: str, name: str, detail: str = "") -> None:
    tail = f" — {detail}" if detail else ""
    print(f"[{status:^4}] {name}{tail}")


def hard(name: str, ok: bool, detail: str = "") -> bool:
    global _hard_failures
    if ok:
        _line("PASS", name, detail)
    else:
        _hard_failures += 1
        _line("FAIL", name, detail)
    return ok


def soft(name: str, ok: bool, detail: str = "") -> None:
    """A non-blocking observation (won't fail the run, but worth reporting)."""
    global _soft_notes
    if ok:
        _line("PASS", name, detail)
    else:
        _soft_notes += 1
        _line("WARN", name, detail)


def _try_import(mod: str) -> tuple[bool, str]:
    try:
        m = importlib.import_module(mod)
        return True, getattr(m, "__version__", "?")
    except Exception as e:  # ImportError, build/ABI errors, etc.
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    print("=" * 60)
    print("hack-house operator — Termux Phase-0 preflight")
    print(f"platform : {platform.platform()}")
    print(f"machine  : {platform.machine()}")
    print(f"python   : {sys.version.split()[0]} ({sys.executable})")
    print("=" * 60)

    # 0.1 — Python version (need 3.11+ for stdlib tomllib used by models.toml).
    hard(
        "python >= 3.11 (tomllib for models.toml)",
        sys.version_info >= (3, 11),
        detail=f"got {sys.version_info.major}.{sys.version_info.minor}",
    )

    # 0.2 — easy pure-Python / wheeled deps.
    for mod in ("requests", "rich", "websockets"):
        ok, ver = _try_import(mod)
        hard(f"import {mod}", ok, ver)

    # 0.4 — cryptography: the operator only uses Fernet; version floor is soft.
    ok, ver = _try_import("cryptography")
    hard("import cryptography", ok, ver)
    if ok:
        try:
            from cryptography.fernet import Fernet

            Fernet(Fernet.generate_key()).encrypt(b"ok")
            hard("cryptography.fernet.Fernet round-trip", True)
        except Exception as e:
            hard("cryptography.fernet.Fernet round-trip", False, str(e))
        # The server pins >=46, but the operator only needs Fernet; flag if lower
        # so we consciously accept it (§0.4), don't hard-fail.
        try:
            major = int(str(ver).split(".")[0])
            soft(
                "cryptography >= 46 (server pin; operator needs only Fernet)",
                major >= 46,
                f"have {ver}; Fernet works on far older — acceptable",
            )
        except ValueError:
            pass

    # 0.5 — srp: real C-ext if it built, else the pure-Python shim MUST cover it.
    ok_real, ver = _try_import("srp")
    if ok_real:
        soft("import srp (C-ext built)", True, ver)
    else:
        soft("import srp (C-ext)", False, "not built — using pure-Python shim")
    ok_shim, detail = _try_import("cmd_chat.client._srp_pure")
    hard("import cmd_chat.client._srp_pure (fallback)", ok_shim, detail)

    # Client binds *some* srp (real or shim) without error.
    ok_client, detail = _try_import("cmd_chat.client.client")
    hard("import cmd_chat.client.client", ok_client, detail)
    if ok_client:
        import cmd_chat.client.client as c

        _line("INFO", "client.srp backend", c.srp.__name__)

    # Operator package imports without the server stack (sanic/pydantic).
    ok_op, detail = _try_import("cmd_chat.operator")
    hard("import cmd_chat.operator (no server deps needed)", ok_op, detail)

    # Phase-1 dependency: AF_UNIX control socket under the runtime root ($TMPDIR
    # on Termux). Prove we can actually bind one there.
    try:
        from cmd_chat.operator.session import runtime_root

        root = runtime_root()
        root.mkdir(parents=True, exist_ok=True)
        sp = root / "preflight.sock"
        if sp.exists():
            sp.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sp))
        srv.close()
        sp.unlink()
        hard("AF_UNIX control socket under runtime root", True, str(root))
    except Exception as e:
        hard("AF_UNIX control socket under runtime root", False, str(e))
        _line("INFO", "runtime root (should be writable)",
              os.environ.get("TMPDIR", tempfile.gettempdir()))

    print("=" * 60)
    if _hard_failures == 0:
        print(f"RESULT: PASS  ({_soft_notes} warning(s)) — operator deps OK on-device")
        return 0
    print(f"RESULT: FAIL  ({_hard_failures} hard failure(s), {_soft_notes} warning(s))")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
