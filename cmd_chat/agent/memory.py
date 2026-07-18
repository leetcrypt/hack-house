"""In-RAM semantic memory for the hack-house AI agent.

Holds embedded past messages in process memory only — no disk, no DB. The
store is bounded and dies with the agent, exactly like the room's own history
and the rolling transcript. Cosine similarity is computed in pure Python (the
vectors are small and the store is capped), so there's no numpy dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cmd_chat.ai.providers import Msg


@dataclass
class _Entry:
    msg: Msg
    vec: list[float]
    norm: float  # precomputed ||vec|| so search is a dot product + divide


class MemoryIndex:
    """A capped, in-memory pool of embedded messages for semantic recall.

    This is the *long-term* store — it deliberately retains far more than the
    verbatim transcript window, so the agent can recall something said long
    before the recent slice. Oldest entries are evicted past ``max_entries`` to
    bound RAM (≈3 MB at 500 × 768-float vectors).
    """

    def __init__(self, max_entries: int = 500):
        self.max_entries = max_entries
        self._entries: list[_Entry] = []

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, msg: Msg, vec: list[float]) -> None:
        norm = math.sqrt(sum(x * x for x in vec)) if vec else 0.0
        if norm == 0.0:
            return  # empty / failed embedding — skip rather than poison search
        self._entries.append(_Entry(msg, vec, norm))
        if len(self._entries) > self.max_entries:
            self._entries = self._entries[-self.max_entries:]

    def search(self, qvec: list[float], k: int) -> list[tuple[float, Msg]]:
        """Top-``k`` entries by cosine similarity, highest first."""
        qnorm = math.sqrt(sum(x * x for x in qvec)) if qvec else 0.0
        if qnorm == 0.0 or not self._entries:
            return []
        scored = [
            (sum(a * b for a, b in zip(qvec, e.vec)) / (qnorm * e.norm), e.msg)
            for e in self._entries
        ]
        scored.sort(key=lambda t: t[0], reverse=True)
        return scored[:k]
