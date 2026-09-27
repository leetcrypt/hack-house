# AI agent: CPU-only performance & code-quality plan

Hardware reality: the box serving local models is **CPU-only** (Intel i5-8350U,
4c/8t, no GPU, 62 GB RAM), Ollama 0.3.9. So we optimize **time-to-first-token**
(prefill is O(context)) and **tokens/sec**, not VRAM. GPU knobs (flash attention,
KV-cache quant) are no-ops here.

## Status

### Tier A — high impact / low effort
1. **`qwen2.5-coder` for the sandbox/code path.** *(done)* `qwen2.5-coder:1.5b`
   pulled and wired as a separate code provider used only by `!task`; chat keeps
   the general model. Same speed, better shell/code. Auto-selected when the chat
   provider is Ollama and the coder model is present; override with `--code-model`.
2. **Lower `num_ctx` to 4096 + expose `num_thread`.** *(done)* OllamaProvider
   default `num_ctx` 8192→4096 (8192 was a GPU-mindset default that inflated TTFT
   on CPU); `token_budget` default 3000→2000 to fit. `--num-ctx`, `--num-thread`,
   `--num-predict` flags added. `num_thread` defaults to Ollama's own (= physical
   cores, 4 here); benchmark 4/6/8.
3. **Token streaming.** *(done)* `OllamaProvider.stream()` yields deltas from
   Ollama's `stream=True` chat endpoint; the agent relays them as throttled
   (~5/sec) cumulative `_ai:"stream"` frames off a worker thread, and the Rust
   client renders a dim in-progress preview bubble (cleared by a `done` frame
   when the final, persisted message lands). On CPU, perceived latency is TTFT —
   this makes a slow reply feel live.
4. **Keep model warm + single-flight.** *(partial)* `keep_alive` already 30m
   (prevents mid-session reload). `OLLAMA_NUM_PARALLEL=1` is a **server-side env**
   read by `ollama serve`, not settable from the agent — set it where Ollama is
   launched (documented below).

### Tier B — code-generation quality
5. **Few-shot in `SANDBOX_SYSTEM`.** *(done)* 1–2 request→shell exemplars to anchor
   the small model's output format.
6. **GBNF constrained output.** *(blocked on #7)* Ollama 0.3.9 only supports
   `format: json`, not custom grammars for fenced shell. Needs the upgrade; the
   existing `_extract_commands` parser + few-shot cover the gap meanwhile.

### Tier C — infra / housekeeping
7. **Upgrade Ollama 0.3.9 → current.** *(manual, user-run)* System-wide action that
   restarts the daemon other projects share — not run automatically. Buys current
   coder builds, structured-output/grammar support (unblocks #6), bugfixes. CPU
   speed gains are incremental. Suggested: `curl -fsSL https://ollama.com/install.sh | sh`.
8. **Matryoshka embedding truncation.** *(done)* nomic-embed-text is MRL-trained;
   truncate vectors to 256-dim (`--embed-dim`) for faster pure-Python cosine and
   less RAM. Query + stored use the same dim, so cosine stays correct.

## Server-side env (set where `ollama serve` runs, e.g. systemd unit or shell)
```
OLLAMA_NUM_PARALLEL=1      # single interactive user → all cores to one request
OLLAMA_KEEP_ALIVE=30m      # or -1 to pin forever (62 GB RAM is plenty)
```

## Notes
All grounded in public sources + the Obsidian vault (`research/2026-06-02-*`):
Q4_K_M is the CPU speed sweet spot, small `num_ctx` beats "context rot", and
qwen2.5-coder beats the general model at equal size for code.
