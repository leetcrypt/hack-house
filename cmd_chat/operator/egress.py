"""Sandbox egress controls — measure and guard the container's outbound path.

Measured fact (2026-09-06): rootless podman (pasta) routes a sandbox container's
outbound traffic through the HOST's default route, so the container presents the
host's egress IP — currently the ProtonVPN exit. The room relay (web / Tor onion)
is ORTHOGONAL: it proxies inbound room access, it never routes container egress.
Two risks follow:
  * a ProtonVPN drop silently falls back to the real ISP IP (the VPN kill-switch is
    kept OFF here on purpose — it would sever Tailscale and lock the box out);
  * the Tor onion relay does NOT anonymize sandbox OUTBOUND ops.

This module is the small, first pair of controls for that:
  1. GUARD  — a launch-time check that the default route is a tunnel before a
     networked sandbox is allowed (``HH_SBX_EGRESS=guard`` = fail-closed, an
     OPT-IN; the default is ``auto`` = Tor-if-available-else-local, never refused;
     ``open`` = warn-only; ``none`` = no egress).
  2. PROBE  — an on-demand measurement of the presented egress IP + reachability
     (``python -m cmd_chat.operator egress``).

stdlib only; the network checks are best-effort and time-bounded.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import urllib.request

# Interfaces that represent a tunneled/VPN egress (NOT the raw LAN / Wi-Fi NIC).
VPN_IFACE_RE = re.compile(
    r"^(proton\d*|wg\d*|tun\d*|tap\d*|tailscale\d*|nordlynx|mullvad.*|ipsec\d*|ppp\d*)$", re.I)

# Host Ollama reachable over a tunnel/tailnet, as "host:port". Configure per-host
# via $HH_TAILNET_OLLAMA (e.g. "10.0.0.5:11434"); unset → the reachability probe
# below is simply skipped. No infra address is baked into the source.
def _tailnet_ollama() -> tuple[str, int] | None:
    raw = os.environ.get("HH_TAILNET_OLLAMA", "").strip()
    if not raw:
        return None
    host, _, port = raw.partition(":")
    return (host, int(port) if port.isdigit() else 11434)


TAILNET_OLLAMA = _tailnet_ollama()


MODES = ("auto", "guard", "open", "none", "local", "scope", "tor")


def egress_mode(override: str | None = None) -> str:
    """Effective egress posture. A per-launch ``override`` (e.g. ``sbx launch --egress
    local``) wins over ``$HH_SBX_EGRESS``, which defaults to **auto**.

    auto (default) — Tor exit if Tor is available on this host, else local; and if the
    Tor gateway can't come up it falls back to local, so a launch is NEVER refused for
    egress reasons (no false-negative connections). This is the reliable, share-friendly
    default; opt into a stricter posture per-host via `$HH_SBX_EGRESS`.
    guard (opt-in, fail-closed) — refuse a networked sandbox unless the route is a
    tunnel, so a dropped tunnel can't silently leak the real IP; open — warn-only;
    none — no egress; local — block the LAN/host/tailnet pivot (allow Ollama+internet);
    scope — local + `$HH_SBX_SCOPE` allowlist; tor — all outbound through a Tor exit."""
    return (override or os.environ.get("HH_SBX_EGRESS") or "auto").strip().lower()


def tor_available() -> bool:
    """Whether a Tor egress gateway can plausibly be brought up on this host — used to
    resolve the ``auto`` default toward Tor. A cheap presence check (the `tor` binary);
    the definitive test is bringing the gateway up, and ``auto`` falls back to ``local``
    if that fails, so a false positive here costs one fallback, never a failed launch."""
    return shutil.which("tor") is not None


def resolve_auto(mode: str) -> str:
    """Map the ``auto`` default to a concrete *preferred* posture: Tor if available,
    else local. Launch-time fallback (tor→local) still applies on top of this."""
    if mode != "auto":
        return mode
    return "tor" if tor_available() else "local"


def default_route_iface(dest: str = "1.1.1.1", _run=None) -> str | None:
    """The interface the host uses to reach ``dest`` (its egress path). ``_run`` is
    injectable for tests (takes an argv, returns something with ``.stdout``)."""
    run = _run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=5))
    try:
        out = run(["ip", "route", "get", dest]).stdout
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"\bdev\s+(\S+)", out or "")
    return m.group(1) if m else None


def is_tunneled(iface: str | None) -> bool:
    return bool(iface and VPN_IFACE_RE.match(iface))


def guard_networked_launch(mode: str | None = None, _iface: str | None = None) -> tuple[bool, str]:
    """Decide whether a NETWORKED sandbox may launch under the given/effective posture.

    Returns ``(allow_network, advisory)``. ``allow_network`` False means the caller
    must refuse (or fall to ``--network=none``); ``advisory`` is a human string
    (a warning to surface, or the block reason). ``_iface`` overrides the probe for tests.
    """
    mode = mode or egress_mode()
    if mode == "none":
        return False, "HH_SBX_EGRESS=none — no egress"
    # Gateway-managed postures handle egress themselves (tor anonymizes via a Tor
    # exit; local/scope pivot-block the LAN/host/tailnet), so the host default-route
    # tunnel check — and its "presents the real IP" warning — does not apply to them.
    if mode in ("local", "scope", "tor"):
        return True, ""
    iface = _iface if _iface is not None else default_route_iface()
    if is_tunneled(iface):
        return True, ""  # egress is via a tunnel — fine
    msg = (f"sandbox egress is NOT tunneled (default route dev={iface or '?'}) — "
           "outbound ops would present the host's real IP, not a VPN/Tor exit")
    if mode == "guard":
        return False, ("REFUSED: " + msg
                       + " — bring up the VPN, or set HH_SBX_EGRESS=open to allow anyway")
    return True, "WARNING: " + msg + " — set HH_SBX_EGRESS=guard to fail-closed"


def probe_public_ip(timeout: float = 10.0) -> str | None:
    """The presented public egress IP (host-side; equals the container's via pasta).
    Best-effort across a few plain-HTTP echo services (no TLS/cert dependency)."""
    for url in ("http://api.ipify.org", "http://ifconfig.me/ip", "http://icanhazip.com"):
        try:
            ip = urllib.request.urlopen(url, timeout=timeout).read().decode().strip()  # noqa: S310
            if ip:
                return ip
        except Exception:  # noqa: BLE001
            continue
    return None


def reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        socket.create_connection((host, int(port)), timeout).close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _container_public_ip(engine: str,
                         image: str = "docker.io/library/python:3.11-slim") -> str | None:
    """Most-faithful measurement: the IP a REAL throwaway container presents."""
    name = "hh-egress-probe"
    try:
        subprocess.run([engine, "rm", "-f", name], capture_output=True, timeout=20)
        subprocess.run([engine, "run", "-d", "--name", name, image, "sleep", "30"],
                       capture_output=True, timeout=90)
        r = subprocess.run(
            [engine, "exec", name, "python3", "-c",
             "import urllib.request;print(urllib.request.urlopen("
             "'http://api.ipify.org',timeout=10).read().decode())"],
            capture_output=True, text=True, timeout=40)
        return (r.stdout or "").strip() or None
    except Exception:  # noqa: BLE001
        return None
    finally:
        subprocess.run([engine, "rm", "-f", name], capture_output=True, timeout=20)


def egress_report(engine: str | None = None, container_probe: bool = False) -> dict:
    """On-demand egress measurement for the ``egress`` verb."""
    iface = default_route_iface()
    host_ip = probe_public_ip()
    rep = {
        "mode": egress_mode(),
        "route_iface": iface,
        "tunneled": is_tunneled(iface),
        "host_public_ip": host_ip,
        "tailnet_ollama_reachable": reachable(*TAILNET_OLLAMA) if TAILNET_OLLAMA else None,
    }
    if container_probe and engine:
        cip = _container_public_ip(engine)
        rep["container_public_ip"] = cip
        rep["container_matches_host"] = (cip is not None and cip == host_ip)
    return rep
