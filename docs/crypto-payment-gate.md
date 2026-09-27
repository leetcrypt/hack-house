# Design: crypto-currency "pay-to-join" gate (second gate after the password)

**Status:** research + plan (no code yet)
**Goal:** require a crypto payment *in addition to* the SRP password before a
client is admitted to a hack-house room, **without weakening** the existing
end-to-end-encryption / zero-knowledge-relay properties.

---

## 1. What the gate must respect (current architecture)

Grounded in the present code so the design slots in cleanly:

- **Two-step SRP handshake over HTTP, then a WebSocket upgrade.**
  - `POST /srp/init` → returns `user_id, B, salt, room_salt` and enforces a *soft*
    capacity / username check (`cmd_chat/server/views.py:30-63`, capacity at `:48`).
  - `POST /srp/verify` → verifies the SRP proof `M`, then **commits the session**
    and issues the `ws_token` (`cmd_chat/server/views.py:66-111`). The
    **authoritative capacity gate is here** (`:84-85`), the proof check at `:87`,
    and `session_store.add(session)` at `:97`.
  - `GET (ws) /ws/chat?user_id=…&ws_token=…` → HMAC-checks the token and joins the
    room (`cmd_chat/server/views.py:114-149`).
- **Rust client mirror:** `hh/src/net.rs:44-104` (`authenticate`) does init →
  `process_challenge` → verify → checks server `H_AMK` (mutual auth, MITM guard)
  → derives the room Fernet → builds the ws URL. The Python client mirrors this
  in `cmd_chat/client/client.py`.
- **Zero-knowledge relay.** The server never derives the room key
  (`HKDF(password, room_salt, "cmd-chat-room-key")` is computed **client-side
  only**) and only relays Fernet ciphertext. It *can* see: usernames, IPs,
  timestamps, `user_id`s, roster, ws-token validity. It *cannot* see: message
  plaintext, the room key, sandbox I/O, `_perm`/`_ft` control frames.
- **Owner is authoritative, not the server.** Chat rooms have no owner; the first
  user to `/sbx launch` becomes sandbox owner and the **owner's client**
  broadcasts the ACL (`_perm:acl`) encrypted — the server relays it blindly. This
  is the precedent we lean on for the trust-minimized tier below.
- **One password per server instance** (`serve --password`); a "room" is just a
  running server. No room-creation endpoint, no persistent accounts, no
  allow-lists/bans. Capacity defaults to 4 (`CMD_CHAT_MAX_USERS`). Per-IP rate
  limit 10/60s on both SRP endpoints.

**Key consequence:** payment is a *money* trust concern, orthogonal to the E2E
*message* trust concern. We must not route the room key or plaintext through any
payment logic, and we should keep the relay as close to "dumb" as possible.

---

## 2. Design principles / security requirements

1. **Non-custodial.** The app never holds user funds or spend-capable keys. Funds
   go **directly to the owner's** node/wallet. We only *issue invoices* and
   *verify receipt*. (Custody would add huge security + regulatory burden.)
2. **Two independent gates.** Password (SRP) and payment are separate; failing
   either denies entry. Payment is checked **only after** SRP succeeds, so an
   unpaid attacker who lacks the password learns nothing and never sees a
   payment request.
3. **Keep the relay dumb where possible.** Prefer designs where the server checks
   a *signature* or a *preimage* rather than running chain logic. Full chain
   access lives in an **owner-operated** component.
4. **Atomic pay-to-join.** Never take money for a seat that isn't granted. Use a
   capacity reservation + Lightning **hold invoice** so payment settles *iff* the
   seat is actually granted; otherwise it auto-cancels (refund).
5. **Single-use, identity-bound proofs.** Every proof is bound to one `user_id`
   + room + nonce, expires quickly, and is burned on use (anti-replay / anti-share).
6. **No fiat price oracle in the hot path.** Price in the native unit (sats /
   atomic units). Optional fiat display is cosmetic and never gates admission.
7. **Least-privilege keys, never in the repo.** Invoice/read-only node
   credentials via env; no admin/spend keys. Testnet/regtest/signet by default;
   mainnet is an explicit opt-in flag.
8. **Fail safe + DoS-resistant.** Payment-backend errors deny entry (don't
   fail-open) but never hang the SRP handshake; invoice issuance is rate-limited.
9. **Privacy-aware.** Prefer rails that don't dox the payer's whole graph
   (Lightning ≫ on-chain; Monero strongest).

