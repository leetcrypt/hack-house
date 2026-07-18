"""hh-agent manifest — the unit of work-transmission between agents (and humans).

A hack-house VM/sandbox can be *handed off*: one operator does work, then another
(human or a freshly-spawned Claude) picks it up. For that to work without a human
briefing, the VM carries a small **ICM-style directory** that documents itself —
purpose, setup, usage, goals, and the live state of the work.

This module is the host-side library + CLI that reads/writes that directory. It
is deliberately dependency-light (stdlib + optional PyYAML) so it runs the same
inside a throwaway container, on the host, or in a nested spawn.

Bundle layout (written under `.hh-agent/` at the VM/work root):

    .hh-agent/
      manifest.yaml   # CANONICAL machine record (schema hh-agent/v1). Source of truth.
      AGENT.md        # narrative entry point — agent reads first, human can too
      last-state.md   # rendered: progress / done / todo / blockers
      summary.md      # rendered: one-paragraph TL;DR
      goals.yaml      # rendered: objective + user intent + stop conditions

ICM alignment (see ~/coding/_core/ICM.md): `manifest.yaml` is the single canonical
record (Layer 3 — "one rule lives in exactly one place"); the `.md`/`goals.yaml`
files are *rendered views* of it (Layer 4 — disposable, regenerated on write).
Editing the markdown by hand is fine for a human handoff note, but `manifest.yaml`
wins on the next `render`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

SCHEMA = "hh-agent/v1"
MANIFEST_DIR = ".hh-agent"
MANIFEST_FILE = "manifest.yaml"


# ── serialization (PyYAML if present, JSON fallback so it never hard-deps) ────
def _dump(obj: dict) -> str:
    try:
        import yaml
        return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False,
                              allow_unicode=True)
    except Exception:  # noqa: BLE001 — yaml missing or chokes → readable JSON
        return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _load(text: str) -> dict:
    text = text.strip()
    if not text:
        return {}
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except Exception:  # noqa: BLE001
        return json.loads(text)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── the record ────────────────────────────────────────────────────────────────
@dataclass
class Manifest:
    """A self-describing hack-house work artifact. `vm`, `goals`, `state` are
    free-form dicts on purpose: a nested Claude designing for its own goal can
    add fields without a schema change, and an older reader just ignores them."""

    name: str
    purpose: str = ""
    created_by: str = "operator"
    id: str = field(default_factory=lambda: f"hh-{uuid.uuid4().hex[:12]}")
    schema: str = SCHEMA
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    # how this VM is realized + how to drive it
    vm: dict = field(default_factory=lambda: {
        "engine": "", "image": "", "state_ref": "", "entrypoint": ""})
    setup: list = field(default_factory=list)   # commands/notes to bring it up
    usage: list = field(default_factory=list)   # how an agent should use it
    # why the work exists — the part a fresh agent most needs
    goals: dict = field(default_factory=lambda: {
        "objective": "", "user_intent": "", "stop_conditions": []})
    # the live state of the work (the handoff-critical bit)
    state: dict = field(default_factory=lambda: {
        "status": "in_progress", "progress": "",
        "done": [], "todo": [], "blockers": []})
    # chain of custody: who touched it, when, with what note
    provenance: list = field(default_factory=list)

    # -- (de)serialize -------------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        # keep schema/id/name first for human scanability of the yaml
        order = ["schema", "id", "name", "purpose", "created_by",
                 "created_at", "updated_at", "vm", "setup", "usage",
                 "goals", "state", "provenance"]
        return {k: d[k] for k in order if k in d}

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    # -- mutation ------------------------------------------------------------
    def touch(self) -> None:
        self.updated_at = _now()

    def add_provenance(self, by: str, note: str = "", action: str = "handoff") -> None:
        self.provenance.append({"by": by, "action": action,
                                "at": _now(), "note": note})
        self.touch()

    def update_state(self, *, status=None, progress=None,
                     done=None, todo=None, blockers=None,
                     append=True) -> None:
        st = self.state
        if status is not None:
            st["status"] = status
        if progress is not None:
            st["progress"] = progress
        for key, val in (("done", done), ("todo", todo), ("blockers", blockers)):
            if val is None:
                continue
            items = [val] if isinstance(val, str) else list(val)
            if append:
                st.setdefault(key, []).extend(items)
            else:
                st[key] = items
        self.touch()


# ── rendered views (derived from the canonical manifest) ──────────────────────
def _bullets(items, empty="_none_") -> str:
    items = items or []
    if not items:
        return empty
    return "\n".join(f"- {x}" for x in items)


def render_summary_md(m: Manifest) -> str:
    g = m.goals or {}
    obj = g.get("objective") or m.purpose or "(no objective set)"
    return (f"# {m.name} — summary\n\n"
            f"**{m.purpose or obj}**\n\n"
            f"Status: `{m.state.get('status', '?')}` · "
            f"{m.state.get('progress') or 'no progress note'}\n\n"
            f"_id `{m.id}` · schema `{m.schema}` · updated {m.updated_at}_\n")


def render_agent_md(m: Manifest) -> str:
    g = m.goals or {}
    vm = m.vm or {}
    vm_line = " · ".join(f"{k}=`{v}`" for k, v in vm.items() if v) or "_unspecified_"
    return (
        f"# AGENT.md — {m.name}\n\n"
        f"> Entry point for whoever (agent or human) picks this VM up next.\n"
        f"> Canonical record is `{MANIFEST_FILE}`; this file is rendered from it.\n\n"
        f"## Purpose\n{m.purpose or '_(none)_'}\n\n"
        f"## Goal\n"
        f"**Objective:** {g.get('objective') or '_(none)_'}\n\n"
        f"**User intent:** {g.get('user_intent') or '_(none)_'}\n\n"
        f"**Stop when:**\n{_bullets(g.get('stop_conditions'))}\n\n"
        f"## VM\n{vm_line}\n\n"
        f"## Setup\n{_bullets(m.setup)}\n\n"
        f"## Usage\n{_bullets(m.usage)}\n\n"
        f"## Where the work stands\nSee `last-state.md`. "
        f"Status: `{m.state.get('status', '?')}`.\n"
    )


def render_last_state_md(m: Manifest) -> str:
    st = m.state or {}
    prov = m.provenance or []
    prov_lines = "\n".join(
        f"- {p.get('at', '?')} · **{p.get('by', '?')}** "
        f"({p.get('action', 'handoff')}): {p.get('note', '')}"
        for p in prov) or "_no handoffs recorded_"
    return (
        f"# last-state — {m.name}\n\n"
        f"_updated {m.updated_at}_\n\n"
        f"**Status:** `{st.get('status', '?')}`\n\n"
        f"**Progress:** {st.get('progress') or '_(none)_'}\n\n"
        f"## Done\n{_bullets(st.get('done'))}\n\n"
        f"## To do\n{_bullets(st.get('todo'))}\n\n"
        f"## Blockers\n{_bullets(st.get('blockers'))}\n\n"
        f"## Provenance\n{prov_lines}\n"
    )


def render_goals_yaml(m: Manifest) -> str:
    return _dump({"objective": (m.goals or {}).get("objective", ""),
                  "user_intent": (m.goals or {}).get("user_intent", ""),
                  "stop_conditions": (m.goals or {}).get("stop_conditions", [])})


# ── bundle I/O ────────────────────────────────────────────────────────────────
def bundle_files(m: Manifest) -> dict:
    """The full set of files (relative names → text) that make up a bundle.
    Returned as data so the caller can write them to disk *or* push them into a
    sandbox via the operator's `write` op without this module touching a VM."""
    return {
        MANIFEST_FILE: _dump(m.to_dict()),
        "AGENT.md": render_agent_md(m),
        "last-state.md": render_last_state_md(m),
        "summary.md": render_summary_md(m),
        "goals.yaml": render_goals_yaml(m),
    }


