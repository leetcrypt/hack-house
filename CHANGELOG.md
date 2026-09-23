# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-08-03

The release that turned hack-house from a shared terminal into an agent-operable
one: an AI joins a room as a member, drives the sandbox, and hands its work to
the next agent.

### Added
- **Operator bridge** (`cmd_chat/operator/`) — a Claude Code session joins a room
  as a first-class member: `up` / `read` / `say`, sandbox drive (`exec`, `write`,
  `get`), a keystroke relay with a documented stop-vocabulary, and a `watch`
  stop-condition engine.
- **Recursive spawn** — an operator can budget and spawn further agent sessions
  (`--depth` / `--fanout`); a runner registry allows any agent CLI, not just
  Claude, and a harness mode drives a room with any function-calling model.
- **`.hh-agent` manifests** — a VM carries a self-describing record of its
  purpose, objective, and progress, so work hands off between agents and humans.
- **VM library + trading** — host-global registry (`~/.hh/registry.json`) with
  `/sbx browse`, `/sbx publish`, `/sbx catalog @user`, `/sbx pull @user <label>`.
- **Cardex** — saved VMs are minted as collectible cards with a 0–1000 PowerScore,
  population-curve rarity, and a browser flip gallery.
- **Native AI harness** — a host-side tool-calling loop against local Ollama with
  two-stage context pruning, a failure ledger, and stuck/loop detection; tuned to
  work on small CPU models. `/grant ai` grants drive to every agent at once.
- **Mobile operator** — runs on Android/Termux and can both join and host rooms;
  a pure-Python SRP shim (client *and* server) removes the `srp` C-extension
  requirement. Includes a stdlib mobile web console.
- **Session music** — bundled CC-BY albums and a `/music` player.

### Fixed
- Sandboxes launch with `--init`, so PID 1 reaps orphans. Previously PID 1 was
  `sleep infinity`, which never calls `wait()`, and every process orphaned inside
  a container became a permanently unreapable zombie.
- Abandoned sandboxes are reclaimed via ownership labels when the owning process
  dies without a clean teardown, and the operator daemon tears down its own
  container on shutdown.

## [0.1.0] - 2026-05-31

### Added
- Graceful shutdown: `Ctrl+C` now quits cleanly in chat mode (still sends an
  interrupt to the shell while driving the sandbox).
- Terminal-restore RAII guard and panic hook so a crash or kill never leaves the
  terminal (or tmux pane) stuck in raw/alternate-screen mode.
- `SIGTERM` / `SIGHUP` handling — the client exits gracefully when killed or
  when its tmux pane closes.
- `/pw` command to reveal the current room's password locally (for out-of-band
  sharing); never broadcast.
- `bootstrap.sh` one-shot setup (prereq checks, Python venv + deps, client build).
- `direnv-autostart/` — `cd` into a directory to launch a session with an
  in-memory room password (nothing written to disk).
- Help overlay (`F1` / `/help`) and scrollback for chat and the sandbox terminal.
- `/drive` command as a mobile-friendly alternative to `F2`.
- Community files: `SECURITY.md`, `CODE_OF_CONDUCT.md`, this changelog, and
  issue/PR templates.

### Changed
- Root `README` now documents hack-house (the Rust client) as the flagship,
  built on the unchanged cmd-chat Python server.
- Renamed "coven" to "clergy" throughout.
- `lets-hack.sh` boots a fresh server by default; `--reuse` keeps a live one.

### Fixed
- `lets-hack.sh` no longer closes the tmux session it was launched from.
- `.gitignore` cleanup; stopped tracking `.venv`.
