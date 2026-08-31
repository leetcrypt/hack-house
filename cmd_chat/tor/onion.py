"""Ephemeral, per-session Tor v3 onion service — spec-tor-p2p-relay.md §2/§3.

Mirrors the cmd_chat/agent headless-member shape: a small sidecar class the
CLI/command layer drives directly, not a server of its own. The private key is
never persisted (`discard_key=True` on ADD_ONION) — nothing survives a `stop()`.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

try:
    from stem.control import Controller
except ImportError:  # pragma: no cover - exercised via monkeypatched Controller=None
    Controller = None


class TorUnavailableError(RuntimeError):
    """The local Tor ControlPort couldn't be reached, authenticated, or used."""


@dataclasses.dataclass(frozen=True)
class OnionService:
    address: str        # "<56-char>.onion"
    port: int            # virtual port advertised on the onion address
    target_port: int     # local port the service forwards to
    service_id: str       # stem's handle, needed to remove the service later


class EphemeralOnion:
    """Create/destroy one ephemeral, in-memory-only v3 onion service."""

    def __init__(self, control_port: int = 9051, control_socket: Optional[str] = None):
        if Controller is None:
            raise TorUnavailableError(
                "the 'stem' package is required for --tor (pip install stem)"
            )
        self._control_port = control_port
        self._control_socket = control_socket
        self._controller = None
        self._service: Optional[OnionService] = None

    def __enter__(self) -> "EphemeralOnion":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _connect(self):
        try:
            if self._control_socket:
                controller = Controller.from_socket_file(self._control_socket)
            else:
                controller = Controller.from_port(port=self._control_port)
            controller.authenticate()
        except Exception as exc:
            raise TorUnavailableError(
                f"could not reach/authenticate to the Tor ControlPort: {exc}"
            ) from exc
        return controller

    def start(self, target_port: int, virtual_port: int = 80, detached: bool = False) -> OnionService:
        """`detached=True` lets the service outlive this controller connection —
        needed for a short-lived script that mints a service and then exits.
        Without it, ADD_ONION ties the service's life to the connection that
        created it (Tor's own default), so it vanishes the moment the process
        holding that connection exits."""
        if self._controller is None:
            self._controller = self._connect()

        response = self._controller.create_ephemeral_hidden_service(
            {virtual_port: target_port},
            await_publication=True,
            discard_key=True,
            detached=detached,
        )
        self._service = OnionService(
            address=f"{response.service_id}.onion",
            port=virtual_port,
            target_port=target_port,
            service_id=response.service_id,
        )
        return self._service

    def stop(self) -> None:
        if self._controller is not None:
            if self._service is not None:
                self._controller.remove_ephemeral_hidden_service(self._service.service_id)
            self._controller.close()
        self._service = None
        self._controller = None
