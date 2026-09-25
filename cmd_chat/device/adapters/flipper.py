"""Flipper Zero adapter — the full multi-tool as a curated room persona.

Transport is local USB serial (`SerialConn`, shelling to the audited `flipper-cli`
wrapper), not SSH: a Flipper is reachable ONLY while physically tethered by a data
cable — no network fallback (contrast the pager). The UI/UX is identical to the
pager persona by construction: the device-agnostic `DeviceBridge` supplies presence,
the `@flipper <verb>` grammar, the menu, arm/disarm, `/grant`, and `/sbx flipper`.

Surface split:
  * READ (open to any member)   — info · status · ls · read · badusb (inventory)
  * OWNER-ONLY (authorized)     — push · mkdir · reboot · cmd (raw CLI passthrough)
  * ARMED (arm + RF-legal ack)  — subghz · nfc · rfid · ir · hid (BadUSB)

The RF/HID actuators shell to `flipper-cli cmd "<subsystem …>"`. The exact CLI
strings are FIRMWARE-DEPENDENT (stock vs Momentum) and can't be verified without the
device on the bus, so each is a single centralized template + a one-line caveat, and
the ground-truth interactive surface is `/sbx flipper` (a raw serial shell). Live radio
capture (subghz rx, nfc read) is inherently interactive → drive it through the shell.
"""
from __future__ import annotations

from typing import AsyncIterator

from .base import DeviceAdapter, Verb
from .serial_conn import SerialConn

# Armed RF/HID actuators → the raw `flipper-cli cmd` token prefix. Centralized so a
# firmware-specific correction is a one-line edit. `hid` (BadUSB) is handled apart.
_RF_TEMPLATES: dict[str, tuple[list[str], str]] = {
    "subghz": (["subghz", "tx_from_file"], "replay a saved Sub-GHz capture (.sub) — TX"),
    "nfc":    (["nfc", "emulate"],         "emulate a saved NFC tag (.nfc)"),
    "rfid":   (["rfid", "emulate"],        "emulate a saved 125 kHz RFID tag (.rfid)"),
    "ir":     (["ir", "tx"],               "transmit a saved IR signal (.ir)"),
}


