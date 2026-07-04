import asyncio
from typing import Optional
from sanic import Websocket


# Per-connection outbound backlog before a client is considered wedged.
#
# Every relayed frame is *enqueued* (never awaited) by the broadcaster, and a
# dedicated per-connection writer task drains the queue with `await ws.send`.
# This decouples the room's read loop from any single slow receiver: the old
# code awaited each `send` serially under a global lock, so the slowest viewer
# (e.g. a remote peer over a laggy Tailscale link) gated delivery to everyone
# *and* stalled the server's read of the next frame — so a sandbox owner's PTY
# stream backed up and then arrived "all at once".
#
# A backlog this deep absorbs a large PTY burst and transient network hiccups.
# A client that falls this far behind is effectively stuck (it would be closed
# by the websocket ping-timeout within ~40s anyway); we close it now so the room
# isn't holding unbounded memory for it. It reconnects (Ctrl-R) and the host
# replays a fresh sandbox snapshot, which is cleaner than feeding it a corrupt,
# partial terminal byte-stream with gaps.
SEND_QUEUE_MAX = 4096


class _Conn:
    """A live websocket plus its outbound queue and the task draining it."""

    __slots__ = ("ws", "queue", "task")

    def __init__(self, ws: Websocket):
        self.ws = ws
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=SEND_QUEUE_MAX)
        self.task: Optional[asyncio.Task] = None


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, _Conn] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self, user_id: str, websocket: Websocket, initial: Optional[str] = None
    ) -> None:
        """Register a connection and start its writer task.

        `initial` (the per-client init snapshot) is enqueued *before* the
        connection becomes visible to broadcasters, guaranteeing it is the first
        frame the client receives — no later broadcast can jump ahead of it.
        """
        conn = _Conn(websocket)
        if initial is not None:
            conn.queue.put_nowait(initial)
        old = None
        async with self._lock:
            old = self.active_connections.get(user_id)
            self.active_connections[user_id] = conn
        if old is not None and old.task is not None:
            old.task.cancel()
        conn.task = asyncio.create_task(self._writer(conn))

    async def _writer(self, conn: _Conn) -> None:
        """Drain one connection's queue in FIFO order, one `send` at a time."""
        try:
            while True:
                msg = await conn.queue.get()
                await conn.ws.send(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Socket died mid-send; the handler's `finally` will disconnect us.
            pass

    async def disconnect(self, user_id: str) -> None:
        async with self._lock:
            conn = self.active_connections.pop(user_id, None)
        if conn is not None and conn.task is not None:
            conn.task.cancel()

    @staticmethod
    def _enqueue(conn: _Conn, message: str) -> bool:
        """Non-blocking enqueue. False means the backlog is full (wedged peer)."""
        try:
            conn.queue.put_nowait(message)
            return True
        except asyncio.QueueFull:
            return False

    async def _drop_wedged(self, user_id: str, conn: _Conn) -> None:
        """Evict a client whose backlog overflowed and nudge it to reconnect."""
        await self.disconnect(user_id)
        try:
            # 1013 = "Try Again Later": the client surfaces a closed socket and
            # can reconnect to resync from a fresh snapshot.
            await conn.ws.close(code=1013)
        except Exception:
            pass

    async def broadcast(self, message: str, exclude_user: Optional[str] = None) -> None:
        async with self._lock:
            targets = [
                (uid, conn)
                for uid, conn in self.active_connections.items()
                if not (exclude_user and uid == exclude_user)
            ]
        # Enqueue is synchronous and fast, so the broadcaster returns immediately
        # — it never waits on any socket. Only genuinely wedged peers overflow.
        wedged = [(uid, conn) for uid, conn in targets if not self._enqueue(conn, message)]
        for uid, conn in wedged:
            await self._drop_wedged(uid, conn)

    async def send_personal(self, user_id: str, message: str) -> bool:
        async with self._lock:
            conn = self.active_connections.get(user_id)
        if conn is None:
            return False
        if self._enqueue(conn, message):
            return True
        await self._drop_wedged(user_id, conn)
        return False