---

## 3. Currency / rail choice

| Rail | Settlement | Fees | Privacy | Proof primitive | Verdict |
|------|-----------|------|---------|-----------------|---------|
| **Bitcoin Lightning** | ~instant | ~0 | good | **payment preimage** (sha256(P)=hash) | **Recommended** |
| Monero | ~2 min | low | **best** | tx proof (tx key + addr) | strong privacy alt |
| On-chain L2 stablecoin (USDC on Base/Arbitrum) | seconds–min | low | poor | tx receipt + log | fiat-stable alt |
| On-chain BTC / ETH L1 | 10 min / minutes | high | poor | tx receipt | bad UX, avoid |

**Recommendation: Bitcoin Lightning.** It matches the ephemeral, instant,
micro-fee nature of "pay a few sats to join a terminal room," and the **preimage
is a self-verifying proof of payment** that needs no trusted third party *if the
verifier knows the invoice's payment hash*. The hold-invoice variant gives us
atomic seat reservation for free.

The implementation is built behind an interface (§5) so Monero / L2-stablecoin
backends can be added without touching the handshake.

---

## 4. The core idea: hold-invoice pay-to-join

Lightning **hold invoices** (a.k.a. HODL invoices, LND `addHoldInvoice` /
CLN `holdinvoice`) let the receiver *accept* a payment (funds locked by the
payer) and decide later whether to **settle** (claim) or **cancel** (refund):

1. Owner's node generates a hold invoice for the join fee, with a `payment_hash`
   it controls and a description that **commits to `user_id`**.
2. Joiner pays; the HTLC is now *accepted* (locked) but **not settled** — the
   payer cannot spend it elsewhere and the owner hasn't claimed it.
3. The gate **reserves a seat** (capacity check) and, iff a seat is available,
   tells the node to **settle** → owner is paid, joiner is admitted.
4. If no seat / timeout / error → **cancel** the invoice → joiner is auto-refunded,
   nobody paid for a seat they didn't get.

This is the cleanest way to satisfy principle #4 (atomicity).

---

## 5. Pluggable `AdmissionGate` abstraction

Introduce one small interface so the payment rail is swappable and the
"no gate" path is the existing behavior (zero risk to current deployments).

```python
# cmd_chat/server/admission.py  (illustrative)
class AdmissionGate(Protocol):
    async def quote(self, user_id: str, username: str) -> Quote | None:
        """Return a payable Quote (invoice/LNURL/address + opaque challenge),
        or None if no payment is required (free room)."""
    async def verify(self, user_id: str, proof: dict) -> bool:
        """True iff `proof` settles `quote` for this user_id, single-use,
        unexpired, correct amount. Reserves+settles atomically for hold invoices."""

# backends:
#   NullGate        -> always free (today's behavior; default)
#   LightningGate   -> Tier 1, self-hosted LNbits/LND/CLN
#   VoucherGate     -> Tier 2, verifies owner-signed Ed25519 admission vouchers
#   (future) MoneroGate, EvmGate
```

Server wiring (`cmd_chat/server/factory.py`): `app.ctx.admission = build_gate(cfg)`.
New `serve` flags (all optional; default = NullGate = unchanged):

```
cmd_chat.py serve <ip> <port> --password <pw> \
    --pay lightning \
    --pay-amount-sats 500 \
    --pay-backend lnbits --pay-url https://lnbits.local --pay-key-env HH_LNBITS_INVOICE_KEY \
    --pay-network signet
# or, trust-minimized:
    --pay voucher --admit-pubkey <ed25519-pub-b64>
```

---

## 6. Two implementation tiers

### Tier 1 — server-mediated Lightning gate (MVP, pragmatic)

The server talks to an **owner-operated** LN backend (LNbits invoice key, or
LND/CLN gRPC with an invoice-only macaroon). Justified because in the common
self-hosted case the **owner *is* the server operator**, so trusting the server
to confirm payment is trusting yourself.

Flow:
1. `POST /srp/init` unchanged, **plus** server returns `pay_required: true` and a
   `pay_challenge` (random nonce bound to `user_id`) when a gate is active.
2. New `POST /pay/quote {user_id}` → server (after a *soft* capacity check) asks
   the node for a **hold invoice** committing `user_id` in its `description_hash`,
   returns `{bolt11, payment_hash, amount_sats, lnurl?, expires_at}`.
