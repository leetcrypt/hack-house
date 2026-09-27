# hack-house → Multi-Tenant Model Hosting — Spec

> **Status:** Implemented (all six phases) · **Date:** 2026-09-25 (impl 2026-09-26)
> **Scope:** Let **anyone who holds sandbox-drive** in a room host or connect
> **any** AI model/agent (Claude Code, Codex, Gemini, local Ollama, any
> OpenAI-compatible or Anthropic-API model, or an MCP-client model) as their
> **own named instance** that other members can query — with the *instance
> owner*, not only the room host, controlling who may query it, gating
> individual prompts (`/allow` / `/reject`), and delegating control (`/grant`).
> **Builds on (single source of truth — cited, not duplicated):**
> `spec-agent-bridge.md` (agent = client, `/ai` grammar, privacy posture),
> `model-agnostic-operator-plan.md` (Provider core, runner registry, harness,
> MCP phase), `command-reference.md` (roster glyphs, `/grant` / `/sudo`).

---

## 0. What is already built vs. what this adds

The **brain-attach** side is largely done and is *already* model-agnostic:

| Attach mechanism | State | Where |
|---|---|---|
| Hosted-provider chat agent (Ollama / OpenAI-compat / Anthropic / `module:Class`) | shipped | `cmd_chat/ai/providers.py`, `profiles.py`, `models.toml`, `/ai start` |
| CLI-subprocess operator (`claude` / `codex` / `gemini` / generic `cmd`) | shipped | `cmd_chat/operator/bootstrap.py` `RUNNERS` |
| Harness-mode operator (native tool-loop, any function-calling model, no CLI) | shipped | `cmd_chat/operator/harness.py` |
| MCP server exposing the operator verbs | **not built** (P6 of the agnostic plan) | *this spec, §4* |

What is **missing** is not another way to attach a brain — it is a **multi-tenant
ownership + permission layer** that unifies all attach modes behind one in-room
concept and moves access control from *room-host-only* to *per-instance owner*.
This spec defines that layer and folds MCP in as one of four attach modes.

---

## 1. Core concept — the **instance**

Every AI presence in a room, regardless of how its brain attaches, is one
**instance**: a named room seat with an owner and a capability record.

```
instance := {
  name        : str        # roster identity, unique in room (e.g. "oracle")
  owner       : str        # the member who spun it up / connected it
  kind        : provider | subprocess | harness | mcp     # attach mode
  brain       : str        # "ollama/qwen2.5:3b", "claude-code/opus", "mcp:cursor", …
  query_acl   : public | [users]      # who may address it (default: public)
  ask_mode    : bool       # hold each non-owner prompt for owner approval (default: off)
  managers    : [users]    # delegated control of THIS instance (owner-granted)
  sandbox     : none | granted        # may it drive the SHARED sandbox (host-gated, §3)
  seat        : counts against max_users (agnostic-plan decision G)
}
```

**Prerequisite to host:** you must already hold **drive** (`◆`, granted by the
room host per `command-reference.md §3`). Drive is the entry ticket; holding it
lets you `/ai start`, `spawn`, `operate`, or attach an MCP client. This is the
literal reading of "anyone with drive permissions can spin up or connect a
model," and it keeps hosting inside the room's existing trust gate.

**Announce-on-join (unchanged privacy posture, `spec-agent-bridge.md §1`).** On
attach the instance posts one visible line so the room knows a model is present
and who owns it:

```
🤖 oracle joined — claude-code/opus (subprocess), owned by alice · query: public · sandbox: none
```

---

## 2. The two-axis permission model

The redesign separates **two different rights** that the current host-centric
model conflates. This separation is the whole design.

| Right | Over what | Who authorizes | Why here |
|---|---|---|---|
| **Query** an instance | someone's *model conversation* | the **instance owner** | it's *their* model, their cost, their creds — they decide who it answers |
| **Drive** the shared sandbox | the room's *one shared PTY* | the **room host** (existing `/grant`) | shared, dangerous, containment-critical — must stay authoritative |

