"""hack-house AI agent bridge — model-agnostic agents that join a room."""

from .bridge import AgentBridge
from .providers import Msg, Provider, make_provider

__all__ = ["AgentBridge", "Msg", "Provider", "make_provider"]
