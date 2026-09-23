"""Named model profiles for the hack-house AI agent.

A *profile* maps a friendly name (``groq-llama``, ``local``, ``claude``) to a
provider + model + endpoint, so operators type ``--profile groq-llama`` instead
of remembering ``--provider openai --base-url … --model …``. This mirrors the
``models:`` list in Continue.dev and the ``model_list`` in a LiteLLM proxy:
each entry is ``{provider, model, base_url, api_key_env}``.

Secrets are **never** stored here — ``api_key_env`` names an environment
variable to read the key from, keeping the file safe to commit and share.

Lookup order (first hit wins):
    1. ``$HH_MODELS_FILE``
    2. ``./models.toml`` (cwd)
    3. ``~/.config/hh/models.toml``
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # stdlib on 3.11+, falls back to the `tomli` backport on 3.10
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from .providers import Provider, make_provider

_RECOGNIZED = {"provider", "model", "base_url", "host", "api_key_env",
               "system", "context_window"}


def _candidate_paths(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    paths = []
    env = os.environ.get("HH_MODELS_FILE")
    if env:
        paths.append(Path(env).expanduser())
    paths.append(Path.cwd() / "models.toml")
    paths.append(Path.home() / ".config" / "hh" / "models.toml")
    return paths


def find_profiles_file(explicit: str | None = None) -> Path | None:
    for p in _candidate_paths(explicit):
        if p.is_file():
            return p
    return None


def load_profiles(explicit: str | None = None) -> dict[str, dict]:
    """Return ``{name: profile_dict}`` from the first models.toml found."""
    path = find_profiles_file(explicit)
    if path is None:
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    profiles: dict[str, dict] = {}
    for name, body in data.items():
        if not isinstance(body, dict) or "provider" not in body:
            continue  # skip non-profile tables / malformed entries
        unknown = set(body) - _RECOGNIZED
        if unknown:
            raise ValueError(
                f"profile '{name}': unknown key(s) {', '.join(sorted(unknown))}"
            )
        profiles[name] = body
    return profiles


def provider_from_profile(prof: dict, *, name: str = "?",
                          model: str | None = None,
                          base_url: str | None = None) -> Provider:
    """Build a :class:`Provider` from a profile dict.

    ``model`` / ``base_url`` (CLI flags) override the profile when given. The
    api key is read from ``$<api_key_env>`` and passed only to providers that
    accept one, so an Ollama profile never sees a stray ``api_key`` kwarg.
    """
    spec = prof["provider"]
    custom = ":" in spec
    opts: dict = {}

    mdl = model or prof.get("model")
    bu = base_url or prof.get("base_url")
    if bu and (spec == "openai" or custom):
        opts["base_url"] = bu
    if spec == "ollama" and prof.get("host"):
        opts["host"] = prof["host"]

    key_env = prof.get("api_key_env")
    if key_env and (spec in ("openai", "anthropic") or custom):
        key = os.environ.get(key_env)
        if not key:
            raise SystemExit(
                f"profile '{name}': ${key_env} is not set — export it first"
            )
        opts["api_key"] = key

    return make_provider(spec, model=mdl, **opts)
