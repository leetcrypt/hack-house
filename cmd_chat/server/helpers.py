import ipaddress
import os
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from dataclasses import asdict
import json
from sanic import Request, Sanic, Websocket


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Only honor X-Forwarded-For when explicitly told we sit behind a trusted proxy
# (TRUST_PROXY=1). Otherwise a direct client can spoof the header to forge a
# source IP and dodge the per-IP rate limiter, so we use the real peer address.
_TRUST_PROXY = os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes")


def get_client_ip(request: Request) -> str:
    if _TRUST_PROXY:
        if forwarded := request.headers.get("x-forwarded-for"):
            return forwarded.split(",")[0].strip()
    return request.ip


def _host_ipv4s() -> list[str]:
    """Non-loopback IPv4 addresses on this host (best-effort, stdlib+`ip`)."""
    out: list[str] = []
    try:
        r = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                           capture_output=True, text=True, timeout=5)
        for line in (r.stdout or "").splitlines():
            m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", line)
            if m and m.group(1) != "127.0.0.1":
                out.append(m.group(1))
    except Exception:  # noqa: BLE001 — best-effort; empty is fine
        pass
    return out


def reach_addresses(bind_host: str | None) -> list[dict]:
    """Shareable connect addresses for a room bound to ``bind_host``, each tagged
    ``tailscale`` (100.64/10 CGNAT), ``lan`` (RFC1918), or ``host`` (public). Empty
    for a loopback-only bind — that room is not reachable off this machine, so
    ``/share`` shows only the loopback note (never a bogus address that won't connect).
    A wildcard bind (0.0.0.0/::) enumerates every host IPv4; a specific bind is itself."""
    bh = (bind_host or "").strip()
    if bh in ("", "127.0.0.1", "localhost", "::1"):
        return []
    ips = _host_ipv4s() if bh in ("0.0.0.0", "::") else [bh]
    tailnet = ipaddress.ip_network("100.64.0.0/10")
    out: list[dict] = []
    seen: set[str] = set()
    for ip in ips:
        if ip in seen:
            continue
        seen.add(ip)
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.is_loopback:
            continue
        if a in tailnet:
            label = "tailscale"
        elif a.is_private:
            label = "lan"
        else:
            label = "host"
        out.append({"label": label, "addr": ip})
    return out


def state_frame(app: Sanic) -> str:
    """Build the per-client `init` snapshot (message backlog + roster) as a JSON
    string. Returned (not sent) so it can be enqueued as the connection's first
    outbound frame, guaranteeing it precedes any later broadcast."""
    messages = app.ctx.message_store.get_all()
    users = app.ctx.session_store.get_all()
    return json.dumps(
        {
            "type": "init",
            "messages": [asdict(m) for m in messages],
            "users": [
                {"user_id": u.user_id, "username": u.username} for u in users
            ],
            # Reachable onion address ("<id>.onion:<port>") when hosted with
            # --tor, else "". Lets the TUI `/share` print a connect block; empty
            # for loopback/direct rooms (the client already knows host/port).
            "onion": getattr(app.ctx, "onion", "") or "",
            # Shareable tailnet/LAN/public connect addresses for this room's bind,
            # so `/share` can offer tor + tailscale + LAN links. Empty for a
            # loopback-only bind. Each: {"label": tailscale|lan|host, "addr": ip}.
            "reach": getattr(app.ctx, "reach", []) or [],
        }
    )


class RateLimiter:
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        timestamps = self._requests[key]
        timestamps[:] = [t for t in timestamps if now - t < self.window]
        if len(timestamps) >= self.max_requests:
            return False
        timestamps.append(now)
        return True
