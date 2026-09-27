#!/bin/bash
# Live end-to-end smoke test for `cmd_chat.operator operate --brain smol` — the
# actual PRODUCT CLI path. Unlike eval/ops/hh-operator/run.sh (which shells
# straight into scripts/hh-smol-operator.py and never exercises `operate`'s
# --brain dispatch, subprocess wiring, or --json-out parsing), this drives the
# real command an operator would type: a loopback room, a real podman sandbox,
# a real Ollama model, graded by ground truth (podman exec, never scrollback).
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
HH_PY="$HERE/.venv/bin/python"
export OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
MODEL="${1:-qwen2.5-coder:7b}"
PORT="${PORT:-8999}"
PW="hh-smoketest-devpw"   # loopback dev-room shared secret; not a real credential
RUSER="smoketest-$$"
CTR="hh-op-$RUSER"

cleanup() {
  "$HH_PY" -m cmd_chat.operator sbx down --session "$RUSER" >/dev/null 2>&1
  "$HH_PY" -m cmd_chat.operator down --session "$RUSER" >/dev/null 2>&1
  [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" >/dev/null 2>&1
}
trap cleanup EXIT

echo "== starting loopback room on :$PORT =="
"$HH_PY" "$HERE/cmd_chat.py" serve 127.0.0.1 "$PORT" --password "$PW" --no-tls >/tmp/smoke-operate-smol-server.log 2>&1 &
SERVER_PID=$!
sleep 1.5

echo "== joining as operator ($RUSER) =="
"$HH_PY" -m cmd_chat.operator up 127.0.0.1 "$PORT" "$RUSER" --password "$PW" --no-tls --session "$RUSER" || exit 1

echo "== launching sandbox =="
"$HH_PY" -m cmd_chat.operator sbx launch --session "$RUSER" || exit 1

echo "== operate --brain smol (the real CLI path) =="
TASK="You ALREADY have drive on a ready sandbox container — act immediately, do NOT \
wait for any grant. Create the file /root/facts.txt in the sandbox with EXACTLY two \
lines. Line 1 must be the output of running exactly: ls /etc | wc -l . Line 2 must be \
the output of running exactly: uname -r . Reply with a single line starting 'DONE:' \
once /root/facts.txt is written."
OUT=$("$HH_PY" -m cmd_chat.operator operate --objective "$TASK" --brain smol \
     --model "$MODEL" --host "$OLLAMA_HOST" --max-turns 8 --session "$RUSER")
RC=$?
echo "$OUT"
[ $RC -eq 0 ] || { echo "FAIL — operate --brain smol exited $RC"; exit 1; }

echo "== ground-truth grade =="
WANT1=$(podman exec "$CTR" sh -c 'ls /etc | wc -l')
WANT2=$(podman exec "$CTR" sh -c 'uname -r')
GOT1=$(podman exec "$CTR" sh -c 'sed -n 1p /root/facts.txt' 2>/dev/null)
GOT2=$(podman exec "$CTR" sh -c 'sed -n 2p /root/facts.txt' 2>/dev/null)
if [ "$GOT1" = "$WANT1" ] && [ "$GOT2" = "$WANT2" ]; then
  echo "PASS — operate --brain smol produced correct /root/facts.txt via the real CLI path"
  exit 0
fi
echo "FAIL — want1=$WANT1 got1=$GOT1 want2=$WANT2 got2=$GOT2"
exit 1
