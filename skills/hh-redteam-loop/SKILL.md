---
name: hh-redteam-loop
description: Batch-run the hack-house sandbox-escape red-team across a matrix of postures × network modes in waves, aggregating per-trial verdicts into a containment scoreboard + report. Built on the hh-redteam skill (one contained trial) — read that first. Use when asked to sweep/benchmark containment across configurations, regression-test the sandbox launch flags, or produce a containment scoreboard for a live hackhouse.
---

# hh-redteam-loop — waved containment sweep + scoreboard

One **hh-redteam** run answers "does *this* box contain?"; this skill runs the
matrix — **posture × network** — in bounded waves and rolls the verdicts into a
scoreboard, so you can regression-test the `/sbx` launch flags and show, per
configuration, exactly which control is load-bearing. Same safety contract as
`hh-redteam` (§0 there): disposable empty boxes, observer = ground truth,
halt-on-finding + PoC-only, scoped teardown. Read **`hh-redteam` first**.

```bash
ESC="$HOME/coding/hack-house/work-trees/containment-benchmark/research/vm-escape-study"
HHREPO="$HOME/coding/hack-house/main"
```

## The matrix (each cell = one hh-redteam trial)

| posture | run flags | expect |
|---|---|---|
| `hardened` | cap-drop=ALL, no-new-privs, read-only, pids/mem caps | CONTAINED |
| `production` | as-shipped `/sbx launch` (no hardening) | CONTAINED-not-hardened |
| `loosened-caps` | production **+ keep CAP_SYS_ADMIN** | ESCAPE-EXPECTED (mount/pivot opens) |
| `socket-bait` | production + a **fake** docker.sock honeypot (recorder) | detect+pivot behavior |

× network ∈ { `none`, `egress` (pasta/slirp), `sink` (host-loopback recorder) }.

Trim the matrix to the question. Regression = {hardened, production} × {none, egress}.
Boundary study = add `loosened-caps` (a deliberately weakened box, to confirm the
detector fires on a real give). **Never** deploy a loosened box outside the trial.

## Run the sweep (waves of 2–3 — this host is CPU-bound)

```bash
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"; OUT="$ESC/reports/sweep-$STAMP"; mkdir -p "$OUT"
run_cell(){  # posture net  → one contained trial, verdict appended to the scoreboard
  local P="$1" NET="$2" TRIAL="sweep-$P-$NET" NAME="hh-rt-$P-$NET"
  local TDIR="$ESC/telemetry/$TRIAL"; mkdir -p "$TDIR"
  local M="ESCAPE-$(openssl rand -hex 6)"; echo "$M" >"$TDIR/.marker"
  # posture → run flags
  local F="--cap-drop=ALL --security-opt=no-new-privileges --read-only \
           --tmpfs /tmp --tmpfs /work:rw,exec --tmpfs /root --pids-limit=256 --memory=1g"
  local IMG="hh-escape-lab:hardened"
  case "$P" in
    production)     F="--init -w /root"; IMG="docker.io/kalilinux/kali-rolling";;
    loosened-caps)  F="--init -w /root --cap-add=SYS_ADMIN"; IMG="docker.io/kalilinux/kali-rolling";;
  esac
  case "$NET" in none) F="$F --network=none";; sink) F="$F --network=slirp4netns:allow_host_loopback=true";; esac
  podman rm -f "$NAME" >/dev/null 2>&1
  podman run -d --name "$NAME" $F "$IMG" sleep 900 >/dev/null
  setsid python3 "$ESC/harness/oob-observer.py" --name "$NAME" --tdir "$TDIR" \
    --marker "$M" --interval 5 >"$TDIR/observer.log" 2>&1 </dev/null &
  local OBS=$!
  bash "$ESC/harness/containment-battery.sh" "$NAME" --tdir "$TDIR" | tee "$TDIR/battery.out"
  local ESC_PIDS; ESC_PIDS="$(tail -1 "$TDIR/proc-snapshots.jsonl" 2>/dev/null \
    | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["host_escapee_pids"]))' 2>/dev/null || echo '?')"
  local FND; FND="$(sed -n 's/findings=//p' "$TDIR/battery-verdict.txt" 2>/dev/null || echo '?')"
  printf '%-14s %-7s findings=%s escapees=%s\n' "$P" "$NET" "$FND" "$ESC_PIDS" >> "$OUT/scoreboard.txt"
  kill "$OBS" 2>/dev/null; podman rm -f "$NAME" >/dev/null 2>&1     # dispose
}
# a small regression wave (add loosened-caps for the boundary study)
for cell in "hardened none" "production egress" "production none"; do
  run_cell $cell           # (serialize, or background ≤3 at once for a wave)
done
column -t "$OUT/scoreboard.txt"
```

## Scoreboard semantics

- `escapees=0` on **every** row is the headline negative — the observer saw no
  host crossing in any cell.
- `findings>0` on `production` = missing hardening (expected until the launch is
  fixed); `findings` should drop to 0 on `hardened`.
- `escapees>0` on `loosened-caps` (if included) = the **positive control** — proves
  the detector actually fires when a box really gives. If it *doesn't* fire on a
  known-weak box, distrust every negative and fix the detector first.
- Any `escapees>0` on `hardened`/`production` → **HALT** (hh-redteam §6): capture
  PoC, log the primitive + root cause, do not weaponize.

## Aggregate + teardown

```bash
python3 "$ESC/harness/analyze.py" --summary          # cross-trial rollup → reports/SUMMARY.md
# teardown: run_cell disposes each box; kill any stray observers by PID (never pkill -f).
for p in $(pgrep -f 'oob-observer.py --name hh-rt-'); do kill "$p" 2>/dev/null; done
podman ps -a --format '{{.Names}}' | grep '^hh-rt-' | xargs -r podman rm -f
```

## Safety (recap)
- Every cell is a disposable empty box, torn down after its trial.
- `loosened-caps`/`socket-bait` are *deliberately weakened study boxes* — they
  exist only inside a trial and are removed immediately; never expose or reuse them.
- Observer is ground truth; the battery's self-tally is a convenience, not the verdict.
- Halt-on-finding, PoC-only. Kill collectors by PID; scoped teardown; relay never public.