class FlipperAdapter(DeviceAdapter):
    KIND = "Flipper Zero"

    def __init__(self, persona: str = "flipper", alias: str | None = None):
        # `alias` is repurposed as an optional explicit device node (only when it looks
        # like one) — the uniform bridge call passes the pager's default alias otherwise.
        dev = alias if (alias and alias.startswith("/dev/")) else None
        self.conn = SerialConn(dev=dev)
        super().__init__(persona)

    async def health(self) -> tuple[bool, str]:
        return await self.conn.health()

    async def open_shell(self, rows: int = 40, cols: int = 120):
        return self.conn.open_pty(rows=rows, cols=cols)

    def register_verbs(self) -> None:
        # ── reads (open) ──
        self.verb(Verb("info", "device info (firmware, hardware, radio)", self._info))
        self.verb(Verb("status", "quick health check (fw, SD, storage)", self._status))
        self.verb(Verb("ls", "list storage: ls [/ext|/int|path]", self._ls, max_args=1))
        self.verb(Verb("read", "read a file from the device: read <path>",
                       self._read, min_args=1, max_args=1))
        self.verb(Verb("badusb", "BadUSB payload inventory (host library); "
                       "'badusb <category>' filters", self._badusb, max_args=1))
        # ── owner-only (authorized, device-mutating; no arm) ──
        self.verb(Verb("push", "upload a host file: push <local> <remote>",
                       self._push, owner_only=True, min_args=2, max_args=2))
        self.verb(Verb("mkdir", "create a device directory: mkdir <path>",
                       self._mkdir, owner_only=True, min_args=1, max_args=1))
        self.verb(Verb("reboot", "reboot the device", self._reboot, owner_only=True))
        self.verb(Verb("cmd", "raw serial CLI passthrough: cmd <command …> "
                       "(can transmit — authorized only)",
                       self._cmd, owner_only=True, min_args=1, max_args=12))
        # ── armed (RF/HID — arm + radio-legal responsibility) ──
        for name, (_tokens, help_) in _RF_TEMPLATES.items():
            self.verb(Verb(name, f"{help_}: {name} <device-file>",
                           self._make_rf(name), armed=True, min_args=1, max_args=3))
        self.verb(Verb("hid", "run a BadUSB payload — HID keystroke injection: hid <name>",
                       self._hid, armed=True, min_args=1, max_args=1))

    # ── shell-exclusivity guard ────────────────────────────────────────────────
    async def dispatch(self, verb: str, args: list[str]) -> AsyncIterator[str]:
        # The serial port is single-holder: while /sbx flipper holds it, batch verbs
        # can't open it. `badusb` is host-side inventory only → always allowed.
        if self.conn.shell_open and verb != "badusb":
            yield ("⏳ serial busy — /sbx flipper shell holds the port. "
                   "`@flipper shell stop` to run batch verbs.")
            return
        async for chunk in super().dispatch(verb, args):
            yield chunk

    # ── read verbs ──────────────────────────────────────────────────────────────
    async def _info(self, args: list[str]) -> AsyncIterator[str]:
        async for line in self.conn.cli_exec(["info"]):
            yield line

    async def _status(self, args: list[str]) -> AsyncIterator[str]:
        async for line in self.conn.cli_exec(["status"]):
            yield line

    async def _ls(self, args: list[str]) -> AsyncIterator[str]:
        path = args[0] if args else "/ext"
        async for line in self.conn.cli_exec(["storage", "ls", path], timeout=20):
            yield line

    async def _read(self, args: list[str]) -> AsyncIterator[str]:
        async for line in self.conn.cli_exec(["storage", "read", args[0]], timeout=20):
            yield line

    async def _badusb(self, args: list[str]) -> AsyncIterator[str]:
        argv = ["--flipper"]
        if args:
            argv += ["-c", args[0]]
        async for line in self.conn.badusb_exec(argv):
            yield line

    # ── owner-only verbs ─────────────────────────────────────────────────────────
    async def _push(self, args: list[str]) -> AsyncIterator[str]:
        yield f"# pushing {args[0]} → {args[1]}"
        async for line in self.conn.cli_exec(["storage", "push", args[0], args[1]],
                                             timeout=120):
            yield line

    async def _mkdir(self, args: list[str]) -> AsyncIterator[str]:
        async for line in self.conn.cli_exec(["storage", "mkdir", args[0]]):
            yield line

    async def _reboot(self, args: list[str]) -> AsyncIterator[str]:
        yield "# rebooting Flipper — presence will flip offline briefly"
        async for line in self.conn.cli_exec(["reboot"], timeout=10):
            yield line

    async def _cmd(self, args: list[str]) -> AsyncIterator[str]:
        async for line in self.conn.cli_exec(["cmd", *args], timeout=20):
            yield line

    # ── armed verbs ──────────────────────────────────────────────────────────────
    def _make_rf(self, name: str):
        tokens = _RF_TEMPLATES[name][0]

        async def _run(args: list[str]) -> AsyncIterator[str]:
            yield (f"⚠ firmware-dependent CLI (`{' '.join(tokens)}`) — verify against "
                   f"`@flipper cmd help` or the /sbx flipper shell if it errors.")
            async for line in self.conn.cli_exec(["cmd", *tokens, *args], timeout=30):
                yield line
        return _run

    async def _hid(self, args: list[str]) -> AsyncIterator[str]:
        name = args[0]
        yield ("☢ BadUSB injects keystrokes into WHATEVER MACHINE this Flipper's USB is "
               "plugged into (here: the host) — authorized targets only, never the host.")
        # BadUSB launches via the app loader (firmware-dependent path under /ext/badusb).
        async for line in self.conn.cli_exec(
                ["cmd", "loader", "open", "BadUSB", f"/ext/badusb/{name}"], timeout=15):
            yield line
