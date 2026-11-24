"""Phase B — deterministic rich world and canonical content planning.

Nothing here renders, parses, annotates or writes a corpus.  It produces
:class:`synthetic_data.model.Corpus` values (semantic world + canonical
documents) that later phases will render.

Entry points
------------
``build_corpus(CorpusConfig(...))``
    Plan one corpus: semantic world, prose documents, workbooks and the
    contradiction schedule, all deterministic in ``(seed, configuration)``.
``plan_contradictions(config)``
    The contradiction schedule alone, so distribution can be checked at sizes
    where planning every document would be wasteful.
``compute_metrics(plan)``
    Depth, diversity and contradiction metrics from plans only.
"""
from __future__ import annotations

from .config import DOC_TYPES, PROSE_TYPES, CorpusConfig
from .metrics import CorpusMetrics, compute_metrics, contradiction_metrics, diversity_metrics
from .plan import CorpusPlan, build_corpus, plan_contradictions
from .world import Matter, WorldDraft, build_world, contradiction_schedule

__all__ = [
    "CorpusConfig",
    "CorpusPlan",
    "CorpusMetrics",
    "DOC_TYPES",
    "PROSE_TYPES",
    "Matter",
    "WorldDraft",
    "build_corpus",
    "build_world",
    "compute_metrics",
    "contradiction_metrics",
    "contradiction_schedule",
    "diversity_metrics",
    "plan_contradictions",
]