The insight that makes "anyone can host" safe: **hosting a model and touching the
shared sandbox are different privileges.** Any driver can host a model and let
the whole room query it *without* that model ever gaining shared-sandbox drive.
Giving the instance shared-sandbox drive is a *separate*, host-gated step (§3).
So decentralizing query control never weakens containment law.

### 2.1 Command grammar (extends `spec-agent-bridge.md §2`)

All per-instance verbs are owner-or-manager only and take an optional
`<instance>` (required only when the caller owns more than one):

| Command | Who | Effect |
|---|---|---|
| `/ai start [model\|profile] [allow]` | any **driver** | Spin up a provider/harness instance; caller becomes **owner**. `allow` = request sandbox drive at spawn (still host-gated, §3). |
| `spawn --runner claude\|codex\|gemini\|cmd …` | any **driver** | Same, for a CLI-subprocess brain. Caller = owner. |
| `/ai attach mcp <name>` | any **driver** | Register an MCP-client brain as an instance (§4). |
| `/ai <name> <question>` | anyone **on the query ACL** | Address the instance. |
| `/ai allow <user> [<instance>]` | **owner/manager** | Add `<user>` to the query ACL. |
| `/ai reject <user> [<instance>]` | **owner/manager** | Remove `<user>` / deny querying. |
| `/ai public\|private [<instance>]` | **owner/manager** | ACL default: `public` = anyone may query (default); `private` = allowlist only. |
| `/ai grant <user> [<instance>]` | **owner** | Delegate management of this instance (a **manager**): may allow/reject/approve, and drive its conversation. |
| `/ai revoke <user> [<instance>]` | **owner** | Withdraw manager rights. |
| `/ai ask-mode on\|off [<instance>]` | **owner/manager** | Per-prompt approval gate (§2.2). |
| `/ai stop [<instance>]` | **owner** | Dismiss the instance (frees its seat). |
| `/ai list` | anyone | Roster of instances: name · brain · owner · query state · sandbox state. |

Reserved first-tokens (an instance name may not collide): `start`, `stop`,
`list`, `attach`, `allow`, `reject`, `grant`, `revoke`, `public`, `private`,
`ask-mode`, `approve`, `deny`. Resolution order: reserved verb → known instance
name → error with a "did you mean" (`command-reference.md §10`).

### 2.2 Per-prompt approval flow (`/allow` / `/reject` "prompts from other people")

Standing ACL is coarse; `ask-mode` is the fine-grained consent gate the ask
calls for. When `ask-mode` is on (or a querier is off the ACL but not hard-
rejected), an incoming prompt is **held**, not delivered to the model. The owner
sees a pending entry and rules on it:

```
⧗ pending #3  bob → oracle:  "summarize the pcap in /root/capture"
   /ai approve 3     → deliver to the model, reply goes to the room
   /ai deny 3        → drop it; bob is told "owner declined"
```

Approvals are per-message and ephemeral; they never widen the standing ACL.
`/ai allow bob` is the standing form ("bob may always query"); `approve` is the
one-shot form.

---

## 3. Shared-sandbox drive stays host-authoritative

Query control is decentralized; **shared-sandbox drive is not.** An instance
touches the room's shared PTY only when the **room host** grants it, exactly as
today (`/grant <instance>` / `Ctrl-X` kill switch, `command-reference.md §3/§8`).

- `/ai start … allow` and `spawn … allow` **request** drive; the request surfaces
  to the host as a pending grant, it is not self-granted.
- Enforcement is authoritative at the relay boundary: the server/host must reject
  sandbox-drive frames (`keys`/`exec`/`write`) from any identity not on the
  host's driver set — the same gate that governs human `/drive` today. This is
  the one control that cannot live in the instance's own runtime.
- Containment law (`CLAUDE.md`) is untouched: `assert engine != local`, the shared
  sandbox is the isolated podman/VM engine, never the host.

An owner who *is* a driver may of course let their instance act **as themselves**
within their own drive rights — but the shared sandbox is one object, so the host
remains the single point that says who drives it.

### 3.1 Where each right is enforced

