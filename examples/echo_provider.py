"""Minimal bring-your-own Provider example.

A Provider just turns (system prompt + messages) into one reply string. Anything
with ``name``, ``model`` and a ``complete()`` method qualifies — no base class,
no SDK. Point the agent at it with the ``module:Class`` spec:

    python -m cmd_chat.agent 127.0.0.1 3000 --no-tls --password hunter2 \
        --provider examples.echo_provider:EchoProvider

or via models.toml:

    [echo]
    provider = "examples.echo_provider:EchoProvider"
    model    = "echo-1"

Implementing ``available_models()`` is optional; it powers ``--list-models``,
``--check`` preflight, and the in-room ``/ai models`` command.
"""

from __future__ import annotations


class EchoProvider:
    name = "echo"

    def __init__(self, model: str = "echo-1"):
        self.model = model

    def complete(self, system: str, messages: list) -> str:
        last = messages[-1].content if messages else ""
        return f"echo: {last}"

    def available_models(self) -> list[str]:  # optional
        return ["echo-1"]
