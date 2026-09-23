"""Device adapter interface for the hack-house device bridge.

An adapter presents ONE physical device's control surface as a small, curated,
allowlisted verb vocabulary. The bridge (`cmd_chat/device/bridge.py`) joins a room
as a persona member and dispatches `@<persona> <verb> …` chat commands to the
adapter; the adapter yields output chunks that the bridge streams back to the room.

Safety contract — every adapter MUST honor:
  * Verbs are an ALLOWLIST — an unknown verb never reaches the device.
  * `armed=True` verbs (RF TX, deauth, injection, payload execution) run ONLY
    after the device is armed by an authorized operator, and auto-disarm.
  * Arguments are validated against a strict token charset before they touch the
    device; a runner builds remote commands as a fixed template + safe args,
    never by interpolating chat text into a shell string.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import AsyncIterator, Callable

# A chat arg is one token; reject anything that could break out of a remote
# command template (no whitespace, quotes, or shell metacharacters).
_SAFE_ARG = re.compile(r"^[A-Za-z0-9._:@/=+-]{1,128}$")


def safe_args(args: list[str]) -> list[str]:
    """Return args unchanged if every token passes the filter; raise otherwise."""
    for a in args:
        if not _SAFE_ARG.match(a):
            raise ValueError(
                f"unsafe argument {a!r} — allowed: letters, digits and . _ : @ / = + -")
    return args


@dataclass
class Verb:
    name: str
    help: str
    run: Callable[[list[str]], AsyncIterator[str]]   # async generator of output chunks
    armed: bool = False          # requires the device to be armed first (⇒ authorized too)
    owner_only: bool = False     # requires an authorized operator, but no arm (device-mutating)
    min_args: int = 0
    max_args: int = 0


class DeviceAdapter:
    """Base class. Subclass, set KIND, and populate verbs in register_verbs()."""

    KIND = "generic device"

    def __init__(self, persona: str):
        self.persona = persona
        self._armed = False
        self._verbs: dict[str, Verb] = {}
        self.register_verbs()

    # ── to override ──────────────────────────────────────────────────────────
    def register_verbs(self) -> None:
        raise NotImplementedError

    async def health(self) -> tuple[bool, str]:
        """(online, one-line detail). Cheap reachability probe — no side effects."""
        return True, "no health check implemented"

    async def open_shell(self, rows: int = 40, cols: int = 120):
        """Return (proc, master_fd) — an interactive PTY for raw-drive
        (`/sbx <persona>`), or None if this device has no shell surface. SSH adapters
        return an `ssh -tt` channel in a real PTY; serial adapters, the serial CLI."""
        return None

    # ── framework ────────────────────────────────────────────────────────────
    def verb(self, v: Verb) -> None:
        self._verbs[v.name] = v

    @property
    def armed(self) -> bool:
        return self._armed

    def arm(self) -> None:
        self._armed = True

    def disarm(self) -> None:
        self._armed = False

    def menu(self) -> list[Verb]:
        return list(self._verbs.values())

    def is_armed_verb(self, verb: str) -> bool:
        v = self._verbs.get(verb)
        return bool(v and v.armed)

    def is_privileged_verb(self, verb: str) -> bool:
        """Owner-only but not arm-gated (device-mutating: push/pull/get/put)."""
        v = self._verbs.get(verb)
        return bool(v and (v.owner_only or v.armed))

    async def dispatch(self, verb: str, args: list[str]) -> AsyncIterator[str]:
        """Validate + run one verb, yielding output chunks. Arming is checked by
        the bridge (it holds the authz context); here we re-assert as defense in
        depth so an adapter is never coaxed into an armed action while disarmed."""
        spec = self._verbs.get(verb)
        if spec is None:
            yield f"unknown verb {verb!r} — try `{self.persona} help`"
            return
        if spec.armed and not self._armed:
            yield f"✋ `{verb}` is an ARMED action — arm the device first."
            return
        if not (spec.min_args <= len(args) <= spec.max_args):
            yield (f"usage: {self.persona} {verb} …  "
                   f"({spec.min_args}–{spec.max_args} args) — {spec.help}")
            return
        try:
            safe_args(args)
        except ValueError as e:
            yield f"✖ {e}"
            return
        async for chunk in spec.run(args):
            yield chunk


async def stream_exec(argv: list[str], timeout: float = 60.0) -> AsyncIterator[str]:
    """Run a local command (argv, NOT a shell string) and yield its stdout/stderr
    lines. Used by adapters to shell out to `ssh <alias> …` / device CLIs safely —
    no shell, so no injection surface beyond the (already-validated) argv tokens."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        yield f"✖ {argv[0]}: not found on the bridge host"
        return
    try:
        assert proc.stdout is not None
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                yield f"✖ timed out after {timeout:.0f}s — killed"
                return
            if not line:
                break
            yield line.decode(errors="replace").rstrip("\n")
        await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
