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

    # a custom provider you wrote
    python -m cmd_chat.agent 127.0.0.1 3000 --provider mypkg.mod:MyProvider
"""

from __future__ import annotations

import argparse

from .bridge import AgentBridge
from .providers import make_provider


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="cmd_chat.agent", description="hack-house AI agent bridge (PoC)"
    )
    ap.add_argument("server")
    ap.add_argument("port", type=int)
    ap.add_argument("--name", default="oracle", help="agent's room display name")
    ap.add_argument("--password", default=None, help="room password")
    ap.add_argument("--provider", default="ollama",
                    help="ollama | anthropic | openai | module:Class")
    ap.add_argument("--model", default=None, help="model name (provider default if omitted)")
    ap.add_argument("--base-url", default=None, help="endpoint for openai-compatible providers")
    ap.add_argument("--system", default=None, help="override the system prompt")
    ap.add_argument("--context-window", type=int, default=12)
    ap.add_argument("--insecure", action="store_true", help="skip TLS cert verification")
    ap.add_argument("--no-tls", action="store_true", help="plain ws/http (local/Tailscale)")
    args = ap.parse_args()

    opts: dict = {}
    if args.base_url and (args.provider == "openai" or ":" in args.provider):
        opts["base_url"] = args.base_url
    provider = make_provider(args.provider, model=args.model, **opts)

    bridge = AgentBridge(
        args.server, args.port, name=args.name, provider=provider,
        password=args.password, insecure=args.insecure, no_tls=args.no_tls,
        system_prompt=args.system, context_window=args.context_window,
    )
    try:
        bridge.run()
    except KeyboardInterrupt:
        print("\nagent stopped")


if __name__ == "__main__":
    main()
