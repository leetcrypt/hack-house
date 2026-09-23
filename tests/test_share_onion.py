"""The `init` snapshot surfaces the reachable onion address so the TUI `/share`
command can print a paste-ready connect block. Onion hosting (`serve --tor`)
threads its `.onion:<port>` through `create_app(onion=...)` → `app.ctx.onion` →
`state_frame`; loopback/direct hosting leaves it empty. See `/share` in
`hh/src/app.rs`.
"""
import json

from cmd_chat.server.factory import create_app
from cmd_chat.server.helpers import reach_addresses, state_frame


def test_init_frame_carries_onion_when_tor_hosted():
    app = create_app(password="x", name="t-onion", onion="abc123.onion:9000")
    frame = json.loads(state_frame(app))
    assert frame["type"] == "init"
    assert frame["onion"] == "abc123.onion:9000"


def test_init_frame_onion_empty_for_loopback():
    app = create_app(password="x", name="t-loopback")
    frame = json.loads(state_frame(app))
    assert frame["onion"] == ""


def test_reach_empty_for_loopback_bind():
    # A loopback-only room is not reachable off this host — /share must not offer
    # a bogus tailnet/LAN address that would fail to connect.
    for bh in ("", "127.0.0.1", "localhost", "::1"):
        assert reach_addresses(bh) == [], bh


def test_reach_classifies_tailscale_lan_public():
    assert reach_addresses("100.64.0.1") == [{"label": "tailscale", "addr": "100.64.0.1"}]
    assert reach_addresses("100.64.0.21") == [{"label": "tailscale", "addr": "100.64.0.21"}]
    assert reach_addresses("192.168.1.5") == [{"label": "lan", "addr": "192.168.1.5"}]
    assert reach_addresses("10.0.0.9") == [{"label": "lan", "addr": "10.0.0.9"}]
    assert reach_addresses("8.8.8.8") == [{"label": "host", "addr": "8.8.8.8"}]


def test_init_frame_carries_reach_for_specific_bind():
    app = create_app(password="x", name="t-reach", bind_host="100.64.0.21")
    frame = json.loads(state_frame(app))
    assert {"label": "tailscale", "addr": "100.64.0.21"} in frame["reach"]
