"""DoS / abuse caps for the web relay (spec §7 Resource/DoS).

RAM-only, per-key counters — nothing here is persisted or logged as content. Two
primitives, both keyed by opaque strings (typically the client IP, or a
`(slug, ip)` pair for PIN attempts):

- `SlidingWindow` — bound the *rate* of an event (subscribe connects) inside a
  time window; used as a connect-rate limiter.
- `Lockout` — after N failures inside a window, refuse further attempts for a
  backoff period; used to blunt PIN brute-forcing (the PIN gates access/DoS,
  the `#k` fragment still gates reading — an attacker needs slug AND PIN AND #k).

`time.monotonic()` is injected as `now` so tests can drive it deterministically.
"""

from __future__ import annotations

import time
from collections import deque


class SlidingWindow:
    """Allow at most `limit` events per `window` seconds per key (sliding)."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self._hits.get(key)
        if q is None:
            q = self._hits[key] = deque()
        cutoff = now - self.window
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def sweep(self, now: float | None = None) -> None:
        """Drop keys with no recent hits so the table can't grow unbounded."""
        now = time.monotonic() if now is None else now
        cutoff = now - self.window
        for key in list(self._hits):
            q = self._hits[key]
            while q and q[0] < cutoff:
                q.popleft()
            if not q:
                del self._hits[key]


class Lockout:
    """Count failures per key; once `max_fails` land inside `window`, the key is
    locked for `lockout` seconds. A success (`clear`) resets it immediately."""

    def __init__(self, max_fails: int, window: float, lockout: float):
        self.max_fails = max_fails
        self.window = window
        self.lockout = lockout
        self._fails: dict[str, deque] = {}
        self._until: dict[str, float] = {}

    def locked(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        until = self._until.get(key)
        if until is None:
            return False
        if now >= until:
            # lockout elapsed — forgive and forget
            self._until.pop(key, None)
            self._fails.pop(key, None)
            return False
        return True

    def fail(self, key: str, now: float | None = None) -> bool:
        """Record a failed attempt. Returns True if the key is now locked out."""
        now = time.monotonic() if now is None else now
        q = self._fails.get(key)
        if q is None:
            q = self._fails[key] = deque()
        cutoff = now - self.window
        while q and q[0] < cutoff:
            q.popleft()
        q.append(now)
        if len(q) >= self.max_fails:
            self._until[key] = now + self.lockout
            return True
        return False

    def clear(self, key: str) -> None:
        self._fails.pop(key, None)
        self._until.pop(key, None)
