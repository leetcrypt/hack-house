#!/usr/bin/env bash
# hack-house smoke test
# Exercises the full use-case path end to end against a live server:
#   rust unit tests → SRP self-test → boot server → rust client handshake +
#   round-trip → cross-language (python decrypts what the rust client sent).
# Run from anywhere:  hh/smoke.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"   # .../hh
ROOT="$(cd "$HERE/.." && pwd)"          # repo root
PY="$ROOT/.venv/bin/python"
BIN="$HERE/target/debug/hack-house"
PORT="${PORT:-4199}"
PW="${PW:-labtest}"

fail() { echo "✖ SMOKE FAIL: $1"; exit 1; }

echo "── 1/5 rust unit tests (srp vectors + fernet interop) ──"
( cd "$HERE" && cargo test --quiet ) || fail "cargo test"

echo "── 2/5 build + SRP self-test (Rust SRP ≡ Python srp) ──"
( cd "$HERE" && cargo build --quiet ) || fail "cargo build"
"$BIN" selftest | grep -q "selftest passed" || fail "selftest"

echo "── 3/5 boot server on :$PORT ──"
"$PY" "$ROOT/cmd_chat.py" serve 127.0.0.1 "$PORT" --password "$PW" --no-tls \
    >/tmp/hh-smoke-srv.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT
for _ in $(seq 1 20); do
    curl -s --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"status":"ok"' && break
    sleep 1
done
curl -s "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"status":"ok"' || fail "server did not come up"

echo "── 4/5 rust client: SRP auth + encrypted round-trip ──"
"$BIN" handshake 127.0.0.1 "$PORT" smoke-rust --password "$PW" --no-tls \
    | tee /tmp/hh-smoke-rust.log | grep -q "round-trip ✓" || fail "rust handshake/round-trip"

echo "── 5/5 cross-language: python decrypts the rust-sent message ──"
"$PY" - "$PORT" "$PW" <<'PYEOF' || fail "python cross-language read"
import sys, asyncio, base64, json
import srp, requests, websockets
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
srp.rfc5054_enable()
port, pw = sys.argv[1], sys.argv[2].encode()
base, wsb = f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}"

async def main():
    usr = srp.User(b"chat", pw, hash_alg=srp.SHA256)
    _, A = usr.start_authentication()
    r = requests.post(f"{base}/srp/init",
                      json={"username": "smoke-py", "A": base64.b64encode(A).decode()}).json()
    uid = r["user_id"]
    B, salt, rs = (base64.b64decode(r[k]) for k in ("B", "salt", "room_salt"))
    M = usr.process_challenge(salt, B)
    v = requests.post(f"{base}/srp/verify",
                      json={"user_id": uid, "username": "smoke-py",
                            "M": base64.b64encode(M).decode()}).json()
    room = Fernet(base64.urlsafe_b64encode(
        HKDF(algorithm=hashes.SHA256(), length=32, salt=rs,
             info=b"cmd-chat-room-key").derive(pw)))
    async with websockets.connect(f"{wsb}/ws/chat?user_id={uid}&ws_token={v['ws_token']}") as ws:
        data = json.loads(await asyncio.wait_for(ws.recv(), 5))
        msgs = [room.decrypt(m["text"].encode()).decode()
                for m in data.get("messages", []) if m.get("text")]
        hits = [m for m in msgs if "house is open" in m]
        assert hits, f"rust-sent message not found/decryptable: {msgs!r}"
        print("  ✓ python decrypted rust-sent message:", hits)

asyncio.run(main())
PYEOF

echo
echo "✓ SMOKE PASS — crypto · SRP · fernet · cross-language relay all green"
