"""Reading-order sanity signals.

Cheap, deterministic checks that detect scrambled extraction — the classic
symptom of a two-column layout read as one column. Every one of these
**flags**; none of them repairs. A document whose reading order is wrong
produces wrong relationships, and the honest response is a visible warning, not
a guess at the right order.

**Why these signals and not the obvious ones.** The first version of this module
counted passages ending without terminal punctuation and flagged anything above
a threshold. It flagged a perfectly good document, because Docling's native PDF
pipeline emits one item *per line*, not per paragraph — so most passages ending
mid-clause is the normal case, not an anomaly. A check that fires on every
document is worse than no check: it trains the reader to ignore it.

So line fragmentation is detected first and reported as a *property* of the
extraction, and the signals that actually discriminate are:

* **vertical regression** — within a page, reading order should march down the
  page. Repeatedly jumping back up is what column interleaving looks like, and
  it is measured from the bounding boxes rather than guessed from the text.
* **horizontal banding** — text clustered into two or more separated x-bands is
  a multi-column layout, which is where scrambling happens.
* **mid-word splits** — the previous passage ends mid-*word* and the next
  resumes it. Genuine token damage, not ordinary line wrapping.
* **length irregularity** — retained, but only as a weak corroborating signal.
"""

from __future__ import annotations

import re
import statistics

from app.config import IngestConfig
from app.ingest.models import OrderFlags, Passage

_TERMINATORS = tuple(".!?:;\"')]}»”’")

#: Previous passage ends with a word character (no hyphen, no punctuation) and
#: the next begins with a lowercase letter: a word split across items.
_ENDS_MIDWORD = re.compile(r"[A-Za-z]$")
_STARTS_LOWER = re.compile(r"^[a-z]")

#: Structural items are short by nature and would dominate every statistic.
_NON_PROSE_LABELS = {
    "section_header", "title", "page_header", "page_footer", "caption",
    "list_item", "table", "formula", "code", "reference", "footnote",
    "document_index", "marker",
}

#: Fraction of a page's height a downward step may exceed before it counts as
#: a regression rather than ordinary line spacing.
_REGRESSION_TOLERANCE = 0.02


def _is_prose(passage: Passage) -> bool:
    if (passage.label or "") in _NON_PROSE_LABELS:
        return False
    return len(passage.text.strip()) >= 20


def _vertical_regression_ratio(passages: list[Passage]) -> tuple[float, int]:
    """Fraction of consecutive same-page pairs that jump back up the page.

    Bounding boxes use a bottom-left origin, so ``t`` is height above the
    bottom edge and reading order should be non-increasing in ``t``. A large
    increase means the next passage sits *above* the current one, which is what
    happens when a second column is read after the first.
    """
    pairs = 0
    regressions = 0
    for previous, current in zip(passages, passages[1:]):
        if previous.page_no is None or previous.page_no != current.page_no:
            continue
        prev_box, curr_box = previous.bbox, current.bbox
        if not prev_box or not curr_box:
            continue
        prev_top, curr_top = prev_box.get("t"), curr_box.get("t")
        if prev_top is None or curr_top is None:
            continue
        pairs += 1
        # Tolerance in absolute points, scaled off a typical page height.
        if curr_top > prev_top + (792.0 * _REGRESSION_TOLERANCE):
            regressions += 1
    return (regressions / pairs if pairs else 0.0), pairs


def _column_bands(passages: list[Passage]) -> tuple[int, dict[str, int]]:
    """Cluster passages into horizontal bands by their left edge.

    Returns the band count and a passage-id -> band-index map. One band is a
    single-column document; two or more means columns, which is where reading
    order goes wrong.
    """
    positioned = [(p.bbox["l"], p.id) for p in passages
                  if p.bbox and p.bbox.get("l") is not None]
    if len(positioned) < 8:
        return 1, {}

    lefts = sorted(left for left, _ in positioned)
    span = lefts[-1] - lefts[0]
    if span < 60:                       # everything starts at roughly one margin
        return 1, {pid: 0 for _, pid in positioned}

    # A gap wider than a fifth of the whole span separates two bands.
    threshold = max(60.0, span * 0.2)
    boundaries: list[float] = []
    for previous, current in zip(lefts, lefts[1:]):
        if current - previous > threshold:
            boundaries.append((previous + current) / 2)

    def band_of(left: float) -> int:
        index = 0
        for boundary in boundaries:
            if left > boundary:
                index += 1
        return index

    return len(boundaries) + 1, {pid: band_of(left) for left, pid in positioned}


def _band_switch_ratio(passages: list[Passage],
                       bands: dict[str, int]) -> tuple[float, int]:
    """Fraction of consecutive same-page pairs that change column.

    This catches the failure vertical regression misses: an extractor that
    interleaves two columns row by row (left line 1, right line 1, left line 2,
    ...) produces text that reads as nonsense while its vertical order stays
    perfectly monotonic. In a correctly ordered two-column page the column
    changes once; in an interleaved one it changes at almost every step.
    """
    pairs = 0
    switches = 0
    for previous, current in zip(passages, passages[1:]):
        if previous.page_no is None or previous.page_no != current.page_no:
            continue
        if previous.id not in bands or current.id not in bands:
            continue
        pairs += 1
        if bands[previous.id] != bands[current.id]:
            switches += 1
    return (switches / pairs if pairs else 0.0), pairs


