"""USB-serial transport for serial-fronted device adapters (Flipper Zero).

The pager rides a multiplexed SSH master (`SshConn`); a Flipper has no network
surface — it is a single-holder `/dev/ttyACM*` CDC-ACM serial port, reachable ONLY
while physically tethered by a DATA cable. `SerialConn` therefore:

  * resolves the device node (`/dev/serial/by-id/*Flipper*` → `/dev/ttyACM*`),
  * shells batch verbs to the audited `flipper-cli` wrapper (open/close-per-call),
  * hands out ONE interactive PTY (`serial_relay`) for `/sbx flipper`, and
  * because the port is exclusive, refuses batch verbs while that shell holds it.

No chat text is interpolated into a shell string: callers pass argv tokens that the
adapter has already validated against the strict arg charset.
"""
from __future__ import annotations

import asyncio
import glob
import os
import subprocess
from typing import AsyncIterator, Optional

from .base import stream_exec

BAUD = 230400
_DEFAULT_CLI = os.path.expanduser("~/coding/hardware/flipper-0/bin/flipper-cli")
_DEFAULT_BADUSB = os.path.expanduser("~/coding/hardware/flipper-0/bin/badusb-list")


class SerialConn:
    def __init__(self, dev: Optional[str] = None, cli: Optional[str] = None):
        # `dev` is an optional explicit node/glob override; default is auto-find.
        self.dev_override = dev
        self.cli = cli or os.environ.get("HH_FLIPPER_CLI", _DEFAULT_CLI)
        self.badusb = os.environ.get("HH_BADUSB_LIST", _DEFAULT_BADUSB)
        self._pty_proc: Optional[subprocess.Popen] = None
        # /dev/ttyACM0 is single-holder; the bridge dispatches each verb as its own
        # asyncio task, so serialize ALL port access (verbs + presence probe) through
        # one lock — else concurrent flipper-cli opens interleave and responses bleed.
        self._port_lock = asyncio.Lock()

    # ── device resolution ──────────────────────────────────────────────────────
    def find_device(self) -> Optional[str]:
        if self.dev_override and os.path.exists(self.dev_override):
            return self.dev_override
        by_id = sorted(glob.glob("/dev/serial/by-id/*Flipper*"))
        if by_id:
            return by_id[0]
        acm = sorted(glob.glob("/dev/ttyACM*"))
        return acm[0] if acm else None

    # ── exclusivity ────────────────────────────────────────────────────────────
    @property
    def shell_open(self) -> bool:
        """True while an interactive `/sbx flipper` PTY holds the serial port."""
        return self._pty_proc is not None and self._pty_proc.poll() is None

    # ── batch exec (shell to flipper-cli / badusb-list) ────────────────────────
    async def cli_exec(self, args: list[str], timeout: float = 30.0) -> AsyncIterator[str]:
        """Run `flipper-cli <args>` (one-shot, opens+closes the port)."""
        if not os.path.exists(self.cli):
            yield f"✖ flipper-cli not found at {self.cli} (set HH_FLIPPER_CLI)"
            return
        # Hold the port lock for the whole one-shot so a second verb waits its turn
        # rather than racing on the serial device.
        async with self._port_lock:
            async for line in stream_exec([self.cli, *args], timeout=timeout):
                yield line

    async def badusb_exec(self, args: list[str], timeout: float = 20.0) -> AsyncIterator[str]:
        """Run the host-side `badusb-list` inventory (no device needed)."""
        if not os.path.exists(self.badusb):
            yield f"✖ badusb-list not found at {self.badusb} (set HH_BADUSB_LIST)"
            return
        async for line in stream_exec([self.badusb, *args], timeout=timeout):
            yield line

    # ── interactive PTY (for /sbx flipper raw-drive) ───────────────────────────
    def open_pty(self, rows: int = 40, cols: int = 120):
        """Spawn the serial↔stdio relay inside a REAL local PTY and return
        (proc, master_fd) — the exact contract the bridge expects from an SSH PTY.
        The relay owns the serial port for its lifetime; killing proc frees it.
        Returns None if no device is present."""
        import fcntl
        import pty
        import struct
        import sys
        import termios

        dev = self.find_device()
        if dev is None:
            return None
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        argv = [sys.executable, "-m", "cmd_chat.device.serial_relay", dev, str(BAUD)]
        proc = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, close_fds=True)
        os.close(slave)
        fl = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        self._pty_proc = proc
        return proc, master

    @staticmethod
    def set_winsize(master_fd: int, rows: int, cols: int) -> None:
        import fcntl
        import struct
        import termios
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))
        except OSError:
            pass

    # ── health ─────────────────────────────────────────────────────────────────
    async def health(self) -> tuple[bool, str]:
        if self.shell_open:
            return True, "shell open — serial port held by /sbx flipper"
        dev = self.find_device()
        if dev is None:
            return False, "no /dev/ttyACM* — data cable? device powered/unlocked?"
        # Node exists; confirm the CLI actually talks to firmware.
        online, detail = False, f"{dev} present but no CLI response"
        async for line in self.cli_exec(["info"], timeout=8):
            low = line.lower()
            if "firmware" in low or "hardware" in low or "flipper" in low:
                online = True
                detail = f"{os.path.basename(dev)} · {line.strip()[:60]}"
                break
        return online, detail

    async def close(self) -> None:
        if self.shell_open:
            try:
                self._pty_proc.kill()
            except Exception:
                pass
