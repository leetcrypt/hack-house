# FLEET-vm-verify.md — vm-verify (worker pane, fleet `hackhouse-dev`)

<!-- Written once into this pane's cwd by forge.sh redesign; never overwritten on a
     re-run if it already exists (same convention as fleet-orchestrator's GOAL.md) —
     edit it by hand once the fleet is live, forge won't clobber your edits.

     Deliberately NOT named CLAUDE.md: this pane's cwd is a real project and very
     likely already has its own CLAUDE.md with domain context. tmux-fleet-forge's own
     non-clobber guard would silently skip writing fleet-role context into a file that
     already exists — so fleet-role context gets a dedicated, collision-free file
     (ICM: one file, one owner). Read BOTH: this file for fleet role/rails/reporting,
     the project's own ./CLAUDE.md (if present) for domain context. -->

You are the **worker** pane of the `hackhouse-dev` fleet, tmux target `hackhouse-dev:vm-verify`.
This is fleet-role context — for domain/project context, also read `./CLAUDE.md` in
this same directory if it exists (this pane's cwd is a real project, not a scratch dir).

## Mission
Advance the single target/task assigned to this pane. You are the only pane responsible for this scope — depth over breadth.

## North star
Do the "Your First Task" from docs/vm-library-contributor-brief.md. KNOWN going in: hh-snapshots/ does not exist on trillsec at all, and all 77 share_path-having entries point to missing local files -- but CLAUDE.md says the ~24G tars are deliberately NOT pulled to this mirror box, laptop is canonical. FIRST determine whether this is that expected mirror gap (check via ssh laptop/laptop-ts whether the tars genuinely exist THERE) or a real bug, before concluding anything is broken. Then verify end to end using a VM YOU actually build+save yourself this session (sidesteps the mirror-gap question entirely) as the real save/verify proof, not just an audit of pre-existing entries.

## Facts (verified — only grows)
- **Real registry lives at `~/coding/hack-house/.hh/registry.json`, not `~/.hh/registry.json`.** The
  `~/.hh` symlink CLAUDE.md describes doesn't exist on trillsec at all (`ls ~/.hh` → No such
  file or directory). 79 entries, 77 shareable, all with non-empty `share_path` values like
  `hh-snapshots/hh-snap-<label>.tar` (relative paths).
- **Confirmed: NOT a mirror-gap — genuine gap, present on laptop (canonical) too.** Checked
  `hh-snapshots/` under every plausible base (`~/coding/hack-house/`, `.../main/`, `.../.hh/`)
  on trillsec: none exist. SSH'd to laptop and ran the identical existence check against
  laptop's own `~/coding/hack-house/.hh/registry.json`: **0/77 share_paths resolve on laptop
  either.** Matches the contributor brief's own admission: "the actual tars may have been
  cleaned up or never fully materialized." This is the real gap to fix, not a sync artifact.
- **Registry writes are safe under fleet concurrency.** `hh/src/registry.rs` uses an advisory
  cross-process lock (O_EXCL sentinel file, 120s stale-reclaim) around read-modify-write, plus
  atomic temp-file+rename on store — confirmed by reading the source, not just inferred. Safe
  to run my own save/publish concurrently with the `vm-builder` sibling pane sharing this cwd.
- **`/sbx save` / `/sbx publish` are TUI-only** — not exposed via the Python operator CLI
  (`cmd_chat.operator sbx` only has launch/status/down for the *operator's own* container;
  `registry` subcommand is read-only list/show). Verifying the artifact layer end-to-end
  requires driving the real Rust TUI (`hh/target/debug/hack-house`), not the operator bridge.
- **`/sbx podman` needs `/drive` afterward** — summoning a sandbox does not auto-focus
  keystrokes into it; you're still in the chat message box until you send `/drive`.
- **Known infra snag reproduced live:** `/sbx podman` (default image `kalilinux/kali-rolling`)
  provisions unix accounts, which runs `apt-get update` inside the container first. Kali's apt
  mirror (`http.kali.org`) resolves IPv6-only here, and rootless podman's slirp4netns network on
  trillsec has broken/absent IPv6 egress (`getent hosts` resolves fine, but the container has no
  IPv6 route) — so this `apt-get update` hangs for minutes without any error surfaced to the TUI
  (client silently stays on "summoning…", no `BrokerMsg::Failed` message appears in chat even
  though `Net::Err` should fire on real failure — this is just a slow success, not yet confirmed
  failed). The container itself comes up fine and is exec-able (`podman exec hack-house` works
  immediately) — only the client-side "ready" handshake is gated behind this slow apt step.

## Working guesses (revisable)
-

## BIG finding — split-brain registry (flagging for human/overseer, not fixing unilaterally)
- **`~/.hh` is supposed to be a symlink to `~/coding/hack-house/.hh/`** (CLAUDE.md: "`.hh/` =
  host-global registry + credentials (`~/.hh` symlinks here; the path is hardcoded in code)").
  On trillsec that symlink **does not exist at all** (not even broken — `ls ~/.hh` was a plain
  "No such file or directory" before this session).
- Both the Rust client (`hh/src/registry.rs::registry_path()`) and the Python side
  (`cmd_chat/cardex.py::_registry_path()`, `cmd_chat/operator/__main__.py`) hardcode
  `Path.home()/".hh"/"registry.json"` — i.e. `~/.hh/registry.json` really is the one true
  canonical path per the actual code, not `~/coding/hack-house/.hh/registry.json` (the 79-entry
  file I was originally pointed at — that one is the orphaned *target* of the missing symlink,
  last touched Jul 7, still holding the real `credentials` file too).
- **My `/sbx save --local` + `/sbx publish` this session auto-created a brand-new, empty
  `~/.hh/` real directory** (not a symlink) and wrote a fresh, disconnected registry.json there
  containing only my `vm-verify-check` entry. Verified this is exactly what the live code does
  — nothing malicious/racy, just falls back to creating the dir since `Path.home()/".hh"` didn't
  exist. Any other agent that saves/publishes here (including `vm-builder`, if its
  chess-engine-lab build takes the same TUI/registry path rather than a different one) will
  silently land in this same forked registry too, NOT the canonical 79-entry one, until the
  symlink is restored.
- **Did not fix this myself** — restoring `~/.hh → ~/coding/hack-house/.hh` (and merging my one
  real entry back into the canonical file) is host-global state outside git, and `vm-builder` may
  currently depend on `~/.hh/` existing as a plain dir mid-build (checked: no active `.lock` file
  in either location as of my check, so no write was in-flight at that moment, but a swap could
  still race a future write). This is bigger than "my pane's task" — escalating via
  OVERSEER-STATUS-vm-verify.md per the rails rather than self-expanding scope.
- **Recommended fix** (for a human / the overseer to approve): once no fleet pane has a save/
  publish in flight, `mv ~/.hh/registry.json /tmp/new-entry.json` (or merge its one `entries`
  key into the canonical file with the registry's own lock-respecting write path), `rmdir ~/.hh`,
  `ln -s ~/coding/hack-house/.hh ~/.hh`, then re-apply the merged entry.

## Done = (measurable)
- **Do the "Your First Task" from docs/vm-library-contributor-brief.md. KNOWN going in: hh-snapshots/ does not exist on trillsec at all, and all 77 share_path-having entries point to missing local files -- but CLAUDE.md says the ~24G tars are deliberately NOT pulled to this mirror box, laptop is canonical. FIRST determine whether this is that expected mirror gap (check via ssh laptop/laptop-ts whether the tars genuinely exist THERE) or a real bug, before concluding anything is broken. Then verify end to end using a VM YOU actually build+save yourself this session (sidesteps the mirror-gap question entirely) as the real save/verify proof, not just an audit of pre-existing entries.** is actually true, verified by something you ran/observed this session — not merely attempted.
- If this cwd is a git worktree meant to be merged: additionally, `forge.sh audit .` (this project's generator, e.g. `~/coding/ai-agents/tmux-fleet-forge/forge.sh audit .`) prints `AUDIT: PASS`. Self-report is not enough for anything headed toward a human merge decision.

## Current frontier (single next step)
- Read `./FLEET-vm-verify.md` (this file) and `./CLAUDE.md` if present, then take the smallest concrete action toward: **Do the "Your First Task" from docs/vm-library-contributor-brief.md. KNOWN going in: hh-snapshots/ does not exist on trillsec at all, and all 77 share_path-having entries point to missing local files -- but CLAUDE.md says the ~24G tars are deliberately NOT pulled to this mirror box, laptop is canonical. FIRST determine whether this is that expected mirror gap (check via ssh laptop/laptop-ts whether the tars genuinely exist THERE) or a real bug, before concluding anything is broken. Then verify end to end using a VM YOU actually build+save yourself this session (sidesteps the mirror-gap question entirely) as the real save/verify proof, not just an audit of pre-existing entries.**

## Rails / autonomy envelope
- Do not reach outside this pane's own cwd/target.
- Escalate to the overseer (via OVERSEER-STATUS-vm-verify.md, not direct pane injection) instead of self-expanding scope.
- Stay strictly inside this pane's own scope; never reach into another pane's cwd or
  target without going through the overseer.
- Active/destructive/external actions are HUMAN-GATED unless this project's own docs
  authorize them explicitly.
- Ground claims in what you actually ran/observed this session; default to
  false-positive over unverified success.

## Concurrency & git isolation (read before writing ANY file here)
This cwd may be shared — by another pane in this fleet, by the operator's own manual
`claude`/shell sessions, or both. Before editing, staging, or committing anything:
1. **Check for concurrent occupants first.** Run
   `tmux list-panes -a -F '#{pane_current_path}'` (works from inside any tmux pane
   regardless of session) and grep for this cwd. If another pane — this fleet's or not —
   already has it open, treat it as occupied: read-only reasoning is fine, but do not
   write until you've noted the overlap in `OVERSEER-STATUS-vm-verify.md` and, if real changes are
   needed, escalated to a human rather than assuming you have exclusive access.
2. **Check for pre-existing uncommitted state.** Run `git status --short` (if this cwd is
   a git repo). Any modification/staged file you did NOT just create yourself is very
   likely the operator's own in-progress manual work — do not edit, stage, `git add`, or
   `git commit` those files, ever, on your own judgment. Note it and stop instead of
   guessing it's safe to touch.
3. **If real edits are actually part of your task and this cwd is a git repo, isolate
   yourself into a worktree — never edit directly in a shared checkout others may also
   have open.** If this fleet was launched via `launch-fleet.sh --worktree`, your worktree
   already exists and THIS cwd most likely already IS it (isolation was enforced before
   you were launched — check `git rev-parse --show-toplevel` and `git branch
   --show-current`; you should be on `agent/hackhouse-dev-vm-verify` already, and a
   `WORKTREE.md` should already be sitting right here explaining the setup). If not — e.g.
   you were launched straight into a shared checkout — create it yourself: `git worktree
   add ../$(basename "$(git rev-parse --show-toplevel)")-worktrees/hackhouse-dev-vm-verify
   -b agent/hackhouse-dev-vm-verify` (idempotent: check `git worktree list` first if you
   might be resuming), then do the actual work in that new directory, not here. This
   mirrors the box's own established multi-worktree convention (`main/` +
   `work-trees/<branch>/`) — same repo history, fully independent working tree, so
   nothing you do can collide with what's checked out here.
4. **Never merge, rebase onto, or push into the operator's own branch — ever, on your own
   initiative.** Commit your own work to your own `agent/hackhouse-dev-vm-verify` branch in
   your worktree and stop there; integrating it back is always a human-gated step, batched
   and reviewed by the operator, not something this pane or the fleet's overseer does
   autonomously. Before claiming a branch is ready, run `forge.sh audit .` (this
   project's generator — path it if not on `$PATH`, e.g. `~/coding/ai-agents/tmux-fleet-
   forge/forge.sh audit .`) — a read-only, non-mutating boolean check (clean tree, ahead
   of base, no conflicts, optional test command, no obvious committed secrets). Only once
   it prints `AUDIT: PASS` should `OVERSEER-STATUS-vm-verify.md` say the branch is ready for human
   review; if it prints `AUDIT: FAIL`, the listed reasons ARE your next frontier, not a
   report-and-stop. The overseer's `status: done` marker for this pane should never be
   written on self-report alone — it should cite the `AUDIT: PASS` line.
5. **cwd is not a git repo at all** (no `.git` anywhere above it): there is no worktree
   option and no commit-safety net — rule 1 (the tmux-panes check) is your only real
   protection here, so apply it strictly.

## Fleet map
- Session: `hackhouse-dev` · Overseer window: `overseer`
- This pane: `hackhouse-dev:vm-verify` (role: worker)
- Peers: vm-builder,vm-verify,status

## Reporting
When you stop (finish a turn), append a ≤5-line summary (what changed / next /
blockers) to `./OVERSEER-STATUS-vm-verify.md` in this cwd. **APPEND — if that file already has
content, read it first and add your entry below it; never `Write` the whole file and
replace what's there.** (If `OVERSEER-STATUS-vm-verify.md` isn't the plain `OVERSEER-STATUS.md`, it's
because another pane in this fleet shares this cwd — this name-suffixed file is yours
alone, so a plain overwrite there is still safe, but keep the append habit anyway; it's
the same file across your own ticks.) Do not self-loop or run `/loop` here — the
overseer pane (or an event-driven orchestrator watching this session) decides your next
tick.
