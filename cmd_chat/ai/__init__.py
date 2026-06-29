"""Shared model-agnostic AI core for hack-house.

Houses the provider abstraction (``providers``) and named model profiles
(``profiles``) consumed by BOTH the in-room ``/ai`` chat agent and the operator
bridge. Hoisted here from ``cmd_chat.agent`` so the operator can run any
function-calling model as an operator, not just Claude. The old
``cmd_chat.agent.{providers,profiles}`` paths remain as thin re-export shims for
backward compatibility.
"""

from .providers import (
    Msg,
    OllamaEmbedder,
    OllamaProvider,
    Provider,
    ToolsUnsupported,
    make_provider,
    preflight,
)
from .profiles import (
    find_profiles_file,
    load_profiles,
    provider_from_profile,
)

__all__ = [
    "Msg",
    "Provider",
    "ToolsUnsupported",
    "OllamaProvider",
    "OllamaEmbedder",
    "make_provider",
    "preflight",
    "load_profiles",
    "find_profiles_file",
    "provider_from_profile",
]
