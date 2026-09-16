"""P0 dev helper (throwaway) — a headless room member that emits fake sandbox
PTY output so the web-relay pipe can be verified end-to-end **without** building
the Rust `hh` host.

It joins a hack-house room exactly like any member (SRP → room key → chat WS),
then periodically sends the same decrypted control frames a real sandbox broker
would put on the wire:

    {"_sbx":"status","state":"ready","backend":"dev-emitter","rows":24,"cols":80}
    {"_sbx":"data","b64":<base64 of raw PTY bytes>}   # repeated

Each frame is Fernet-encrypted under the room key before it hits the socket, so
the server only ever brokers ciphertext — identical to a real member. The web
publisher (`cmd_chat.web.publisher`), another member in the same room, decrypts
these `_sbx:data` frames and republishes them to the relay.

This is a verification aid for P0, not production code. Delete once a real Rust
host is available to source `_sbx:data`.

Example
-------
    python -m cmd_chat.web.emit_sbx 127.0.0.1 3000 -p hunter2 --no-tls
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time

import websockets

from cmd_chat.client.client import MAX_WS_FRAME, Client


class Broker:
    """The throwaway stand-in's shared state + wire helpers. In P2 the emitter
    plays a *minimal broker* so the driver-token ACL can be exercised without the
    Rust host: it publishes `_perm:acl`, and only echoes `_sbx:input` from members
    in its `drivers` set (mirroring hh/src/app.rs: `if drivers.contains(&from)`)."""

    def __init__(self, client: Client, owner: str):
        self.client = client
        self.owner = owner
        self.drivers: set[str] = set()   # read-only default: nobody drives yet
        self.sudoers: set[str] = {owner}
        self._lock = asyncio.Lock()      # serialize concurrent sends on one socket
        self.ws = None

    async def send(self, obj: dict) -> None:
        """Fernet-encrypt one control frame with the room key — the exact wire shape
        a real member sends (server broadcasts it as {type:message,data:{text}})."""
        if self.ws is None:
            return
        raw = self.client.room_fernet.encrypt(json.dumps(obj).encode()).decode()
        async with self._lock:
            await self.ws.send(raw)

    async def broadcast_acl(self) -> None:
        """Publish the driver-token ACL exactly like the broker (spec §6, hh app.rs
        `_perm:acl`). The web publisher keys Gate A off its membership in `drivers`."""
        await self.send({
            "_perm": "acl", "owner": self.owner,
            "drivers": sorted(self.drivers), "sudoers": sorted(self.sudoers),
        })
        self.client.info(f"[broker] acl drivers={sorted(self.drivers)}")


async def _emit(broker: Broker, cols: int, rows: int, interval: float) -> None:
    # Announce a ready sandbox first so viewers learn the dims (mirrors the
    # broker's `_sbx:status`), then stream fake PTY output forever.
    await broker.send({
        "_sbx": "status", "state": "ready", "backend": "dev-emitter",
        "rows": rows, "cols": cols,
    })
    # A clear-screen + home so xterm starts from a known state.
    await broker.send({"_sbx": "data", "b64": base64.b64encode(b"\x1b[2J\x1b[H").decode()})
    # Publish the initial (empty) driver ACL — read-only until an operator grants.
    await broker.broadcast_acl()
    n = 0
    while True:
        n += 1
        # Exercise raw bytes *and* ANSI escapes so xterm.js re-runs the escape
        # codes, not just prints text. Colour + carriage return + counter.
        payload = (
            f"\x1b[32m[dev-emitter]\x1b[0m frame {n:04d}  "
            f"{time.strftime('%H:%M:%S')}  "
            f"\x1b[36mthe pipe is alive\x1b[0m\r\n"
        ).encode()
        await broker.send({"_sbx": "data", "b64": base64.b64encode(payload).decode()})
        await asyncio.sleep(interval)


async def _broker_recv(broker: Broker) -> None:
    """Broker-side input gate: only echo `_sbx:input` from members in `drivers`
    (the driver-token ACL). The echoed bytes become `_sbx:data`, so a browser's
    approved keystrokes are observable end-to-end without a real PTY."""
    client = broker.client
    async for raw in broker.ws:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if data.get("type") != "message":
            continue
        msg = client.decrypt_message(data.get("data", {}))
        sender = msg.get("username")
        text = msg.get("text", "")
        if not text or text == "[decrypt failed]" or not text.startswith('{"_'):
            continue
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            continue
        if frame.get("_sbx") != "input":
            continue
        # THE driver-token check — drop input from anyone not holding drive.
        if sender not in broker.drivers:
            continue
        b64 = frame.get("b64")
        if not b64:
            continue
        try:
            kbytes = base64.b64decode(b64)
        except (ValueError, TypeError):
            continue
        await broker.send({"_sbx": "data", "b64": base64.b64encode(kbytes).decode()})


async def _console(broker: Broker) -> None:
    """stdin control to drive the ACL for verification: `grant <user>` /
    `grab` (force-grab → only owner drives) / `revoke <user>`."""
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
            broker.drivers.add(parts[1])
            await broker.broadcast_acl()
        elif op == "grab":
            # TUI force-grab: drive collapses to the owner alone (hh app.rs).
            broker.drivers = {broker.owner}
            await broker.broadcast_acl()
        elif op == "revoke" and len(parts) >= 2:
            broker.drivers.discard(parts[1])
            await broker.broadcast_acl()
        elif op == "flood":
            # Emit a burst of PTY output fast (P3 backpressure test): proves a slow
            # viewer's backlog is dropped-to-latest without stalling the others.
            n = int(parts[1]) if len(parts) >= 2 else 4000
            for i in range(n):
                payload = (f"\x1b[31mFLOOD {i:06d}\x1b[0m "
                           + "y" * 4000 + "\r\n").encode()
                await broker.send({"_sbx": "data",
                                   "b64": base64.b64encode(payload).decode()})
                if i % 100 == 0:
                    await asyncio.sleep(0)  # yield so sends actually flush


async def _run(args) -> None:
    client = Client(
        server=args.server, port=args.port, username=args.name,
        password=args.password, insecure=args.insecure, no_tls=args.no_tls,
    )
    client.srp_authenticate()
    url = f"{client.ws_url}/ws/chat?user_id={client.user_id}&ws_token={client.ws_token}"
    async with websockets.connect(url, ssl=client._ws_ssl_context(),
                                  max_size=MAX_WS_FRAME) as ws:
        broker = Broker(client, owner=args.name)
        broker.ws = ws
        client.success(f"emitter/broker '{args.name}' online — _sbx:data + driver ACL")
        try:
            await asyncio.gather(
                _emit(broker, args.cols, args.rows, args.interval),
                _broker_recv(broker),
                _console(broker),
            )
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="cmd_chat.web.emit_sbx",
        description="P0 dev helper: emit fake sandbox PTY frames into a hh room",
    )
    ap.add_argument("server", help="room host")
    ap.add_argument("port", type=int, help="room port")
    ap.add_argument("--name", default="dev-emitter", help="room display name")
    ap.add_argument("--password", "-p", default=None, help="room password")
    ap.add_argument("--cols", type=int, default=80)
    ap.add_argument("--rows", type=int, default=24)
    ap.add_argument("--interval", type=float, default=1.0,
                    help="seconds between fake frames (default %(default)s)")
    ap.add_argument("--insecure", "-k", action="store_true",
                    help="skip TLS cert verification (self-signed)")
    ap.add_argument("--no-tls", action="store_true", help="plain ws/http (local)")
    args = ap.parse_args()
    if args.password is None:
        import getpass
        args.password = getpass.getpass("Room password: ")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nemitter stopped")


if __name__ == "__main__":
    main()
