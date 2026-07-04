# hack-house → AI Agent Bridge — Spec

> **Status:** Draft v1 · **Date:** 2026-06-01
> **Scope:** Let model-agnostic AI agents participate in a hack-house room as
> first-class encrypted clients — addressed on demand to answer questions, do
> research, check work, and give hints — without weakening the zero-knowledge
> server, E2E Fernet, or SRP guarantees.
> **Baseline reviewed:** `cmd_chat/` server + `hh/` client @ `dev`.

---

## 0. Decisions locked (from product owner)

| # | Decision | Choice |
|---|----------|--------|
| A | Runtime language | **Python** agent runtime (`cmd_chat/agent/`), reusing the existing client's SRP/Fernet/WS plumbing. Not in the Rust TUI. |
| B | Default model | **Ollama, local, recommended.** Privacy-preserving default. |
| C | Model-agnostic | A **provider plugin interface** any backend can implement (ollama, anthropic, openai-compatible, or a custom `module:Class`). |
| D | Addressing | **`/ai` command.** `/ai <question>` if exactly one agent present; `/ai <agent> <question>` to target one by name. |
| E | Sandbox access | Per-agent, owner-gated, mirroring `/drive`: `/ai enable-sbx <agent>` / `/ai disable-sbx <agent>`. Default **off**. |
| F | Operator permission | Operating an agent is privileged. The **room admin** (host) may `/ai grant <user>` / `/ai revoke <user>` to let non-hosts bring/operate an agent. |
| G | Capacity | Each agent consumes one room seat against `max_users`. MVP documents raising `CMD_CHAT_MAX_USERS`; a server-side "agent pool" is a possible later change. |

---

## 1. Core model

**An agent is just another client.** It runs SRP with the room password + a
display name, derives the room key `HKDF(password, room_salt)`, opens the WS,
decrypts every broadcast, and — only when explicitly addressed — encrypts a
reply and sends it like any human. The server (`chat_ws`, a blind relay that
stamps the authenticated `username` on each frame) needs **no changes** for the
MVP.

```
  human TUI ─┐                          ┌─ agent runtime (headless python client)
             ├── encrypted WS ─ SERVER ─┤    decrypt → addressed? → Provider.complete()
  human TUI ─┘     (blind relay)        └─    → encrypt reply → send as chat
```

This preserves all existing guarantees: the server still sees only ciphertext;
the agent only reads a room it already has the password for.