3. Client shows the invoice (BOLT11 + QR + LNURL) in the TUI and waits.
4. Joiner pays → HTLC accepted (held).
5. `POST /srp/verify {user_id, M, payment_hash}` → server: SRP-verify →
   **authoritative capacity gate** → ask node "is this invoice ACCEPTED, amount ok,
   user_id committed, not yet used?" → if yes **settle** + mark used + add session
   + issue `ws_token`; if no seat/timeout → **cancel** (refund) and return `402`.

HTTP semantics: `402 Payment Required` (+ a small JSON `{error, pay_required,
pay_challenge}`) when payment is missing/invalid; `409` still means full.

### Tier 2 — owner-signed admission vouchers (trust-minimized, end state)

Mirrors the existing "owner broadcasts the ACL, server relays blindly" model and
keeps the server **payment-blind** (no node creds, no chain access on the relay).

Components:
- The owner runs a tiny **doorman** next to their node (could live in the Rust
  client or a sidecar): an LNURL-pay endpoint + voucher signer holding an
  **Ed25519** key. The server is started with only the **public** key
  (`--admit-pubkey`).
- A joiner pays the owner directly (LNURL/BOLT11). On settlement the doorman signs
  an **admission voucher**:
  `voucher = Ed25519_sign( sk_owner, {user_id, room_id, amount, nonce, exp} )`.
- Joiner submits the voucher at `POST /srp/verify {…, voucher}`.
- Server verifies: signature against `admit_pubkey`, `user_id` matches, `exp`
  fresh, `nonce` unseen (single-use ledger), then admits. **The server verifies a
  signature, not a payment** — it never sees funds, invoices, or chain data.

Tradeoff: the owner/doorman must be reachable to *issue* vouchers; the relay no
longer needs to be trusted with money. This is the philosophically correct
end-state for a zero-knowledge relay; Tier 1 is the faster path to ship.

---

## 7. End-to-end sequence (Tier 1, hold-invoice)

```
client                         relay (server)                 owner LN node
  | POST /srp/init {A, user}        |                                |
  |-------------------------------->|  soft cap/username check       |
  |<-- {user_id,B,salt,room_salt,   |                                |
  |     pay_required, pay_challenge}|                                |
  | process_challenge               |                                |
  | POST /pay/quote {user_id}       |                                |
  |-------------------------------->|  addHoldInvoice(desc_hash=     |
  |                                 |    H(user_id|nonce|room)) ----->|
  |<-- {bolt11, payment_hash, amt,  |<------- invoice ---------------|
  |     lnurl, expires_at}          |                                |
  |  [TUI shows invoice/QR; pay] ------------------- pay ----------->| (HTLC accepted/held)
  | POST /srp/verify {user_id,M,    |                                |
  |     payment_hash}               |                                |
  |-------------------------------->|  SRP verify (:87)              |
  |                                 |  AUTHORITATIVE cap gate (:84)  |
  |                                 |  lookupInvoice == ACCEPTED? --->|
  |                                 |  amount ok? user_id committed? |
  |                                 |  unused?  -> settle ----------->| (owner paid)
  |                                 |  session_store.add (:97)       |
  |<-- {H_AMK, ws_token}            |                                |
  | check H_AMK (MITM guard)        |                                |
  | ws /ws/chat?user_id&ws_token    |                                |
  |================ joined =========|                                |
        (on any failure between verify+settle: node.cancelInvoice -> refund, return 402)
```

---

## 8. Anti-replay, binding, atomicity (the security-critical bits)

- **Per-join invoice.** A fresh invoice/nonce per join attempt; never a static
  address. Static addresses enable replay and can't bind identity.
- **Identity binding.** Commit `user_id` (and room id) into the invoice
  `description_hash` (BOLT11) or the voucher payload, so a proof minted for one
  joiner can't admit another — defeats proof theft by a malicious relay/peer.
- **Single-use ledger.** In-RAM set of consumed `payment_hash`/`nonce` for the
  server's lifetime; reject reuse. (Matches the project's ephemeral, RAM-only
  ethos — no DB needed.)
- **Expiry.** Short invoice/voucher TTL (e.g. 5 min) tied to the SRP
  `pay_challenge`; expired ⇒ re-quote.
