"""OperatorBridge — a headless room client a Claude Code session drives.

Inverts the `cmd_chat/agent` model: instead of the room calling a model per
message, this daemon owns the websocket and exposes the room as a local API.
A Claude Code session pumps it through the `hh-bridge` CLI (`read`/`say`/…),
which talks to a unix control socket served in this same event loop.

Phase 1 scope: join + roster + read (with long-poll) + say. Sandbox drive,
delegation and nesting are later phases — `_perm:acl`/`_sbx:status` frames are
*recorded* into the inbox for awareness but not acted on yet.

Reuses the protocol wholesale from `Client` (SRP, room key, encrypt/decrypt)
and mirrors `AgentBridge`'s reconnect/serve shape (cmd_chat/agent/bridge.py).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time

import websockets

from ..client.client import Client
from .session import Session


class OperatorBridge(Client):
    _PING_INTERVAL = 20.0
    _PING_TIMEOUT = 60.0
    _RECONNECT_MAX_BACKOFF = 30.0
    _RECONNECT_STABLE_SECS = 30.0
    MAX_EVENTS = 2000  # in-RAM inbox ring; full history lives in inbox.jsonl

    def __init__(self, server: str, port: int, name: str,
                 password: str | None = None, insecure: bool = False,
                 no_tls: bool = False, session: str | None = None,
                 trigger: str | None = None):
        super().__init__(server, port, username=name, password=password,
                         insecure=insecure, no_tls=no_tls)
        self.name = name
        self.trigger = (trigger or "").strip()
        self.session = Session(session or name)

        # Inbox: monotonically-seq'd events + an async condition to wake waiters.
        self.events: list[dict] = []
        self.seq = 0
        self.cond = asyncio.Condition()

        # Mirrored room awareness (recorded only in Phase 1).
        self.granted = False
        self.can_sudo = False
        self.sbx_engine: str | None = None
        self.sbx_name: str = ""

        # Live websocket (None while reconnecting); set in _connect_and_serve.
        self._ws = None
        self._stop = asyncio.Event()
        self._seeded = False         # backfill room history only on first init
        self._inbox_fp = None        # append handle to inbox.jsonl
        self._addr_re = self._build_addr_re()

    # ── addressing ───────────────────────────────────────────────────────
    def _build_addr_re(self) -> re.Pattern:
        toks = [re.escape(self.name), re.escape("@" + self.name)]
        if self.trigger:
            toks.append(re.escape(self.trigger))
        return re.compile(r"(?<!\w)(" + "|".join(toks) + r")(?!\w)", re.IGNORECASE)

    def _is_addressed(self, text: str) -> bool:
        return bool(self._addr_re.search(text or ""))

    # ── inbox ────────────────────────────────────────────────────────────
    async def _emit(self, kind: str, **fields) -> None:
        async with self.cond:
            self.seq += 1
            ev = {"seq": self.seq, "ts": round(time.time(), 3), "kind": kind, **fields}
            self.events.append(ev)
            if len(self.events) > self.MAX_EVENTS:
                self.events = self.events[-self.MAX_EVENTS:]
            if self._inbox_fp is not None:
                try:
                    self._inbox_fp.write(json.dumps(ev) + "\n")
                    self._inbox_fp.flush()
                except OSError:
                    pass
            self.cond.notify_all()

    # ── frame handling ───────────────────────────────────────────────────
    async def _handle_frame(self, ws, raw) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        mtype = data.get("type")
        if mtype == "init":
            self.users = data.get("users", [])
            await self._emit("roster", users=self._roster())
            if not self._seeded:
                for m in data.get("messages", []):
                    dec = self.decrypt_message(dict(m))
                    text = dec.get("text", "")
                    sender = dec.get("username", "?")
                    if (not text or text == "[decrypt failed]"
                            or text.startswith('{"_') or sender == self.name):
                        continue
                    await self._emit("message", **{"from": sender}, text=text,
                                     addressed=False, backfill=True)
                self._seeded = True
            return
        if mtype == "roster":
            self.users = data.get("users", [])
            await self._emit("roster", users=self._roster())
            return
        if mtype == "user_left":
            left = data.get("user_id")
            self.users = [u for u in self.users if u.get("user_id") != left]
            await self._emit("roster", users=self._roster())
            return
        if mtype != "message":
            return
        msg = self.decrypt_message(data.get("data", {}))
        text = msg.get("text", "")
        sender = msg.get("username", "?")
        if sender == self.name:
            return
        if text.startswith('{"_'):
            await self._record_control(text)
            return
        await self._emit("message", **{"from": sender}, text=text,
                         addressed=self._is_addressed(text))

    async def _record_control(self, text: str) -> None:
        """Surface ACL + sandbox-status changes into the inbox (awareness only).
        Ignore high-volume / irrelevant control frames (_sbx:data, _ai, _ft)."""
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            return
        if frame.get("_sbx") == "status":
            ready = frame.get("state") == "ready"
            self.sbx_engine = frame.get("engine") if ready else None
            self.sbx_name = (frame.get("name") or "") if ready else ""
            await self._emit("sandbox", state=frame.get("state"),
                             engine=self.sbx_engine, name=self.sbx_name,
                             backend=frame.get("backend"))
        elif frame.get("_perm") == "acl":
            self.granted = self.name in frame.get("drivers", [])
            self.can_sudo = self.name in frame.get("sudoers", [])
            await self._emit("acl", owner=frame.get("owner"),
                             granted=self.granted, can_sudo=self.can_sudo,
                             drivers=frame.get("drivers", []))

    def _roster(self) -> list[str]:
        return [u.get("username", "?") for u in self.users]

    # ── connection lifecycle (mirrors AgentBridge) ───────────────────────
    async def run_async(self) -> None:
        self.session.ensure_dir()
        self.session.cleanup()  # drop a stale socket from a prior run
        self._inbox_fp = open(self.session.inbox_path, "a")  # noqa: SIM115
        server = await asyncio.start_unix_server(
            self._serve_control, path=str(self.session.sock_path))
        try:
            os.chmod(self.session.sock_path, 0o600)
        except OSError:
            pass
        await self._emit("system", event="starting",
                         host=self.server, port=self.port, user=self.name)
        try:
            await self._reconnect_loop()
        finally:
            server.close()
            try:
                await server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            if self._inbox_fp is not None:
                self._inbox_fp.close()
            self.session.cleanup()

    async def _reconnect_loop(self) -> None:
        backoff = 1.0
        first = True
        while not self._stop.is_set():
            loop = asyncio.get_running_loop()
            started = loop.time()
            try:
                await self._connect_and_serve(reconnect=not first)
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except (websockets.ConnectionClosed, OSError) as e:
                await self._emit("system", event="reconnecting",
                                 reason=type(e).__name__)
            except Exception as e:  # noqa: BLE001 — auth/transient: report + retry
                await self._emit("system", event="error",
                                 reason=f"{type(e).__name__}: {e}")
            else:
                if not self._stop.is_set():
                    await self._emit("system", event="reconnecting",
                                     reason="closed")
            if self._stop.is_set():
                break
            if loop.time() - started >= self._RECONNECT_STABLE_SECS:
                backoff = 1.0
            first = False
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self._RECONNECT_MAX_BACKOFF)

    async def _connect_and_serve(self, reconnect: bool) -> None:
        self.srp_authenticate()  # fresh token each attempt; old one died on drop
        url = f"{self.ws_url}/ws/chat?user_id={self.user_id}&ws_token={self.ws_token}"
        async with websockets.connect(
            url, ssl=self._ws_ssl_context(),
            ping_interval=self._PING_INTERVAL, ping_timeout=self._PING_TIMEOUT,
        ) as ws:
            self._ws = ws
            self.running = True
            announce = (f"{self.name} (operator) "
                        f"{'back online' if reconnect else 'online'} — Claude Code bridge")
            await ws.send(self.room_fernet.encrypt(announce.encode()).decode())
            await self._emit("system", event="connected", reconnect=reconnect)
            try:
                await self._serve(ws)
            finally:
                self._ws = None
                self.running = False

    async def _serve(self, ws) -> None:
        async for raw in ws:
            if not self.running:
                break
            try:
                await self._handle_frame(ws, raw)
            except (websockets.ConnectionClosed, asyncio.CancelledError):
                raise
            except Exception as e:  # noqa: BLE001 — one bad frame mustn't drop us
                await self._emit("system", event="frame_error",
                                 reason=f"{type(e).__name__}: {e}")

    # ── control socket (the CLI talks here) ──────────────────────────────
    async def _serve_control(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                req = json.loads(line.decode())
            except json.JSONDecodeError:
                resp = {"ok": False, "error": "bad json"}
            else:
                resp = await self._dispatch(req)
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _dispatch(self, req: dict) -> dict:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "pong": True, "name": self.name,
                    "connected": self._ws is not None}
        if op == "status":
            return {"ok": True, "name": self.name,
                    "connected": self._ws is not None,
                    "granted": self.granted, "can_sudo": self.can_sudo,
                    "sbx_engine": self.sbx_engine, "sbx_name": self.sbx_name,
                    "users": self._roster(), "seq": self.seq,
                    "host": self.server, "port": self.port}
        if op == "roster":
            return {"ok": True, "users": self._roster()}
        if op == "read":
            return await self._op_read(req)
        if op == "say":
            return await self._op_say(req)
        if op == "down":
            asyncio.get_running_loop().call_soon(self._begin_shutdown)
            return {"ok": True, "stopping": True}
        return {"ok": False, "error": f"unknown op {op!r}"}

    async def _op_read(self, req: dict) -> dict:
        since = int(req.get("since", 0))
        wait = bool(req.get("wait", False))
        timeout = float(req.get("timeout", 30.0))
        async with self.cond:
            new = [e for e in self.events if e["seq"] > since]
            if not new and wait:
                try:
                    await asyncio.wait_for(self.cond.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
                new = [e for e in self.events if e["seq"] > since]
        return {"ok": True, "events": new, "seq": self.seq}

    async def _op_say(self, req: dict) -> dict:
        text = str(req.get("text", ""))
        if not text:
            return {"ok": False, "error": "empty text"}
        if self._ws is None:
            return {"ok": False, "error": "not connected to room"}
        try:
            await self._ws.send(self.room_fernet.encrypt(text.encode()).decode())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"send failed: {type(e).__name__}: {e}"}
        await self._emit("sent", **{"from": self.name}, text=text, addressed=False)
        return {"ok": True}

    def _begin_shutdown(self) -> None:
        self._stop.set()
        self.running = False
        if self._ws is not None:
            asyncio.create_task(self._ws.close())
