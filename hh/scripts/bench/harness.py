"""Capability benchmark orchestration.

For one (model, language): load N problems, get `samples` completions each,
assemble + execute each in the chosen runtime, and fold the per-problem pass
rates into a pass@1 (and pass@k when samples>k) using the standard unbiased
estimator from the HumanEval paper.

The output is a plain dict (see `LangResult`) so score.py can aggregate across
languages and models without knowing anything about how a result was produced.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import completion, datasets
from .langs import Lang, resolve
from .runtime import get_runtime


def _pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k for n samples with c correct (HumanEval, Chen et al. 2021)."""
    if n - c < k:
        return 1.0
    prod = 1.0
    for i in range(n - c + 1, n + 1):
        prod *= 1.0 - k / i
    return 1.0 - prod


@dataclass
class ProblemResult:
    name: str
    correct: int
    samples: int
    first_error: str = ""


@dataclass
class LangResult:
    model: str
    language: str
    samples: int
    problems: list[ProblemResult] = field(default_factory=list)
    elapsed: float = 0.0
    runtime: str = ""

    def pass_at(self, k: int) -> float:
        if not self.problems:
            return 0.0
        return sum(_pass_at_k(p.samples, p.correct, k)
                   for p in self.problems) / len(self.problems)

    def to_dict(self) -> dict:
        return {
            "model": self.model, "language": self.language,
            "samples": self.samples, "runtime": self.runtime,
            "n_problems": len(self.problems), "elapsed": round(self.elapsed, 1),
            "pass@1": round(self.pass_at(1), 4),
            "pass@10": round(self.pass_at(10), 4) if self.samples >= 10 else None,
            "problems": [{"name": p.name, "correct": p.correct,
                          "samples": p.samples, "error": p.first_error}
                         for p in self.problems],
        }


def run_language(model: str, language: str, *, limit: int = 20, samples: int = 1,
                 runtime: str = "auto", temperature: float = 0.2,
                 gen_timeout: float = 300.0, exec_timeout: float = 30.0,
                 host: str = "http://127.0.0.1:11434",
                 progress=None) -> LangResult:
    lang: Lang = resolve(language)
    rt = get_runtime(runtime, lang)
    rows = datasets.load(lang.dataset, lang.config, limit=limit)
    res = LangResult(model=model, language=lang.id, samples=samples,
                     runtime=rt.name)
    t0 = time.time()
    for idx, row in enumerate(rows):
        prompt = row["prompt"]
        stop = _stop_tokens(row)
        correct = 0
        first_error = ""
        for _ in range(samples):
            comp = completion.complete(model, prompt, stop, host=host,
                                       temperature=temperature,
                                       timeout=gen_timeout)
            if not comp.ok:
                first_error = first_error or f"gen: {comp.error}"
                continue
            source = lang.assemble(prompt, comp.text, row)
            ex = rt.run(lang, source, exec_timeout)
            if ex.ok:
                correct += 1
            elif not first_error:
                first_error = ex.note or f"rc={ex.rc}"
        name = row.get("name") or row.get("task_id") or f"p{idx}"
        res.problems.append(ProblemResult(name, correct, samples, first_error))
        if progress:
            progress(idx + 1, len(rows), res)
    res.elapsed = time.time() - t0
    return res


def _stop_tokens(row: dict) -> list[str]:
    raw = row.get("stop_tokens")
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            import ast
            v = ast.literal_eval(raw)
            return v if isinstance(v, list) else []
        except (ValueError, SyntaxError):
            return []
    return []
