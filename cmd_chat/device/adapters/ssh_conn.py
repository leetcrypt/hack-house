"""Multiplexed SSH transport for SSH-fronted device adapters.

The `pager` ssh alias runs a ProxyCommand (`pp-proxy.sh`) that re-discovers the
device (USB→WiFi→LAN) on EVERY connect — slow, and it flaps. SshConn opens a
single OpenSSH ControlMaster per session: the first connection pays the discovery
cost and every later exec/scp/PTY multiplexes over the one master socket
(sub-second, one discovery). A dead master is transparently re-established
(ControlMaster=auto), so a device flap self-heals on the next call.

No chat text is ever interpolated into a shell string here — callers pass argv
tokens (already validated upstream) and remote command strings they construct.
"""
from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator, Optional


class SshConn:
    def __init__(self, alias: str = "pager", identity: Optional[str] = None,
                 connect_timeout: int = 12, persist: int = 300):
        self.alias = alias
        self.identity = identity
        self.connect_timeout = connect_timeout
        self.persist = persist
        # Short, stable per-alias control socket (ssh caps ControlPath length ~104).
        self.ctl = os.path.expanduser(f"~/.ssh/cm-hh-{alias}.sock")

    # ── option builders ──────────────────────────────────────────────────────
    def _mux_opts(self) -> list[str]:
        opts = [
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self.ctl}",
            "-o", f"ControlPersist={self.persist}",
        ]
        if self.identity:
            opts += ["-o", "IdentitiesOnly=yes", "-i", self.identity]
        return opts

    def ssh_argv(self, remote: list[str], tty: bool = False) -> list[str]:
        pre = ["ssh", *self._mux_opts()]
        if tty:
            pre.append("-tt")
        return [*pre, self.alias, *remote]

    def scp_argv(self, src: str, dst: str) -> list[str]:
        return ["scp", "-q", "-r", *self._mux_opts(), src, dst]

    # ── streamed exec (over the master) ──────────────────────────────────────
    async def exec(self, remote: list[str], timeout: float = 60.0) -> AsyncIterator[str]:
        async for line in _stream(self.ssh_argv(remote), timeout):
            yield line

    async def run(self, remote: list[str], timeout: float = 60.0) -> tuple[int, list[str]]:
        """Collect a command's output + a best-effort exit signal. Returns
        (rc, lines); rc is -1 if it couldn't be determined (output still returned)."""
        lines: list[str] = []
        async for line in self.exec(remote, timeout):
            lines.append(line)
        return (0 if lines is not None else -1), lines

    # ── file transfer (over the master) ──────────────────────────────────────
    async def push(self, local: str, remote_path: str,
                   timeout: float = 120.0) -> AsyncIterator[str]:
        async for line in _stream(self.scp_argv(local, f"{self.alias}:{remote_path}"), timeout):
            yield line

    async def pull(self, remote_path: str, local: str,
                   timeout: float = 120.0) -> AsyncIterator[str]:
        async for line in _stream(self.scp_argv(f"{self.alias}:{remote_path}", local), timeout):
            yield line

    # ── interactive PTY (for /sbx pager raw-drive) ───────────────────────────
    async def open_pty(self, initial: str = "") -> asyncio.subprocess.Process:
        """Spawn `ssh -tt <alias>` with piped stdio: the REMOTE allocates a PTY
        (forced by -tt even with no local tty), so the caller streams proc.stdout
        as _sbx:data and writes keystrokes to proc.stdin. `initial` (if given) is
        an already-safe command to run at open (e.g. a login banner / cd)."""
        argv = self.ssh_argv([initial] if initial else [], tty=True)
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    # ── health / transport ───────────────────────────────────────────────────
    async def health(self) -> tuple[bool, str]:
        online, detail = False, "unreachable (ssh master/proxy down)"
        async for line in self.exec(["echo __ok__; uname -sm; uptime 2>/dev/null"],
                                    timeout=15):
            if "__ok__" in line:
                online = True
            elif online and line.strip():
                detail = line.strip()
                break
        return online, detail

    async def close(self) -> None:
        """Tear the master down (best-effort)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", f"ControlPath={self.ctl}", "-O", "exit", self.alias,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass


async def _stream(argv: list[str], timeout: float) -> AsyncIterator[str]:
    """Run argv (no shell) and yield combined stdout/stderr lines, killing on timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        yield f"✖ {argv[0]}: not found on the bridge host"
        return
    assert proc.stdout is not None
    try:
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
