---
name: hh-relay-audit
description: Pre-exposure security gate for the hack-house web-relay — regression-checks the PIN-lockout/proxy-trust fix (research/relay-pin-redteam) and the ACL/grant surface before a room goes tailnet or public. Use before running hh-go relay/public, hh-serve, or hh-share --public/--tailnet, or when asked to audit/harden hack-house's own auth rather than the sandbox it hosts.
---

# hh-relay-audit — pre-exposure gate for the relay's own auth surface

`hh-redteam` asks whether the **sandbox** contains a hostile agent. This skill asks
the other question: does the **relay in front of the room** hold against a hostile
*viewer*? It is a regression gate, run before `hh-go relay|public` or `hh-serve
--cloudflare|--unsafe|--status` change what's reachable — not a new offensive study.
Built on the verified PoC in `research/relay-pin-redteam/` (read the finding first:
project memory `hh-relay-pin-lockout-bypass`).

```bash
HHREPO="${HH_REPO:-$HOME/coding/hack-house/main}"
POC="$HHREPO/research/relay-pin-redteam/pin-bypass-poc.py"
```

## 0. What's actually being checked

`relay/app.py`'s PIN gate (proto v8, Ed25519-sig over a relay nonce, PBKDF2-200k
seed) rate-limits brute force with `Lockout(5 fails/60s/30s)` keyed on
`pin_key = f"{slug}:{_client_ip(request)}"`. `_client_ip` trusts a client-supplied
`CF-Connecting-IP` header **whenever the socket peer is a trusted proxy**
(default `{127.0.0.1, ::1}`) — which is exactly the loopback peer cloudflared/
tailscale-funnel terminates through. Two knobs matter, not one:

- **per-IP lockout** — trips at 5 wrong PINs from one key. **Known bypassable**:
  rotating `CF-Connecting-IP` per attempt gives every attempt a fresh key. This is
  inherent to trusting a spoofable header on loopback and is not itself the bug.
  Per the last verified PoC run it is still open — 12/12 rotated attempts came
  back `4403` (wrong-pin), never `4429` (locked).
  The real defense is:
- **slug-level global backstop** — a lockout keyed on the slug alone, independent
  of the (spoofable) per-IP key. This is what closes the bypass. **The audit's job
  is to confirm this backstop exists and still trips within a low attempt count
  before any exposure change** — if it's ever missing, raised, or refactored away,
  the PIN degrades back to online-bruteforceable (10⁷ space, no rate limit).

## 1. Regression-run the PoC (disposable loopback relay — never the live house)

```bash
cd "$HHREPO"
python3 "$POC" --relay-dir "$HHREPO" --out "$HHREPO/research/relay-pin-redteam/telemetry"
```
Stands up its own throwaway relay on a free loopback port, mints a PIN-gated room,
and runs three scenarios: **A legit** (correct PIN passes), **B lockout** (fixed IP,
7 wrong PINs → expect `4403×4→4429×3`), **C bypass** (rotating
`CF-Connecting-IP`, 12 wrong PINs → per-IP lockout is expected to *not* fire, that's
scenario B's job not C's). Read the emitted verdict, don't assume:

```bash
python3 -c 'import json;d=json.load(open("research/relay-pin-redteam/telemetry/pin-bypass-result.json"));\
print("verdict:", d["verdict"]);\
print("slug backstop present:", d["slug_backstop_present"], "trips@", d.get("slug_backstop_trips_at"));\
print("per-IP lockout still spoofable:", d["ip_rotation_defeats_per_ip_lockout"])'
```

**Gate condition — block exposure unless all hold:**
- `verdict == "BYPASS-CLOSED"` (not `BYPASS-CONFIRMED`)
- `slug_backstop_present == true` and `slug_backstop_trips_at` is a small integer
  (single digits — if it's crept up into the dozens, the backstop has been
  effectively disabled by a well-meaning tuning change)
- `legit_pin_passes_gate == true` (the fix didn't collaterally break real logins)

If any fails: **do not run `hh-go relay|public` or `hh-serve --cloudflare|--unsafe`**.
Report the exact field that regressed and stop — this mirrors hh-redteam's
halt-on-finding discipline, just for the relay instead of the sandbox.

## 2. ACL / grant-surface spot-check (live room, read-only)

Complements the PIN gate: a passed PIN only proves *entry*, not that grant state
matches intent. Before exposing a room further, or after granting anyone `can_sudo`:

```bash
HH="$HHREPO/.venv/bin/python -m cmd_chat.operator"     # or: hh-op
$HH status        # confirm your own grant state matches what you expect
$HH roster         # who's actually seated — cross-check against who you invited
```
No dedicated ACL-diff tool exists yet; this is a manual cross-check until one is
built. Flag (don't silently accept) any `can_sudo` grant you didn't personally issue
via `/grant <name>` this session — treat it the same as an RQ4 finding in
`hh-ir-watch`: ask who granted it before assuming it's benign.

## 3. Which exposure command is which (know what you're gating)

| command | reach | gate this skill runs before |
|---|---|---|
| `hh-go` (bare) | local only | — (no exposure, skip) |
| `hh-go relay tailnet` / `hh-serve` | tailnet only, needs Tailscale | §1 recommended |
| `hh-go relay` / `hh-serve --unsafe` | **public internet**, funnel | §1 **required** |
| `hh-go public` / `hh-serve --cloudflare` | **public internet**, branded domain | §1 **required** |

`hh-serve --unsafe`/`--cloudflare` already force a human to type `expose` at a TTY
— this skill is the check that should happen in the moment *before* that keystroke,
not a replacement for it.

## Safety

- The PoC only ever touches its own disposable, loopback-only relay instance — it
  never sends traffic at a live house, never brute-forces a real PIN.
- This is a **regression gate**, not new exploitation: don't extend §1 into probing
  the live relay's actual lockout with real attempts.
- A failing gate is a stop condition — block the exposure change, log the exact
  regressed field, and hand off to whoever owns `relay/app.py` (the fix lives in
  the **web-relay** worktree, not `main`).
