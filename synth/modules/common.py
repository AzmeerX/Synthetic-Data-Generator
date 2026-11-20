"""Shared plumbing for every module: the stage context, document views and the
small writers (spans, coverage events, flags, stage metrics) that keep the
provenance and coverage invariants in one place.

Contract for a module (CLAUDE.md conventions): a module file
``synth/modules/uNN_name.py`` starts with a docstring (purpose, contract, stores
written, token caps, spec section) and exposes a plain function taking a
:class:`StageContext` and a unit id, e.g. ``run_document(ctx, doc_id)``. It
writes only through ``ctx.store.begin(...)`` transactions (change log included)
and calls models only through ``ctx.gateway``.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Iterator, Sequence

from sqlalchemy import func, select

from synth.config import SynthConfig
from synth.contracts import GlinerBackend
from synth.gateway import Gateway
from synth.ids import new_id
from synth.ontology import Ontology
from synth.provenance import Located, Locator, SentenceRef, render_sentences
from synth.store import Store
from synth.store.repo import Tx
from synth.store.tables import doc_texts


@dataclass
class StageContext:
    """Everything a module needs. Built by J0 (or by a test)."""

    config: SynthConfig
    ontology: Ontology
    store: Store
    gateway: Gateway
    run_id: str
    arm: str = "full"
    gliner: GlinerBackend | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @contextmanager
    def tx(self, stage: str, module: str) -> Iterator[Tx]:
        with self.store.begin(run_id=self.run_id, stage=stage, module=module) as tx:
            yield tx

    @contextmanager
    def reader(self) -> Iterator[Tx]:
        with self.store.reader() as r:
            yield r

    def doc(self, doc_id: str) -> "DocView":
        with self.reader() as r:
            return DocView.load(r, doc_id, self.config)


@dataclass
class DocView:
    """A document's canonical text and sentence index, loaded once."""

    doc: dict[str, Any]
    text: str
    text_version: int
    sentences: list[dict[str, Any]]
    config: SynthConfig | None = None

    @classmethod
    def load(cls, r: Tx, doc_id: str, config: SynthConfig | None = None) -> "DocView":
        doc = r.get("documents", doc_id=doc_id)
        if doc is None:
            raise KeyError(f"no document {doc_id}")
        version = r.execute(select(func.max(doc_texts.c.text_version))
                            .where(doc_texts.c.doc_id == doc_id)).scalar()
        text_row = r.get("doc_texts", doc_id=doc_id, text_version=version)
        sentences = r.select("sentences", order_by="idx", doc_id=doc_id, text_version=version)
        return cls(doc=doc, text=text_row["canonical_text"] if text_row else "",
                   text_version=version or 1, sentences=sentences, config=config)

    @property
    def doc_id(self) -> str:
        return self.doc["doc_id"]

    @cached_property
    def by_idx(self) -> dict[int, dict[str, Any]]:
        return {s["idx"]: s for s in self.sentences}

    @cached_property
    def by_id(self) -> dict[str, dict[str, Any]]:
        return {s["sentence_id"]: s for s in self.sentences}

    def sentence_text(self, s: dict[str, Any]) -> str:
        return self.text[s["char_start"]:s["char_end"]]

    def refs(self, sentences: Sequence[dict[str, Any]] | None = None) -> list[SentenceRef]:
        return [SentenceRef(s["sentence_id"], s["idx"], s["char_start"], s["char_end"])
                for s in (self.sentences if sentences is None else sentences)]

    @cached_property
    def locator(self) -> Locator:
        g = self.config.grounding if self.config else None
        return Locator(self.text, self.refs(),
                       fuzzy_accept=g.fuzzy_accept if g else 85.0,
                       fuzzy_pass=g.fuzzy_pass if g else 92.0,
                       min_words=g.min_quote_words if g else 5)

    def range(self, first_idx: int, last_idx: int, include_filtered: bool = False) -> list[dict[str, Any]]:
        return [s for s in self.sentences if first_idx <= s["idx"] <= last_idx
                and (include_filtered or not s["filtered"])]

    def render(self, sentences: Sequence[dict[str, Any]], doc_no: int | None = None) -> str:
        return render_sentences([(s["idx"], self.sentence_text(s)) for s in sentences], doc_no)


# ------------------------------------------------------------------ writers
def write_span(tx: Tx, doc: DocView, located: Located, created_by: str,
               run_id: str | None = None) -> str:
    """Persist a grounded span (never call with NOT_FOUND)."""
    if not located.grounded or located.char_start is None:
        raise ValueError("refusing to store an ungrounded span")
    pages = [doc.by_id[sid]["page"] for sid in located.sentence_ids if sid in doc.by_id]
    span_id = new_id("spn")
    tx.insert("spans", dict(
        span_id=span_id, doc_id=doc.doc_id, text_version=doc.text_version,
        char_start=located.char_start, char_end=located.char_end,
        sentence_ids=located.sentence_ids, page=pages[0] if pages else None,
        quote=doc.text[located.char_start:located.char_end], grounding=located.grounding,
        fuzzy_score=located.fuzzy_score, created_by=created_by, run_id=run_id or tx.run_id))
    return span_id


def emit_coverage(tx: Tx, sentence_ids: Sequence[str], stage: str, outcome: str,
                  ref_id: str | None = None) -> None:
    for sid in sentence_ids:
        tx.insert("coverage_events", dict(sentence_id=sid, stage=stage, outcome=outcome, ref_id=ref_id))


def raise_flag(tx: Tx, kind: str, object_ids: Sequence[str], raised_by: str, *,
               state: str = "POTENTIAL", span_ids: Sequence[str] = (), note: str = "",
               rule_id: str | None = None) -> str:
    flag_id = new_id("flg")
    tx.insert("flags", dict(flag_id=flag_id, kind=kind, state=state, object_ids=list(object_ids),
                            raised_by=raised_by, span_ids=list(span_ids), note=note), rule_id=rule_id)
    return flag_id


def record_stage_metrics(tx: Tx, stage: str, unit_id: str, **values: int) -> None:
    allowed = {"source_tokens", "candidate_tokens", "contracted_tokens", "items_in", "items_out", "wall_ms"}
    unknown = set(values) - allowed
    if unknown:
        raise KeyError(f"unknown stage metric(s): {sorted(unknown)}")
    tx.insert("stage_metrics", dict(stage=stage, unit_id=unit_id, **values))


class Stopwatch:
    def __init__(self) -> None:
        self._start = time.monotonic()

    @property
    def ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)
