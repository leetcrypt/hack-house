"""Scorecard aggregation + workflow-weighted model picker.

The harness emits one LangResult per (model, language). This module:

  • persists/loads them as a flat scorecard JSON (the durable artifact a future
    model-picker UI would read), and
  • collapses a scorecard into a per-model ranking under a chosen workflow
    profile (weights from workflows.json), so "which model for my work?" becomes
    a single sorted list.

Keeping scoring separate from running means the same captured results can be
re-ranked for any workflow without re-executing a single model.
"""

from __future__ import annotations

import json
from pathlib import Path

_WORKFLOWS = Path(__file__).resolve().parent / "workflows.json"


def load_workflows() -> dict:
    data = json.loads(_WORKFLOWS.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def save_scorecard(results: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "results": results}, indent=2))


def load_scorecard(path: Path) -> list[dict]:
    return json.loads(path.read_text()).get("results", [])


def _matrix(results: list[dict], metric: str) -> dict[str, dict[str, float]]:
    """{model: {language: metric}} from a flat results list."""
    m: dict[str, dict[str, float]] = {}
    for r in results:
        val = r.get(metric)
        if val is None:
            continue
        m.setdefault(r["model"], {})[r["language"]] = val
    return m


def rank(results: list[dict], workflow: str = "balanced",
         metric: str = "pass@1") -> list[dict]:
    """Return models ranked by workflow-weighted score (desc).

    Each row: {model, score, per_language, covered}. A model is only scored on
    languages it has results for; `covered` flags whether it has all the weighted
    languages (a partial run still ranks, but the gap is visible)."""
    profiles = load_workflows()
    if workflow not in profiles:
        raise KeyError(f"unknown workflow {workflow!r}; "
                       f"known: {', '.join(profiles)}")
    weights = profiles[workflow]["weights"]
    matrix = _matrix(results, metric)
    rows = []
    for model, per_lang in matrix.items():
        num = den = 0.0
        for lang, w in weights.items():
            if lang in per_lang:
                num += w * per_lang[lang]
                den += w
        score = num / den if den else 0.0
        covered = all(lang in per_lang for lang, w in weights.items() if w > 0)
        rows.append({"model": model, "score": round(score, 4),
                     "per_language": per_lang, "covered": covered})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows
