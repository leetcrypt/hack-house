"""Backward-compatibility shim.

The provider abstraction moved to :mod:`cmd_chat.ai.providers` so the operator
bridge and the ``/ai`` chat agent share one model-agnostic core. This module
re-exports the public API so existing imports
(``from cmd_chat.agent.providers import …``) keep working. Prefer importing from
``cmd_chat.ai.providers`` in new code.
"""

from cmd_chat.ai.providers import *  # noqa: F401,F403
from cmd_chat.ai.providers import (  # noqa: F401  (explicit for star-safety)
    Msg,
    OllamaEmbedder,
    OllamaProvider,
    AnthropicProvider,
    OpenAICompatibleProvider,
    Provider,
    ToolsUnsupported,
    make_provider,
    preflight,
)
