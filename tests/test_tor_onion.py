"""Ephemeral onion-service sidecar (P0, spec-tor-p2p-relay.md).

Exercised against a fake stem Controller — no real Tor daemon required.
"""

import pytest

from cmd_chat.tor.onion import EphemeralOnion, TorUnavailableError


class FakeCreateResponse:
    def __init__(self, service_id):
        self.service_id = service_id


class FakeController:
    """Stands in for stem.control.Controller."""

    def __init__(self, connect_ok=True, auth_ok=True):
        self.connect_ok = connect_ok
        self.auth_ok = auth_ok
        self.authenticated = False
        self.closed = False
        self.created = []   # [(ports_dict, kwargs), ...]
        self.removed = []   # [service_id, ...]
        self._next_id = "fakeaddr3xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    def authenticate(self):
        if not self.auth_ok:
            raise RuntimeError("auth failed")
        self.authenticated = True

    def create_ephemeral_hidden_service(self, ports, await_publication=True, discard_key=True):
        assert discard_key is True, "the private key must never be persisted"
        self.created.append({"ports": ports, "await_publication": await_publication})
        return FakeCreateResponse(self._next_id)

    def remove_ephemeral_hidden_service(self, service_id):
        self.removed.append(service_id)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_controller(monkeypatch):
    controller = FakeController()

    class FakeControllerModule:
        @staticmethod
        def from_port(port=9051):
            if not controller.connect_ok:
                raise ConnectionRefusedError("no tor on that port")
            return controller

        @staticmethod
        def from_socket_file(path):
            return controller

    monkeypatch.setattr("cmd_chat.tor.onion.Controller", FakeControllerModule)
    return controller


def test_start_creates_ephemeral_service_with_discarded_key(fake_controller):
    onion = EphemeralOnion()
    service = onion.start(target_port=9001, virtual_port=9001)

    assert service.address == f"{fake_controller._next_id}.onion"
    assert service.port == 9001
    assert service.target_port == 9001
    assert fake_controller.created == [
        {"ports": {9001: 9001}, "await_publication": True}
    ]


def test_stop_removes_the_service_and_closes_the_controller(fake_controller):
    onion = EphemeralOnion()
    service = onion.start(target_port=9001, virtual_port=9001)

    onion.stop()

    assert fake_controller.removed == [service.service_id]
    assert fake_controller.closed is True


def test_stop_before_start_is_a_safe_noop(fake_controller):
    onion = EphemeralOnion()
    onion.stop()  # must not raise

    assert fake_controller.removed == []


def test_context_manager_tears_down_on_exit(fake_controller):
    with EphemeralOnion() as onion:
        service = onion.start(target_port=9001, virtual_port=9001)

    assert fake_controller.removed == [service.service_id]
    assert fake_controller.closed is True


def test_unreachable_control_port_raises_tor_unavailable(monkeypatch):
    controller = FakeController(connect_ok=False)

    class FakeControllerModule:
        @staticmethod
        def from_port(port=9051):
            raise ConnectionRefusedError("no tor on that port")

    monkeypatch.setattr("cmd_chat.tor.onion.Controller", FakeControllerModule)

    with pytest.raises(TorUnavailableError):
        EphemeralOnion().start(target_port=9001, virtual_port=9001)


def test_failed_authentication_raises_tor_unavailable(monkeypatch):
    controller = FakeController(auth_ok=False)

    class FakeControllerModule:
        @staticmethod
        def from_port(port=9051):
            return controller

    monkeypatch.setattr("cmd_chat.tor.onion.Controller", FakeControllerModule)

    with pytest.raises(TorUnavailableError):
        EphemeralOnion().start(target_port=9001, virtual_port=9001)


def test_stem_not_installed_raises_tor_unavailable(monkeypatch):
    monkeypatch.setattr("cmd_chat.tor.onion.Controller", None)

    with pytest.raises(TorUnavailableError):
        EphemeralOnion()
