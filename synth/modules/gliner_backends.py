"""GLiNER backends for U2: the real GLiNER2 model, a deterministic fake, and a regex stand-in.

Purpose: implement :class:`synth.contracts.GlinerBackend` three ways.

* :class:`Gliner2Backend` — GLiNER2 (``fastino/gliner2.5-base-v1``). ``gliner2`` is
  imported lazily (the ``gliner`` extra pulls PyTorch); the mapping from its
  output to :class:`GlinerSpan` / :class:`GlinerRelation` lives in two small
  functions (:func:`parse_gliner2_entities`, :func:`parse_gliner2_relations`) so
  it is easy to fix against the real package.
* :class:`FakeGlinerBackend` — deterministic, for tests: every exact occurrence
  of each lexicon surface (longest first, no overlaps), score 0.9.
* :class:`HeuristicGlinerBackend` — a no-GPU regex stand-in for demo runs. It
  is NOT GLiNER: its ``name`` says so, it logs a warning when built, and U2
  logs again for every document it reads with it.

Contract: offsets are character offsets into the text passed in, and
``text[start:end] == span.text``. Stores written: none. Token caps: none
(windows are cut by U2). Spec: P1 §1 E1, §3 U2, §9 (``gliner`` settings).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from synth.contracts import GlinerRelation, GlinerSpan

log = logging.getLogger("synth.u2")


# ------------------------------------------------------------------ GLiNER2 (live)
def _find_free(text: str, surface: str, used: set[tuple[int, int]]) -> tuple[int, int] | None:
    """First occurrence of ``surface`` in ``text`` not already used."""
    if not surface:
        return None
    pos = text.find(surface)
    while pos >= 0:
        key = (pos, pos + len(surface))
        if key not in used:
            return key
        pos = text.find(surface, pos + 1)
    return None


def _span_from_item(item: Any, label: str, text: str, used: set[tuple[int, int]],
                    default_score: float) -> GlinerSpan | None:
    if isinstance(item, str):
        surface, start, end, score = item, None, None, None
    elif isinstance(item, dict):
        surface = item.get("text") or item.get("span") or ""
        start, end = item.get("start"), item.get("end")
        score = item.get("confidence", item.get("score"))
        label = item.get("label", label)
    elif isinstance(item, (list, tuple)) and item:
        surface = str(item[0])
        start = item[1] if len(item) > 2 else None
        end = item[2] if len(item) > 2 else None
        score = item[3] if len(item) > 3 else None
    else:
        return None
    if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text)
            and (not surface or text[start:end] == surface)):
        hit = _find_free(text, surface, used)
        if hit is None:
            return None
        start, end = hit
    used.add((start, end))
    return GlinerSpan(start, end, text[start:end], str(label),
                      float(score) if score is not None else default_score)


def parse_gliner2_entities(result: Any, text: str, threshold: float) -> list[GlinerSpan]:
    """Map ``GLiNER2.extract_entities`` output to spans.

    Accepted shapes: ``{"entities": {label: [str | {"text","start","end","confidence"}]}}``,
    the inner ``{label: [...]}`` alone, or a list of ``{"text","label","start","end","score"}``.
    Items without offsets are located in ``text`` (first unused occurrence)."""
    groups = result.get("entities", result) if isinstance(result, dict) else result
    pairs: list[tuple[str, Any]] = []
    if isinstance(groups, dict):
        for label, items in groups.items():
            for item in items or []:
                pairs.append((label, item))
    elif isinstance(groups, list):
        pairs = [(item.get("label", "") if isinstance(item, dict) else "", item) for item in groups]
    used: set[tuple[int, int]] = set()
    spans = []
    for label, item in pairs:
        span = _span_from_item(item, label, text, used, default_score=threshold)
        if span is not None and span.score >= threshold:
            spans.append(span)
    return spans


def parse_gliner2_relations(result: Any, text: str, threshold: float) -> list[GlinerRelation]:
    """Map ``GLiNER2.extract_relations`` output to relations.

    Accepted shapes: ``{"relation_extraction": {rel: [(head, tail) | {"head": …, "tail": …,
    "confidence"}]}}`` (or ``"relations"``), where head/tail are strings or span dicts."""
    groups = result
    if isinstance(result, dict):
        groups = result.get("relation_extraction", result.get("relations", result))
    out: list[GlinerRelation] = []
    if not isinstance(groups, dict):
        return out
    for rel, items in groups.items():
        for item in items or []:
            if isinstance(item, dict):
                head_raw, tail_raw = item.get("head"), item.get("tail")
                score = item.get("confidence", item.get("score"))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                head_raw, tail_raw = item[0], item[1]
                score = item[2] if len(item) > 2 else None
            else:
                continue
            head = _span_from_item(head_raw, "entity", text, set(), threshold)
            tail = _span_from_item(tail_raw, "entity", text, set(), threshold)
            if head is None or tail is None:
                continue
            if score is None:
                score = min(head.score, tail.score)
            if float(score) >= threshold:
                out.append(GlinerRelation(head, tail, str(rel), float(score)))
    return out


class Gliner2Backend:
    """GLiNER2 on the GPU (or CPU). Loads the model on first use."""

    stand_in = False

    def __init__(self, model: str = "fastino/gliner2.5-base-v1", device: str = "cuda") -> None:
        self.model_id = model
        self.device = device
        self.name = "gliner2"
        self._model = None

    def _load(self):
        if self._model is None:
            # gliner2 >= 2.x: GLiNER2.5 checkpoints load through AutoExtractor (needs the
            # `local` extra: torch + transformers). Plain `import gliner2` succeeds on the
            # torch-free base package, so the import alone proves nothing.
            try:
                from gliner2 import AutoExtractor as _Loader
            except ImportError:
                try:
                    from gliner2 import GLiNER2 as _Loader
                except ImportError as exc:
                    raise RuntimeError('gliner2 is not installed (py -m pip install -e ".[gliner]")') from exc
            try:
                model = _Loader.from_pretrained(self.model_id)
            except ImportError as exc:
                raise RuntimeError("gliner2 is installed without its local model stack; "
                                   'py -m pip install "gliner2[local]"') from exc
            if self.device and hasattr(model, "to"):
                try:
                    model = model.to(self.device) or model
                except Exception as exc:  # no CUDA: stay on the default device, but say so
                    log.warning("GLiNER2 could not move to %s (%s); using its default device", self.device, exc)
            self._model = model
        return self._model

    def entities(self, text: str, labels: list[str], threshold: float) -> list[GlinerSpan]:
        model = self._load()
        try:
            raw = model.extract_entities(text, list(labels), threshold=threshold,
                                         include_confidence=True, include_spans=True)
        except TypeError:          # older signature: no span/confidence flags
            raw = model.extract_entities(text, list(labels))
        return parse_gliner2_entities(raw, text, threshold)

    def relations(self, text: str, relation_labels: list[str], entity_labels: list[str],
                  threshold: float) -> list[GlinerRelation]:
        model = self._load()
        display = {r.replace("_", " "): r for r in relation_labels}
        try:
            raw = model.extract_relations(text, list(display), threshold=threshold,
                                          include_confidence=True, include_spans=True)
        except TypeError:
            raw = model.extract_relations(text, list(display))
        rels = parse_gliner2_relations(raw, text, threshold)
        return [GlinerRelation(r.head, r.tail, display.get(r.relation, r.relation), r.score) for r in rels]


# ------------------------------------------------------------------ fake (tests)
def _word_bounded(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else " "
    after = text[end] if end < len(text) else " "
    return not (before.isalnum() and text[start].isalnum()) and not (after.isalnum() and text[end - 1].isalnum())


def lexicon_spans(text: str, lexicon: dict[str, str], score: float = 0.9) -> list[GlinerSpan]:
    """Every exact, word-bounded occurrence of each surface; longest first, no overlaps."""
    taken: list[tuple[int, int]] = []
    spans: list[GlinerSpan] = []
    for surface in sorted(lexicon, key=lambda s: (-len(s), s)):
        pos = text.find(surface)
        while pos >= 0:
            end = pos + len(surface)
            if _word_bounded(text, pos, end) and not any(pos < e and end > s for s, e in taken):
                taken.append((pos, end))
                spans.append(GlinerSpan(pos, end, surface, lexicon[surface], score))
            pos = text.find(surface, pos + 1)
    return sorted(spans, key=lambda s: s.start)


class FakeGlinerBackend:
    """Deterministic GLiNER stand-in for tests."""

    stand_in = True

    def __init__(self, lexicon: dict[str, str], relations: Iterable[tuple[str, str, str]] = (),
                 entity_score: float = 0.9, relation_score: float = 0.8) -> None:
        self.name = "fake_gliner"
        self.lexicon = dict(lexicon)
        self.relation_triples = list(relations)
        self.entity_score = entity_score
        self.relation_score = relation_score
        self.calls: list[str] = []          # window texts seen (tests inspect the windowing)

    def entities(self, text: str, labels: list[str], threshold: float) -> list[GlinerSpan]:
        self.calls.append(text)
        if self.entity_score < threshold:
            return []
        return [s for s in lexicon_spans(text, self.lexicon, self.entity_score) if s.label in labels]

    def relations(self, text: str, relation_labels: list[str], entity_labels: list[str],
                  threshold: float) -> list[GlinerRelation]:
        """(head, relation, tail) when both surfaces occur in the window; the closest
        pair of occurrences is used. Relation names are NOT filtered (U2 checks them)."""
        spans = lexicon_spans(text, self.lexicon, self.entity_score)
        out = []
        for head_s, rel, tail_s in self.relation_triples:
            heads = [s for s in spans if s.text == head_s]
            tails = [s for s in spans if s.text == tail_s]
            pairs = [(abs(h.start - t.start), h.start, h, t) for h in heads for t in tails if h != t]
            if not pairs:
                continue
            _, _, head, tail = min(pairs, key=lambda p: (p[0], p[1]))
            out.append(GlinerRelation(head, tail, rel, self.relation_score))
        return out


# ------------------------------------------------------------------ heuristic (demo, no GPU)
_ORG_SUFFIX = (r"(?:plc|PLC|Plc|Ltd|LTD|Limited|LIMITED|LLP|LLC|Inc|INC|Corp|Corporation|Group|GROUP|Holdings"
               r"|HOLDINGS|Bank|BANK|Court|COURT|Partners|Trust|Council|Authority|Commission)")
_CAPW = r"[A-Z][\w'’-]*"
_ORG_RE = re.compile(rf"\b{_CAPW}(?:[ \t]+(?:{_CAPW}|&))*[ \t]+{_ORG_SUFFIX}(?:[ \t]+{_ORG_SUFFIX})*(?![\w])")
_HONORIFIC = r"(?:Mr|Mrs|Ms|Miss|Dr|Sir|Dame|Lord|Lady|Prof|Professor|Justice|Judge)\.?"
_NAME_WORD = r"[A-Z][a-zA-Z]*[a-z](?:[-'’][A-Z][a-zA-Z]*[a-z])*"
_PERSON_RUN = re.compile(rf"(?:\b{_HONORIFIC}[ \t]+)?\b{_NAME_WORD}(?:[ \t]+{_NAME_WORD})*")
_HONORIFIC_RE = re.compile(rf"^{_HONORIFIC}$")
_MONEY_RE = re.compile(r"(?:\b(?:GBP|USD|EUR|CHF)[ \t]?|[£$€])\d[\d,]*(?:\.\d+)?(?:[ \t]?(?:million|billion|thousand"
                       r"|bn|m|k)\b)?|\b\d[\d,]*(?:\.\d+)?[ \t]?(?:million|billion)?[ \t]?(?:pounds|dollars|euros)\b")
_PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?[ \t]?(?:%|per[ \t]?cent\b|percent\b)")
_ROLE_RE = re.compile(r"\b(?:(?:non-executive|executive|managing|finance|independent|senior)[ \t]+)?"
                      r"(?:director|chair(?:man|woman|person)?|chief[ \t]+(?:executive|financial|operating)[ \t]+officer"
                      r"|company[ \t]+secretary|treasurer|ceo|cfo|coo)\b", re.IGNORECASE)
_STOP = set("""
The A An This That These Those Present Minutes Board Directors Director Chair Chairman Chairwoman Chief
Financial Officer Executive Non-Executive Secretary Company Group Committee Credit Memo Subject Date Published
Industry Next Meeting Declarations Declaration Freight Haulage Harbour Trade Press Terminal Bank Court High
Supreme Agreement Facility Section Clause Schedule Part Article Annex Appendix In On At By For From To Of And
But If As It Its He She They We No Not Item Items Notes Note Report Annual General Resolved Approved Budget
Northern Ireland England Scotland Wales London Belfast Larne Street Road House Square Park Lease Interest
Total Revenue Sales Loan Deed Contract Order Judgment Claim Claimant Defendant Respondent Applicant Plaintiff
January February March April May June July August September October November December Monday Tuesday
Wednesday Thursday Friday Saturday Sunday Dear Yours Regards Re Per Under With Between After Before During
Today Tomorrow Yesterday Following Previous
""".split())
_LEAD_STOP = {"The", "In", "On", "At", "By", "From", "To", "For", "Of", "And", "With", "Subject", "Date",
              "Present", "Between", "Under", "Per", "Re", "Dear", "As", "If", "When", "While", "Since"}
_HEURISTIC_RELATIONS = [
    ("director_of", r"^,?[ \t]*(?:a[ \t]+|an[ \t]+|the[ \t]+)?(?:(?:non-executive|executive|managing|independent)[ \t]+)?"
                    r"director[ \t]+of[ \t]+$", "person"),
    ("chair_of", r"^,?[ \t]*(?:(?:was|is|became|remains)[ \t]+)?(?:the[ \t]+)?chair(?:man|woman|person)?[ \t]+of[ \t]+$",
     "person"),
    ("subsidiary_of", r"^[ \t]+is[ \t]+a[ \t]+(?:wholly[ -]owned[ \t]+)?subsidiary[ \t]+of[ \t]+$", None),
    ("shareholder_of", r"^,?[ \t]*(?:who[ \t]+is[ \t]+|is[ \t]+)?a[ \t]+shareholder[ \t]+(?:of|in)[ \t]+$", None),
    ("lent_to", r"^[ \t]+lent[ \t]+[^.]{0,40}?[ \t]to[ \t]+$", None),
    ("owes", r"^[ \t]+owes[ \t]+$", None),
    ("represents", r"^[ \t]+represents[ \t]+$", None),
    ("employed_by", r"^,?[ \t]*(?:who[ \t]+)?(?:works[ \t]+for|is[ \t]+employed[ \t]+by)[ \t]+$", "person"),
    ("partner_at", r"^,?[ \t]*(?:a[ \t]+)?partner[ \t]+(?:at|of|in)[ \t]+$", "person"),
]


def _person_spans(text: str, blocked: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out = []
    for m in _PERSON_RUN.finditer(text):
        words = [(w.start() + m.start(), w.end() + m.start(), w.group(0))
                 for w in re.finditer(r"\S+", m.group(0))]
        run: list[tuple[int, int, str]] = []
        segments = []
        for w in words + [(-1, -1, "")]:
            if w[2] and w[2] not in _STOP:
                run.append(w)
                continue
            if run:
                segments.append(run)
            run = []
        for seg in segments:
            honorific = bool(_HONORIFIC_RE.match(seg[0][2]))
            names = seg[1:] if honorific else seg
            if not names or len(names) > 3 or (not honorific and len(names) < 2):
                continue
            start, end = seg[0][0], seg[-1][1]
            if not any(start < e and end > s for s, e in blocked):
                out.append((start, end))
    return out


class HeuristicGlinerBackend:
    """Regex stand-in for GLiNER (demo runs without a GPU). NOT the real model."""

    stand_in = True

    def __init__(self) -> None:
        self.name = "heuristic_regex_not_gliner"
        log.warning("HeuristicGlinerBackend in use: regex stand-in for demo runs, NOT GLiNER2")

    def entities(self, text: str, labels: list[str], threshold: float) -> list[GlinerSpan]:
        from synth.modules.u03_time import find_time_expressions

        wanted = set(labels)
        found: list[GlinerSpan] = []
        taken: list[tuple[int, int]] = []

        def add(start: int, end: int, label: str, score: float) -> None:
            if label not in wanted or score < threshold or end <= start:
                return
            if any(start < e and end > s for s, e in taken):
                return
            taken.append((start, end))
            found.append(GlinerSpan(start, end, text[start:end], label, score))

        for m in _ORG_RE.finditer(text):
            start, words = m.start(), m.group(0).split()
            while len(words) > 2 and words[0] in _LEAD_STOP:
                start = text.index(words[1], start + len(words[0]))
                words = words[1:]
            suffix = words[-1].rstrip(".").lower()
            label = {"bank": "bank", "court": "court"}.get(suffix, "organization")
            add(start, m.end(), label if label in wanted else "organization", 0.7)
        for start, end, label in find_time_expressions(text):
            add(start, end, label, 0.7)
        for m in _MONEY_RE.finditer(text):
            add(m.start(), m.end(), "money amount", 0.7)
        for m in _PERCENT_RE.finditer(text):
            add(m.start(), m.end(), "percentage", 0.7)
        for m in _ROLE_RE.finditer(text):
            add(m.start(), m.end(), "role title", 0.6)
        for start, end in _person_spans(text, taken):
            add(start, end, "person", 0.65)
        return sorted(found, key=lambda s: s.start)

    def relations(self, text: str, relation_labels: list[str], entity_labels: list[str],
                  threshold: float) -> list[GlinerRelation]:
        """A handful of explicit patterns between adjacent entity spans ("X, a director of Y")."""
        spans = [s for s in self.entities(text, list(entity_labels) + ["person", "organization", "bank"], 0.0)
                 if s.label not in ("date", "time period", "duration", "role title", "money amount",
                                    "percentage")]
        wanted = set(relation_labels)
        out = []
        for i, head in enumerate(spans):
            for tail in spans[i + 1:i + 2]:
                gap = text[head.end:tail.start]
                if len(gap) > 80 or "\n" in gap:
                    continue
                for rel, pattern, head_label in _HEURISTIC_RELATIONS:
                    if rel in wanted and (head_label is None or head.label == head_label) \
                            and re.search(pattern, gap, re.IGNORECASE):
                        if 0.6 >= threshold:
                            out.append(GlinerRelation(head, tail, rel, 0.6))
                        break
        return out
