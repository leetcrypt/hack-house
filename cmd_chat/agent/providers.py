"""Model-agnostic provider interface for the hack-house AI agent bridge.

A Provider turns a system prompt + conversation into a single reply string.
The bundled adapters speak plain HTTP via ``requests`` (already a dependency),
so no extra SDKs are required and any backend can be plugged in — including a
custom one via the ``module:Class`` spec.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import requests


@dataclass
class Msg:
    role: str  # "system" | "user" | "assistant"
    content: str


@runtime_checkable
class Provider(Protocol):
    name: str
    model: str

    def complete(self, system: str, messages: list[Msg]) -> str:
        ...

    # Optional: list models the backend can serve, for discovery/preflight.
    # Providers that can't enumerate (e.g. a bespoke endpoint) may omit this.
    def available_models(self) -> list[str]:
        ...


class OllamaProvider:
    """Local Ollama (default, recommended). No API key — privacy-preserving."""

    name = "ollama"

    def __init__(self, model: str = "llama3", host: str | None = None, timeout: int = 120,
                 num_ctx: int = 8192, num_predict: int = 512, keep_alive: str = "30m"):
        self.model = model
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.timeout = timeout
        # Honor the larger backfilled window (num_ctx) — Ollama defaults to a tiny
        # 2048 — and bound reply length. keep_alive pins the model in VRAM so the
        # next /ai doesn't pay a cold reload.
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.keep_alive = keep_alive

    def complete(self, system: str, messages: list[Msg]) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": self.num_ctx, "num_predict": self.num_predict},
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
        }
        r = requests.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
        r.raise_for_status()
        return (r.json().get("message", {}).get("content") or "").strip()

    def available_models(self) -> list[str]:
        r = requests.get(f"{self.host}/api/tags", timeout=self.timeout)
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", [])]


class AnthropicProvider:
    """Anthropic Messages API. Cloud — opt-in. Needs ANTHROPIC_API_KEY."""

    name = "anthropic"

    def __init__(self, model: str = "claude-opus-4-6", api_key: str | None = None,
                 timeout: int = 120, max_tokens: int = 1024):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.timeout = timeout
        self.max_tokens = max_tokens
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")

    def complete(self, system: str, messages: list[Msg]) -> str:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [
                {"role": m.role, "content": m.content}
                for m in messages
                if m.role in ("user", "assistant")
            ],
        }
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            json=payload,
            timeout=self.timeout,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        r.raise_for_status()
        blocks = r.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks).strip()

    def available_models(self) -> list[str]:
        r = requests.get(
            "https://api.anthropic.com/v1/models",
            timeout=self.timeout,
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
        )
        r.raise_for_status()
        return [m.get("id", "") for m in r.json().get("data", [])]


class OpenAICompatibleProvider:
    """OpenAI-style /chat/completions — OpenAI, Groq, Together, local vLLM, etc."""

    name = "openai"

    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None,
                 base_url: str | None = None, timeout: int = 120):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.timeout = timeout

    def complete(self, system: str, messages: list[Msg]) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
        }
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        r = requests.post(
            f"{self.base_url}/chat/completions", json=payload, headers=headers, timeout=self.timeout
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    def available_models(self) -> list[str]:
        headers = {}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        r = requests.get(f"{self.base_url}/models", headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return [m.get("id", "") for m in r.json().get("data", [])]


_BUILTINS = {
    "ollama": OllamaProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAICompatibleProvider,
}


def make_provider(spec: str, model: str | None = None, **opts) -> Provider:
    """Build a provider.

    ``spec`` is a builtin name (``ollama`` / ``anthropic`` / ``openai``) or a
    ``module:Class`` path to a custom Provider implementation.
    """
    if ":" in spec:
        mod_name, _, cls_name = spec.partition(":")
        cls = getattr(importlib.import_module(mod_name), cls_name)
    else:
        cls = _BUILTINS.get(spec)
        if cls is None:
            raise ValueError(f"unknown provider '{spec}' (builtins: {', '.join(_BUILTINS)})")
    if model is not None:
        opts["model"] = model
    return cls(**opts)


def preflight(provider: Provider) -> tuple[bool, str]:
    """Cheap reachability + model-presence check before joining a room.

    Returns ``(ok, message)``. Lets ``/ai start`` fail fast with a clear reason
    (backend down / model not pulled / key missing) instead of erroring on the
    first question. Providers without ``available_models`` are assumed reachable.
    """
    discover = getattr(provider, "available_models", None)
    if discover is None:
        return True, f"{provider.name}: no discovery endpoint — assuming reachable"
    try:
        models = discover()
    except Exception as e:  # noqa: BLE001 — any failure means "not reachable yet"
        return False, f"{provider.name}: cannot reach backend ({e})"
    if provider.model in models:
        return True, f"{provider.name}/{provider.model}: reachable"
    if models:
        sample = ", ".join(models[:8])
        more = "…" if len(models) > 8 else ""
        return False, (
            f"{provider.name}: model '{provider.model}' not available. "
            f"reachable models: {sample}{more}"
        )
    return True, f"{provider.name}: reachable (empty model list — skipping check)"
