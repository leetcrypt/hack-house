"""Operator bridge — a Claude Code session drives a hack-house room as a client.

The room becomes an API: a headless daemon (`OperatorBridge`) owns the
websocket, and the `hh-bridge` CLI (`python -m cmd_chat.operator`) reads/sends
through a local unix socket. See `bridge.py` for the protocol reuse map.
"""

from .bridge import OperatorBridge
from .session import Session

__all__ = ["OperatorBridge", "Session"]
