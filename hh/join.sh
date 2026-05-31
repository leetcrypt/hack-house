#!/usr/bin/env bash
# Join the live hack-house room.  Usage: ./join.sh <yourname> [host]
#   local:  ./join.sh alice
#   remote: ./join.sh alice 100.117.177.50
NAME="${1:-guest}"
HOST="${2:-127.0.0.1}"
cd "$(dirname "$0")"
exec ./target/debug/hack-house connect "$HOST" 4173 "$NAME" --password malware-bless --no-tls
