"""Device bridge — a headless room member that presents a physical device as an
interactive persona. Structurally a sibling of `cmd_chat/web/emit_sbx.py`'s Broker:
it joins a room like any member, learns the driver ACL, and owns a device surface.
Here the surface is a real device (via an adapter) driven by curated chat commands
rather than raw keystrokes (the persona model — raw-drive is a later phase).

Members address it as `@<persona> <verb> [args]` (also `/dev …` / `/<persona> …`).
Read-only verbs answer any member; ARMED verbs (and `arm`/`disarm`) require an
authorized operator — the room owner (from `_perm:acl` or `--owner`) or a member
granted via the bridge's stdin console (`grant <user>`), mirroring the sandbox ACL.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time

import websockets

from cmd_chat.client.client import MAX_WS_FRAME, Client

from .adapters.pager import PagerAdapter
from .adapters.ssh_conn import SshConn

ADAPTERS = {"pager": PagerAdapter}

MAX_OUT_LINES = 60          # cap a single command's chat reply
ARM_TTL = 120.0            # seconds an armed device stays armed before auto-disarm
PRESENCE_INTERVAL = 45.0   # seconds between health probes


class DeviceBridge:
    def __init__(self, client: Client, adapter, owner: str | None):
        self.client = client
        self.adapter = adapter
        self.owner = owner                 # trusted operator (may be None until acl seen)
        self.drivers: set[str] = set()
        self.ws = None
        self._lock = asyncio.Lock()
        self._online: bool | None = None
        self._disarm_task: asyncio.Task | None = None
        self._pty = None                      # active ssh -tt PTY subprocess (or None)
        self._pty_fd: int | None = None       # its controlling-PTY master fd
        self._pty_task: asyncio.Task | None = None
        self._pty_q: asyncio.Queue = asyncio.Queue()
        self._rows, self._cols = 40, 120      # advertised shell dims (TUI resizes us)

    # ── wire helpers ─────────────────────────────────────────────────────────
    async def post(self, text: str) -> None:
        """Send a plain chat line as this member (never starts with '{\"_', so the
        server + peers treat it as chat, not a control frame)."""
        if self.ws is None:
            return
        raw = self.client.room_fernet.encrypt(text.encode()).decode()
        async with self._lock:
            try:
                await self.ws.send(raw)
            except websockets.ConnectionClosed:
                pass

    async def post_block(self, header: str, lines: list[str]) -> None:
        shown = lines[:MAX_OUT_LINES]
        body = "\n".join(shown) if shown else "(no output)"
        extra = len(lines) - len(shown)
        tail = f"\n…(+{extra} more lines truncated)" if extra > 0 else ""
        await self.post(f"⌂ {header}\n{body}{tail}")

    async def send_frame(self, obj: dict) -> None:
        """Send a room control frame (starts with '{\"_' → peers treat it as _sbx/_perm,
        not chat). Same wire shape a sandbox broker uses."""
        if self.ws is None:
            return
        raw = self.client.room_fernet.encrypt(json.dumps(obj).encode()).decode()
        async with self._lock:
            try:
                await self.ws.send(raw)
            except websockets.ConnectionClosed:
                pass

    # ── authz ────────────────────────────────────────────────────────────────
    def _authorized(self, sender: str) -> bool:
        return sender == self.owner or sender in self.drivers

    # ── /sbx <persona> — raw device shell as an _sbx:data stream ──────────────
    async def _broadcast_acl(self) -> None:
        # The acl `owner` MUST equal the bridge's own room username (the server-stamped
        # sender) — a hardened TUI's parse_perm drops any acl whose owner != sender as a
        # forgery. The BRIDGE is the sandbox owner (it owns the device shell); it grants
        # drive to members via `drivers`. (Human command-authz — arm/push/shell — is a
        # separate concept, self.owner, used only for the @persona verb gating.)
        me = self.adapter.persona
        await self.send_frame({
            "_perm": "acl", "owner": me,
            "drivers": sorted(self.drivers),
            "sudoers": sorted(self.drivers | {me}),
        })

    async def _open_shell(self, sender: str) -> None:
        if self._pty is not None:
            await self.post(f"⌂ {self.adapter.persona} shell already open — /drive to type, "
                            f"`@{self.adapter.persona} shell stop` to close.")
            return
        result = await self.adapter.open_shell(rows=self._rows, cols=self._cols)
        if result is None:
            await self.post(f"✖ {self.adapter.persona} has no shell surface.")
            return
        proc, fd = result
        self._pty, self._pty_fd = proc, fd
        self.drivers.add(sender)              # the summoner drives by default
        await self.post(f"🖥  {self.adapter.persona} SHELL opened by {sender} — it renders "
                        f"in the sandbox pane; granted members /drive to type. "
                        f"`@{self.adapter.persona} shell stop` closes it.")
        # A fd reader fills a queue; a pump task sends _sbx:data in ORDER (so the
        # PTY byte stream never reorders across event-loop turns).
        asyncio.get_event_loop().add_reader(fd, self._on_pty_readable)
        self._pty_task = asyncio.ensure_future(self._pty_pump())
        await self.send_frame({"_sbx": "status", "state": "ready",
                               "backend": self.adapter.persona,
                               "rows": self._rows, "cols": self._cols})
        await self._broadcast_acl()

    def _on_pty_readable(self) -> None:
        try:
            data = os.read(self._pty_fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:                          # EOF — the remote shell exited
            asyncio.ensure_future(self._close_shell("shell exited"))
            return
        self._pty_q.put_nowait(data)

    async def _pty_pump(self) -> None:
        try:
            while True:
                data = await self._pty_q.get()
                await self.send_frame({"_sbx": "data",
                                       "b64": base64.b64encode(data).decode()})
        except asyncio.CancelledError:
            pass

    async def _close_shell(self, sender: str) -> None:
        if self._pty is None:
            await self.post(f"⌂ {self.adapter.persona} has no shell open.")
            return
        fd = self._pty_fd
        if fd is not None:
            try:
                asyncio.get_event_loop().remove_reader(fd)
            except Exception:
                pass
        if self._pty_task:
            self._pty_task.cancel()
        try:
            self._pty.kill()
        except Exception:
            pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        self._pty, self._pty_fd = None, None
        await self.send_frame({"_sbx": "status", "state": "ended",
                               "backend": self.adapter.persona})
        await self.post(f"🖥  {self.adapter.persona} shell closed ({sender}).")

    def _pty_resize(self, rows: int, cols: int) -> None:
        self._rows, self._cols = max(1, rows), max(1, cols)
        if self._pty_fd is not None:
            SshConn.set_winsize(self._pty_fd, self._rows, self._cols)

    async def _pty_write(self, sender: str, data: bytes) -> None:
        """Write approved keystrokes to the device PTY — driver-token gated."""
        if self._pty_fd is None or sender not in self.drivers:
            return
        try:
            os.write(self._pty_fd, data)
        except OSError:
            pass

    # ── presence ─────────────────────────────────────────────────────────────
    async def _presence_loop(self) -> None:
        while True:
            online, detail = await self.adapter.health()
            if online != self._online:
                self._online = online
                mark = "🟢" if online else "🔴"
                state = "online" if online else "offline"
                await self.post(f"{mark} {self.adapter.persona} ({self.adapter.KIND}) "
                                f"{state} — {detail}")
            await asyncio.sleep(PRESENCE_INTERVAL)

    async def _auto_disarm_after(self, ttl: float) -> None:
        try:
            await asyncio.sleep(ttl)
        except asyncio.CancelledError:
            return
        if self.adapter.armed:
            self.adapter.disarm()
            await self.post(f"🔒 {self.adapter.persona} auto-disarmed after {ttl:.0f}s.")

    # ── command surface ──────────────────────────────────────────────────────
    def _menu_text(self) -> str:
        lines = [f"🛰  {self.adapter.persona} — {self.adapter.KIND}. "
                 f"Address me: @{self.adapter.persona} <verb> [args]"]
        for v in self.adapter.menu():
            tag = "  [ARMED]" if v.armed else ""
            lines.append(f"  • {v.name} — {v.help}{tag}")
        lines.append("  • arm / disarm — (authorized) enable/disable ARMED verbs")
        lines.append(f"  • shell [stop] — (authorized) open a raw device shell in the "
                     f"sandbox pane (/sbx {self.adapter.persona}); /drive to type")
        lines.append("  • help — this menu")
        return "\n".join(lines)

    async def _run_command(self, sender: str, rest: str) -> None:
        """Command task wrapper — never let a command exception die silently."""
        try:
            await self._handle_command(sender, rest)
        except Exception as e:      # noqa: BLE001 — surface any failure to the room
            self.client.error(f"[device] command error: {e}")
            try:
                await self.post(f"✖ {self.adapter.persona}: command failed — {e}")
            except Exception:
                pass

    async def _handle_command(self, sender: str, rest: str) -> None:
        parts = rest.split()
        if not parts:
            await self.post(self._menu_text())
            return
        verb, args = parts[0].lower(), parts[1:]

        if verb in ("help", "menu", "?"):
            await self.post(self._menu_text())
            return

        if verb == "arm":
            if not self._authorized(sender):
                await self.post(f"✋ {sender}: only an authorized operator may arm "
                                f"{self.adapter.persona}.")
                return
            self.adapter.arm()
            if self._disarm_task:
                self._disarm_task.cancel()
            self._disarm_task = asyncio.ensure_future(self._auto_disarm_after(ARM_TTL))
            await self.post(f"🔓 {self.adapter.persona} ARMED by {sender} "
                            f"(auto-disarm in {ARM_TTL:.0f}s). ARMED verbs are live.")
            return

        if verb == "disarm":
            if not self._authorized(sender):
                await self.post(f"✋ {sender}: not authorized.")
                return
            self.adapter.disarm()
            if self._disarm_task:
                self._disarm_task.cancel()
            await self.post(f"🔒 {self.adapter.persona} disarmed by {sender}.")
            return

        if verb == "shell":
            # /sbx <persona> raw-drive: owner opens a device PTY into the sandbox pane.
            if not self._authorized(sender):
                await self.post(f"✋ {sender}: only an authorized operator may open the "
                                f"{self.adapter.persona} shell.")
                return
            if self._online is False:
                await self.post(f"🔴 {self.adapter.persona} is offline.")
                return
            if args and args[0] == "stop":
                await self._close_shell(sender)
            else:
                await self._open_shell(sender)
            return

        # Everything else is an adapter verb. Refuse if the device is offline, and
        # re-check authorization for ARMED verbs (the adapter re-asserts arm state
        # too, as defense in depth).
        if self._online is False:
            await self.post(f"🔴 {self.adapter.persona} is offline — command dropped.")
            return
        if self.adapter.is_privileged_verb(verb) and not self._authorized(sender):
            kind = "an ARMED" if self.adapter.is_armed_verb(verb) else "an owner-only"
            await self.post(f"✋ {sender}: `{verb}` is {kind} action — not authorized.")
            return

        self.client.info(f"[device] {sender} → {self.adapter.persona} {verb} {args}")
        # Immediate ack for verbs that reach out to the device (so the room isn't
        # silent while a payload runs / a scan completes), then the result block.
        if verb in ("run", "scan", "push", "pull", "stop"):
            gerund = {"run": "launching payload", "scan": "scanning", "push": "pushing",
                      "pull": "pulling loot", "stop": "stopping"}[verb]
            await self.post(f"⏳ {self.adapter.persona}: {gerund} "
                            f"{' '.join(args)}".rstrip() + " …")
        out: list[str] = []
        async for line in self.adapter.dispatch(verb, args):
            out.append(line)
        header = f"{self.adapter.persona} {verb} {' '.join(args)}".rstrip()
        if verb == "run":
            running = next((ln for ln in out if "@@RUNNING" in ln), None)
            done = any("@@DONE@@" in ln for ln in out)
            out = [ln for ln in out if "@@RUNNING" not in ln and "@@DONE@@" not in ln]
            if running:
                pid = running.split("pid=")[-1].rstrip("@ ").strip()
                tag = (f"▶ RUNNING in background (pid {pid}) — "
                       f"`@{self.adapter.persona} stop {args[0]}` to halt · "
                       f"`@{self.adapter.persona} loot` for results")
            elif done and any("exited (code 0)" in ln for ln in out):
                tag = "✓ COMPLETED (exit 0)"
            elif done:
                tag = "✖ FINISHED (non-zero exit)"
            else:
                tag = "◁ done"
            header = f"{tag} — {header}"
        await self.post_block(header, out)

    # ── loops ────────────────────────────────────────────────────────────────
    async def _chat_recv(self) -> None:
        persona = self.adapter.persona
        prefixes = (f"@{persona}", f"/{persona}", "/dev", f"{persona}:")
        async for raw in self.ws:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if data.get("type") != "message":
                continue
            msg = self.client.decrypt_message(data.get("data", {}))
            sender = msg.get("username", "?")
            text = (msg.get("text") or "").strip()
            if not text or text == "[decrypt failed]" or sender == persona:
                continue
            # Control frames: absorb an ACL (learn owner/drivers) or route keystrokes
            # to the device PTY when a `/sbx <persona>` shell is open.
            if text.startswith('{"_'):
                await self._handle_control(sender, text)
                continue
            for p in prefixes:
                if text == p or text.startswith(p + " "):
                    # Run as a task so a long-running payload never blocks the chat loop
                    # — the bridge stays responsive to `help`/`stop`/other commands.
                    asyncio.ensure_future(self._run_command(sender, text[len(p):].strip()))
                    break

    async def _handle_control(self, sender: str, text: str) -> None:
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            return
        if frame.get("_perm") == "acl":
            owner = frame.get("owner")
            if owner and self.owner is None:
                self.owner = owner
            # Adopt an external drivers list only when WE aren't the shell broker,
            # so a `/sbx <persona>` session stays authoritative over who may type.
            if self._pty is None:
                drivers = frame.get("drivers")
                if isinstance(drivers, list):
                    self.drivers = set(drivers)
            return
        if frame.get("_sbx") == "input" and self._pty is not None:
            b64 = frame.get("b64")
            if b64:
                try:
                    data = base64.b64decode(b64)
                except (ValueError, TypeError):
                    return
                await self._pty_write(sender, data)
            return
        if frame.get("_sbx") == "resize" and self._pty_fd is not None:
            # The driving TUI advertises its real sandbox-pane dims — mirror them to
            # the device PTY (SIGWINCH) so the shell reflows to the same width and
            # nothing wraps/staggers. Any member's resize is harmless (dims only).
            r, c = frame.get("rows"), frame.get("cols")
            if isinstance(r, int) and isinstance(c, int):
                self._pty_resize(r, c)

    async def _console(self) -> None:
        """Operator stdin: grant/revoke a driver, arm/disarm, quit."""
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if line == "":
                return
            parts = line.split()
            if not parts:
                continue
            op = parts[0]
            if op == "grant" and len(parts) >= 2:
                self.drivers.add(parts[1])
                self.client.info(f"[device] drivers={sorted(self.drivers)}")
            elif op == "revoke" and len(parts) >= 2:
                self.drivers.discard(parts[1])
            elif op == "arm":
                self.adapter.arm()
                self.client.info("[device] armed via console")
            elif op == "disarm":
                self.adapter.disarm()


async def _run(args) -> None:
    client = Client(
        server=args.server, port=args.port, username=args.persona,
        password=args.password, insecure=args.insecure, no_tls=args.no_tls,
    )
    client.srp_authenticate()
    adapter = ADAPTERS[args.device](persona=args.persona, alias=args.alias)
    url = f"{client.ws_url}/ws/chat?user_id={client.user_id}&ws_token={client.ws_token}"
    async with websockets.connect(url, ssl=client._ws_ssl_context(),
                                  max_size=MAX_WS_FRAME) as ws:
        bridge = DeviceBridge(client, adapter, owner=args.owner)
        bridge.ws = ws
        client.success(f"device bridge '{args.persona}' online — {adapter.KIND}")
        await bridge.post(bridge._menu_text())
        try:
            await asyncio.gather(
                bridge._chat_recv(),
                bridge._presence_loop(),
                bridge._console(),
            )
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass
        finally:
            if bridge._pty is not None:
                try:
                    bridge._pty.kill()
                except Exception:
                    pass
            if hasattr(adapter, "conn"):
                await adapter.conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="cmd_chat.device",
        description="Bridge a physical device into a hack-house room as a persona member")
    ap.add_argument("server", help="room host")
    ap.add_argument("port", type=int, help="room port")
    ap.add_argument("--persona", default="pager", help="room display name for the device")
    ap.add_argument("--device", default="pager", choices=sorted(ADAPTERS),
                    help="which device adapter to load")
    ap.add_argument("--alias", default="pager", help="ssh alias / transport handle")
    ap.add_argument("--owner", default=None,
                    help="trusted operator username (else learned from the room ACL)")
    ap.add_argument("--password", "-p", default=None, help="room password")
    ap.add_argument("--insecure", "-k", action="store_true", help="skip TLS verify")
    ap.add_argument("--no-tls", action="store_true", help="plain ws/http (local)")
    args = ap.parse_args()
    if args.password is None:
        import getpass
        args.password = getpass.getpass("Room password: ")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\ndevice bridge stopped")


if __name__ == "__main__":
    main()
