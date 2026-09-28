#!/usr/bin/env bash
# install.sh — interactive setup wizard for hack-house.
#
# The one-liner (`bash hh/scripts/bootstrap.sh`) is the fast path; this walks a
# new user through the choices instead: build type, the AI layer (none / local
# model / bring-your-own cloud), and putting `hh-go` on your PATH. It only ever
# calls bootstrap.sh + creates a symlink — nothing it does can't be done by hand.
#
#   bash hh/scripts/install.sh              # interactive
#   bash hh/scripts/install.sh --defaults   # accept every default, no prompts
set -uo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SELF/../.." && pwd)"
DEFAULTS=0
[[ "${1:-}" == "--defaults" || ! -t 0 ]] && DEFAULTS=1

c_b=$'\033[1m'; c_0=$'\033[0m'; c_g=$'\033[32m'
say()  { printf '%s\n' "$*"; }
head() { printf '\n%s%s%s\n' "$c_b" "$*" "$c_0"; }
ask()  {  # ask "prompt" "default" -> echoes answer
  local p="$1" d="$2" a
  if [[ $DEFAULTS -eq 1 ]]; then echo "$d"; return; fi
  read -rp "$p [$d]: " a; echo "${a:-$d}"
}
askyn() { local a; a="$(ask "$1 (y/n)" "$2")"; [[ "$a" =~ ^[Yy] ]]; }

head "hack-house setup"
say  "Repo: $ROOT"
say  "This installs the baseline (venv + server deps + Rust client) and lets you"
say  "add options. Prerequisites: python3 and cargo (Rust)."

# 1. build type
head "1) Client build"
say  "  debug   — quicker to build, fine for trying it out"
say  "  release — optimized, snappier TUI (slower first build)"
build="$(ask "  build" "debug")"
BFLAG=(); [[ "$build" == release ]] && BFLAG=(--release)

# 2. AI layer
head "2) The in-room /ai agent"
say  "  none  — skip it (add later with hh/scripts/bootstrap-ai.sh)"
say  "  local — install Ollama + pull a local model (multi-GB, private, no key)"
say  "  byo   — bring your own: a cloud/API or custom model, no Ollama download"
ai="$(ask "  ai" "none")"
AIFLAG=()
case "$ai" in
  local) AIFLAG=(--ai --yes) ;;
  byo)   say "  → BYO needs no install now. After setup, register a backend in"
         say "    models.toml (it stores an api_key_env NAME, never the key) and"
         say "    summon it with /ai start <profile>. See docs/providers.md."
         say "    Local stays the default; you can mix local + BYO in one room." ;;
  none|*) ;;
esac

# 3. run the baseline
head "3) Building"
say  "  running: bootstrap.sh ${BFLAG[*]} ${AIFLAG[*]}"
if askyn "  proceed?" "y"; then
  "$ROOT/hh/scripts/bootstrap.sh" "${BFLAG[@]}" "${AIFLAG[@]}" || {
    say "bootstrap reported an error — fix the prerequisite it named and re-run."; exit 1; }
else
  say "  aborted before building."; exit 0
fi

# 4. hh-go on PATH
head "4) hh-go launcher"
say  "  Symlink 'hh-go' into ~/.local/bin so you can run it from anywhere"
say  "  (hh-go up | host | join | ai | device | share | status | down)."
if askyn "  add hh-go to ~/.local/bin?" "y"; then
  mkdir -p "$HOME/.local/bin"
  ln -sf "$ROOT/hh/scripts/hh-go.sh" "$HOME/.local/bin/hh-go"
  say "  ${c_g}linked${c_0} $HOME/.local/bin/hh-go"
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) say "  (add ~/.local/bin to PATH to use the bare 'hh-go' name)";; esac
fi

# 5. done
head "Done."
say  "Start a local room + your own seat:"
say  "    hh-go up            # (or: cd hh && ./scripts/lets-hack.sh)"
[[ "$ai" == byo ]] && say  "Register your model:  edit models.toml, then in-room /ai start <profile>"
say  "Share to a browser:   hh-go share   then  /share  in the TUI for the URL"
