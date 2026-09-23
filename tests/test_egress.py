"""Offline tests for sandbox egress controls (cmd_chat/operator/egress.py).

No real network/subprocess: route output is injected and HH_SBX_EGRESS is set per-case.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cmd_chat.operator import egress as eg  # noqa: E402


class _FakeRun:
    def __init__(self, stdout):
        self.stdout = stdout

    def __call__(self, argv):
        return self


def _route(iface):
    # mimic `ip route get 1.1.1.1`
    return _FakeRun(f"1.1.1.1 dev {iface} table 245 src 10.2.0.2 uid 1000 \n    cache \n")


def _with_mode(mode, fn):
    prev = os.environ.get("HH_SBX_EGRESS")
    if mode is None:
        os.environ.pop("HH_SBX_EGRESS", None)
    else:
        os.environ["HH_SBX_EGRESS"] = mode
    try:
        return fn()
    finally:
        if prev is None:
            os.environ.pop("HH_SBX_EGRESS", None)
        else:
            os.environ["HH_SBX_EGRESS"] = prev


def test_default_route_iface_parses_dev():
    assert eg.default_route_iface(_run=_route("proton0")) == "proton0"
    assert eg.default_route_iface(_run=_route("wlp0s20f3")) == "wlp0s20f3"


def test_is_tunneled_recognises_vpn_ifaces():
    for good in ("proton0", "wg0", "tun0", "tap1", "tailscale0", "nordlynx", "ppp0"):
        assert eg.is_tunneled(good), good
    for bad in ("eth0", "wlan0", "wlp0s20f3", "enp3s0", "docker0", "lo", None, ""):
        assert not eg.is_tunneled(bad), bad


def test_mode_default_is_auto():
    # Default is `auto` (Tor-if-available-else-local), NOT the fail-closed guard —
    # so a missing VPN can't produce a false-negative refused launch.
    assert _with_mode(None, eg.egress_mode) == "auto"


def test_resolve_auto_prefers_tor_when_available(monkeypatch):
    monkeypatch.setattr(eg, "tor_available", lambda: True)
    assert eg.resolve_auto("auto") == "tor"


def test_resolve_auto_falls_back_to_local_without_tor(monkeypatch):
    monkeypatch.setattr(eg, "tor_available", lambda: False)
    assert eg.resolve_auto("auto") == "local"


def test_resolve_auto_passes_through_explicit_modes():
    for m in ("guard", "open", "none", "local", "scope", "tor"):
        assert eg.resolve_auto(m) == m


def test_guard_none_refuses():
    allow, msg = _with_mode("none", lambda: eg.guard_networked_launch(_iface="proton0"))
    assert allow is False and "no egress" in msg.lower()


def test_guard_open_tunneled_allows_silently():
    allow, msg = _with_mode("open", lambda: eg.guard_networked_launch(_iface="proton0"))
    assert allow is True and msg == ""


def test_guard_open_untunneled_warns_but_allows():
    allow, msg = _with_mode("open", lambda: eg.guard_networked_launch(_iface="wlan0"))
    assert allow is True and msg.startswith("WARNING") and "real IP" in msg


def test_guard_mode_tunneled_allows():
    allow, msg = _with_mode("guard", lambda: eg.guard_networked_launch(_iface="proton0"))
    assert allow is True and msg == ""


def test_guard_mode_untunneled_refuses():
    allow, msg = _with_mode("guard", lambda: eg.guard_networked_launch(_iface="wlan0"))
    assert allow is False and msg.startswith("REFUSED") and "wlan0" in msg
