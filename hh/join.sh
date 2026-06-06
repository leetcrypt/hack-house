#!/usr/bin/env bash
# Join the live hack-house room.  Usage: ./join.sh <yourname> [host]
#   local:  ./join.sh alice
#   remote: ./join.sh alice 100.117.177.50
# The room password (a shared secret) comes from $HH_PASSWORD, else a no-echo
# prompt — never hardcoded, so it can't drift from the room's random password.
NAME="${1:-guest}"
HOST="${2:-127.0.0.1}"
cd "$(dirname "$0")"

# Sync the latest code before joining, then rebuild so it takes effect. Pulls
# are best-effort and fast-forward only: an unreachable remote or diverged
# history just warns and is skipped — it never blocks you from joining.
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
for remote in gitea origin; do
  if git remote get-url "$remote" >/dev/null 2>&1; then
    echo "⛧ syncing $BRANCH from $remote…"
    git pull --ff-only "$remote" "$BRANCH" 2>&1 \
      || echo "  (skipped — couldn't fast-forward from $remote/$BRANCH)"
  fi
done
echo "⛧ building client…"
cargo build --quiet 2>&1 || echo "✖ build failed — joining with the existing binary"

PASSWORD="${HH_PASSWORD:-}"
if [[ -z "$PASSWORD" ]]; then
  read -rsp "⛧ room password: " PASSWORD < /dev/tty
  echo
fi
[[ -n "$PASSWORD" ]] || { echo "✖ a password is required" >&2; exit 1; }
exec ./target/debug/hack-house connect "$HOST" 4173 "$NAME" --password "$PASSWORD" --no-tls