- **Atomic seat.** Capacity is reserved at the authoritative gate
  (`views.py:84-85`) *and* the hold invoice is only settled after the seat is
  secured; otherwise cancel→refund. Consider a short-lived "seat hold" so two
  payers don't race the last seat (reserve before settle, release on failure).
- **Amount check.** Enforce `amount >= price`; reject underpayment; for overpay,
  settle and (optionally) note credit — never silently keep extra without policy.

---

## 9. Client UX (Rust TUI + Python)

- **Connect command** gains optional payment handling. When `/srp/init` returns
  `pay_required`, the client:
  - fetches a quote, renders a **payment panel**: amount (sats), a copyable BOLT11,
    an `lnurl:`/`lightning:` URI, and a **QR code** (ratatui can draw a QR via a
    unicode/half-block widget; Python client prints an ASCII QR).
  - shows live status: `waiting for payment → received (held) → admitted`.
  - then proceeds to `/srp/verify` automatically once paid.
- **CLI flags** for non-interactive/automation: `--pay-bolt11-out <file>` (dump the
  invoice) or `--voucher <file>` (Tier 2, present a pre-obtained voucher).
- **Help menu:** add to the existing clustered help (`hh/src/ui.rs`
  `help_clusters`) — a short note under a new `ACCESS` or extended `KEYS`/intro
  cluster: *"rooms may require a Lightning payment to join; you'll be shown an
  invoice after the password check."*
- **Failure copy:** `402` ⇒ "this house requires payment to enter"; expired ⇒
  "invoice expired — press R to refresh"; full-after-pay ⇒ "house filled while
  paying — you were refunded."

---

## 10. State, config, key management

- **New server ctx:** `admission` gate, `paid_nonces` (single-use set),
  `seat_holds` (transient reservations). All **in-RAM**, cleared on restart —
  consistent with the existing ephemeral stores (`cmd_chat/server/stores.py`).
- **Secrets via env only** (never in repo, never in `serve` argv where it'd hit
  `ps`): `HH_LNBITS_INVOICE_KEY`, `HH_LND_MACAROON_PATH` (invoice-only macaroon),
  `HH_LND_TLS_CERT`, etc. Pass *names* on the CLI (`--pay-key-env`), read values
  from the environment.
- **Least privilege:** LNbits **invoice/read key** (not admin); LND macaroon baked
  to `invoices:write/read` + `invoices:settle`/`cancel` only — **no `onchain`/
  `offchain` spend** permissions.
- **Network guard:** default `--pay-network signet|regtest|testnet`; require an
  explicit `--pay-network mainnet --i-understand` to use real funds.

---

## 11. Threat model

