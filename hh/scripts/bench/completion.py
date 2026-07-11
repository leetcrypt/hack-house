"""Model completions via Ollama's raw /api/generate endpoint.

We deliberately do *not* go through the agent's chat provider here: the
capability benchmark wants a raw HumanEval-style completion of the function
prefix (not a chat turn), and we want full control of the read timeout — the
agent's OllamaProvider hard-codes 120s, which throttles reasoning models. This
module owns its own timeout knob.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests


@dataclass
class Completion:
    text: str
    ok: bool
    error: str | None = None
    elapsed: float = 0.0


def complete(model: str, prompt: str, stop: list[str] | None = None,
             *, host: str = "http://127.0.0.1:11434", temperature: float = 0.2,
             num_predict: int = 512, timeout: float = 300.0) -> Completion:
    """Ask the model to continue `prompt`. Stop tokens are passed to Ollama and
    re-applied client-side (Ollama strips the stop string, which is what we want
    — the assembled program must not contain the test's leading token twice)."""
    import time
    t0 = time.time()
    options = {"temperature": temperature, "num_predict": num_predict}
    if stop:
        options["stop"] = stop
    try:
        # raw=True bypasses the chat template so an instruct model *continues*
        # the code (HumanEval-style) instead of replying conversationally with
        # prose + markdown fences, which is what MultiPL-E's assembly expects.
        r = requests.post(f"{host}/api/generate", json={
            "model": model, "prompt": prompt, "stream": False,
            "options": options, "raw": True}, timeout=timeout)
        r.raise_for_status()
        text = r.json().get("response", "")
    except Exception as e:  # noqa: BLE001 — surface as a failed completion row
        return Completion("", False, str(e), time.time() - t0)
    return Completion(_truncate(text, stop), True, None, time.time() - t0)


def _truncate(text: str, stop: list[str] | None) -> str:
    """Defensive client-side stop truncation (covers the no-stop / streamed
    cases and any model that ignores the option)."""
    if not stop:
        return text
    cut = len(text)
    for s in stop:
        i = text.find(s)
        if i != -1:
            cut = min(cut, i)
    return text[:cut]
