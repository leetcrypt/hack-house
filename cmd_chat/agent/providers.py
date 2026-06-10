"""Model-agnostic provider interface for the hack-house AI agent bridge.

A Provider turns a system prompt + conversation into a single reply string.
The bundled adapters speak plain HTTP via ``requests`` (already a dependency),
so no extra SDKs are required and any backend can be plugged in — including a
custom one via the ``module:Class`` spec.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import requests


@dataclass
class Msg:
    role: str  # "system" | "user" | "assistant"
    content: str


class ToolsUnsupported(RuntimeError):
    """Raised by ``complete_with_tools`` when the backend model can't do function
    calling — the native harness catches it and degrades to the simple injector."""


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

    def __init__(self, model: str = "llama3", host: str | None = None, timeout: int = 240,
                 num_ctx: int = 4096, num_predict: int = 512, num_thread: int | None = None,
                 keep_alive: str = "30m"):
        self.model = model
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        # Default 240s: the native tool-calling turn is NON-streaming, so on a
        # contended CPU box a long write_file turn can exceed a tighter cap and
        # surface as `[ai error: read timed out]`. Generous here, bounded loop above.
        self.timeout = timeout
        # On CPU, time-to-first-token is O(num_ctx) prefill, so keep the window
        # modest (4096) rather than a GPU-mindset 8192. keep_alive pins the model
        # so the next /ai doesn't pay a cold reload. num_thread defaults to
        # Ollama's own (≈physical cores); set it explicitly to benchmark 4/6/8.
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.num_thread = num_thread
        self.keep_alive = keep_alive
        # Tri-state tool-calling capability cache: None=unprobed, True/False once a
        # real /api/chat with `tools` either succeeds or is rejected by the model.
        # The native harness reads this to skip retrying tools on a model that
        # can't do them (and fall straight to the simple injector).
        self._tools_ok: bool | None = None

    def _options(self) -> dict:
        opts = {"num_ctx": self.num_ctx, "num_predict": self.num_predict}
        if self.num_thread is not None:
            opts["num_thread"] = self.num_thread
        return opts

    def _raise_for_status(self, r: requests.Response) -> None:
        """Turn an Ollama HTTP error into an actionable message.

        Ollama answers /api/chat with 404 + ``{"error": "model ... not found"}``
        when the model isn't pulled on the box running this agent. Because the
        agent talks to *its own* localhost:11434, a teammate who summoned /ai
        without that model pulled hits this even when the host has it. The bare
        ``raise_for_status`` only reports "404 Not Found for url", hiding the
        cause — so name the model, the host, and the fix instead. This text is
        what the bridge posts to the room as ``[ai error: …]``.
        """
        if r.ok:
            return
        try:
            detail = (r.json().get("error") or "").strip()
        except ValueError:
            detail = (r.text or "").strip()
        if r.status_code == 404:
            raise RuntimeError(
                f"model '{self.model}' isn't pulled on the ollama at {self.host} "
                f"(the agent uses ollama on the machine that ran /ai, not the host). "
                f"fix: `ollama pull {self.model}` there, or `/ai start <profile>` "
                f"for a cloud model. [{detail or 'model not found'}]"
            )
        raise RuntimeError(
            f"ollama at {self.host} returned {r.status_code}"
            + (f": {detail}" if detail else "")
        )

    def complete(self, system: str, messages: list[Msg]) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": self._options(),
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
        }
        r = requests.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
        self._raise_for_status(r)
        return (r.json().get("message", {}).get("content") or "").strip()

    def supports_tools(self) -> bool | None:
        """Cached tool-calling capability: None until the first ``complete_with_tools``
        call has either succeeded or been rejected by the model."""
        return self._tools_ok

    def complete_with_tools(
        self, system: str, messages: list[dict], tools: list[dict]
    ) -> tuple[str, list[dict]]:
        """One non-streaming ``/api/chat`` turn carrying a ``tools`` schema. Used by
        the native harness loop. ``messages`` are raw Ollama wire dicts (so the
        caller can round-trip assistant ``tool_calls`` and ``tool`` results across
        turns); ``system`` is prepended. Returns ``(text, tool_calls)`` where each
        call is ``{"name": str, "arguments": dict}``. Raises ``ToolsUnsupported`` if
        the model can't do function calling so the bridge can fall back to simple."""
        payload = {
            "model": self.model,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": self._options(),
            "tools": tools,
            "messages": [{"role": "system", "content": system}] + messages,
        }
        r = requests.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
        if not r.ok:
            try:
                detail = (r.json().get("error") or "").strip()
            except ValueError:
                detail = (r.text or "").strip()
            if "does not support tools" in detail.lower():
                self._tools_ok = False
                raise ToolsUnsupported(detail or f"{self.model} does not support tools")
            self._raise_for_status(r)
        self._tools_ok = True
        msg = r.json().get("message", {}) or {}
        text = (msg.get("content") or "").strip()
        calls: list[dict] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append({"name": fn.get("name", ""), "arguments": args or {}})
        # Small/quantized models (notably qwen2.5 on CPU) intermittently emit a valid
        # tool call as literal text in `content` instead of the structured `tool_calls`
        # field — qwen's `<tool_call>{…}</tool_call>`, but also bare/fenced JSON and
        # alternate wrappers (`<tools>`, `<function_call>`). This is the single biggest
        # score sink in the native-harness benchmark, so recover any well-formed JSON
        # call here. Gate on the known tool names from `tools` so a stray JSON blob in
        # prose can never be coerced into an action the model didn't structurally ask
        # for. Only adopt the recovery when it actually found a call (a plain `DONE:`
        # or prose turn is left untouched).
        if not calls and text:
            valid = {(t.get("function") or {}).get("name") for t in (tools or [])}
            valid.discard(None)
            recovered_text, recovered = self._extract_text_tool_calls(text, valid)
            if recovered:
                text, calls = recovered_text, recovered
        return text, calls

    # Wrapper tags a weak model wraps a leaked call (or its prose) in; stripped
    # from the chat-facing text once the JSON inside is recovered.
    _WRAP_TAGS = re.compile(
        r"</?(?:tool_call|tool_calls|function_call|function|tools|native)>",
        re.I,
    )

    @classmethod
    def _coerce_call(cls, obj, valid_names) -> dict | None:
        """Turn a decoded JSON object into a `{"name","arguments"}` call IF it
        structurally is one for a KNOWN tool — else None. Unwraps the OpenAI-style
        `{"function": {...}}` / `{"tool_call": {...}}` nesting and accepts either
        `arguments` or qwen's `parameters` key. The `valid_names` gate is what makes
        scanning arbitrary text safe: a random JSON blob in prose has no known tool
        name, so it can never be coerced into an action."""
        if not isinstance(obj, dict):
            return None
        inner = obj.get("function") or obj.get("tool_call")
        if isinstance(inner, dict):
            obj = inner
        name = obj.get("name")
        if not isinstance(name, str) or not name:
            return None
        if valid_names and name not in valid_names:
            return None
        args = obj.get("arguments")
        if args is None:
            args = obj.get("parameters")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        return {"name": name, "arguments": args}

    @classmethod
    def _extract_text_tool_calls(
        cls, text: str, valid_names: set | None = None
    ) -> tuple[str, list[dict]]:
        """Recover tool calls a small/quantized model emitted as TEXT in `content`
        instead of the structured `tool_calls` field. Handles qwen's
        `<tool_call>{json}</tool_call>` blocks plus the looser CPU-model leaks: bare
        JSON, ```json fenced blocks, and alternate wrapper tags (`<tools>`,
        `<function_call>`). Scans for every JSON object via a decoder (so nested
        braces in arguments parse correctly) and keeps ONLY those that `_coerce_call`
        accepts as a known tool — never freeform prose, so it can't fabricate an
        action the model didn't structurally request. Returns the text with the
        recovered JSON (and now-orphaned wrapper tags / code fences) stripped, plus
        the calls."""
        dec = json.JSONDecoder()
        calls: list[dict] = []
        spans: list[tuple[int, int]] = []
        i, n = 0, len(text)
        while i < n:
            brace = text.find("{", i)
            if brace == -1:
                break
            try:
                obj, end = dec.raw_decode(text, brace)
            except ValueError:
                i = brace + 1
                continue
            call = cls._coerce_call(obj, valid_names)
            if call is not None:
                calls.append(call)
                spans.append((brace, end))
            i = end
        if spans:
            kept, last = [], 0
            for start, stop in spans:
                kept.append(text[last:start])
                last = stop
            kept.append(text[last:])
            text = "".join(kept)
            # The JSON is gone; drop the wrapper tags and any now-empty code fences
            # it sat in so the chat summary reads as clean prose.
            text = cls._WRAP_TAGS.sub("", text)
            text = re.sub(r"```[a-zA-Z]*\s*```", "", text)
            text = re.sub(r"```[a-zA-Z]*|```", "", text)
            text = text.strip()
        return text, calls

    def stream(self, system: str, messages: list[Msg]):
        """Yield reply text incrementally as Ollama generates it. On CPU the
        perceived latency is TTFT, so streaming makes a slow reply feel live."""
        payload = {
            "model": self.model,
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": self._options(),
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
        }
        with requests.post(f"{self.host}/api/chat", json=payload,
                           timeout=self.timeout, stream=True) as r:
            self._raise_for_status(r)
            for line in r.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                piece = chunk.get("message", {}).get("content")
                if piece:
                    yield piece
                if chunk.get("done"):
                    break

    def available_models(self) -> list[str]:
        r = requests.get(f"{self.host}/api/tags", timeout=self.timeout)
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", [])]


class OllamaEmbedder:
    """Local text embeddings via Ollama (default ``nomic-embed-text``), used for
    the agent's in-RAM semantic recall. Local + free, so it stays on by default
    regardless of which provider answers chat. No key, nothing persisted."""

    name = "ollama-embed"

    def __init__(self, model: str = "nomic-embed-text", host: str | None = None,
                 timeout: int = 60, truncate_dim: int | None = 256):
        self.model = model
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.timeout = timeout
        # nomic-embed-text is Matryoshka (MRL)-trained, so its 768-dim vector can
        # be truncated to a shorter prefix with little quality loss — faster
        # pure-Python cosine and less RAM. Query + stored use the same dim, so
        # cosine stays correct. None keeps the full vector.
        self.truncate_dim = truncate_dim

    def embed(self, text: str) -> list[float]:
        r = requests.post(
            f"{self.host}/api/embeddings",
            json={"model": self.model, "prompt": text},
            timeout=self.timeout,
        )
        r.raise_for_status()
        vec = r.json().get("embedding") or []
        if self.truncate_dim is not None:
            vec = vec[: self.truncate_dim]
        return vec


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