| Threat | Mitigation |
|--------|-----------|
| Replay a proof to join repeatedly / share with friends | per-join invoice + single-use nonce ledger + `user_id` binding in `description_hash`/voucher |
| Malicious relay steals the preimage to join itself | identity-bound invoice (preimage only admits the committed `user_id`); Tier 2 removes relay from the money path entirely |
| Pay but no seat (race / full) | hold invoice + seat reservation; cancel→auto-refund on failure; clear UX |
| Payment-backend down → fail-open | gate denies entry on backend error (fail-closed); never silently admits |
| Invoice-spam DoS / griefing | reuse existing per-IP rate limit (`helpers.py`) on `/pay/quote`; cap concurrent unpaid holds per IP; short TTLs |
| Front-running the last seat | reserve seat *before* settle; release on abort |
| Fiat-oracle manipulation | price natively in sats; no oracle in the admission path |
| Key leakage | invoice/read-only creds, env-only, no spend keys; mainnet behind explicit flag |
| MITM on the HTTP leg | unchanged SRP `H_AMK` mutual-auth guard (`net.rs:87-90`); run behind TLS in prod (today's `--no-tls` is dev-only) |
| Privacy deanonymization | Lightning over on-chain; document Monero option; never log payer metadata beyond the ephemeral nonce |
| Regulatory/custody risk | strictly non-custodial; funds never touch app-controlled spend keys |

---

## 12. Privacy & legal notes (design guidance, not legal advice)

- **Privacy:** Lightning leaks far less than on-chain; the relay should log only
  the ephemeral nonce/payment_hash, never amounts tied to usernames/IPs longer
  than the session. Offer Monero for the strongest payer privacy.
- **Compliance:** staying **non-custodial** (funds go owner→owner, app never
  holds spend keys) keeps this closest to "the owner accepts tips/entry fees,"
  but money-transmission / KYC-AML obligations vary by jurisdiction and volume.
  Flag this to the operator; do not build custody or fiat on/off-ramps into the
  app. This document is engineering guidance, not legal advice.

---

## 13. Testing strategy

- **Unit:** `NullGate` (free), `VoucherGate` signature + expiry + replay vectors
  (golden Ed25519 cases, mirroring the existing offline SRP vectors in
  `test_srp.py` / Rust `Selftest`).
- **Integration (regtest):** spin LND/CLN in `regtest` (Polar or docker), drive a
  full pay-to-join: quote → pay → settle → join; and the negative paths
  (underpay, expire, full-after-pay→cancel/refund, backend-down→deny).
- **Interop:** extend the Rust live `Handshake` self-test to optionally carry a
  voucher; ensure Rust and Python clients produce identical proof framing.
- **Headless demo:** a `demo-pay-to-join.sh` (sibling of `demo-save-load.sh`)
  using a regtest node to film the beat: password → invoice in the TUI → pay →
  admitted.

---

## 14. Phased rollout

1. **Phase 0 — interface only.** Add `AdmissionGate` + `NullGate`, wire
   `app.ctx.admission`, thread an optional `proof` field through `/srp/verify`.
   Default behavior identical to today. (Lowest risk; everything else builds on it.)
2. **Phase 1 — VoucherGate (Tier 2 verify side).** Ed25519 voucher verification on
   the server (`--admit-pubkey`), single-use ledger, `402` path + client flag to
   present a voucher. Server stays payment-blind. Testable with a CLI signer.
3. **Phase 2 — LightningGate (Tier 1) + `/pay/quote`.** Hold-invoice issuance via
   LNbits/LND, settle/cancel atomicity, TUI payment panel + QR. regtest e2e.
4. **Phase 3 — owner doorman (Tier 2 issue side)** + LNURL, so payment is fully
   owner-authoritative and the relay never touches money. Optional Monero backend.

---

## 15. File-change map (when we implement)

| Area | File(s) | Change |
|------|---------|--------|
| Gate interface + backends | `cmd_chat/server/admission.py` (new) | `AdmissionGate`, `NullGate`, `VoucherGate`, `LightningGate` |
| Server wiring + flags | `cmd_chat/server/factory.py`, `cmd_chat.py` | build gate from `--pay*` flags; ctx state |
| Init advertises gate | `cmd_chat/server/views.py:30-63` | add `pay_required`, `pay_challenge` to `/srp/init` |
| New quote endpoint | `cmd_chat/server/routes.py`, `views.py` | `POST /pay/quote` (Tier 1) |
| Verify enforces payment | `cmd_chat/server/views.py:66-111` | accept `proof`/`payment_hash`; gate between `:87` and `:97`; `402` path; settle/cancel |
| Single-use + seat state | `cmd_chat/server/stores.py` | `paid_nonces`, `seat_holds` (in-RAM) |
| Python client UX | `cmd_chat/client/client.py` | quote fetch, invoice display/QR, wait-for-pay, send proof |
| Rust client UX | `hh/src/net.rs:44-104`, `hh/src/app.rs`, `hh/src/ui.rs` | quote step, payment panel + QR, proof in verify, help entry |
| Owner doorman (Tier 2) | new sidecar or `hh/` subcommand | LNURL-pay + Ed25519 voucher signer (holds keys) |
| Tests | `cmd_chat/tests/`, Rust `Cmd::*` | gate unit + regtest integration + interop |

---

## 16. Open decisions (need owner input)

1. **Trust posture:** ship Tier 1 (server-mediated, simplest) first, or hold out
   for Tier 2 (relay stays payment-blind)? Recommendation: Phase 0→1 (voucher
   verify) gets us trust-minimized verification quickly; add Lightning issuance
   (Phase 2) for UX.
2. **Rail:** Lightning only at first? (Recommended.) Monero as a fast-follow for
   privacy?
3. **Pricing:** flat sats per join? per-room configurable? time-boxed
   (pay-per-hour) vs one-shot entry?
4. **Backend:** LNbits (fastest to integrate, semi-custodial unless self-hosted)
   vs direct LND/CLN (more setup, fully self-custodial)?
5. **What payment buys:** plain entry, or also a role (e.g. auto-`/grant` drive)?
   Note: roles are owner-broadcast ACL today, so coupling payment→role belongs in
   the owner's client, not the relay.
