"""Dependency-free problem loader.

Pulls rows from the Hugging Face datasets-server REST API (plain `requests`, no
`datasets`/`pyarrow`) and caches them on disk so repeated benchmark runs are
offline and fast. One JSON file per (dataset, config), under ~/.cache.
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

_API = "https://datasets-server.huggingface.co/rows"
_CACHE = Path.home() / ".cache" / "hh-bench" / "datasets"
_PAGE = 100  # datasets-server caps `length` at 100 rows per call


def _cache_path(dataset: str, config: str, split: str) -> Path:
    safe = f"{dataset}__{config}__{split}".replace("/", "_")
    return _CACHE / f"{safe}.json"


def load(dataset: str, config: str, split: str = "test",
         limit: int | None = None, refresh: bool = False) -> list[dict]:
    """Return a list of row dicts for one dataset config.

    Cached after first fetch. `limit` slices the returned list (the full set is
    still cached). `refresh` forces a re-download.
    """
    cp = _cache_path(dataset, config, split)
    if cp.exists() and not refresh:
        rows = json.loads(cp.read_text())
    else:
        rows = _download(dataset, config, split)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(rows))
    return rows[:limit] if limit else rows


def _download(dataset: str, config: str, split: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        r = requests.get(_API, params={
            "dataset": dataset, "config": config, "split": split,
            "offset": offset, "length": _PAGE}, timeout=60)
        r.raise_for_status()
        payload = r.json()
        batch = payload.get("rows", [])
        if not batch:
            break
        rows.extend(item["row"] for item in batch)
        total = payload.get("num_rows_total")
        offset += len(batch)
        if total is not None and offset >= total:
            break
        if len(batch) < _PAGE:
            break
    if not rows:
        raise RuntimeError(
            f"no rows for {dataset}/{config}/{split} — check the config name")
    return rows
