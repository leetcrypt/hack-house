"""`cmd_chat.py serve --tor` wiring (P0, spec-tor-p2p-relay.md).

The onion sidecar itself is covered in tests/test_tor_onion.py; here we only
check that `main()` starts it before serving and always tears it down after,
even when the server crashes.
"""

import sys

import pytest

import cmd_chat
from cmd_chat.tor.onion import OnionService


class FakeOnion:
    instances = []

    def __init__(self, *args, **kwargs):
        self.started = None
        self.stopped = False
        self.control_socket = kwargs.get("control_socket")
        FakeOnion.instances.append(self)

    def start(self, target_port, virtual_port):
        self.started = (target_port, virtual_port)
        return OnionService(
            address="fakeaddr.onion",
            port=virtual_port,
            target_port=target_port,
            service_id="fakeaddr",
        )

    def stop(self):
        self.stopped = True


@pytest.fixture
def fake_onion(monkeypatch):
    FakeOnion.instances = []
    monkeypatch.setattr("cmd_chat.tor.onion.EphemeralOnion", FakeOnion)
    return FakeOnion


def test_serve_without_tor_flag_never_touches_the_sidecar(monkeypatch, fake_onion):
    monkeypatch.setattr(sys, "argv", ["cmd_chat", "serve", "0.0.0.0", "9001", "--password", "x", "--no-tls"])
    monkeypatch.setattr("cmd_chat.server.server.run_server", lambda **kwargs: None)

    cmd_chat.main()

    assert fake_onion.instances == []


def test_serve_with_tor_flag_starts_then_stops_the_onion_service(monkeypatch, fake_onion):
    monkeypatch.setattr(sys, "argv", ["cmd_chat", "serve", "127.0.0.1", "9001", "--password", "x", "--no-tls", "--tor"])
    monkeypatch.setattr("cmd_chat.server.server.run_server", lambda **kwargs: None)

    cmd_chat.main()

    assert len(fake_onion.instances) == 1
    onion = fake_onion.instances[0]
    assert onion.started == (9001, 9001)
    assert onion.stopped is True


def test_tor_service_is_stopped_even_if_the_server_crashes(monkeypatch, fake_onion):
    monkeypatch.setattr(sys, "argv", ["cmd_chat", "serve", "127.0.0.1", "9001", "--password", "x", "--no-tls", "--tor"])

    def boom(**kwargs):
        raise RuntimeError("server exploded")

    monkeypatch.setattr("cmd_chat.server.server.run_server", boom)

    with pytest.raises(RuntimeError, match="server exploded"):
        cmd_chat.main()

    assert fake_onion.instances[0].stopped is True


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_are_accepted_without_the_override_flag(monkeypatch, fake_onion, host):
    monkeypatch.setattr(sys, "argv", ["cmd_chat", "serve", host, "9001", "--password", "x", "--no-tls", "--tor"])
    monkeypatch.setattr("cmd_chat.server.server.run_server", lambda **kwargs: None)

    cmd_chat.main()  # must not raise / exit

    assert len(fake_onion.instances) == 1


def test_public_bind_with_tor_is_refused_by_default(monkeypatch, fake_onion, capsys):
    monkeypatch.setattr(sys, "argv", ["cmd_chat", "serve", "0.0.0.0", "9001", "--password", "x", "--no-tls", "--tor"])
    monkeypatch.setattr("cmd_chat.server.server.run_server", lambda **kwargs: None)

    with pytest.raises(SystemExit):
        cmd_chat.main()

    assert fake_onion.instances == []  # refused before the onion service was ever created
    assert "--tor-allow-public-bind" in capsys.readouterr().err


def test_public_bind_with_tor_proceeds_when_explicitly_allowed(monkeypatch, fake_onion):
    monkeypatch.setattr(sys, "argv", [
        "cmd_chat", "serve", "0.0.0.0", "9001", "--password", "x", "--no-tls",
        "--tor", "--tor-allow-public-bind",
    ])
    monkeypatch.setattr("cmd_chat.server.server.run_server", lambda **kwargs: None)

    cmd_chat.main()  # must not raise / exit

    assert len(fake_onion.instances) == 1


def test_tor_control_socket_flag_is_passed_through(monkeypatch, fake_onion):
    monkeypatch.setattr(sys, "argv", [
        "cmd_chat", "serve", "127.0.0.1", "9001", "--password", "x", "--no-tls",
        "--tor", "--tor-control-socket", "/run/tor/control.sock",
    ])
    monkeypatch.setattr("cmd_chat.server.server.run_server", lambda **kwargs: None)

    cmd_chat.main()

    assert fake_onion.instances[0].control_socket == "/run/tor/control.sock"


def test_public_bind_without_tor_is_unaffected(monkeypatch, fake_onion):
    """The guardrail is --tor-specific; plain LAN/Tailscale hosting is untouched."""
    monkeypatch.setattr(sys, "argv", ["cmd_chat", "serve", "0.0.0.0", "9001", "--password", "x", "--no-tls"])
    monkeypatch.setattr("cmd_chat.server.server.run_server", lambda **kwargs: None)

    cmd_chat.main()  # must not raise / exit

    assert fake_onion.instances == []
