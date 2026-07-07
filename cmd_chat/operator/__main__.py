"""CLI for the operator bridge — `python -m cmd_chat.operator <verb>`.

Verbs
-----
    serve  HOST PORT USER   run the daemon (normally spawned by `up`, not by hand)
    up     HOST PORT USER   start the daemon detached and wait for it to connect
    read   [--wait]         print new inbox events as JSONL (cursor-tracked)
    say    TEXT…            send a chat line to the room
    roster                  list who's in the room
    status                  daemon + connection state
    down                    stop the daemon, leave the room

Phase 1: join + read + say. The reusable skill wrapper comes in a later phase;
for now a Claude Code session can drive everything straight from Bash.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .cli_client import BridgeUnreachable, request
from .session import Session, resolve


def _add_conn_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("host")
    p.add_argument("port", type=int)
    p.add_argument("user", help="room display name to join as (also the session id)")
    p.add_argument("--password", default=None)
    p.add_argument("--no-tls", action="store_true", help="plain ws/http")
    p.add_argument("--insecure", action="store_true", help="skip TLS verify")
    p.add_argument("--session", default=None, help="session id (default: USER)")
    p.add_argument("--trigger", default=None,
                   help="extra phrase that marks a message 'addressed' to us "
                        "(the operator name / @name always count)")
    p.add_argument("--depth", type=int, default=1,
                   help="recursion depth budget (levels of nested operators)")
    p.add_argument("--fanout", type=int, default=2,
                   help="recursion fan-out budget (children per level)")
    p.add_argument("--cost", type=float, default=5.0,
                   help="soft cost ceiling (USD) carried to children")


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cmd_chat.operator")
    sub = ap.add_subparsers(dest="verb", required=True)

    _add_conn_args(sub.add_parser("serve", help="run the daemon (foreground)"))
    _add_conn_args(sub.add_parser("up", help="start the daemon detached"))

    r = sub.add_parser("read", help="print new inbox events")
    r.add_argument("--session", default=None)
    r.add_argument("--since", type=int, default=None,
                   help="seq to read after (default: saved cursor)")
    r.add_argument("--wait", action="store_true", help="long-poll until an event arrives")
    r.add_argument("--timeout", type=float, default=30.0)
    r.add_argument("--all", action="store_true", help="ignore cursor, dump full ring")

    s = sub.add_parser("say", help="send a chat line")
    s.add_argument("text", nargs="+")
    s.add_argument("--session", default=None)

    # ── sandbox drive ───────────────────────────────────────────────────
    sb = sub.add_parser("sbx", help="manage the operator's own container")
    sb.add_argument("action", choices=["launch", "status", "down"], nargs="?",
                    default="status")
    sb.add_argument("--engine", default=None, help="podman|docker (default: auto)")
    sb.add_argument("--image", default=None, help="container image (default per engine)")
    sb.add_argument("--name", default=None, help="container name (default: hh-op-<user>)")
    sb.add_argument("--session", default=None)

    ex = sub.add_parser("exec", help="run a shell command in the sandbox")
    ex.add_argument("cmd", nargs="+")
    ex.add_argument("--session", default=None)

    wr = sub.add_parser("write", help="write a file in the sandbox (content from stdin or --text)")
    wr.add_argument("path")
    wr.add_argument("--text", default=None, help="inline content (else read stdin)")
    wr.add_argument("--session", default=None)

    gt = sub.add_parser("get", help="read a file out of the sandbox")
    gt.add_argument("path")
    gt.add_argument("--out", default=None, help="write to this local path (else stdout)")
    gt.add_argument("--session", default=None)

    sp = sub.add_parser("spawn", help="spawn a nested operator (budgeted)")
    sp.add_argument("objective", help="what the nested operator should achieve")
    sp.add_argument("--room-host", required=True)
    sp.add_argument("--room-port", type=int, required=True)
    sp.add_argument("--room-name", default="operator")
    sp.add_argument("--runner", default="claude",
                    choices=["claude", "codex", "gemini", "cmd"],
                    help="which agent CLI to spawn as the child operator "
                         "(default: claude; 'cmd' reads $HH_OPERATOR_CMD)")
    sp.add_argument("--stop", action="append", default=None,
                    help="a stop condition (repeatable)")
    sp.add_argument("--target", choices=["host"], default="host")
    sp.add_argument("--config-dir", default=None,
                    help="isolated CLAUDE_CONFIG_DIR for the child")
    sp.add_argument("--allow-creds", action="store_true",
                    help="carry our credentials to the child (default: off)")
    sp.add_argument("--skip-permissions", action="store_true",
                    help="run the child unattended (--dangerously-skip-permissions)")
    sp.add_argument("--go", action="store_true",
                    help="actually launch (default is a dry-run plan)")
    sp.add_argument("--session", default=None)

    # ── harness-mode operator (drive the room with any function-calling model) ──
    op = sub.add_parser("operate",
                        help="run a function-calling model as the operator brain "
                             "(native loop over the bridge verbs)")
    op.add_argument("--objective", required=True,
                    help="what this operator should achieve in the room")
    op.add_argument("--profile", default=None,
                    help="named models.toml profile (e.g. local, groq-llama)")
    op.add_argument("--provider", default=None,
                    help="provider spec when not using --profile "
                         "(ollama|anthropic|openai|module:Class)")
    op.add_argument("--model", default=None, help="model name/id")
    op.add_argument("--base-url", default=None, help="OpenAI-compatible base URL")
    op.add_argument("--host", default=None, help="Ollama host URL")
    op.add_argument("--stop", action="append", default=None,
                    help="a stop condition (repeatable)")
    op.add_argument("--max-turns", type=int, default=None,
                    help="ceiling on model turns (default 12)")
    op.add_argument("--token-ceiling", type=int, default=None,
                    help="cumulative prompt+completion token hard stop (default 20000)")
    op.add_argument("--leave-on-done", action="store_true",
                    help="`down` the daemon when the objective is met")
    op.add_argument("--depth", type=int, default=1)
    op.add_argument("--fanout", type=int, default=2)
    op.add_argument("--cost", type=float, default=5.0)
    op.add_argument("--session", default=None)

    wb = sub.add_parser("web", help="serve a mobile chat UI in front of the daemon")
    wb.add_argument("--bind", default="127.0.0.1",
                    help="listen address (default: 127.0.0.1 — loopback only; "
                         "the control socket grants room-send, so keep it local)")
    wb.add_argument("--port", type=int, default=8790, help="listen port (default: 8790)")
    wb.add_argument("--session", default=None)

    sc = sub.add_parser("screen", help="print the relayed sandbox terminal buffer")
    sc.add_argument("--tail", type=int, default=None, help="last N bytes only")
    sc.add_argument("--ansi", action="store_true", help="keep ANSI codes (raw)")
    sc.add_argument("--session", default=None)

    wt = sub.add_parser("watch", help="block until a stop condition fires")
    wt.add_argument("--for", dest="pattern", default=None,
                    help="regex to match (stops on first hit)")
    wt.add_argument("--in", dest="source", choices=["events", "screen"],
                    default="events", help="where to match (default: events)")
    wt.add_argument("--idle", type=float, default=None,
                    help="stop after this many seconds of quiet")
    wt.add_argument("--timeout", type=float, default=30.0)
    wt.add_argument("--session", default=None)

    ky = sub.add_parser("keys", help="inject keystrokes into the room's shared PTY")
    ky.add_argument("keys", nargs="*",
                    help="tokens: literal text, named keys (enter, ctrl-c, esc, "
                         "up…), text:<verbatim>, hex:<bytes>")
    ky.add_argument("--help-keys", action="store_true", help="print the key vocabulary")
    ky.add_argument("--session", default=None)

    # ── agent-manifest (make a sandbox carry its own handoff record) ─────
    mf = sub.add_parser("manifest", help="push/pull/update the sandbox's .hh-agent bundle")
    mf.add_argument("action", choices=["push", "pull", "update"])
    mf.add_argument("--root", default="/root", help="dir the .hh-agent/ lives under in the sandbox")
    mf.add_argument("--name", default=None)
    mf.add_argument("--purpose", default=None)
    mf.add_argument("--objective", default=None)
    mf.add_argument("--intent", dest="user_intent", default=None)
    mf.add_argument("--stop", action="append", default=None, help="stop condition (repeatable)")
    mf.add_argument("--engine", default=None)
    mf.add_argument("--image", default=None)
    mf.add_argument("--state-ref", dest="state_ref", default=None)
    mf.add_argument("--entrypoint", default=None)
    mf.add_argument("--setup", action="append", default=None)
    mf.add_argument("--usage", action="append", default=None)
    mf.add_argument("--status", default=None)
    mf.add_argument("--progress", default=None)
    mf.add_argument("--done", action="append", default=None)
    mf.add_argument("--todo", action="append", default=None)
    mf.add_argument("--blocker", dest="blockers", action="append", default=None)
    mf.add_argument("--note", default=None, help="provenance note for update")
    mf.add_argument("--replace", action="store_true", help="replace state lists instead of appending")
    mf.add_argument("--session", default=None)

    # ── VM registry (discovery: what saved VMs exist + what each is for) ──
    rg = sub.add_parser("registry",
                        help="browse the host-global saved-VM registry (~/.hh/registry.json)")
    rg.add_argument("action", choices=["list", "show"], nargs="?", default="list")
    rg.add_argument("--label", default=None, help="for show: which VM")
    rg.add_argument("--repo", default=None, help="for list: filter to this origin repo")
    rg.add_argument("--json", action="store_true", help="machine-readable output")

    for v in ("roster", "status", "down"):
        sub.add_parser(v).add_argument("--session", default=None)
    return ap


# ── daemon ───────────────────────────────────────────────────────────────
def _run_serve(args) -> int:
    from .bridge import OperatorBridge  # heavy imports only on the daemon path
    from .bootstrap import Budget
    budget = Budget(depth=args.depth, fanout=args.fanout, cost_usd=args.cost)
    bridge = OperatorBridge(
        args.host, args.port, name=args.user, password=args.password,
        insecure=args.insecure, no_tls=args.no_tls,
        session=args.session, trigger=args.trigger, budget=budget)
    try:
        bridge.run()
    except KeyboardInterrupt:
        pass
    return 0


def _run_up(args) -> int:
    import subprocess

    sess = Session(args.session or args.user)
    sess.ensure_dir()

    # Already alive?
    if sess.sock_path.exists():
        try:
            st = request(sess.sock_path, {"op": "status"}, read_timeout=5)
            if st.get("connected"):
                print(f"session '{sess.name}' already up (connected)")
                return 0
        except BridgeUnreachable:
            sess.cleanup()  # stale socket → fall through and respawn

    sess.write_meta(host=args.host, port=args.port, user=args.user,
                    trigger=args.trigger or "", no_tls=args.no_tls,
                    started=time.time())

    cmd = [sys.executable, "-m", "cmd_chat.operator", "serve",
           args.host, str(args.port), args.user]
    if args.password is not None:
        cmd += ["--password", args.password]
    if args.no_tls:
        cmd.append("--no-tls")
    if args.insecure:
        cmd.append("--insecure")
    if args.session:
        cmd += ["--session", args.session]
    if args.trigger:
        cmd += ["--trigger", args.trigger]
    cmd += ["--depth", str(args.depth), "--fanout", str(args.fanout),
            "--cost", str(args.cost)]

    log = open(sess.log_path, "a")  # noqa: SIM115 — handed to the child
    proc = subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                            start_new_session=True, close_fds=True)

    # Wait for the daemon to connect (or report a fatal-looking error).
    deadline = time.time() + 15.0
    last_err = None
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"daemon exited early (code {proc.returncode}); see {sess.log_path}",
                  file=sys.stderr)
            return 1
        try:
            st = request(sess.sock_path, {"op": "status"}, read_timeout=3)
        except BridgeUnreachable:
            time.sleep(0.3)
            continue
        if st.get("connected"):
            print(f"session '{sess.name}' up — connected to {args.host}:{args.port} "
                  f"as '{args.user}' (pid {proc.pid})")
            return 0
        # Connected socket but not in-room yet: peek for an error event.
        try:
            rd = request(sess.sock_path, {"op": "read", "since": 0}, read_timeout=3)
            errs = [e for e in rd.get("events", []) if e.get("event") == "error"]
            if errs:
                last_err = errs[-1].get("reason")
        except BridgeUnreachable:
            pass
        time.sleep(0.4)

    msg = f"session '{sess.name}' started (pid {proc.pid}) but not connected yet"
    if last_err:
        msg += f" — last error: {last_err}"
    print(msg + f"; tail {sess.log_path}", file=sys.stderr)
    return 1


# ── client verbs ─────────────────────────────────────────────────────────
def _client_request(args, obj: dict, read_timeout: float = 35.0) -> dict:
    sess = resolve(getattr(args, "session", None))
    if not sess.sock_path.exists():
        print(f"no live bridge for session '{sess.name}' "
              f"(run `up` first)", file=sys.stderr)
        sys.exit(2)
    try:
        return request(sess.sock_path, obj, read_timeout=read_timeout)
    except BridgeUnreachable as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)


def _run_read(args) -> int:
    sess = resolve(getattr(args, "session", None))
    if args.all:
        since = 0
    elif args.since is not None:
        since = args.since
    else:
        since = sess.read_cursor()
    timeout = args.timeout
    resp = _client_request(args, {"op": "read", "since": since,
                                  "wait": args.wait, "timeout": timeout},
                           read_timeout=timeout + 5.0)
    events = resp.get("events", [])
    for ev in events:
        print(json.dumps(ev))
    if events and not args.all and args.since is None:
        sess.write_cursor(events[-1]["seq"])
    return 0


def _run_say(args) -> int:
    text = " ".join(args.text)
    resp = _client_request(args, {"op": "say", "text": text})
    if not resp.get("ok"):
        print(resp.get("error", "say failed"), file=sys.stderr)
        return 1
    return 0


def _run_sbx(args) -> int:
    resp = _client_request(args, {"op": "sbx", "action": args.action,
                                  "engine": args.engine, "image": args.image,
                                  "name": args.name})
    print(json.dumps(resp))
    return 0 if resp.get("ok") else 1


def _run_exec(args) -> int:
    cmd = " ".join(args.cmd)
    resp = _client_request(args, {"op": "exec", "cmd": cmd})
    out = resp.get("output", "")
    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    if not resp.get("ok"):
        if "error" in resp:
            print(resp["error"], file=sys.stderr)
        return resp.get("rc", 1) or 1
    return 0


def _run_write(args) -> int:
    text = args.text if args.text is not None else sys.stdin.read()
    resp = _client_request(args, {"op": "write", "path": args.path, "content": text})
    if not resp.get("ok"):
        print(resp.get("error", "write failed"), file=sys.stderr)
        return 1
    print(f"wrote {resp.get('path')} ({resp.get('bytes')} bytes)", file=sys.stderr)
    return 0


def _run_get(args) -> int:
    import base64
    resp = _client_request(args, {"op": "get", "path": args.path})
    if not resp.get("ok"):
        print(resp.get("error", "get failed"), file=sys.stderr)
        return 1
    raw = base64.b64decode(resp.get("b64", ""))
    if args.out:
        Path(args.out).write_bytes(raw)
        print(f"wrote {args.out} ({len(raw)} bytes)", file=sys.stderr)
    else:
        sys.stdout.buffer.write(raw)
    return 0


def _run_screen(args) -> int:
    resp = _client_request(args, {"op": "screen", "tail": args.tail,
                                  "ansi": args.ansi})
    if not resp.get("ok"):
        print(resp.get("error", "screen failed"), file=sys.stderr)
        return 1
    sys.stdout.write(resp.get("text", ""))
    return 0


def _run_watch(args) -> int:
    resp = _client_request(args, {"op": "watch", "for": args.pattern,
                                  "in": args.source, "idle": args.idle,
                                  "timeout": args.timeout},
                           read_timeout=args.timeout + 5.0)
    print(json.dumps(resp))
    return 0 if resp.get("ok") else 1


def _run_spawn(args) -> int:
    resp = _client_request(args, {
        "op": "spawn", "objective": args.objective,
        "room": {"host": args.room_host, "port": args.room_port,
                 "name": args.room_name},
        "runner": args.runner,
        "stop": args.stop, "target": args.target,
        "config_dir": args.config_dir, "allow_creds": args.allow_creds,
        "skip_permissions": args.skip_permissions, "dry_run": not args.go})
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


def _build_operate_provider(args):
    """Resolve a Provider from --profile OR --provider/--model (+ endpoint flags).
    Returns the provider or exits with a clear message."""
    from cmd_chat.ai import load_profiles, make_provider, provider_from_profile, preflight
    if args.profile:
        profs = load_profiles()
        prof = profs.get(args.profile)
        if prof is None:
            avail = ", ".join(sorted(profs)) or "(none found)"
            print(f"no profile '{args.profile}' — available: {avail}", file=sys.stderr)
            sys.exit(2)
        provider = provider_from_profile(prof, name=args.profile, model=args.model,
                                         base_url=args.base_url)
    elif args.provider:
        opts = {}
        if args.base_url:
            opts["base_url"] = args.base_url
        if args.host:
            opts["host"] = args.host
        provider = make_provider(args.provider, model=args.model, **opts)
    else:
        print("operate needs --profile NAME or --provider SPEC --model M",
              file=sys.stderr)
        sys.exit(2)
    ok, msg = preflight(provider)
    if not ok:
        print(f"preflight failed: {msg}", file=sys.stderr)
        sys.exit(2)
    return provider


def _run_operate(args) -> int:
    from .bootstrap import Budget
    from .harness import OperatorHarness

    sess = resolve(getattr(args, "session", None))
    if not sess.sock_path.exists():
        print(f"no live bridge for session '{sess.name}' (run `up` first)",
              file=sys.stderr)
        return 2
    # Confirm we're connected and learn our own room name (used to filter echoes).
    try:
        st = request(sess.sock_path, {"op": "status"}, read_timeout=5)
    except BridgeUnreachable as e:
        print(str(e), file=sys.stderr)
        return 2
    if not st.get("connected"):
        print("bridge is up but not connected to the room yet", file=sys.stderr)
        return 2

    provider = _build_operate_provider(args)
    budget = Budget(depth=args.depth, fanout=args.fanout, cost_usd=args.cost)
    kw = {}
    if args.max_turns is not None:
        kw["max_turns"] = args.max_turns
    if args.token_ceiling is not None:
        kw["token_ceiling"] = args.token_ceiling
    harness = OperatorHarness(
        provider=provider, sock_path=str(sess.sock_path),
        objective=args.objective, stop=args.stop, budget=budget,
        me=st.get("name", "operator"), **kw)
    result = harness.run()
    print(json.dumps({"final": result.final, "reason": result.reason,
                      "turns": result.turns, "tokens": result.tokens,
                      "tool_calls": result.tool_calls}, indent=2))
    if args.leave_on_done and result.reason == "done":
        try:
            request(sess.sock_path, {"op": "down"}, read_timeout=5)
        except BridgeUnreachable:
            pass
    return 0 if result.reason in ("done", "token-ceiling", "turn-cap") else 1


def _run_keys(args) -> int:
    from . import sandbox as sbx
    if args.help_keys:
        print(sbx.KEYS_HELP)
        return 0
    if not args.keys:
        print("no keys given (try --help-keys)", file=sys.stderr)
        return 2
    resp = _client_request(args, {"op": "keys", "keys": args.keys})
    if not resp.get("ok"):
        print(resp.get("error", "keys failed"), file=sys.stderr)
        return 1
    return 0


def _run_manifest(args) -> int:
    req = {"op": "manifest", "action": args.action, "root": args.root}
    # forward only the fields the user actually set, so the daemon's defaults hold
    for k in ("name", "purpose", "objective", "user_intent", "stop", "engine",
              "image", "state_ref", "entrypoint", "setup", "usage", "status",
              "progress", "done", "todo", "blockers", "note"):
        v = getattr(args, k, None)
        if v is not None:
            req[k] = v
    if args.replace:
        req["replace"] = True
    resp = _client_request(args, req)
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


def _run_registry(args) -> int:
    """Read the host-global VM registry written by the TUI's `/sbx save`. Standalone
    (no daemon/room needed) so an agent can survey available work before joining."""
    path = Path.home() / ".hh" / "registry.json"
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        print("no VM registry yet (~/.hh/registry.json) — save a VM with `/sbx save`",
              file=sys.stderr)
        return 0 if args.action == "list" else 1
    except (json.JSONDecodeError, OSError) as e:
        print(f"registry unreadable: {e}", file=sys.stderr)
        return 1

    entries = list((data.get("entries") or {}).values())
    entries.sort(key=lambda e: e.get("created_unix", 0), reverse=True)

    if args.action == "show":
        if not args.label:
            print("usage: registry show --label <name>", file=sys.stderr)
            return 2
        match = next((e for e in entries if e.get("label") == args.label), None)
        if not match:
            print(f"no saved VM '{args.label}'", file=sys.stderr)
            return 1
        print(json.dumps(match, indent=2))
        return 0

    if args.repo:
        entries = [e for e in entries if e.get("repo") == args.repo]
    if args.json:
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print("no saved VMs yet")
        return 0
    for e in entries:
        todo = e.get("todo") or []
        nxt = f" · next: {todo[0]}" if todo else ""
        # Phase B: flag tradeable VMs so an agent knows what it can `/sbx pull`.
        share = ""
        if e.get("shareable"):
            tags = e.get("tags") or []
            share = " · shareable" + (f" {{{', '.join(tags)}}}" if tags else "")
        print(f"• {e.get('label')} · [{e.get('status') or '?'}] · "
              f"{e.get('purpose') or '—'}{nxt}{share}")
        print(f"    backend={e.get('backend')} artifact={e.get('artifact_ref')} "
              f"by={e.get('created_by')}")
    return 0


def _run_web(args) -> int:
    from .web import serve
    return serve(args.session, args.bind, args.port)


def _run_simple(args, op: str) -> int:
    resp = _client_request(args, {"op": op})
    print(json.dumps(resp))
    return 0 if resp.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    verb = args.verb
    if verb == "serve":
        return _run_serve(args)
    if verb == "up":
        return _run_up(args)
    if verb == "read":
        return _run_read(args)
    if verb == "say":
        return _run_say(args)
    if verb == "sbx":
        return _run_sbx(args)
    if verb == "exec":
        return _run_exec(args)
    if verb == "write":
        return _run_write(args)
    if verb == "get":
        return _run_get(args)
    if verb == "keys":
        return _run_keys(args)
    if verb == "spawn":
        return _run_spawn(args)
    if verb == "operate":
        return _run_operate(args)
    if verb == "manifest":
        return _run_manifest(args)
    if verb == "registry":
        return _run_registry(args)
    if verb == "screen":
        return _run_screen(args)
    if verb == "watch":
        return _run_watch(args)
    if verb == "web":
        return _run_web(args)
    if verb in ("roster", "status", "down"):
        return _run_simple(args, verb)
    return 2


if __name__ == "__main__":
    sys.exit(main())
