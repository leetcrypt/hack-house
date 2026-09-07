"""Operator-side adapter for the sandbox egress gateway.

The gateway logic (nft rulesets, tor setup, lifecycle) is single-sourced in the
standalone CLI ``scripts/hh-sbx-egress.py`` so the Python operator AND the Rust TUI
(`hh/src/sbx.rs`) share exactly one implementation of the security-critical bits.
This module is a thin async wrapper the operator's ``launch_container`` calls; it
shells out to that script. Backs ``HH_SBX_EGRESS=local|scope|tor`` (guard/open/none
need no gateway and are handled in ``egress.py`` / the launcher). See
research/sandbox-egress-posture-2026-09-07/SPIKE.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SCRIPT = str(Path(__file__).resolve().parents[2] / "scripts" / "hh-sbx-egress.py")


def gw_name(sandbox_name: str) -> str:
    return f"hh-egw-{sandbox_name}"


def _argv(cmd: str, sandbox_name: str, mode: str | None = None) -> list[str]:
    av = [sys.executable, _SCRIPT, cmd, sandbox_name]
    if mode is not None:
        av += ["--egress", mode]
    scope = os.environ.get("HH_SBX_SCOPE")
    if scope:
        av += ["--scope", scope]
    return av


async def launch_gateway(engine: str, sandbox_name: str, mode: str) -> tuple[bool, str, str | None]:
    """Create the egress gateway via the canonical script. Returns (ok, message, gw_name).
    Fail-closed: a non-zero/`REFUSED:` result means the caller must refuse the sandbox."""
    from .sandbox import exec_capture
    # tor mode blocks on tor bootstrap (~30-75s) — give the up call room beyond EXEC_TIMEOUT.
    out, rc = await exec_capture(_argv("up", sandbox_name, mode) + ["--engine", engine],
                                 timeout=120.0)
    text = (out or "").strip()
    name = gw_name(sandbox_name)
    if rc == 0 and text.startswith("NETARG="):
        return True, f"egress={mode} via gateway {name}", name
    reason = text.splitlines()[-1] if text else f"gateway up failed (rc={rc})"
    return False, reason, None


async def post_join(engine: str, sandbox_name: str, mode: str) -> None:
    """Sandbox-side setup after it joined the gateway netns — the script points the
    resolver at a reachable nameserver (tor: tor DNSPort; local/scope: public IPv4)."""
    if mode not in ("local", "scope", "tor"):
        return
    from .sandbox import exec_capture
    await exec_capture(_argv("postjoin", sandbox_name, mode) + ["--engine", engine])


async def teardown_gateway(engine: str, sandbox_name: str) -> None:
    """Remove the sandbox's gateway (best-effort; no-op if none)."""
    from .sandbox import exec_capture
    await exec_capture(_argv("down", sandbox_name) + ["--engine", engine])
