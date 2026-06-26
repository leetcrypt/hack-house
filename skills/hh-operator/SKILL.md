---
name: hh-operator
description: Operate inside a hack-house chat room as a first-class participant — join, read/answer humans and AI, and (when granted) drive a sandbox with full shell + keystroke control. Use when asked to enter/run/watch a hackhouse room, bridge a Claude session into one, or autonomously monitor and respond in a room.
---

# hh-operator — drive a hack-house room from a Claude Code session

You become a room member. A small **daemon** owns the encrypted websocket; you
poll it through the **`hh-bridge` CLI**. The daemon survives across your tool
calls, so you can read → think → act → read again, indefinitely.

```bash
# Run from the hack-house repo. Pin the interpreter to its venv.
HH=".venv/bin/python -m cmd_chat.operator"          # = "hh-bridge"
```

## 1. Join (once)

```bash
$HH up <host> <port> <name> --password <pw> [--no-tls] [--trigger "@bot"]
```
Spawns the daemon detached and waits until it's connected. `<name>` is your room
display name **and** the session id. `--trigger` adds an extra phrase that marks
a line "addressed" to you (your name / `@name` always count). Re-running `up`
when already connected is a no-op.

`$HH status` → connection, roster, grant state, sandbox target, last seq.

## 2. The operator loop

This is the core pattern. Long-poll for activity, decide, respond, repeat.

```bash
$HH read --wait --timeout 30     # blocks until events arrive (or timeout)
# → JSONL events; cursor auto-advances so the next bare `read` is "since last"
$HH say "your reply here"        # send a chat line to the room
```

Each event: `{seq, ts, kind, ...}`. Kinds you act on:
- `message` — `{from, text, addressed}`. Answer when `addressed:true` (or when
  context clearly invites you). Ignore idle chatter unless asked to.
- `roster` — who's present. `acl` — grant changes (`granted`, `can_sudo`).
- `sandbox` — a room sandbox came up/down. `system` — connect/reconnect.

**In-turn autonomy:** chain `read --wait` → act → `read --wait` within one turn
to stay responsive without burning idle tokens (the poll blocks server-side).

**Indefinite watch (`/loop`):** keep looping across turns — the daemon persists,
so you lose nothing between calls. Stop the loop when:
- the room/owner tells you to (`stop`, `halt`, `that's all`, `you can leave`),
- your given objective/stop-condition is met, or
- you're idle past your remit.
Then `$HH down` to leave cleanly (drops the socket, leaves the roster).

## 3. Sandbox drive (when granted)

Sandbox actions are **permission-gated**: the room owner runs `/grant <name>`.
Until then exec/keys are refused/inert — that's by design, not a bug.

Two surfaces:

**Co-located exec** — capture output, scriptable, invisible to the room:
```bash
$HH sbx launch [--image IMG] [--engine podman|docker]  # your own container
$HH sbx status                                          # what you'd target
$HH exec 'whoami; uname -a'                             # run a shell command
echo "data" | $HH write /root/file.txt                 # stdin → file (binary-safe)
$HH get /root/file.txt [--out local.txt]               # read a file out
$HH sbx down                                            # tear your container down
```
`sbx launch` makes a throwaway container you own (podman→Kali, docker→Parrot).
When the **room** has a sandbox and you're granted, `exec`/`write`/`get` target
it directly (you're co-located on the host) — no launch needed.

**Keystroke relay** — drive the room's *shared* terminal live, like a human:
```bash
$HH keys "ls -la" enter          # type + run
$HH keys ctrl-c                  # interrupt the running program
$HH keys --help-keys             # print the full vocabulary
```

### Keystroke cheat-sheet (how to *stop* things matters most)

Tokens combine: literal text types verbatim; named keys send control bytes;
`text:<x>` forces verbatim; `hex:<bytes>` sends raw.

| Key | Effect |
|-----|--------|
| `enter` `tab` `space` `esc` | `\r` `\t` `' '` `\x1b` |
| **`ctrl-c`** | **SIGINT — interrupt the foreground program (your main "stop")** |
| `ctrl-d` | EOF — end stdin / exit an empty shell |
| `ctrl-z` | suspend to background (resume with `fg`); `ctrl-\` = SIGQUIT |
| `ctrl-u` `ctrl-l` | clear a half-typed line / clear the screen |
| `up` `down` `left` `right` `home` `end` `pageup` `pagedown` `delete` `backspace` | navigation |
| `q` | quits most pagers (`less`/`man`); `:q` + `enter` quits vim |

**Rule:** end a stuck command with `ctrl-c`; if ignored, `ctrl-\` then `ctrl-c`
again. Always know how to exit an interactive program *before* you start it.

## 4. Delegate to a local model (optional)

A room often has an `/ai <name>` Ollama agent. To offload a task, just `say`
`/ai <name> !<task>` and read its reply — cheaper than doing trivial work
yourself. (Direct operator→Ollama delegation is a later phase.)

## Safety

- One bad action's blast radius is the container; `local` exec is opt-in only.
- Never run destructive host commands. Keystrokes/exec require an owner grant.
- Leave with `$HH down` when done — don't abandon a live daemon.
