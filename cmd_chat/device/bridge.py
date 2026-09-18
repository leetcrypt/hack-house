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
import json
import sys
import time

import websockets

from cmd_chat.client.client import MAX_WS_FRAME, Client

from .adapters.pager import PagerAdapter

ADAPTERS = {"pager": PagerAdapter}

MAX_OUT_LINES = 40          # cap a single command's chat reply
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

    # ── authz ────────────────────────────────────────────────────────────────
    def _authorized(self, sender: str) -> bool:
        return sender == self.owner or sender in self.drivers

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
        lines.append("  • help — this menu")
        return "\n".join(lines)

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
        out: list[str] = []
        async for line in self.adapter.dispatch(verb, args):
            out.append(line)
        await self.post_block(f"{self.adapter.persona} {verb} {' '.join(args)}".rstrip(), out)

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
            # Learn the room owner / drivers from the sandbox ACL if one is broadcast.
            if text.startswith('{"_'):
                self._maybe_absorb_acl(text)
                continue
            for p in prefixes:
                if text == p or text.startswith(p + " "):
                    await self._handle_command(sender, text[len(p):].strip())
                    break

    def _maybe_absorb_acl(self, text: str) -> None:
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            return
        if frame.get("_perm") != "acl":
            return
        owner = frame.get("owner")
        if owner and self.owner is None:
            self.owner = owner
        drivers = frame.get("drivers")
        if isinstance(drivers, list):
            self.drivers = set(drivers)

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
