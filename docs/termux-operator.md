# Spec — hack-house operator from Android / Termux

**Status:** planning · **Owner:** andre · **Drafted:** 2026-07-07
**Goal:** run a hack-house *operator/user* from a phone (Termux, aarch64 Android)
that can **join a room as a first-class member** — read, chat, and drive a
room-owned shared sandbox — with autonomy driven by a **remote model provider**
(no on-device model). Reachability via **Tailscale** first, with port-forward and
reverse-SSH documented as alternates.

Target device is the Murena Fairphone 6 (Android 15/16 base) running Termux. A
future postmarketOS path (full Linux userland) is noted in Appendix C.

---

## 1. Why this is tractable

The operator side is **pure Python** and, for everything except `sbx launch`,
needs **only a network connection** — the shared sandbox lives on the server
host, and the operator drives it by relaying keystrokes/reads over the room
websocket. So Android's lack of podman/systemd/X11 does **not** block us.

Grounding (verified in-repo):

| Concern | Reality | Source |
|---|---|---|
| Operator↔room transport | SRP-auth HTTP handshake → Fernet-encrypted websocket | `cmd_chat/client/client.py:106-157,502-505` |
| Operator reuses the same client | `from ..client.client import Client` | `cmd_chat/operator/bridge.py:27` |
| TLS is optional | `--no-tls` (ws/http), `--insecure` (skip cert verify) | `cmd_chat/client/client.py:41,69-89` |
| Sandbox drive needs no local engine | `keys`/`screen`/`watch` relay frames to the broker-owned PTY | bridge `keys`/`screen` verbs |
| Only `sbx launch` needs podman | operator's *own* container path; skip it on Android | bridge sandbox target logic |
| Autonomy via remote model | `operate --profile NAME` \| `--provider SPEC --model M` | `cmd_chat/operator/__main__.py:113-123,408-436` |
| Model profiles file | `models.toml`, secrets via `api_key_env` | `cmd_chat/ai/profiles.py:1-70` |

**Non-goals (this spec):** running a local LLM on-device; `sbx launch` /
operator-owned containers on Android; the Rust TUI on Android; recursive
`spawn` of `claude -p` on-device (needs Node; deferred, see Phase 3B).

---

## 2. Phase 0 — Dependency spike (the risk gate)

**Objective:** prove the operator's Python deps install & import in Termux on
aarch64. This is the make-or-break step; everything else is wiring.

### 0.1 Base packages
```bash
pkg update && pkg upgrade
pkg install python openssl clang rust libffi make git
python -V            # expect 3.11+ (gives stdlib tomllib for models.toml)
```

### 0.2 Easy deps (pure-Python or aarch64 wheels)
```bash
pip install requests rich websockets
python -c "import requests, rich, websockets; print('ok')"
```
Expected: clean. `websockets` ships aarch64 wheels; `requests`/`rich` are pure
Python.

### 0.3 The two hard deps
```bash
pip install "cryptography"      # see 0.4 on the >=46 pin
pip install srp                 # <-- most likely failure
python -c "import srp; from cryptography.fernet import Fernet; print('ok')"
```

### 0.4 `cryptography>=46.0.0` pin
`requirements.txt` pins `cryptography>=46.0.0`. Two outcomes:
- **Rust build succeeds** (we installed `rust`+`openssl`) → done.
- **Build too heavy / OOM on device** → install Termux's packaged build and
  relax the pin *for the operator install only*:
  ```bash
  pkg install python-cryptography
  python -c "import cryptography, sys; print(cryptography.__version__)"
  ```
  If the packaged version is `< 46`, confirm the operator actually needs the
  floor. It uses only `cryptography.fernet.Fernet` (`client.py:12`), stable for
  years — so a lower version is almost certainly fine. **Action if needed:** add
  an extras/loosened constraint (e.g. `requirements-operator.txt` with
  `cryptography>=41`) rather than editing the server's pin. Do **not** weaken the
  server requirement.

### 0.5 `srp==1.0.22` — the gate + fallback
`srp` is a C extension with no reliable aarch64 wheel. If `pip install srp`
fails to build under Termux, the fix is a **pure-Python SRP** shim behind the
exact interface `client.py` already uses.

**Interface to preserve** (from `cmd_chat/client/client.py`):
```python
srp.rfc5054_enable()                                   # module-level, line 19
usr = srp.User(b"chat", password, hash_alg=srp.SHA256) # line 106
_, A = usr.start_authentication()                      # line 107  -> (username, A_bytes)
M     = usr.process_challenge(salt, B)                 # line 134  -> M_bytes (or None on failure)
usr.verify_session(H_AMK)                              # line 152  -> raises/None on bad server proof
usr.authenticated()                                    # -> bool (guard after verify)
```
Server endpoints consumed: `POST /srp/…` (challenge) and `POST /srp/verify`
(`client.py:109,139-157`). The wire params are `salt`, `B`, `A`, `M`, `H_AMK`,
`user_id`, `ws_token` — a pure-Python SRP-6a (RFC 5054 group, SHA-256) must
reproduce the **same** N/g group, `k`, `x`, `u`, and `M1`/`H_AMK` derivation as
the `srp` package with `rfc5054_enable()`.

