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
from . import bootstrap as boot
from . import sandbox as sbx
from .session import Session


class OperatorBridge(Client):
    _PING_INTERVAL = 20.0
    _PING_TIMEOUT = 60.0
    _RECONNECT_MAX_BACKOFF = 30.0
    _RECONNECT_STABLE_SECS = 30.0
    MAX_EVENTS = 2000  # in-RAM inbox ring; full history lives in inbox.jsonl
    TERM_CAP = 65536   # rolling relayed-PTY buffer (bytes) kept for `screen`

    def __init__(self, server: str, port: int, name: str,
                 password: str | None = None, insecure: bool = False,
                 no_tls: bool = False, session: str | None = None,
                 trigger: str | None = None, budget: boot.Budget | None = None):
        super().__init__(server, port, username=name, password=password,
                         insecure=insecure, no_tls=no_tls)
        self.name = name
        self.trigger = (trigger or "").strip()
        self.session = Session(session or name)
        # Recursion budget this operator may spend spawning nested operators.
        self.budget = budget or boot.Budget()

        # Inbox: monotonically-seq'd events + an async condition to wake waiters.
        self.events: list[dict] = []
        self.seq = 0
        self.cond = asyncio.Condition()

        # Mirrored room awareness (recorded only in Phase 1).
        self.granted = False
        self.can_sudo = False
        self.sbx_engine: str | None = None
        self.sbx_name: str = ""

        # Co-located sandbox the operator launched itself (Phase 2). When set we
        # exec straight into it (out-of-band); independent of the room's broker
        # sandbox, which we can also drive when co-located + granted.
        self._own_engine: str | None = None
        self._own_name: str = ""

        # Relay terminal buffer (Phase 6): rolling raw PTY bytes the broker
        # relays as `_sbx:data`. Lets the operator *read* a remote/broker-owned
        # sandbox it drives via `keys` — the output half of relay mode.
        self._term = bytearray()
        self._term_seq = 0  # bumps on each chunk so `watch` can poll for growth

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

    def _absorb_term(self, b64: str) -> None:
        """Append a relayed PTY chunk to the rolling terminal buffer (capped).
        High-volume, so we don't emit an inbox event per chunk — `watch`/`screen`
        poll `_term_seq` instead."""
        if not b64:
            return
        try:
            chunk = base64.b64decode(b64)
        except (ValueError, TypeError):
            return
        self._term += chunk
        if len(self._term) > self.TERM_CAP:
            del self._term[:-self.TERM_CAP]
        self._term_seq += 1

    def _screen_text(self, tail: int | None, ansi: bool) -> str:
        raw = bytes(self._term[-tail:] if tail else self._term)
        s = raw.decode(errors="replace")
        return s if ansi else sbx.strip_ansi(s)

    async def _record_control(self, text: str) -> None:
        """Surface ACL + sandbox-status changes into the inbox (awareness only)
        and absorb relayed PTY output (`_sbx:data`) into the terminal buffer.
        Ignore other high-volume control frames (_ai, _ft)."""
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            return
        if frame.get("_sbx") == "data":
            self._absorb_term(frame.get("b64", ""))
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
            # The daemon's own co-located container is scoped to the daemon: if
            # we're going away it has no owner left, so tear it down rather than
            # leave it running `sleep infinity` until someone notices. A SIGKILL
            # skips this — `sandbox.sweep_stale` reclaims those on next launch.
            if self._own_engine:
                await sbx.teardown_container(self._own_engine, self._own_name)
                self._own_engine, self._own_name = None, ""
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
        if op == "sbx":
            return await self._op_sbx(req)
        if op == "exec":
            return await self._op_exec(req)
        if op == "write":
            return await self._op_write(req)
        if op == "get":
            return await self._op_get(req)
        if op == "keys":
            return await self._op_keys(req)
        if op == "spawn":
            return await self._op_spawn(req)
        if op == "manifest":
            return await self._op_manifest(req)
        if op == "screen":
            return await self._op_screen(req)
        if op == "watch":
            return await self._op_watch(req)
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

    # ── sandbox drive (Phase 2) ──────────────────────────────────────────
    def _target(self) -> tuple[str | None, str, str | None]:
        """Pick the sandbox to exec into and report why.

        Returns ``(engine, name, error)``. Prefer the operator's *own* container
        (always ours to drive). Else, if the room granted us drive AND its broker
        sandbox is a kind we can exec directly (we're co-located on this host),
        target that. ``local`` is refused here — execing on the host is opt-in
        only via our own `local` launch, never inherited from the room."""
        if self._own_engine:
            return self._own_engine, self._own_name, None
        if self.granted and self.sbx_engine in ("docker", "podman", "multipass") \
                and self.sbx_name:
            return self.sbx_engine, self.sbx_name, None
        if self.sbx_engine and not self.granted:
            return None, "", "not a granted driver (owner must `/grant`)"
        return None, "", "no sandbox — `sbx launch` one or join a room with one"

    async def _op_sbx(self, req: dict) -> dict:
        """Manage the operator's own co-located container: launch / status / down."""
        action = req.get("action", "status")
        if action == "launch":
            engine = req.get("engine") or sbx.pick_engine()
            if not engine:
                return {"ok": False, "error": "no container engine (need podman or docker)"}
            image = req.get("image") or sbx.default_image(engine)
            name = req.get("name") or f"hh-op-{self.name}"
            ok, msg = await sbx.launch_container(engine, name, image,
                                                 egress=req.get("egress"),
                                                 harden=req.get("harden"))
            if ok:
                self._own_engine, self._own_name = engine, name
                await self._emit("sandbox", state="own-ready", engine=engine,
                                 name=name, image=image)
            return {"ok": ok, "engine": engine, "name": name, "image": image,
                    "message": msg}
        if action == "down":
            if not self._own_engine:
                return {"ok": False, "error": "no own sandbox to tear down"}
            ok, msg = await sbx.teardown_container(self._own_engine, self._own_name)
            if ok:
                await self._emit("sandbox", state="own-down",
                                 engine=self._own_engine, name=self._own_name)
                self._own_engine, self._own_name = None, ""
            return {"ok": ok, "message": msg}
        # status
        engine, name, err = self._target()
        return {"ok": True, "target_engine": engine, "target_name": name,
                "reason": err, "own_engine": self._own_engine,
                "own_name": self._own_name, "room_engine": self.sbx_engine,
                "room_name": self.sbx_name, "granted": self.granted}

    async def _op_exec(self, req: dict) -> dict:
        """Run a shell command in the target sandbox, capture combined output +
        exit code. The command runs under `sh -c` (a real shell, intended) inside
        the container — the container is the blast radius."""
        cmd = str(req.get("cmd", ""))
        if not cmd:
            return {"ok": False, "error": "empty cmd"}
        engine, name, err = self._target()
        if err:
            return {"ok": False, "error": err}
        prefix = sbx.exec_prefix(engine, name)
        if prefix is None:
            return {"ok": False, "error": f"can't address {engine}/{name}"}
        out, rc = await sbx.exec_capture(prefix + ["sh", "-c", cmd])
        await self._emit("exec", engine=engine, name=name, cmd=cmd, rc=rc)
        return {"ok": rc == 0, "rc": rc, "output": out, "engine": engine, "name": name}

    async def _op_write(self, req: dict) -> dict:
        """Write a file in the target sandbox. Content arrives on stdin (never
        shell-parsed); the path is a positional arg. `mkdir -p` the parent first
        so an absolute path into a new dir works. Mirrors the native harness."""
        path = str(req.get("path", "")).strip()
        if not path:
            return {"ok": False, "error": "missing path"}
        content = req.get("content", "")
        data = content.encode() if isinstance(content, str) else bytes(content)
        engine, name, err = self._target()
        if err:
            return {"ok": False, "error": err}
        prefix = sbx.exec_prefix(engine, name)
        if prefix is None:
            return {"ok": False, "error": f"can't address {engine}/{name}"}
        out, rc = await sbx.exec_capture(
            prefix + ["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"',
                      "hh-write", path],
            stdin=data)
        await self._emit("write", engine=engine, name=name, path=path, rc=rc)
        return {"ok": rc == 0, "rc": rc, "path": path, "bytes": len(data),
                "output": out if (out or "").strip() else ""}

    async def _op_get(self, req: dict) -> dict:
        """Read a file out of the target sandbox. Returns base64 (binary-safe);
        the content is plumbing, so it's uncapped (unlike exec output)."""
        path = str(req.get("path", "")).strip()
        if not path:
            return {"ok": False, "error": "missing path"}
        engine, name, err = self._target()
        if err:
            return {"ok": False, "error": err}
        prefix = sbx.exec_prefix(engine, name)
        if prefix is None:
            return {"ok": False, "error": f"can't address {engine}/{name}"}
        raw, rc = await sbx.exec_capture(prefix + ["cat", "--", path], text=False)
        if rc != 0:
            return {"ok": False, "rc": rc,
                    "error": f"read failed (exit={rc}): {raw.decode(errors='replace')[:200]}"}
        return {"ok": True, "rc": 0, "path": path,
                "b64": base64.b64encode(raw).decode(), "bytes": len(raw)}

    async def _op_keys(self, req: dict) -> dict:
        """Relay raw keystrokes into the room's *shared* PTY — the same
        `_sbx:input` frame a human driver's terminal emits. The broker writes our
        bytes to the shared shell iff we're a granted driver, so this is how the
        operator drives a broker-owned sandbox (and tests interactive programs:
        ctrl-c to stop, q to quit a pager, esc to leave vim). See
        `sandbox.KEYS_HELP` for the byte vocabulary."""
        if self._ws is None:
            return {"ok": False, "error": "not connected to room"}
        if not self.granted:
            return {"ok": False, "error": "not a granted driver — keystrokes inert until `/grant`"}
        spec = req.get("keys")
        if spec in (None, "", []):
            return {"ok": False, "error": "no keys", "help": sbx.KEYS_HELP}
        try:
            data = sbx.encode_keys(spec)
        except ValueError as e:
            return {"ok": False, "error": f"bad key spec: {e}"}
        frame = json.dumps({"_sbx": "input", "b64": base64.b64encode(data).decode()})
        try:
            await self._ws.send(self.room_fernet.encrypt(frame.encode()).decode())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"send failed: {type(e).__name__}: {e}"}
        await self._emit("keys", bytes=len(data))
        return {"ok": True, "bytes": len(data)}

    # ── relay read + stop-condition engine (Phase 6) ─────────────────────
    async def _op_screen(self, req: dict) -> dict:
        """Return the current relayed terminal buffer — the output half of relay
        mode (drive with `keys`, read with `screen`). ANSI-stripped by default;
        `tail` bounds the bytes returned."""
        tail = req.get("tail")
        tail = int(tail) if tail else None
        ansi = bool(req.get("ansi", False))
        return {"ok": True, "text": self._screen_text(tail, ansi),
                "bytes": len(self._term), "seq": self._term_seq}

    async def _op_watch(self, req: dict) -> dict:
        """Block until a stop condition fires, then report which one. Formalizes
        the `/loop` autonomy stops so a driver can `keys` then wait for a result.

        Conditions (first to fire wins):
        - ``for`` regex matched in the chosen source ``in`` = "events" (default,
          matches event text) or "screen" (matches relayed terminal output);
        - ``idle`` seconds with no new event/output (quiescence);
        - ``timeout`` seconds elapsed (the hard ceiling).
        """
        pattern = req.get("for")
        source = req.get("in", "events")
        timeout = float(req.get("timeout", 30.0))
        idle = req.get("idle")
        idle = float(idle) if idle is not None else None
        match_since = int(req.get("since", self.seq))  # fixed scan cursor
        try:
            rx = re.compile(pattern) if pattern else None
        except re.error as e:
            return {"ok": False, "error": f"bad regex: {e}"}

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_change = loop.time()
        prev_seq, prev_term = self.seq, self._term_seq
        while True:
            if rx and source == "screen":
                m = rx.search(self._screen_text(None, False))
                if m:
                    return {"ok": True, "reason": "match", "where": "screen",
                            "match": m.group(0), "seq": self.seq}
            if rx and source == "events":
                for e in self.events:
                    if e["seq"] <= match_since:
                        continue
                    if rx.search(str(e.get("text", "")) or json.dumps(e)):
                        return {"ok": True, "reason": "match", "where": "events",
                                "event": e, "seq": self.seq}
            now = loop.time()
            if self.seq != prev_seq or self._term_seq != prev_term:
                last_change, prev_seq, prev_term = now, self.seq, self._term_seq
            if idle is not None and now - last_change >= idle:
                return {"ok": True, "reason": "idle", "idle": idle, "seq": self.seq}
            if now >= deadline:
                return {"ok": True, "reason": "timeout", "matched": False,
                        "seq": self.seq}
            await asyncio.sleep(min(0.3, max(0.0, deadline - now)))

    # ── recursive spawn (Phase 5) ────────────────────────────────────────
    async def _op_spawn(self, req: dict) -> dict:
        """Stand up a nested Claude Code operator against another room.

        Guardrails, in order:
        - **budget** — refuse unless this node may still spawn (depth>0 &
          fanout>0); the child inherits a decremented budget, we record one spent
          slot. This is the hard stop against a runaway tree.
        - **creds gating** — we carry our own credentials to the child *only* when
          `allow_creds` is explicitly set; otherwise the child authenticates
          itself. Never implicit, never to a system we don't own.
        - **dry_run** — default returns the full plan (argv, budget, creds
          decision) and launches nothing, so a tree can be inspected before it's
          grown.

        `target` is "host" (Popen detached here) — sandbox/VM placement is the
        operator's to run via `exec` using the returned plan."""
        objective = str(req.get("objective", "")).strip()
        if not objective:
            return {"ok": False, "error": "spawn needs an objective"}
        room = req.get("room") or {}
        if not room.get("host") or not room.get("port"):
            return {"ok": False, "error": "spawn needs room {host, port, name}"}

        if not self.budget.can_spawn():
            return {"ok": False, "error": "recursion budget exhausted "
                    f"(depth={self.budget.depth}, fanout={self.budget.fanout})"}
        try:
            child_budget = self.budget.descend()
        except boot.BudgetExhausted as e:
            return {"ok": False, "error": str(e)}

        allow_creds = bool(req.get("allow_creds", False))
        dry_run = bool(req.get("dry_run", True))
        skip_perms = bool(req.get("skip_permissions", False))
        config_dir = req.get("config_dir")
        try:
            runner = boot.get_runner(req.get("runner"))
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        stop = req.get("stop") or None
        directive = boot.compose_directive(objective, room, child_budget,
                                            stop=stop, runner=runner)
        argv = boot.build_run_argv(directive, skip_permissions=skip_perms,
                                   config_dir=config_dir, runner=runner)
        creds = boot.plan_creds(config_dir or "~", allow=allow_creds, runner=runner)
        present = boot.runner_present(runner)
        plan = {"argv": argv, "budget": child_budget.as_dict(), "creds": creds,
                "target": req.get("target", "host"),
                "runner": runner.name, "runner_present": present}

        if dry_run:
            return {"ok": True, "dry_run": True, "plan": plan}

        if not present:
            return {"ok": False, "error": f"{runner.name} CLI not installed",
                    "install_plan": boot.install_plan(runner=runner), "plan": plan}

        # Carry creds only behind the explicit gate.
        if creds.get("staged"):
            try:
                dest = boot.Path(creds["dest"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(boot.Path(creds["src"]).read_bytes())
                dest.chmod(0o600)
            except OSError as e:
                return {"ok": False, "error": f"creds staging failed: {e}"}

        # Launch detached so the child outlives this daemon; logs to the session.
        import subprocess
        try:
            logf = open(self.session.dir / f"spawn-{self.seq + 1}.log", "a")  # noqa: SIM115
            proc = subprocess.Popen(
                argv, env=boot.child_env(config_dir, runner=runner),
                stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                start_new_session=True, close_fds=True)
        except OSError as e:
            return {"ok": False, "error": f"spawn launch failed: {e}"}

        self.budget = self.budget.spent()  # consume one child slot here
        await self._emit("spawn", objective=objective, pid=proc.pid,
                         budget=child_budget.as_dict(),
                         creds_staged=creds.get("staged", False))
        return {"ok": True, "pid": proc.pid, "plan": plan,
                "remaining": self.budget.as_dict()}

    # ── agent-manifest: make a sandbox carry its own handoff record ──────────
    async def _op_manifest(self, req: dict) -> dict:
        """Read/write the `.hh-agent/` manifest bundle *inside* the target
        sandbox, so a VM travels with a self-describing handoff record.

        Actions:
        - ``push`` — build a bundle from the given fields and write all five
          files under ``<root>/.hh-agent`` in the sandbox. This is what makes a
          VM tradeable: the next agent reads AGENT.md and knows the purpose.
        - ``pull`` — cat the canonical ``manifest.yaml`` back out, parsed, so a
          handoff/spawn can resume exactly where the last one left off.
        - ``update`` — pull, apply a state change (status/progress/done/todo/
          blockers + a provenance entry), and re-push the rendered bundle.

        All manifest logic lives in ``manifest.py``; the bridge only moves bytes
        in and out of the sandbox via the same exec path as `write`/`get`."""
        from . import manifest as mf

        action = req.get("action", "push")
        root = str(req.get("root", "/root")).rstrip("/") or "/"
        bdir = f"{root}/{mf.MANIFEST_DIR}"
        engine, name, err = self._target()
        if err:
            return {"ok": False, "error": err}
        prefix = sbx.exec_prefix(engine, name)
        if prefix is None:
            return {"ok": False, "error": f"can't address {engine}/{name}"}

        async def _put(path: str, data: bytes):
            return await sbx.exec_capture(
                prefix + ["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"',
                          "hh-manifest", path], stdin=data)

        async def _read_manifest() -> mf.Manifest | None:
            raw, rc = await sbx.exec_capture(
                prefix + ["cat", "--", f"{bdir}/{mf.MANIFEST_FILE}"], text=False)
            if rc != 0:
                return None
            return mf.Manifest.from_dict(mf._load(raw.decode(errors="replace")))

        async def _write_bundle(m: mf.Manifest):
            for fname, text in mf.bundle_files(m).items():
                _, rc = await _put(f"{bdir}/{fname}", text.encode())
                if rc != 0:
                    return False, fname
            return True, ""

        if action == "pull":
            m = await _read_manifest()
            if m is None:
                return {"ok": False, "error": f"no manifest at {bdir}"}
            return {"ok": True, "dir": bdir, "manifest": m.to_dict()}

        if action == "push":
            name_ = str(req.get("name") or "").strip()
            if not name_:
                return {"ok": False, "error": "manifest push needs a name"}
            m = mf.Manifest(
                name=name_, purpose=str(req.get("purpose", "")),
                created_by=self.name,
                vm={"engine": req.get("engine") or engine,
                    "image": req.get("image", ""),
                    "state_ref": req.get("state_ref", ""),
                    "entrypoint": req.get("entrypoint", "")},
                setup=list(req.get("setup") or []),
                usage=list(req.get("usage") or []),
                goals={"objective": req.get("objective", ""),
                       "user_intent": req.get("user_intent", ""),
                       "stop_conditions": list(req.get("stop") or [])})
            m.add_provenance(by=self.name, action="init", note=f"created {m.name}")
            ok, bad = await _write_bundle(m)
            if not ok:
                return {"ok": False, "error": f"failed writing {bad} into sandbox"}
            await self._emit("manifest", action="push", id=m.id, dir=bdir)
            return {"ok": True, "id": m.id, "dir": bdir,
                    "files": list(mf.bundle_files(m))}

        if action == "update":
            m = await _read_manifest()
            if m is None:
                return {"ok": False, "error": f"no manifest at {bdir} (push first)"}
            m.update_state(
                status=req.get("status"), progress=req.get("progress"),
                done=req.get("done") or None, todo=req.get("todo") or None,
                blockers=req.get("blockers") or None,
                append=not bool(req.get("replace", False)))
            note = req.get("note")
            if note:
                m.add_provenance(by=self.name, action="update", note=str(note))
            ok, bad = await _write_bundle(m)
            if not ok:
                return {"ok": False, "error": f"failed writing {bad} into sandbox"}
            await self._emit("manifest", action="update", id=m.id, dir=bdir)
            return {"ok": True, "id": m.id, "dir": bdir,
                    "status": m.state.get("status")}

        return {"ok": False, "error": f"unknown manifest action {action!r}"}

    def _begin_shutdown(self) -> None:
        self._stop.set()
        self.running = False
        if self._ws is not None:
            asyncio.create_task(self._ws.close())