### Privacy posture (non-negotiable defaults)
1. **Addressed-only.** The agent reads everything (it's a client) but forwards to
   the model *only* what triggers it. No passive surveillance; lower cost/noise.
2. **Local-first.** Ollama/llama.cpp are the recommended default; cloud providers
   are opt-in.
3. **Announce-on-join.** On joining, the agent posts a visible chat line so humans
   know an agent is present and where their words go, e.g.
   `🤖 oracle joined — ollama/llama3 (local), operated by alice`.

---

## 2. Command grammar (`/ai`)

Reserved first-tokens (an agent name may **not** collide with these):
`list`, `grant`, `revoke`, `enable-sbx`, `disable-sbx`.

| Command | Who | Effect |
|---|---|---|
| `/ai <question>` | any member | Address the sole agent (error if 0 or >1 present). |
| `/ai <agent> <question>` | any member | Address a named agent. |
| `/ai list` | any member | List agents present + provider/model, operator, sbx state. |
| `/ai grant <user>` | **admin** | Allow `<user>` to operate agents. |
| `/ai revoke <user>` | **admin** | Withdraw operator permission. |
| `/ai enable-sbx <agent>` | **sandbox owner** | Allow agent to run sandbox commands. |
| `/ai disable-sbx <agent>` | **sandbox owner** | Withdraw sandbox access. |

Resolution order for the first token: reserved verb → known agent name →
otherwise treat the whole remainder as a question to the sole agent.

---

## 3. Permission tiers (mirrors the existing drive/sudo ACL)

| Tier | Who | Granted by | What it unlocks |
|---|---|---|---|
| **admin** | room/server host | seeded at launch | grant/revoke operators |
| **operator** | admin-granted (or admin) | `/ai grant` | summon & operate an agent the room will honor |
| **sandbox-owner** | whoever hosts `/sbx` | implicit | `/ai enable-sbx` per agent |
| **member** | anyone in the room | — | `/ai <question>` to a present agent |

### Identifying the admin
The agent and operator clients learn the admin's username at agent launch
(`--admin <username>`), defaulting to the room/server host. Because the server
stamps the authenticated sender on every frame, an agent can trust that an
`/ai grant bob` line genuinely came from the admin before honoring it.

### Enforcement honesty (matches the drive-ACL trust model)
- **Sandbox-exec gating is hard-enforced:** the sandbox owner's broker is the
  sole writer to the PTY (it already filters drive input), so an agent's
  `sandbox_exec` only runs if the owner has `enable-sbx`'d it. Real.
- **Operator ACL is app-layer:** a well-behaved agent obeys the admin's
  broadcast ACL and refuses to act if its operator isn't authorized. A hostile
  process that already has the room password could ignore this — the same class
  of trust as the app-level drive ACL (minus the OS backstop that `sudo` adds).
  Documented, not hidden.

---

## 4. Wire conventions

All agent control rides the existing "encrypted JSON with a prefix" convention
(`{"_sbx":…}`, `{"_ft":…}`, `{"_perm":…}` → add `{"_ai":…}`), so it's E2E like
everything else.

- **Announce / meta** (agent → room, on join):
  `{"_ai":"meta","agent":"oracle","operator":"alice","provider":"ollama","model":"llama3","sbx":false}`
- **Operator ACL** (admin → room): `{"_ai":"acl","operators":["alice"],"sbx":["oracle"]}`

**MVP simplification:** to avoid Rust TUI work up front, the announce can be a
plain visible chat line and `/ai` queries can be sent as ordinary chat that the
agent scans for. Structured `_ai` frames + a 🤖 roster glyph are Phase 3.

---

## 5. Provider interface (the "API to plug in")

```python
class Provider(Protocol):
    name: str
    supports_tools: bool
    def complete(self, system: str, messages: list[Msg],
                 tools: list[Tool] | None = None) -> Reply: ...
```

- Bundled adapters: `ollama` (default), `anthropic`, `openai-compatible`
  (OpenAI / Groq / Together / local vLLM …).
- Third-party: `--provider mypkg.module:MyProvider`.
- Per-agent config (TOML): `name, provider, model, admin, context_window,
  system_prompt, tool_allowlist, rate_limit`.

When triggered, the agent assembles `system` + a bounded window of recent room
transcript + the trigger, calls `complete()`, buffers the reply, and sends it as
one encrypted chat message. (Streaming = Phase 3; the protocol is append-only.)

---

## 6. Phasing

| Phase | Scope |
|---|---|
| **0** | Design + `dev` branch ✓ |
| **1 (MVP)** | Python runtime; `/ai` query (addressed-only); providers ollama + anthropic + openai-compatible; bounded context; buffered reply; plain-text announce-on-join; operator ACL (`/ai grant`/`revoke`) honored by the agent. Minimal Rust: route `/ai …` to chat send. |
| **2** | Tool layer: `web_fetch` + `sandbox_exec` (owner-gated via `/ai enable-sbx`); `/research` `/check` `/hint` verbs; provider tool-use where supported, text-only fallback elsewhere. |
| **3** | Rust TUI polish: structured `{"_ai":…}` frames, 🤖 roster glyph, "typing…" line, `/ai list` rendering, response streaming/coalescing. |

---

## 7. Security notes
- **Prompt injection:** room text is untrusted LLM input. Harden the system
  prompt; the agent never exposes the password/key; `sandbox_exec` is capped +
  owner-gated.
- **Secrets:** provider API keys live in the agent's env, never in the room.
- **Abuse/cost:** respond only when addressed + per-agent rate limit.
- **Capacity:** agent = one seat; document raising `CMD_CHAT_MAX_USERS`.

---

## 8. Open question
- **Designating the room admin in-protocol.** MVP uses `--admin <username>` at
  agent launch (defaults to host). A sturdier option (server marks an admin via
  `CMD_CHAT_ADMIN` env or an admin token) would make the operator ACL
  tamper-resistant but requires a small server change — deferred unless wanted.
