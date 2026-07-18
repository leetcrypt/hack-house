"""Backward-compatibility shim.

Named model profiles moved to :mod:`cmd_chat.ai.profiles` so the operator bridge
and the ``/ai`` chat agent share one core. This module re-exports the public API
so existing imports (``from cmd_chat.agent.profiles import …``) keep working.
Prefer importing from ``cmd_chat.ai.profiles`` in new code.
"""

from cmd_chat.ai.profiles import *  # noqa: F401,F403
from cmd_chat.ai.profiles import (  # noqa: F401  (explicit for star-safety)
    find_profiles_file,
    load_profiles,
    provider_from_profile,
)
