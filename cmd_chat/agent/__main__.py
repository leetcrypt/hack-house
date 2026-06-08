"""CLI: run an AI agent that joins a hack-house room.

Examples
--------
    # local Ollama (default, recommended) — joins as "qwen2.5:3b"
    python -m cmd_chat.agent 127.0.0.1 3000 \
        --password hunter2 --model qwen2.5:3b --no-tls

    # cloud, opt-in
    python -m cmd_chat.agent 127.0.0.1 3000 --name claude \
        --provider anthropic --model claude-opus-4-6 --password hunter2 --no-tls

    # any OpenAI-compatible endpoint (Groq, Together, local vLLM…)
    python -m cmd_chat.agent 127.0.0.1 3000 --provider openai \
        --base-url https://api.groq.com/openai/v1 --model llama-3.1-70b --password hunter2

    # a named profile from models.toml (provider + model + endpoint + key env)
    python -m cmd_chat.agent 127.0.0.1 3000 --profile groq-llama --password hunter2

    # a custom provider you wrote
    python -m cmd_chat.agent 127.0.0.1 3000 --provider mypkg.mod:MyProvider

    # discovery / preflight (no room join)
    python -m cmd_chat.agent --profile groq-llama --list-models
    python -m cmd_chat.agent --profile groq-llama --check
"""

from __future__ import annotations

import argparse
import sys

from .bridge import GOOSE_MAX_TURNS, AgentBridge
from .profiles import load_profiles, provider_from_profile
from .providers import OllamaEmbedder, make_provider, preflight


def _build_provider(args, ap):
    """Resolve a Provider from either --profile or the explicit flags."""
    if args.profile:
        profiles = load_profiles(args.models_file)
        if args.profile not in profiles:
            known = ", ".join(profiles) or "(none — create models.toml)"
            ap.error(f"unknown profile '{args.profile}'. known: {known}")
        prof = profiles[args.profile]
        provider = provider_from_profile(
            prof, name=args.profile, model=args.model, base_url=args.base_url
        )
        # Profile may also supply non-provider defaults.
        if args.system is None and prof.get("system"):
            args.system = prof["system"]
        if args.context_window == 12 and prof.get("context_window"):
            args.context_window = int(prof["context_window"])
        if args.harness is None and prof.get("harness"):
            args.harness = prof["harness"]
        return provider

    opts: dict = {}
    if args.base_url and (args.provider == "openai" or ":" in args.provider):
        opts["base_url"] = args.base_url
    return make_provider(args.provider, model=args.model, **opts)


def _apply_ollama_tuning(provider, args) -> None:
    """Push CPU-perf flags onto an Ollama chat/code provider. No-op otherwise —
    the knobs (num_ctx/num_thread/num_predict) only exist on OllamaProvider."""
    if getattr(provider, "name", None) != "ollama":
        return
    if args.num_ctx is not None:
        provider.num_ctx = args.num_ctx
    if args.num_thread is not None:
        provider.num_thread = args.num_thread
    if args.num_predict is not None:
        provider.num_predict = args.num_predict


# Coder models preferred for the sandbox path, fastest-first (CPU).
_CODER_MODELS = ("qwen2.5-coder:1.5b", "qwen2.5-coder:3b", "qwen2.5-coder")