- **Query ACL / ask-mode:** in the **instance's own runtime** (the agent/operator
  daemon the owner launched). The daemon decrypts all room traffic (it is a
  client) but only calls the model when the addresser passes the owner's ACL.
  This is correct and sufficient: it is the owner's model refusing to answer — no
  server trust needed, consistent with `spec-agent-bridge.md §1` "addressed-only."
- **Sandbox drive:** at the **server/host relay** (authoritative), per §3.
- **Seat count:** server, against `max_users` (agnostic-plan decision G).

---

## 4. MCP as the fourth attach mode (build P6, scoped by ownership)

MCP is **not** the universal answer — it is one of four attach modes, valuable
because it lets models we don't wrap (Cursor, Cline, Continue, Claude Desktop,
an Agents-SDK app) drive a room with zero hack-house glue.

- **`cmd_chat/operator/mcp_server.py`** exposes the operator verbs as MCP tools
  (`hh.say`, `hh.read`, `hh.keys`, `hh.exec`, `hh.write`, `hh.get`, `hh.screen`,
  `hh.watch`, `hh.manifest`, `hh.spawn`) over stdio (local client) or SSE
  (remote), backed by the **same operator bridge** — no new side-effect surface.
- **Registration handshake:** first tool the client must call is
  `hh.join(host, port, name, password, owner)`, which creates the instance record
  (`kind = mcp`, `owner = <the launching member>`) and fires announce-on-join.
  Until `hh.join` succeeds, no other verb resolves.
- **Ownership:** whoever launched the MCP session with room creds is the owner;
  their instance obeys the §2 ACL/ask-mode/grant model like any other.
- **Sandbox drive** via MCP is still host-gated (§3): `hh.keys`/`hh.exec` fail
  with a clear "not granted — ask the room host to `/grant <name>`" until granted.

MCP and harness-mode are complementary: harness serves raw-HTTP models that
aren't MCP clients; MCP serves MCP clients for free. Gate the server behind an
explicit enable flag; ship after the §2 permission layer lands.

---

## 5. One uniform instance registry

All four attach modes must produce the **same** `instance` record (§1) so the
roster, `/ai list`, ACL checks, and seat accounting are mode-agnostic.

- **In-room, ephemeral (source of truth for live state):** the room carries the
  instance table as control frames (join / leave / acl-change / grant / pending /
  approve). The host TUI renders it; every client can compute the roster from it.
- **`.hh` host-global (durable, optional):** persist an owner's instance
  *definitions* (name → attach recipe: kind, profile/runner, default ACL) so a
  member can re-launch "my oracle" with one command across sessions. Never persist
  creds — recipes name `api_key_env` / a runner creds path, per
  `providers.md` and the runner registry.
- **Capability probe on attach:** reuse `ToolsUnsupported` degrade
  (agnostic-plan §Phase-2 risk) — a non-function-calling brain hosts as a
  chat-only instance (query yes, sandbox no) rather than pretending to operate.

---

## 6. Phased delivery

Ordered by dependency; each independently reviewable (matches the repo's
one-PR-per-phase norm).

1. **Instance record + registry frames.** Define the `instance` struct and the
   control-frame set (join/leave/acl/grant/pending/approve). Make `/ai start`,
   `spawn`, `operate` all emit a uniform record + announce-on-join. Roster shows
   owner + query state. *No behaviour change to who-can-do-what yet.*
2. **Decentralize query control.** Move `/ai start` from admin-gated to
   **driver-gated**; spinner = owner. Implement `/ai allow|reject|public|private`
   enforced in the instance runtime. Owner-scoped, multi-instance disambiguation.
3. **Delegation + per-prompt approval.** `/ai grant|revoke` (managers) and
   `ask-mode` + `/ai approve|deny` with the pending queue.
4. **Sandbox-drive request path.** `… allow` becomes a host-surfaced *request*;
   confirm the relay authoritatively rejects ungranted drive frames (audit the
   existing `/drive` gate; close any gap).
5. **`.hh` instance recipes.** Persist/replay an owner's instance definitions
   (no creds). One-command re-launch.
