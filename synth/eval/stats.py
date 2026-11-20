"""Statistics for the scorecard: bootstrap intervals over documents.

Purpose: honest uncertainty on a small gold set (P1 §12 Statistics, §16 risk 6).
Every metric is a function of summed per-document counters, so a bootstrap
replicate resamples documents with replacement, sums their counters and recomputes
the metric. Percentile intervals; ``random.Random(seed)`` with the seed from
``config.eval.bootstrap_seed`` (resamples ``eval.bootstrap_resamples``, default
1,000), so the same inputs always give the same interval.

Contract:
* :func:`bootstrap_many` ``(units, fns, …) -> {key: (low, high)}``;
  :func:`bootstrap_ci` for one metric;
* :func:`paired_bootstrap` — arm A vs arm B on the same documents (``diff`` or
  ``ratio``), with a one-sided share of replicates ≤ 0 (diff) or ≥ 1 (ratio);
* :func:`nondeterminism` / :func:`run_variation` — spread of a metric over repeated runs.
Stores written: none (reads ``eval_scores`` in :func:`run_variation`). Spec: P1 §12.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from typing import Any, Callable, Mapping, Sequence

Stat = Callable[[Counter], "float | None"]

DEFAULT_RESAMPLES = 1000
DEFAULT_SEED = 7


def settings(config=None) -> tuple[int, int]:
    """(resamples, seed) from ``config.eval`` (spec defaults otherwise)."""
    ev = getattr(config, "eval", None) or {}
    return int(ev.get("bootstrap_resamples", DEFAULT_RESAMPLES)), int(ev.get("bootstrap_seed", DEFAULT_SEED))


def total(units: Sequence[Counter]) -> Counter:
    out: Counter = Counter()
    for u in units:
        out.update(u)
    return out


def _percentile(sorted_values: list[float], q: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _interval(values: list[float], alpha: float) -> tuple[float | None, float | None]:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None, None
    return _percentile(values, alpha / 2), _percentile(values, 1 - alpha / 2)


def _replicates(n: int, resamples: int, seed: int) -> list[list[int]]:
    rng = random.Random(seed)
    return [[rng.randrange(n) for _ in range(n)] for _ in range(resamples)]


def bootstrap_many(units: Sequence[Counter], fns: Mapping[Any, Stat], *,
                   resamples: int = DEFAULT_RESAMPLES, seed: int = DEFAULT_SEED,
                   alpha: float = 0.05) -> dict[Any, tuple[float | None, float | None]]:
    """Percentile intervals for many metrics from one set of replicates."""
    units = list(units)
    if not units:
        return {k: (None, None) for k in fns}
    values: dict[Any, list[float]] = {k: [] for k in fns}
    for idx in _replicates(len(units), resamples, seed):
        summed = total(units[i] for i in idx)
        for k, fn in fns.items():
            v = fn(summed)
            if v is not None:
                values[k].append(v)
    return {k: _interval(v, alpha) for k, v in values.items()}


def bootstrap_ci(units: Sequence[Counter], fn: Stat, *, resamples: int = DEFAULT_RESAMPLES,
                 seed: int = DEFAULT_SEED, alpha: float = 0.05) -> tuple[float | None, float | None]:
    return bootstrap_many(units, {"x": fn}, resamples=resamples, seed=seed, alpha=alpha)["x"]


def paired_bootstrap(units_a: Mapping[str, Counter], units_b: Mapping[str, Counter],
                     fn_a: Stat, fn_b: Stat | None = None, *, mode: str = "diff",
                     resamples: int = DEFAULT_RESAMPLES, seed: int = DEFAULT_SEED,
                     alpha: float = 0.05) -> dict[str, Any]:
    """Arm A vs arm B, resampling the documents both arms share.

    ``mode="diff"`` -> A − B; ``mode="ratio"`` -> A ÷ B. ``p`` = share of replicates
    with diff ≤ 0 (ratio ≥ 1), a one-sided bootstrap p-value for "A beats B"."""
    fn_b = fn_b or fn_a
    docs = sorted(set(units_a) & set(units_b))

    def combine(a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return None
        if mode == "ratio":
            return a / b if b else None
        return a - b
    point = combine(fn_a(total(units_a[d] for d in docs)), fn_b(total(units_b[d] for d in docs))) \
        if docs else None
    if not docs:
        return {"value": None, "ci_low": None, "ci_high": None, "p": None, "n": 0}
    values = []
    for idx in _replicates(len(docs), resamples, seed):
        v = combine(fn_a(total(units_a[docs[i]] for i in idx)), fn_b(total(units_b[docs[i]] for i in idx)))
        if v is not None:
            values.append(v)
    lo, hi = _interval(values, alpha)
    if values:
        worse = sum(1 for v in values if (v >= 1 if mode == "ratio" else v <= 0))
        p = worse / len(values)
    else:
        p = None
    return {"value": point, "ci_low": lo, "ci_high": hi, "p": p, "n": len(docs)}


def nondeterminism(values: Sequence[float | None]) -> dict[str, Any]:
    """Spread of one metric over repeated runs of the same arm."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"n": 0, "mean": None, "sd": None, "min": None, "max": None, "range": None, "cv": None}
    mean = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) if len(vals) > 1 else 0.0
    return {"n": len(vals), "mean": mean, "sd": sd, "min": min(vals), "max": max(vals),
            "range": max(vals) - min(vals), "cv": (sd / mean) if mean else None}


def run_variation(store, run_ids: Sequence[str], metric: str, slice_: str = "all",
                  arm: str | None = None) -> dict[str, Any]:
    """:func:`nondeterminism` of an ``eval_scores`` metric across repeated runs."""
    values: dict[str, float | None] = {}
    with store.reader() as r:
        for rid in run_ids:
            rows = [row for row in r.select("eval_scores", run_id=rid, metric=metric, slice=slice_)
                    if arm is None or row["arm"] == arm]
            values[rid] = rows[-1]["value"] if rows else None
    return {**nondeterminism(list(values.values())), "values": values, "metric": metric, "slice": slice_}