**Fallback plan (code change, only if 0.5 build fails):**
1. Vendor a small `cmd_chat/client/_srp_pure.py` implementing SRP-6a (RFC 5054
   2048-bit group, SHA-256) exposing a drop-in `rfc5054_enable`, `User`,
   `SHA256`, matching the five calls above.
2. In `client.py`, `try: import srp` / `except ImportError: from . import
   _srp_pure as srp`. No other call-site changes.
3. **Interop test is mandatory** (server uses the real `srp`): run the pure
   client against a real server room and confirm auth succeeds — SRP is
   unforgiving about constant/hash mismatches.

**Phase 0 acceptance:** on the phone, `python -c "import srp, websockets,
requests, rich; from cryptography.fernet import Fernet; print('ok')"` prints
`ok` (via wheel/build **or** the pure-Python shim).

---

## 3. Phase 1 — Read-only presence (transport proof)

**Objective:** join a live room from the phone; prove SRP + Fernet + ws over the
network. No sandbox.

Layout on device (clone or rsync the repo; operator only needs `cmd_chat/`):
```bash
git clone <gitea>/hacker-house && cd hacker-house
python -m venv .venv && . .venv/bin/activate
pip install requests rich websockets cryptography   # + srp or the shim
HH=".venv/bin/python -m cmd_chat.operator"
```
Join + observe + speak (over Tailscale, `--no-tls` acceptable — see App. A):
```bash
$HH up <server-tailscale-host> <port> phone-op --password <pw> --no-tls
$HH read --wait --timeout 30
$HH say "phone operator online"
$HH roster ; $HH status
$HH down
```
**Acceptance:** roster shows `phone-op`; `say` appears in the room; `read`
streams room events as JSONL. Daemon control socket lands under
`$XDG_RUNTIME_DIR` → in Termux falls back to `$PREFIX/tmp/hh-bridge/<session>/`
(AF_UNIX works there — verify `control.sock` is created).

---

## 4. Phase 2 — Sandbox drive (core operator loop)

**Objective:** drive a **room-owned** sandbox from the phone with no local
container engine.
```bash
$HH keys "uname -a" enter
$HH screen                                  # read the PTY buffer back
$HH watch --for 'PASS|FAIL' --in screen --timeout 60
$HH exec "ls /root"                         # only if the room sandbox is co-located & granted
```
**Skip** `sbx launch` (wants podman). If a verb reports "no sandbox / not a
granted driver", that's the grant model working — the room must `/grant
phone-op` first.
**Acceptance:** keystrokes reach the shared PTY, `screen` returns output,
`watch` resolves a stop-condition — all from the phone.

---

## 5. Phase 3 — Autonomy via a remote provider

**Objective:** let a **remote** model be the operator brain — no on-device
inference.

### 3A (primary) — `operate` with a remote provider
Create `~/.config/hh/models.toml` (secrets via env, never in the file):
```toml
[claude]
provider     = "anthropic"
model        = "claude-sonnet-4-6"
api_key_env  = "ANTHROPIC_API_KEY"

