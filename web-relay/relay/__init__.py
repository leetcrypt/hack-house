"""hack-house web relay (P0) — a self-hosted, zero-knowledge broker that fans a
host's live terminal to browsers over WSS. Separate trust domain from the chat
server. See docs/spec-lobby-web-relay.md."""

from .app import create_app

__all__ = ["create_app"]
