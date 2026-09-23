#!/usr/bin/env bash
# bootstrap-ai.sh — optional AI layer for hack-house (Ollama, local-first)
#
# Runs the baseline ./bootstrap.sh first, then sets up everything the /ai agent
# bridge needs to run a LOCAL model: installs Ollama (if missing) and pulls a
# default model. Cloud providers (anthropic / openai) need no install — just an
# API key in the agent's env — so this script only handles the local default.
#
# The baseline is untouched: ./bootstrap.sh alone never installs or enables AI.
#
# The /ai agent's sandbox `!task` harness runs host-side (native tool-calling /
# one-shot injector) and needs no extra binary, so this script installs nothing
# beyond Ollama + the model.
#
# usage:
#   ./bootstrap-ai.sh              # baseline setup + Ollama + default model
#   ./bootstrap-ai.sh --release    # ...and build the client in release mode
#   ./bootstrap-ai.sh --check      # report only; install/pull nothing
#   ./bootstrap-ai.sh --yes        # don't prompt before installing Ollama / pulling the model
#   ./bootstrap-ai.sh --ai-only    # only the AI layer; skip the baseline ./bootstrap.sh
#   ./bootstrap-ai.sh -h | --help  # this help
#
# environment:
#   HH_AI_MODEL   model to pull   (default: qwen2.5:3b)
#   OLLAMA_HOST   daemon URL      (default: http://localhost:11434)
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MODEL="${HH_AI_MODEL:-qwen2.5:3b}"
OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
INSTALLER_URL="https://ollama.com/install.sh"

RELEASE_ARGS=()
CHECK_ONLY=0
ASSUME_YES=0
AI_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --release)  RELEASE_ARGS+=(--release) ;;
        --check)    CHECK_ONLY=1 ;;
        --yes|-y)   ASSUME_YES=1 ;;
        --ai-only)  AI_ONLY=1 ;;
        -h|--help|-help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "✖ unknown arg: $arg (try --release / --check / --yes / --ai-only / --help)" >&2; exit 2 ;;
    esac
done

have() { command -v "$1" >/dev/null 2>&1; }
ollama_up() { curl -s --max-time 3 "$OLLAMA_HOST/api/tags" >/dev/null 2>&1; }

# 0. Baseline setup (venv + server/agent deps + client build). The agent's own
#    runtime deps (requests, websockets) are already in requirements.txt, so the
#    baseline install covers them — this script adds only the model runtime.
#    --no-ai stops bootstrap.sh re-entering this script (it now runs the AI layer
#    by default). --ai-only skips the baseline entirely (caller already ran it).
if [[ $AI_ONLY -ne 1 ]]; then
    if [[ $CHECK_ONLY -eq 1 ]]; then
        "$ROOT/hh/scripts/bootstrap.sh" --no-ai --check || exit $?
    else
        "$ROOT/hh/scripts/bootstrap.sh" --no-ai "${RELEASE_ARGS[@]}" || exit $?
    fi
fi

echo
echo "── AI layer (Ollama, local) ──"

# 1. Report current state.
if have ollama; then echo "  ✓ ollama ($(ollama --version 2>&1 | head -1))"
else echo "  · ollama not installed"; fi
if ollama_up; then echo "  ✓ ollama daemon reachable at $OLLAMA_HOST"
else echo "  · ollama daemon not reachable at $OLLAMA_HOST"; fi

if [[ $CHECK_ONLY -eq 1 ]]; then
    echo "--check: no changes made"
    exit 0
fi

# 2. Install Ollama if missing. The official installer pipes a remote script into
#    a shell, so we print the exact command and — on an interactive terminal —
#    ask first (skip with --yes). Already installed → nothing to do.
if ! have ollama; then
    if [[ "$(uname -s)" != "Linux" ]]; then
        echo "  ✖ automatic install is Linux-only." >&2
        echo "    install Ollama for your OS from https://ollama.com/download, then re-run." >&2
        exit 1
    fi
    echo "  Ollama is not installed. The official installer will run:"
    echo "      curl -fsSL $INSTALLER_URL | sh"
    if [[ $ASSUME_YES -ne 1 && -t 0 ]]; then
        read -r -p "  proceed? [y/N] " ans
        [[ "$ans" == [yY]* ]] || { echo "  aborted — install Ollama yourself, then re-run." >&2; exit 1; }
    fi
    curl -fsSL "$INSTALLER_URL" | sh || { echo "  ✖ Ollama install failed" >&2; exit 1; }
    have ollama || { echo "  ✖ ollama still not on PATH after install" >&2; exit 1; }
    echo "  ✓ ollama installed"
fi

# 3. Make sure the daemon is up. The Linux installer usually registers a systemd
#    service; if it isn't running we start one in the background so we can pull.
if ! ollama_up; then
    echo "  starting ollama daemon…"
    if have systemctl && systemctl start ollama 2>/dev/null; then :
    else nohup ollama serve >/tmp/hh-ollama.log 2>&1 & fi
    for _ in $(seq 1 20); do ollama_up && break; sleep 1; done
    ollama_up || { echo "  ✖ could not reach ollama at $OLLAMA_HOST (see /tmp/hh-ollama.log)" >&2; exit 1; }
    echo "  ✓ daemon up"
fi

# 4. Pull the default model (idempotent). Pulling downloads several GB onto THIS
#    machine, so ask first on an interactive terminal (skip with --yes) — never
#    pull a model onto someone's box without their say-so.
if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -Fxq "$MODEL"; then
    echo "  ✓ model '$MODEL' already present"
else
    echo "  model '$MODEL' is not present — pulling it downloads several GB to THIS machine."
    if [[ $ASSUME_YES -ne 1 ]]; then
        if [[ -t 0 ]]; then
            read -r -p "  pull '$MODEL' now? [y/N] " ans
            [[ "$ans" == [yY]* ]] || { echo "  aborted — no model pulled. pull it yourself with: ollama pull $MODEL" >&2; exit 1; }
        else
            # No terminal to confirm on — never pull onto someone's machine
            # unasked. Require explicit --yes (or an interactive yes) instead.
            echo "  ✖ not pulling '$MODEL' without confirmation (no terminal to ask)." >&2
            echo "    re-run with --yes to allow it, or pull manually: ollama pull $MODEL" >&2
            exit 1
        fi
    fi
    echo "  pulling model '$MODEL' (first pull can take a while)…"
    ollama pull "$MODEL" || { echo "  ✖ failed to pull '$MODEL'" >&2; exit 1; }
    echo "  ✓ model '$MODEL' ready"
fi

echo
echo "AI ready. start a local agent against a running room with:"
echo "    .venv/bin/python -m cmd_chat.agent <host> <port> \\"
echo "        --password <room-pw> --provider ollama --model $MODEL --no-tls"
echo
echo "tip: pick a different model with  HH_AI_MODEL=llama3 ./bootstrap-ai.sh"
