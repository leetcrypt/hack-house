"""CLI: run an AI agent that joins a hack-house room.

Examples
--------
    # local Ollama (default, recommended)
    python -m cmd_chat.agent 127.0.0.1 3000 --name oracle \
        --password hunter2 --model llama3 --no-tls

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

from .bridge import AgentBridge
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
        return provider

    opts: dict = {}
    if args.base_url and (args.provider == "openai" or ":" in args.provider):
        opts["base_url"] = args.base_url
    return make_provider(args.provider, model=args.model, **opts)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="cmd_chat.agent", description="hack-house AI agent bridge (PoC)"
    )
    ap.add_argument("server", nargs="?", help="room host (omit with --list-models/--check)")
    ap.add_argument("port", type=int, nargs="?", help="room port")
    ap.add_argument("--name", default="oracle", help="agent's room display name")
    ap.add_argument("--password", default=None, help="room password")
    ap.add_argument("--provider", default="ollama",
                    help="ollama | anthropic | openai | module:Class")
    ap.add_argument("--profile", default=None,
                    help="named profile from models.toml (overrides --provider/--model)")
    ap.add_argument("--models-file", default=None,
                    help="path to models.toml (default: $HH_MODELS_FILE, ./models.toml, ~/.config/hh/models.toml)")
    ap.add_argument("--model", default=None, help="model name (provider default if omitted)")
    ap.add_argument("--base-url", default=None, help="endpoint for openai-compatible providers")
    ap.add_argument("--system", default=None, help="override the system prompt")
    ap.add_argument("--context-window", type=int, default=12,
                    help="max prior messages fed to the model per reply")
    ap.add_argument("--token-budget", type=int, default=3000,
                    help="approx token cap on the context window (whichever is smaller wins)")
    ap.add_argument("--no-rag", action="store_true",
                    help="disable in-RAM semantic recall (recency-only context)")
    ap.add_argument("--embed-model", default="nomic-embed-text",
                    help="Ollama model used to embed messages for recall")
    ap.add_argument("--embed-host", default=None,
                    help="Ollama host for embeddings (default: chat host or $OLLAMA_HOST)")
    ap.add_argument("--rag-top-k", type=int, default=4,
                    help="how many recalled messages to surface per reply")
    ap.add_argument("--list-models", action="store_true",
                    help="list models the backend can serve, then exit")
    ap.add_argument("--check", action="store_true",
                    help="run a reachability/model preflight, then exit (0 ok, 1 fail)")
    ap.add_argument("--insecure", action="store_true", help="skip TLS cert verification")
    ap.add_argument("--no-tls", action="store_true", help="plain ws/http (local/Tailscale)")
    args = ap.parse_args()

    provider = _build_provider(args, ap)

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
        )

    bridge = AgentBridge(
        args.server, args.port, name=args.name, provider=provider,
        password=args.password, insecure=args.insecure, no_tls=args.no_tls,
        system_prompt=args.system, context_window=args.context_window,
        token_budget=args.token_budget, embedder=embedder, rag_top_k=args.rag_top_k,
    )
    try:
        bridge.run()
    except KeyboardInterrupt:
        print("\nagent stopped")


if __name__ == "__main__":
    main()
