# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
