"""Unit tests for the per-connection-queue ConnectionManager.

These pin the latency-resilience contract: the broadcaster never awaits a
socket, so one slow viewer can't stall the room (the bug behind a sandbox PTY
stream arriving "all at once" for non-host viewers), while per-client ordering
stays FIFO and a hopelessly-backed-up peer is evicted rather than buffered
without bound.
"""

import asyncio

import cmd_chat.server.managers as managers
from cmd_chat.server.managers import ConnectionManager


class FakeWS:
    """Minimal stand-in for a Sanic Websocket: records sends, can be made slow
    or gated, and records the close code."""

    def __init__(self, delay: float = 0.0, gate: "asyncio.Event | None" = None):
        self.sent: list[str] = []
        self.delay = delay
        self.gate = gate
        self.closed: "int | None" = None

    async def send(self, msg: str) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(msg)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


async def _drain(timeout: float = 1.0) -> None:
    """Yield control so writer tasks can flush their queues."""
    await asyncio.sleep(0)
    await asyncio.sleep(0.02)


def test_broadcast_is_fifo_per_client():
    async def go():
        mgr = ConnectionManager()
        a, b = FakeWS(), FakeWS()
        await mgr.connect("a", a, initial="INIT_A")
        await mgr.connect("b", b, initial="INIT_B")
        for i in range(5):
            await mgr.broadcast(f"m{i}")
        await _drain()
        assert a.sent == ["INIT_A", "m0", "m1", "m2", "m3", "m4"]
        assert b.sent == ["INIT_B", "m0", "m1", "m2", "m3", "m4"]
        await mgr.disconnect("a")
        await mgr.disconnect("b")

    asyncio.run(go())


def test_exclude_user_is_skipped():
    async def go():
        mgr = ConnectionManager()
        a, b = FakeWS(), FakeWS()
        await mgr.connect("a", a)
        await mgr.connect("b", b)
        await mgr.broadcast("hello", exclude_user="a")
        await _drain()
        assert a.sent == []
        assert b.sent == ["hello"]
        await mgr.disconnect("a")
        await mgr.disconnect("b")

    asyncio.run(go())


def test_slow_client_does_not_block_broadcast():
    """The broadcaster must return promptly even when a peer's socket is slow —
    delivery is decoupled via the per-connection writer task."""

    async def go():
        mgr = ConnectionManager()
        slow = FakeWS(delay=0.5)
        fast = FakeWS()
        await mgr.connect("slow", slow)
        await mgr.connect("fast", fast)

        start = asyncio.get_event_loop().time()
        await mgr.broadcast("x")
        elapsed = asyncio.get_event_loop().time() - start
        # Enqueue-only: must not wait anywhere near the slow socket's 0.5s.
        assert elapsed < 0.1, f"broadcast blocked on slow peer ({elapsed:.3f}s)"

        # Fast client gets it almost immediately; slow one lags behind.
        await asyncio.sleep(0.05)
        assert fast.sent == ["x"]
        assert slow.sent == []
        await mgr.disconnect("slow")
        await mgr.disconnect("fast")

    asyncio.run(go())


def test_wedged_client_is_evicted_and_closed(monkeypatch):
    """A peer that overflows its backlog is dropped (closed 1013) instead of
    growing memory without bound; other clients are unaffected."""

    async def go():
        monkeypatch.setattr(managers, "SEND_QUEUE_MAX", 3)
        mgr = ConnectionManager()
        gate = asyncio.Event()  # never set → this peer's send never completes
        wedged = FakeWS(gate=gate)
        healthy = FakeWS()
        await mgr.connect("wedged", wedged)
        await mgr.connect("healthy", healthy)
        await asyncio.sleep(0)  # let each writer pull its first frame

        # Flood with a yield between frames: the healthy peer drains each time, so
        # it never overflows; the wedged peer's writer is stuck on the gate, so
        # its backlog fills (max 3) and then overflows → eviction.
        n = 20
        for i in range(n):
            await mgr.broadcast(f"f{i}")
            await asyncio.sleep(0)
        await _drain()

        assert "wedged" not in mgr.active_connections, "wedged peer must be evicted"
        assert wedged.closed == 1013
        # The healthy peer was never penalised for its neighbour: still live and
        # it received everything (far more than the wedged peer's tiny backlog).
        assert "healthy" in mgr.active_connections
        assert healthy.sent == [f"f{i}" for i in range(n)]
        await mgr.disconnect("healthy")

    asyncio.run(go())


def test_reconnect_replaces_old_connection():
    async def go():
        mgr = ConnectionManager()
        first = FakeWS()
        await mgr.connect("u", first, initial="INIT1")
        second = FakeWS()
        await mgr.connect("u", second, initial="INIT2")
        await mgr.broadcast("after")
        await _drain()
        # Only the live (second) connection receives the broadcast.
        assert second.sent == ["INIT2", "after"]
        assert "after" not in first.sent
        assert mgr.active_connections["u"].ws is second
        await mgr.disconnect("u")

    asyncio.run(go())


def test_send_personal_to_missing_user_is_false():
    async def go():
        mgr = ConnectionManager()
        assert await mgr.send_personal("ghost", "hi") is False

    asyncio.run(go())