[groq-llama]
provider     = "openai"          # OpenAI-compatible endpoint
base_url     = "https://api.groq.com/openai/v1"
model        = "llama-3.3-70b-versatile"
api_key_env  = "GROQ_API_KEY"
```
```bash
export ANTHROPIC_API_KEY=…            # in the Termux session / profile
$HH operate --profile claude          # or: $HH operate --provider openai --model … --base-url …
```
Resolution + a `preflight` credential/endpoint check happen in
`_build_operate_provider` (`__main__.py:408-436`); a bad/missing key fails fast
with a clear message. `models.toml` lookup order:
`$HH_MODELS_FILE` → `./models.toml` → `~/.config/hh/models.toml`
(`ai/profiles.py:34-43`).
**Acceptance:** `operate --profile claude` runs the read→think→act loop against
the room using a cloud model; the phone stays cool (no local weights).

### 3B (deferred) — recursive `spawn`
`spawn` shells out to `claude -p` (needs Node + `@anthropic-ai/claude-code`).
Heavy on-device; out of scope for v1. Revisit only if the phone becomes a
persistent autonomous node (or under postmarketOS — App. C).

---

## 6. Phase 4 — Persistence & UX on Android

Android Doze/OOM will reap a detached daemon. Mitigations:
- **`termux-wake-lock`** before `up` (release with `termux-wake-unlock` on stop).
  Requires the Termux:API addon.
- **Termux:Boot** addon → a boot script that `wake-lock`s and runs the operator,
  for auto-start after reboot.
- **Wrapper script** `~/bin/hh-phone` capturing host/port/name/password (from
  Termux secure storage or a `chmod 600` env file — never commit) →
  `termux-wake-lock; $HH up …; $HH operate --profile claude`.
- **Reconnect-on-drop:** mobile networks flap. Confirm the daemon's behaviour on
  ws drop; if it exits, wrap `up`+`operate` in a supervised restart loop
  (backoff). (Potential small code hardening — track separately.)
- **Battery:** on cellular + wake-lock this is a real drain; expect to run it
  plugged in for long sessions.

**Acceptance:** operator survives screen-off / Doze for ≥30 min and auto-resumes
after a network blip.

---

## Appendix A — Connectivity (primary: Tailscale)

**Tailscale (chosen for now).** First-class Termux support. Put phone + server on
one tailnet; the server gets a stable MagicDNS name reachable from anywhere.
- Pros: zero port-forwarding, NAT-traversal handled, WireGuard-encrypted, ACLs.
- TLS: over the tailnet the link is already encrypted → **`--no-tls` is
  acceptable** (SRP still authenticates and Fernet still encrypts room payloads
  end-to-end). For belt-and-suspenders, use Tailscale MagicDNS + a cert and drop
  `--no-tls`.
- Setup: install Tailscale on phone + server, `tailscale up` both, then
  `$HH up <server-magicdns> <port> …`.

## Appendix B — Connectivity alternates

**B1. Port-forward (router).** Forward `server_port` on the home router to the
server host.
- Pros: no extra software; direct.
- Cons: exposes the port to the public internet — **must** use `wss://` (real
  cert, drop `--no-tls`), a strong SRP password, and ideally an allowlist/geo
  filter or fail2ban. Dynamic home IP → needs DDNS. Weakest security posture;
  use only with TLS + strong auth.

**B2. Reverse-SSH tunnel.** Server opens an outbound SSH tunnel to a VPS that has
a public IP; phone connects to the VPS:
```bash
# on the server (autossh keeps it up):
autossh -M 0 -N -R 0.0.0.0:8443:localhost:<server_port> user@vps
# phone → $HH up <vps-host> 8443 …
```
- Pros: works behind CGNAT / no router access; only the VPS is exposed; can bind
  the tunnel to the VPS loopback and reach it via a second SSH hop for zero
  public exposure.
- Cons: needs a VPS; extra hop adds latency; manage `autossh`/keys. Bind to
  `0.0.0.0` only with TLS, else prefer `localhost` + SSH-hop from the phone.

**Recommendation:** Tailscale for daily use; reverse-SSH (B2) as the fallback
when a tailnet isn't an option; public port-forward (B1) only as a last resort
and only with real TLS.

## Appendix C — Future: postmarketOS full-Linux path

Per the Fairphone-6 C2 notes, pmOS has a mainline FP6 port. On a full Linux
userland the calculus changes: real package manager, easier `cryptography`/`srp`
builds, Node for `spawn`, and — with a working container stack — even
operator-owned `sbx launch`. If the phone graduates to pmOS, this spec collapses
to "install like a normal Linux operator" and Phases 0/3B/skip-podman caveats
mostly evaporate.

---

## Risk register

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| R1 | `srp` C-ext won't build in Termux | High | Pure-Python SRP shim (§0.5) + interop test vs real server |
| R2 | `cryptography>=46` too heavy to build | Med | `pkg install python-cryptography`; loosen pin in an operator-only reqs file (§0.4) |
| R3 | Daemon reaped by Doze/OOM | High | `termux-wake-lock`, Termux:Boot, supervised restart (§Phase 4) |
| R4 | Public exposure via port-forward | Med | Prefer Tailscale/reverse-SSH; if forwarding, mandatory `wss://` + strong SRP (§App B1) |
| R5 | Mobile network flap drops ws | High | Reconnect/backoff wrapper; possible daemon hardening (§Phase 4) |
| R6 | SRP shim constants mismatch server | Med | Mirror RFC 5054 2048-bit group + SHA-256 exactly; gate on a live-auth test |

## Milestone acceptance summary
- **P0:** all operator deps import on device (wheel/build or shim).
- **P1:** phone appears in roster; can `read`/`say`.
- **P2:** phone drives a granted room sandbox via `keys`/`screen`/`watch`.
- **P3:** `operate --profile <remote>` runs the loop with a cloud model.
- **P4:** survives Doze + network blip, auto-resumes.
