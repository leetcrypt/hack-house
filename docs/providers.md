# Connecting any model — provider guide

The hack-house AI agent is **model-agnostic**: a *provider* is anything that can
turn a system prompt + a conversation into one reply string. You can use a
bundled adapter, point an OpenAI-compatible adapter at any endpoint, name a
reusable profile, or drop in a provider you wrote yourself.

> Design note: this mirrors the BYO-model conventions used in the wider
> ecosystem — a named `models:` list with `{provider, model, apiBase, apiKey}`
> entries (Continue.dev) and a `model_list` of `{model, api_base, api_key}`
> behind one unified interface (LiteLLM, which `aider` builds on). One thin
> adapter for the OpenAI `/chat/completions` shape covers most backends.

---

## 1. The fastest path — a named profile

Add (or edit) `models.toml` in the repo root (or `~/.config/hh/models.toml`):

```toml
[groq-llama]
provider    = "openai"
base_url    = "https://api.groq.com/openai/v1"
model       = "llama-3.3-70b-versatile"
api_key_env = "GROQ_API_KEY"
```

Export the key, then start the agent by name:

```bash
export GROQ_API_KEY=sk-...
python -m cmd_chat.agent <host> <port> --profile groq-llama --password <pw> --no-tls
# or from the TUI:  /ai start groq-llama
```

`api_key_env` names an **environment variable**, never the key itself, so
`models.toml` is safe to commit and share. Lookup order for the file:
`$HH_MODELS_FILE` → `./models.toml` → `~/.config/hh/models.toml` (override with
`--models-file`).

Profile keys: `provider` (required), `model`, `base_url`, `host` (Ollama),
`api_key_env`, `system`, `context_window`. CLI `--model` / `--base-url` override
the profile.

## 2. Without a profile — explicit flags

```bash
# local Ollama (default, private — no key)
python -m cmd_chat.agent <host> <port> --provider ollama --model qwen2.5:3b --no-tls

# any OpenAI-compatible endpoint (OpenAI, Groq, Together, vLLM, LM Studio, llama.cpp…)
python -m cmd_chat.agent <host> <port> --provider openai \
    --base-url https://api.together.xyz/v1 --model <id>

# Anthropic
ANTHROPIC_API_KEY=sk-ant-... python -m cmd_chat.agent <host> <port> \
    --provider anthropic --model claude-opus-4-6
```

Built-in providers: `ollama`, `anthropic`, `openai`. The `openai` adapter is the
universal one — most backends speak `/chat/completions`, so "any model" is
usually just `base_url` + `model` + a key.

## 3. Discovery & preflight

Check a backend before joining a room (neither joins):

```bash
python -m cmd_chat.agent --profile groq-llama --list-models   # enumerate models
python -m cmd_chat.agent --profile groq-llama --check         # exit 0 ok / 1 fail
```

On a normal start the agent runs a non-fatal preflight and prints a `⚠ preflight`
warning if the backend is unreachable or the model isn't pulled — so you find out
immediately, not on the first question. In-room:

- `/ai list` — each present agent answers with its roster line
  (`name (ai) — provider/model, context N`); use it to find an agent's name
  before addressing it with `/ai <name> <question>`.
- `/ai models` — the active agent lists what its backend can serve
  (`*` marks the active model).

## 4. Bring your own provider

Implement three things — `name`, `model`, and `complete()`:

```python
class MyProvider:
    name = "mine"

    def __init__(self, model: str = "my-default"):
        self.model = model

    def complete(self, system: str, messages: list) -> str:
        # messages: list of objects with .role ("user"/"assistant") and .content
        ...
        return "the reply"

    def available_models(self) -> list[str]:   # optional: powers discovery/preflight
        return ["my-default"]
```

Point the agent at it with `module:Class` (no repo changes needed):

```bash
python -m cmd_chat.agent <host> <port> --provider mypkg.mymodule:MyProvider
```

or reference it from a profile:

```toml
[mine]
provider = "mypkg.mymodule:MyProvider"
model    = "my-default"
```

A complete, runnable example lives in
[`examples/echo_provider.py`](../examples/echo_provider.py):

```bash
python -m cmd_chat.agent <host> <port> --no-tls --password <pw> \
    --provider examples.echo_provider:EchoProvider
```

`available_models()` is optional — implement it to light up `--list-models`,
`--check`, and `/ai models`; omit it and those degrade gracefully.
