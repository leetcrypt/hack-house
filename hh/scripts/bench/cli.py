"""bench CLI — the multi-language capability benchmark + model picker.

Subcommands:
  langs                 list known languages and their runtimes
  run                   benchmark model(s) across language(s) -> scorecard JSON
  pick                  rank an existing scorecard for a workflow profile
  workflows             list workflow weighting profiles

Run via the launcher:  .venv/bin/python hh/scripts/bench-lang.py run --help
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import score
from .harness import LangResult, run_language
from .langs import LANGS, resolve

DEFAULT_SCORECARD = Path("/tmp/hh-bench/scorecard.json")


def _progress(model: str, lang: str):
    def cb(done: int, total: int, res: LangResult):
        p1 = res.pass_at(1)
        print(f"\r  {model} · {lang}: {done}/{total} problems  "
              f"pass@1={p1:.2f}", end="", flush=True)
        if done == total:
            print()
    return cb


def cmd_langs(args) -> int:
    from .runtime import get_runtime
    print(f"{'lang':<12}{'dataset/config':<34}{'runtime':<10}run")
    print("-" * 78)
    for lang in LANGS.values():
        rt = get_runtime(args.runtime, lang)
        print(f"{lang.id:<12}{lang.config:<34}{rt.name:<10}{lang.run}")
    return 0


def cmd_workflows(args) -> int:
    for name, prof in score.load_workflows().items():
        weights = " ".join(f"{k}:{v}" for k, v in prof["weights"].items())
        print(f"{name:<12}{prof['label']:<26}{weights}")
    return 0


def cmd_run(args) -> int:
    languages = args.languages or list(LANGS)
    results: list[dict] = []
    # Merge into an existing scorecard so successive runs accumulate.
    if args.scorecard.exists() and not args.fresh:
        results = score.load_scorecard(args.scorecard)

    for model in args.models:
        for lang in languages:
            resolve(lang)  # validate early
            print(f"── {model} · {lang}  (limit={args.limit}, samples={args.samples}) ──")
            res = run_language(
                model, lang, limit=args.limit, samples=args.samples,
                runtime=args.runtime, temperature=args.temperature,
                gen_timeout=args.gen_timeout, exec_timeout=args.exec_timeout,
                host=args.host, progress=_progress(model, lang))
            d = res.to_dict()
            # Replace any prior row for this (model, language, samples).
            results = [r for r in results
                       if not (r["model"] == model and r["language"] == res.language)]
            results.append(d)
            print(f"  → pass@1={d['pass@1']:.3f} on {d['n_problems']} problems "
                  f"({d['elapsed']:.0f}s, {d['runtime']})\n")

    score.save_scorecard(results, args.scorecard)
    print(f"scorecard → {args.scorecard}")
    _print_ranking(results, args.workflow)
    return 0


def cmd_pick(args) -> int:
    results = score.load_scorecard(args.scorecard)
    if not results:
        print(f"no results in {args.scorecard} — run `bench-lang.py run` first")
        return 1
    _print_ranking(results, args.workflow)
    return 0


def _print_ranking(results: list[dict], workflow: str) -> None:
    rows = score.rank(results, workflow)
    profile = score.load_workflows()[workflow]
    langs = [l for l, w in profile["weights"].items() if w > 0]
    print("\n" + "=" * (24 + 8 * len(langs) + 8))
    print(f"workflow: {workflow} ({profile['label']})")
    header = f"{'model':<24}" + "".join(f"{l[:6]:>8}" for l in langs) + f"{'SCORE':>8}"
    print(header)
    print("-" * len(header))
    for r in rows:
        cells = "".join(
            f"{r['per_language'].get(l, float('nan')):>8.2f}"
            if l in r["per_language"] else f"{'—':>8}" for l in langs)
        flag = "" if r["covered"] else "  (partial)"
        print(f"{r['model']:<24}{cells}{r['score']:>8.2f}{flag}")
    print("=" * len(header))
    if rows:
        print(f"→ best for '{workflow}': {rows[0]['model']}  "
              f"(score {rows[0]['score']:.2f})")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="bench-lang",
                                 description="multi-language model capability benchmark + picker")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("langs", help="list known languages")
    p.add_argument("--runtime", default="auto", choices=["auto", "podman", "local"])
    p.set_defaults(func=cmd_langs)

    p = sub.add_parser("workflows", help="list workflow profiles")
    p.set_defaults(func=cmd_workflows)

    p = sub.add_parser("run", help="benchmark model(s) across language(s)")
    p.add_argument("--models", nargs="+", required=True, help="ollama model tags")
    p.add_argument("--languages", nargs="+", default=None,
                   help=f"subset of: {', '.join(LANGS)} (default: all)")
    p.add_argument("--limit", type=int, default=20, help="problems per language")
    p.add_argument("--samples", type=int, default=1, help="completions per problem")
    p.add_argument("--runtime", default="auto", choices=["auto", "podman", "local"])
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--gen-timeout", type=float, default=300.0)
    p.add_argument("--exec-timeout", type=float, default=30.0)
    p.add_argument("--host", default="http://127.0.0.1:11434")
    p.add_argument("--scorecard", type=Path, default=DEFAULT_SCORECARD)
    p.add_argument("--fresh", action="store_true", help="ignore any existing scorecard")
    p.add_argument("--workflow", default="balanced", help="profile for the summary ranking")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("pick", help="rank an existing scorecard for a workflow")
    p.add_argument("--scorecard", type=Path, default=DEFAULT_SCORECARD)
    p.add_argument("--workflow", default="balanced")
    p.set_defaults(func=cmd_pick)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