def write_bundle(m: Manifest, root: str = ".") -> str:
    """Write the `.hh-agent/` bundle under `root`. Returns the bundle dir path."""
    bdir = os.path.join(root, MANIFEST_DIR)
    os.makedirs(bdir, exist_ok=True)
    for name, text in bundle_files(m).items():
        with open(os.path.join(bdir, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    return bdir


def read_bundle(root: str = ".") -> Manifest:
    """Load a bundle's canonical manifest from under `root`. Accepts either the
    work root (containing `.hh-agent/`) or the bundle dir itself."""
    path = _resolve_manifest_path(root)
    if path is None:
        raise FileNotFoundError(f"no {MANIFEST_DIR}/{MANIFEST_FILE} under {root!r}")
    with open(path, encoding="utf-8") as fh:
        return Manifest.from_dict(_load(fh.read()))


def _resolve_manifest_path(root: str) -> str | None:
    for cand in (os.path.join(root, MANIFEST_DIR, MANIFEST_FILE),
                 os.path.join(root, MANIFEST_FILE)):
        if os.path.isfile(cand):
            return cand
    return None


# ── CLI ───────────────────────────────────────────────────────────────────────
def _kv_list(values) -> list:
    return list(values or [])


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hh-manifest",
        description="Create/inspect/update an hh-agent VM manifest bundle.")
    p.add_argument("-C", "--root", default=".",
                   help="work root the .hh-agent/ bundle lives under (default: .)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a new bundle")
    s.add_argument("name")
    s.add_argument("--purpose", default="")
    s.add_argument("--by", dest="created_by", default="operator")
    s.add_argument("--objective", default="")
    s.add_argument("--intent", dest="user_intent", default="")
    s.add_argument("--stop", action="append", default=[], help="a stop condition (repeatable)")
    s.add_argument("--engine", default="")
    s.add_argument("--image", default="")
    s.add_argument("--state-ref", dest="state_ref", default="")
    s.add_argument("--entrypoint", default="")
    s.add_argument("--setup", action="append", default=[])
    s.add_argument("--usage", action="append", default=[])

    sub.add_parser("show", help="print the canonical manifest as JSON")

    sub.add_parser("render", help="re-render the .md/.yaml views from manifest.yaml")

    s = sub.add_parser("update", help="update live work state")
    s.add_argument("--status")
    s.add_argument("--progress")
    s.add_argument("--done", action="append", default=[])
    s.add_argument("--todo", action="append", default=[])
    s.add_argument("--blocker", dest="blockers", action="append", default=[])
    s.add_argument("--replace", action="store_true",
                   help="replace lists instead of appending")

    s = sub.add_parser("handoff", help="record a provenance entry (who/what/note)")
    s.add_argument("--by", required=True)
    s.add_argument("--note", default="")
    s.add_argument("--action", default="handoff")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    root = args.root

    if args.cmd == "init":
        m = Manifest(
            name=args.name, purpose=args.purpose, created_by=args.created_by,
            vm={"engine": args.engine, "image": args.image,
                "state_ref": args.state_ref, "entrypoint": args.entrypoint},
            setup=_kv_list(args.setup), usage=_kv_list(args.usage),
            goals={"objective": args.objective, "user_intent": args.user_intent,
                   "stop_conditions": _kv_list(args.stop)})
        m.add_provenance(by=args.created_by, action="init",
                         note=f"created {m.name}")
        bdir = write_bundle(m, root)
        print(f"initialized {m.id} → {bdir}")
        return 0

    # all remaining verbs operate on an existing bundle
    try:
        m = read_bundle(root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.cmd == "show":
        print(json.dumps(m.to_dict(), indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "render":
        bdir = write_bundle(m, root)
        print(f"rendered views → {bdir}")
        return 0

    if args.cmd == "update":
        m.update_state(
            status=args.status, progress=args.progress,
            done=args.done or None, todo=args.todo or None,
            blockers=args.blockers or None, append=not args.replace)
        write_bundle(m, root)
        print(f"updated {m.id} (status={m.state.get('status')})")
        return 0

    if args.cmd == "handoff":
        m.add_provenance(by=args.by, note=args.note, action=args.action)
        write_bundle(m, root)
        print(f"recorded {args.action} by {args.by}")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