def _build_code_provider(provider, args):
    """A code-specialized provider for the sandbox `!task` path. Only meaningful
    for Ollama: use --code-model if given, else auto-select a present
    qwen2.5-coder build. Returns None to fall back to the chat provider."""
    if getattr(provider, "name", None) != "ollama":
        return None
    code_model = args.code_model
    if code_model is None:
        try:
            models = set(provider.available_models())
        except Exception:  # noqa: BLE001 — discovery down → no separate code path
            models = set()
        code_model = next((m for m in _CODER_MODELS if m in models), None)
    if not code_model or code_model == provider.model:
        return None
    code = make_provider("ollama", model=code_model, host=provider.host)
    _apply_ollama_tuning(code, args)
    return code


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="cmd_chat.agent", description="hack-house AI agent bridge (PoC)"
    )
    ap.add_argument("server", nargs="?", help="room host (omit with --list-models/--check)")
    ap.add_argument("port", type=int, nargs="?", help="room port")
    ap.add_argument("--name", default=None,
                    help="agent's room display name (default: the model tag, e.g. qwen2.5:3b)")
    ap.add_argument("--password", default=None, help="room password")
    ap.add_argument("--provider", default="ollama",
                    help="ollama | anthropic | openai | module:Class")
    ap.add_argument("--profile", default=None,
                    help="named profile from models.toml (overrides --provider/--model)")
    ap.add_argument("--models-file", default=None,
                    help="path to models.toml (default: $HH_MODELS_FILE, ./models.toml, ~/.config/hh/models.toml)")
    ap.add_argument("--model", default=None, help="model name (provider default if omitted)")
    ap.add_argument("--code-model", default=None,
                    help="Ollama model for the sandbox/code path (default: auto-select qwen2.5-coder if present)")
    ap.add_argument("--base-url", default=None, help="endpoint for openai-compatible providers")
    ap.add_argument("--num-ctx", type=int, default=None,
                    help="Ollama context window (CPU: smaller = faster prefill; default 4096)")
    ap.add_argument("--num-thread", type=int, default=None,
                    help="Ollama CPU threads (default: Ollama's own ≈ physical cores; benchmark 4/6/8)")
    ap.add_argument("--num-predict", type=int, default=None,
                    help="Ollama max reply tokens (default 512)")
    ap.add_argument("--harness", choices=["goose", "simple"], default=None,
                    help="sandbox !task harness: goose (agentic loop run inside the "
                         "sandbox; default) or simple (legacy one-shot injector). "
                         "Goose auto-degrades to simple if its binary isn't in the sandbox.")
    ap.add_argument("--goose-max-turns", type=int, default=GOOSE_MAX_TURNS,
                    help="max turns for a Goose agentic run (default %(default)s)")
    ap.add_argument("--system", default=None, help="override the system prompt")
    ap.add_argument("--context-window", type=int, default=12,
                    help="max prior messages fed to the model per reply")
    ap.add_argument("--token-budget", type=int, default=2000,
                    help="approx token cap on the context window (whichever is smaller wins)")
    ap.add_argument("--no-rag", action="store_true",
                    help="disable in-RAM semantic recall (recency-only context)")
    ap.add_argument("--embed-model", default="nomic-embed-text",
                    help="Ollama model used to embed messages for recall")
    ap.add_argument("--embed-host", default=None,
                    help="Ollama host for embeddings (default: chat host or $OLLAMA_HOST)")
    ap.add_argument("--rag-top-k", type=int, default=4,
                    help="how many recalled messages to surface per reply")
    ap.add_argument("--embed-dim", type=int, default=256,
                    help="truncate embedding vectors to this many dims (MRL; 0 = full vector)")
    ap.add_argument("--list-models", action="store_true",
                    help="list models the backend can serve, then exit")
    ap.add_argument("--check", action="store_true",
                    help="run a reachability/model preflight, then exit (0 ok, 1 fail)")
    ap.add_argument("--insecure", action="store_true", help="skip TLS cert verification")
    ap.add_argument("--no-tls", action="store_true", help="plain ws/http (local/Tailscale)")
    args = ap.parse_args()

    provider = _build_provider(args, ap)
    _apply_ollama_tuning(provider, args)

    # Discovery / preflight modes never join a room.
    if args.list_models:
        discover = getattr(provider, "available_models", None)
        if discover is None:
            ap.error(f"provider '{provider.name}' has no model discovery")
        for m in discover():
            print(m)
        return
    if args.check:
        ok, msg = preflight(provider)
        print(("ok: " if ok else "FAIL: ") + msg, file=sys.stderr if not ok else sys.stdout)
        sys.exit(0 if ok else 1)

    if args.server is None or args.port is None:
        ap.error("server and port are required to join a room")

    # Non-fatal preflight: warn early, but still try (discovery may be blocked
    # while completion works).
    ok, msg = preflight(provider)
    if not ok:
        print(f"⚠ preflight: {msg}", file=sys.stderr)

    # In-RAM semantic recall is on by default and local (Ollama embeddings),
    # independent of which provider answers chat. Reuse the chat host if it's an
    # Ollama provider so a single --host/profile covers both.
    embedder = None
    if not args.no_rag:
        embedder = OllamaEmbedder(
            model=args.embed_model,
            host=args.embed_host or getattr(provider, "host", None),
            truncate_dim=args.embed_dim or None,
        )

    # Separate coder model for the sandbox path (Ollama only); None → reuse chat.
    code_provider = _build_code_provider(provider, args)
    if code_provider is not None:
        print(f"sandbox/code path → {code_provider.name}/{code_provider.model}", file=sys.stderr)

    # Default the room handle to the model tag (model name + parameter size,
    # e.g. "qwen2.5:3b") so the roster shows what's actually answering.
    name = args.name or provider.model
    bridge = AgentBridge(
        args.server, args.port, name=name, provider=provider,
        password=args.password, insecure=args.insecure, no_tls=args.no_tls,
        system_prompt=args.system, context_window=args.context_window,
        token_budget=args.token_budget, embedder=embedder, rag_top_k=args.rag_top_k,
        code_provider=code_provider, harness=args.harness or "goose",
        goose_max_turns=args.goose_max_turns,
    )
    try:
        bridge.run()
    except KeyboardInterrupt:
        print("\nagent stopped")


if __name__ == "__main__":
    main()
