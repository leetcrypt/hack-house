# AI agent: context & local-performance plan

How the `/ai` agent gets conversational context, how to deepen it **without
breaking the RAM-only philosophy**, and how to make the local (Ollama) path
faster. Everything here stays in process memory — no disk persistence of
conversation data, no embeddings on disk. Context dies with the agent process,
exactly like the room itself.

## Current state (baseline)

- `AgentBridge.transcript: list[Msg]` — one flat in-RAM list (`bridge.py`).
- Passive capture: every non-addressed line is appended as `Msg("user", "sender: text")`, trimmed to `context_window * 2` (24) messages.
- On `/ai`: sends `system_prompt + transcript[-context_window:]` (last 12).
- Sandbox: same last-12 window + the task.
- `OllamaProvider.complete` posts `stream=False`, no `options`, no `keep_alive`.

### Limitations
1. **Recency-only window** — no relevance; old context is dropped forever.
2. **Join amnesia** — the agent only knows messages seen since connecting,
   **even though the server already sends it the full backlog** and the bridge
   throws it away (`init` handler reads only `users`).
3. **Message-count budget, not token budget** — fragile on small models.
4. **Flat, untyped transcript** — all senders flattened to role `user`.
5. **`stream=False` + cold model** — high perceived latency.

### Key enabling fact
The server keeps the last 1000 (encrypted) messages in RAM (`MessageStore`,
`stores.py`) and ships them all in the `init` frame (`helpers.send_state`).
That is a RAM-only history the agent can backfill from on join at zero new cost.

## Plan

### Tier 1 — context foundation (this branch)
1. **Backfill on join.** Consume `init.messages`: decrypt with `room_fernet`,
   drop control frames (`{"_…`) and our own lines, append to `transcript`,
   trim to budget. Pure RAM, ephemeral. *(implementing)*
2. **Token-budget windowing.** Replace the fixed `[-12:]` slices with a
   tail-by-token-budget selector (char/4 estimate), capped by a max message
   count. Used by both the answer and sandbox paths. *(implementing)*

### Tier 1.5 — local performance (this branch)
6. **Pin the model in VRAM** via Ollama `keep_alive` to kill cold-reload stalls.
8. **Tune Ollama `options`** — explicit `num_ctx` (so the larger window in #1/#2
   is actually honored) and bounded `num_predict`. *(implementing)*

### Tier 2 — deeper context
3. **In-RAM semantic retrieval (RAG, no disk).** *(done)* Each captured message
   is embedded with the already-present `nomic-embed-text` and held in a capped
   in-memory `MemoryIndex` (pure-Python cosine, no numpy). On a `/ai` question
   the agent embeds the query, retrieves top-k, drops weak/duplicate hits, and
   prepends them as a clearly-fenced "recalled context" preamble (never system
   role — keeps untrusted text from instructing). Embedding runs on a background
   worker so it can't stall the recv loop; if the embedder is unreachable it
   degrades to recency-only. Toggle with `--no-rag` / `--rag-top-k`.
4. **In-RAM hierarchical compaction.** *(staged)* When over budget, summarize the oldest
   chunk into a single rolling `Msg("system", "earlier: …")` instead of dropping
   it — the Claude Code auto-compaction pattern, kept in RAM.

### Tier 3 — latency & throughput (next branch)
5. **Token streaming** to the room (incremental chat frames) so replies appear
   as they generate.
7. **Stable prompt prefix** (system + summary + retrieved block in fixed order)
   for Ollama KV-cache reuse across turns.
9. **Single-flight queue** so concurrent `/ai` calls don't pile threads onto one
   Ollama instance.

## Notes on provenance
All patterns above are grounded in Anthropic's **public** documentation (context
compaction, prompt caching, token-budgeted assembly) and the open Agent SDK —
no leaked/proprietary source was used.
