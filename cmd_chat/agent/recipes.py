"""Instance recipes — durable launch definitions for a hosted AI instance.

Phase 5 of docs/spec-multi-tenant-model-hosting.md. A *recipe* is the reusable
part of an `/ai start` (which model + how it is scoped): provider/model or a
profile name, the query ACL, the allowlist, ask-mode, and the owner. It lets a
member re-launch "my oracle" with one command across sessions.

A recipe NEVER stores credentials — a profile already names an `api_key_env`,
and local models need none — so the store is safe to keep and share. It also
omits the runtime coordinates (host/port/password), which belong to whichever
room you join, not to the instance definition.

Store: JSON object keyed by recipe name at ``$HH_INSTANCES_FILE`` (override for
tests), else ``~/.hh/instances.json``.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


def recipes_path() -> Path:
    """Where recipes live. `$HH_INSTANCES_FILE` wins (tests/CI); else the
    host-global `~/.hh/instances.json` beside the VM registry."""
    override = os.environ.get("HH_INSTANCES_FILE")
    if override:
        return Path(override)
    return Path(os.environ.get("HOME", ".")) / ".hh" / "instances.json"


@dataclass
class Recipe:
    """A reusable instance launch definition. No creds, no runtime coordinates."""

    name: str
    provider: str | None = None
    model: str | None = None
    profile: str | None = None
    harness: str | None = None
    query_acl: str = "public"
    allow: list[str] = field(default_factory=list)
    ask_mode: bool = False
    owner: str | None = None

    def to_argv(self) -> list[str]:
        """Reconstruct the ``python -m cmd_chat.agent`` flags this recipe implies.
        Host/port/password/TLS are runtime and added by the caller, not here."""
        argv = ["--name", self.name]
        if self.profile:
            argv += ["--profile", self.profile]
        else:
            if self.provider:
                argv += ["--provider", self.provider]
            if self.model:
                argv += ["--model", self.model]
        if self.harness:
            argv += ["--harness", self.harness]
        argv += ["--query-acl", self.query_acl]
        if self.allow:
            argv += ["--allow", ",".join(self.allow)]
        if self.ask_mode:
            argv += ["--ask-mode"]
        if self.owner:
            argv += ["--owner", self.owner]
        return argv


def _load_all(path: Path | None = None) -> dict[str, dict]:
    p = path or recipes_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_all(store: dict[str, dict], path: Path | None = None) -> None:
    p = path or recipes_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store, indent=2, sort_keys=True))


def save(recipe: Recipe, path: Path | None = None) -> None:
    """Persist (or overwrite) a recipe by name."""
    store = _load_all(path)
    store[recipe.name] = {k: v for k, v in asdict(recipe).items() if k != "name"}
    _write_all(store, path)


def load(name: str, path: Path | None = None) -> Recipe | None:
    """Return the named recipe, or None if absent."""
    store = _load_all(path)
    entry = store.get(name)
    if entry is None:
        return None
    # Tolerate old/extra keys: keep only the fields Recipe declares.
    fields = Recipe.__dataclass_fields__.keys()
    clean = {k: v for k, v in entry.items() if k in fields}
    return Recipe(name=name, **clean)


def list_recipes(path: Path | None = None) -> list[str]:
    """Names of all saved recipes, sorted."""
    return sorted(_load_all(path).keys())


def delete(name: str, path: Path | None = None) -> bool:
    """Remove a recipe. Returns True if it existed."""
    store = _load_all(path)
    if name not in store:
        return False
    del store[name]
    _write_all(store, path)
    return True