def assess(passages: list[Passage], config: IngestConfig | None = None) -> OrderFlags:
    """Compute the order signals for one document. Flags only; never repairs."""
    config = config or IngestConfig()
    flags = OrderFlags(passage_count=len(passages))

    prose = [p for p in passages if _is_prose(p)]
    if len(prose) < 4:
        # Too little prose to say anything. Saying so beats implying the
        # document passed a check that never ran.
        flags.reasons.append(
            f"only {len(prose)} prose passage(s); reading order not assessable")
        return flags

    lengths = [len(p.text) for p in prose]
    median_length = statistics.median(lengths)
    unterminated = sum(1 for p in prose
                       if not p.text.rstrip().endswith(_TERMINATORS)) / len(prose)

    # Line-fragmented extraction: items are lines rather than sentences. The
    # direct measure is how often a passage *starts* mid-sentence — near zero
    # for paragraph-level extraction, substantial for line-level. Thresholding
    # on unterminated endings instead turned out to be brittle: a document sat
    # exactly on 0.50 and flipped the classification.
    continuation_ratio = sum(
        1 for p in prose if _STARTS_LOWER.match(p.text.lstrip())) / len(prose)
    line_fragmented = median_length < 100 and continuation_ratio > 0.15

    flags.unterminated_ratio = unterminated
    mean = statistics.fmean(lengths)
    if mean > 0 and len(lengths) > 1:
        flags.length_cv = statistics.pstdev(lengths) / mean

    # -- mid-word splits ---------------------------------------------------
    # Only meaningful for paragraph-level extraction. In line-fragmented text a
    # wrapped line ending in a whole word followed by a lowercase word is
    # ordinary, and text alone cannot distinguish that from a true word split
    # without a lexicon. Rather than guess, the check is simply not run.
    midword = 0
    comparable = 0
    if not line_fragmented:
        for previous, current in zip(prose, prose[1:]):
            prev_text, curr_text = previous.text.rstrip(), current.text.lstrip()
            if not prev_text or not curr_text:
                continue
            comparable += 1
            if _ENDS_MIDWORD.search(prev_text) and _STARTS_LOWER.match(curr_text):
                midword += 1
    flags.midword_start_ratio = midword / comparable if comparable else 0.0

    # -- geometry ----------------------------------------------------------
    regression_ratio, geometry_pairs = _vertical_regression_ratio(passages)
    band_count, band_of = _column_bands(passages)
    switch_ratio, switch_pairs = _band_switch_ratio(passages, band_of)

    if line_fragmented:
        flags.reasons.append(
            f"line-fragmented extraction (median passage {median_length:.0f} chars, "
            f"{continuation_ratio:.0%} of passages start mid-sentence, "
            f"{unterminated:.0%} unterminated) — normal for the native PDF "
            "pipeline; not treated as a fault")

    if geometry_pairs >= 8 and regression_ratio > 0.25:
        flags.reasons.append(
            f"SUSPECT ORDER: {regression_ratio:.0%} of consecutive passages sit "
            f"higher on the page than the one before ({geometry_pairs} pairs "
            "compared) — consistent with column interleaving")
        flags.suspicious = True

    if band_count >= 2 and switch_pairs >= 8 and switch_ratio > 0.5:
        flags.reasons.append(
            f"SUSPECT ORDER: text falls into {band_count} columns and the column "
            f"changes between {switch_ratio:.0%} of consecutive passages "
            f"({switch_pairs} pairs compared) — columns read interleaved rather "
            "than one after the other")
        flags.suspicious = True
    elif band_count >= 2 and regression_ratio > 0.1:
        flags.reasons.append(
            f"SUSPECT ORDER: text falls into {band_count} columns with "
            f"{regression_ratio:.0%} vertical regressions — multi-column layout "
            "read out of order")
        flags.suspicious = True

    if flags.midword_start_ratio > config.max_midword_ratio:
        flags.reasons.append(
            f"SUSPECT ORDER: {flags.midword_start_ratio:.0%} of passages resume a "
            f"word broken by the previous one (threshold "
            f"{config.max_midword_ratio:.0%})")
        flags.suspicious = True

    if flags.length_cv > config.max_length_cv:
        flags.reasons.append(
            f"passage lengths are wildly irregular (coefficient of variation "
            f"{flags.length_cv:.2f}, threshold {config.max_length_cv:.2f})")
        flags.suspicious = True

    # Documents with no geometry at all (office formats) cannot be checked for
    # order. Say so rather than implying they passed.
    if geometry_pairs == 0 and not flags.reasons:
        flags.reasons.append(
            "no positional provenance available; reading order not verifiable")

    return flags
