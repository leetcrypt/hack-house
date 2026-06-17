#!/usr/bin/env python3
"""bench-ai.py — end-to-end latency/throughput benchmark for the /ai agent.

Stands up the real relay server, summons each model as a real agent that joins
the encrypted room, then sends a fixed prompt as an ordinary user and measures
the round trip the way a teammate actually experiences it:

  TTFT   time to the agent's first streamed token (perceived latency on CPU)
  total  time to the final, persisted reply
  gen    total - TTFT (decode time)
  tok/s  estimated reply tokens / gen  (~4 chars/token)

Everything travels the real path: SRP auth -> Fernet -> WebSocket -> provider.
Nothing is mocked. Run from the repo root with the project venv:

    .venv/bin/python hh/scripts/bench-ai.py \
        --models llama3.2:3b qwen2.5:3b granite3.1-dense:2b

Useful flags: --prompt, --num-thread, --num-ctx, --timeout, --runs, --port.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import websockets

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from cmd_chat.client.client import Client  # noqa: E402


def _est_tokens(text: str) -> int:
    return len(text) // 4 + 1


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_port(host: str, port: int, deadline: float) -> bool:
    while time.time() < deadline:
        if _port_open(host, port):
            return True
        time.sleep(0.2)
    return False


class BenchUser(Client):
    """A normal encrypted client that asks one question and times the reply."""

    def __init__(self, host: str, port: int, password: str):
        super().__init__(host, port, username="bench", password=password, no_tls=True)

    async def wait_for_agent(self, ws, agent_name: str, deadline: float) -> bool:
        """Block until the named agent posts its '(ai) online' announcement."""
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                return False
            data = json.loads(raw)
            if data.get("type") != "message":
                continue
            dec = self.decrypt_message(data.get("data", {}))
            if dec.get("username") == agent_name and "online" in dec.get("text", ""):
                return True
        return False

    async def ask(self, ws, agent_name: str, prompt: str, deadline: float) -> dict:
        """Send `/ai <agent> <prompt>` and time TTFT + total reply.

        Returns {ttft, total, reply, streamed, ok, error}.
        """
        t0 = time.time()
        await ws.send(self.room_fernet.encrypt(f"/ai {agent_name} {prompt}".encode()).decode())

        ttft: float | None = None
        streamed = False
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
            except asyncio.TimeoutError:
                return {"ok": False, "error": "timeout waiting for reply",
                        "ttft": ttft, "total": None, "reply": "", "streamed": streamed}
            except websockets.ConnectionClosed:
                return {"ok": False, "error": "connection closed",
                        "ttft": ttft, "total": None, "reply": "", "streamed": streamed}
            data = json.loads(raw)
            if data.get("type") != "message":
                continue
            dec = self.decrypt_message(data.get("data", {}))
            if dec.get("username") != agent_name:
                continue
            text = dec.get("text", "")
            # Control frames: streamed previews + typing indicator.
            if text.startswith('{"_'):
                try:
                    frame = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if frame.get("_ai") == "stream" and frame.get("text") and not frame.get("done"):
                    if ttft is None:
                        ttft = time.time() - t0
                    streamed = True
                continue
            # First non-control message from the agent = the final reply.
            total = time.time() - t0
            err = text.startswith("[ai error")
            return {"ok": not err, "error": text if err else None,
                    "ttft": ttft if ttft is not None else total,
                    "total": total, "reply": text, "streamed": streamed}
        return {"ok": False, "error": "deadline exceeded",
                "ttft": ttft, "total": None, "reply": "", "streamed": streamed}


async def bench_model(host: str, port: int, password: str, name: str, prompt: str,
                      timeout: float, runs: int) -> list[dict]:
    user = BenchUser(host, port, password)
    user.srp_authenticate()
    url = f"{user.ws_url}/ws/chat?user_id={user.user_id}&ws_token={user.ws_token}"
    results: list[dict] = []
    async with websockets.connect(url) as ws:
        if not await user.wait_for_agent(ws, name, time.time() + timeout):
            return [{"ok": False, "error": "agent never came online",
                     "ttft": None, "total": None, "reply": "", "streamed": False}]
        for _ in range(runs):
            res = await user.ask(ws, name, prompt, time.time() + timeout)
            results.append(res)
    return results


def spawn_agent(py: str, host: str, port: int, password: str, model: str,
                num_thread: int | None, num_ctx: int | None, logf) -> subprocess.Popen:
    cmd = [py, "-m", "cmd_chat.agent", host, str(port),
           "--model", model, "--password", password, "--no-tls", "--no-rag"]
    if num_thread is not None:
        cmd += ["--num-thread", str(num_thread)]
    if num_ctx is not None:
        cmd += ["--num-ctx", str(num_ctx)]
    return subprocess.Popen(cmd, cwd=str(REPO), stdout=logf, stderr=subprocess.STDOUT)


def bench_direct(model: str, prompt: str, num_thread, num_ctx, runs: int,
                 timeout: float) -> list[dict]:
    """Time OllamaProvider.stream() directly — no server, no room, no websocket.

    Isolates raw model TTFT/throughput from the relay path, so a num-thread sweep
    measures the model rather than asyncio event-loop starvation under CPU load.
    """
    from cmd_chat.agent.providers import OllamaProvider, Msg
    kw: dict = {"timeout": int(timeout)}
    if num_thread is not None:
        kw["num_thread"] = num_thread
    if num_ctx is not None:
        kw["num_ctx"] = num_ctx
    prov = OllamaProvider(model=model, **kw)
    system = "You are a helpful assistant. Be concise."
    results: list[dict] = []
    for _ in range(runs):
        t0 = time.time()
        ttft = None
        parts: list[str] = []
        try:
            for piece in prov.stream(system, [Msg("user", prompt)]):
                if ttft is None:
                    ttft = time.time() - t0
                parts.append(piece)
            total = time.time() - t0
            results.append({"ok": True, "ttft": ttft if ttft is not None else total,
                            "total": total, "reply": "".join(parts), "streamed": True,
                            "error": None})
        except Exception as e:  # noqa: BLE001 — surface provider failure as a FAIL row
            results.append({"ok": False, "ttft": ttft, "total": None, "reply": "",
                            "streamed": False, "error": str(e)})
    return results


def run_e2e_model(args, model: str, port: int, logdir: Path) -> list[dict]:
    """Full end-to-end run for one model: fresh server + real agent + bench user."""
    py = sys.executable
    print(f"── {model}  (server :{port}) ──")
    srv_log = open(logdir / f"server-{port}.log", "w")
    srv = subprocess.Popen(
        [py, "cmd_chat.py", "serve", args.host, str(port),
         "--password", args.password, "--no-tls"],
        cwd=str(REPO), stdout=srv_log, stderr=subprocess.STDOUT)
    agent = None
    log = None
    try:
        if not _wait_port(args.host, port, time.time() + 30):
            return [{"ok": False, "error": f"server never bound (server-{port}.log)",
                     "ttft": None, "total": None, "reply": "", "streamed": False}]
        log = open(logdir / f"agent-{model.replace('/', '_').replace(':', '_')}.log", "w")
        agent = spawn_agent(py, args.host, port, args.password, model,
                            args.num_thread, args.num_ctx, log)
        return asyncio.run(bench_model(
            args.host, port, args.password, model,
            args.prompt, args.timeout, args.runs))
    finally:
        if agent is not None:
            agent.terminate()
            try:
                agent.wait(timeout=10)
            except subprocess.TimeoutExpired:
                agent.kill()
        if log is not None:
            log.close()
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
        srv_log.close()


def fmt(v, suffix="s"):
    return f"{v:.2f}{suffix}" if isinstance(v, (int, float)) else "  —"


def _summarize(model: str, results: list[dict]) -> tuple:
    """Average the ok runs into one printed line + a summary-table row."""
    oks = [r for r in results if r["ok"]]
    if not oks:
        err = results[0].get("error") if results else "no result"
        print(f"  ✗ FAIL — {err}\n")
        return (model, None, None, None, None, False, err)
    avg = lambda k: sum(r[k] for r in oks) / len(oks)  # noqa: E731
    ttft, total = avg("ttft"), avg("total")
    gen = max(total - ttft, 1e-6)
    toks = sum(_est_tokens(r["reply"]) for r in oks) / len(oks)
    tps = toks / gen
    streamed = oks[0]["streamed"]
    sample = oks[0]["reply"].replace("\n", " ")[:80]
    print(f"  ✓ ttft={fmt(ttft)}  total={fmt(total)}  ~{tps:.1f} tok/s"
          f"  (streamed={streamed})")
    print(f"    “{sample}…”\n")
    return (model, ttft, total, gen, tps, True, sample)


def main() -> int:
    ap = argparse.ArgumentParser(description="end-to-end /ai agent benchmark")
    ap.add_argument("--models", nargs="+", required=True, help="ollama model tags to benchmark")
    ap.add_argument("--prompt", default="In one sentence, what is a cryptographic hash function?")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4555)
    ap.add_argument("--password", default="bench-pass")
    ap.add_argument("--timeout", type=float, default=180.0, help="per-reply ceiling (s)")
    ap.add_argument("--runs", type=int, default=1, help="prompts per model (averaged)")
    ap.add_argument("--num-thread", type=int, default=None)
    ap.add_argument("--num-ctx", type=int, default=None)
    ap.add_argument("--direct", action="store_true",
                    help="benchmark the provider directly (no room/websocket) — "
                         "isolates raw model speed from event-loop contention")
    args = ap.parse_args()

    logdir = Path("/tmp/hh-bench")
    logdir.mkdir(exist_ok=True)

    rows: list[tuple] = []
    # E2E: a fresh server per model — the SRP rate limiter (10 req/60s/IP) is
    # in-memory per process, so a new process resets the budget and each model
    # gets an isolated room. Direct mode skips all of that.
    for i, model in enumerate(args.models):
        if args.direct:
            print(f"── {model}  (direct provider) ──")
            results = bench_direct(model, args.prompt, args.num_thread,
                                   args.num_ctx, args.runs, args.timeout)
        else:
            results = run_e2e_model(args, model, args.port + i, logdir)
        rows.append(_summarize(model, results))

    # Summary table.
    print("=" * 72)
    print(f"{'model':<22}{'TTFT':>9}{'total':>9}{'gen':>9}{'tok/s':>9}  status")
    print("-" * 72)
    for model, ttft, total, gen, tps, ok, _ in rows:
        status = "ok" if ok else "FAIL"
        tps_s = f"{tps:.1f}" if isinstance(tps, (int, float)) else "—"
        print(f"{model:<22}{fmt(ttft):>9}{fmt(total):>9}{fmt(gen):>9}{tps_s:>9}  {status}")
    print("=" * 72)
    failed = [r for r in rows if not r[5]]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
