#!/usr/bin/env bash
# setup.sh — one-time setup for the hack-house direnv autostart ⛧
#
# Installs direnv (if missing), hooks your shell, and allows the .envrc in this
# directory so that simply `cd`-ing here launches your hack-house session.
#
# usage:
#   ./setup.sh            # set up autostart for THIS directory
#   ./setup.sh --help
#
# After it finishes: open a new shell (or `source ~/.bashrc`), then `cd` into
# this directory — your single-user clergy boots automatically.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"   # the autostart dir (holds .envrc)

case "${1:-}" in
    -h|--help|-help)
        sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
        exit 0 ;;
    "") : ;;
    *)  echo "✖ unknown argument: $1 (try --help)" >&2; exit 2 ;;
esac

# 1. ensure direnv is installed
if command -v direnv >/dev/null 2>&1; then
    echo "⛧ direnv already installed ($(direnv version 2>/dev/null))"
elif command -v apt-get >/dev/null 2>&1; then
    echo "⛧ installing direnv via apt…"
    sudo apt-get update -qq && sudo apt-get install -y direnv
elif command -v brew >/dev/null 2>&1; then
    echo "⛧ installing direnv via brew…"
    brew install direnv
else
    echo "⛧ installing direnv via the official script (→ ~/.local/bin)…"
    mkdir -p "$HOME/.local/bin"
    export bin_path="$HOME/.local/bin"
    curl -sfL https://direnv.net/install.sh | bash
fi
command -v direnv >/dev/null 2>&1 || { echo "✖ direnv install failed — see https://direnv.net/docs/installation.html" >&2; exit 1; }

# 2. hook direnv into the shell rc (idempotent). Covers bash and zsh.
hook_line_bash='eval "$(direnv hook bash)"'
hook_line_zsh='eval "$(direnv hook zsh)"'
add_hook() { # $1 = rc file, $2 = hook line
    local rc="$1" line="$2"
    [[ -f "$rc" ]] || return 0
    if grep -qF 'direnv hook' "$rc"; then
        echo "⛧ shell hook already present in $rc"
    else
        printf '\n# direnv (hack-house autostart)\n%s\n' "$line" >> "$rc"
        echo "⛧ added direnv hook to $rc"
    fi
}
add_hook "$HOME/.bashrc" "$hook_line_bash"
[[ -n "${ZDOTDIR:-}" && -f "$ZDOTDIR/.zshrc" ]] && add_hook "$ZDOTDIR/.zshrc" "$hook_line_zsh"
[[ -f "$HOME/.zshrc" ]] && add_hook "$HOME/.zshrc" "$hook_line_zsh"

# 3. allow this directory's .envrc
if [[ ! -f "$HERE/.envrc" ]]; then
    echo "✖ no .envrc in $HERE — is this the autostart directory?" >&2
    exit 1
fi
direnv allow "$HERE"
echo "⛧ allowed $HERE/.envrc"

echo
echo "⛧ done. open a new shell (or: source ~/.bashrc), then:  cd $HERE"
echo "  your single-user hack-house boots automatically (fresh server, generated password)."