6. **MCP attach mode (P6).** `mcp_server.py` + `hh.join` handshake + ownership
   wiring, behind an explicit enable flag.

**Acceptance (end-to-end):** two non-host drivers each `/ai start` a different
model (one local Ollama, one `spawn --runner codex`); a third member queries
both; each owner sets one `private` + `ask-mode on` and approves/denies a prompt;
one owner `/ai grant`s a co-manager who then approves on their behalf; neither
instance can touch the shared sandbox until the room host `/grant`s it; a Claude
Desktop MCP client joins as a fourth instance under the same rules.

---

## 6b. Delivery status (2026-09-26)

All six phases landed on `feat/multi-tenant-model-hosting` (local; not published).

| Phase | State | Where |
|---|---|---|
| P1 registry + announce | done | agent `_instance_frame`/announce (`cmd_chat/agent/bridge.py`); Rust `Net::AiInstance` parse + `App.instances` (`hh/src/{net,app}.rs`) |
| P2 decentralize query | done | `_may_query` + verbs (bridge.py); `/ai start --owner <me>` at spawn (`hh/src/app.rs`) |
| P3 delegation + approval | done | `_handle_management` / `_gate_query` / `_resolve_prompt` (bridge.py); sole-form `/ai <verb>` expansion (app.rs) |
| P4 sandbox-drive gate | **verified, no change** | authoritative at the relay: `_sbx:input.from` is the SRP-stamped sender (`hh/src/net.rs`), host writes the PTY only `if app.drivers.contains(&from)` (`hh/src/app.rs`, `Net::SbxInput`). Query control was decentralized without weakening this. |
| P5 instance recipes | done | `cmd_chat/agent/recipes.py` + `--recipe`/`--save-recipe`/`--list-recipes` |
| P6 MCP attach | done (gated) | `cmd_chat/operator/mcp_server.py`, off by default (`--enable`/`HH_MCP_ENABLE`), optional `mcp` dep |

Tests: `tests/test_agent_acl.py`, `test_agent_recipes.py`, `test_mcp_server.py`
(Python); `net::tests` instance-frame parse (Rust). Python 256 passed, Rust 73.

Known follow-ups (not blocking): a TUI `/ai save`/`/ai recipes` surface for P5
recipes; a first-class `_ai:instance` roster line vs. the current per-row
annotation; live end-to-end MCP validation against a real client.

## 7. Non-negotiables carried forward

- **Zero-knowledge relay unchanged.** Server still sees only ciphertext; the
  registry rides as encrypted control frames; query enforcement lives in the
  owner's instance runtime (`spec-agent-bridge.md §1`).
- **Containment law.** Shared sandbox = isolated engine only; host relay is the
  authoritative drive gate; `assert engine != local` (`CLAUDE.md`).
- **Secrets from the environment.** Profiles/recipes name `api_key_env` or a
  runner creds path, never a key; `.hh` recipes persist no creds
  (`providers.md`, `CLAUDE.md`).
- **Seat accounting** against `max_users` (agnostic-plan decision G); each
  instance is one seat; document raising `CMD_CHAT_MAX_USERS` for busy rooms.

---

## 8. Open questions

- **Manager vs. owner on `stop`:** may a manager `/ai stop` an instance, or is
  teardown owner-only? (Leaning owner-only; managers can `reject`-all instead.)
- **ACL persistence:** does a `private` list survive an instance restart via a
  `.hh` recipe, or reset each launch? (Leaning: recipe stores it, owner can wipe.)
- **Room-host override:** should the host be able to force-`/ai stop` any
  instance (abuse control), paralleling `/kick`? (Likely yes — host owns the room
  and the seat budget.)
- **MCP remote transport:** stdio vs SSE first — pick from the first target
  client (agnostic-plan §6 open question, inherited).
- **Cross-instance addressing:** when >1 instance is `public`, does bare `/ai
  <question>` broadcast, error, or round-robin? (Inherited from
  `spec-agent-bridge.md`; keep "error if ambiguous.")
