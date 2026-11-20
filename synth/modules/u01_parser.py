"""U1 — Docling Parser (+ OCR fallback): files -> the S1 provenance store.

Purpose: turn each input file into Synth's **canonical text** and everything
that points into it: the page map, the sentence index, the document-date
candidate, the micro chunks (cut B) and the T2 sections. Every later offset in
Synth indexes this text.

Contract:

* :func:`ingest_path` ``(store, config, path, *, sovereign=False) -> list[dict]``
  — a file or a folder (recursive, Datanaut's ``discover``). S1 is
  run-independent (no ``run_id``). Idempotent by sha256: the same bytes return
  the existing ``doc_id`` and write nothing. One summary dict per file
  (``doc_id, status, sentences, filtered, chunks, sections, pages, notes, …``).
  Every input reaches exactly one terminal ``documents.status``: ``SEGMENTED``,
  ``EMPTY`` (no text) or ``FAILED`` (error in ``notes.error``); a failing file
  never stops the folder.
* :func:`emit_ingest_coverage` ``(ctx, doc_id) -> dict`` — per run: one
  ``SEGMENTED`` coverage event per non-filtered sentence, one ``SKIPPED_FILTER``
  per filtered sentence (idempotent within a run).

How:

* **Parsing.** ``.txt`` is read directly (its ``\\f`` characters, if any, are page
  breaks). Everything else goes through the existing Datanaut converter
  (``app.ingest.adapter.Ingestor``: Docling ``NativePdfPipeline``, no models;
  independent pypdfium2 page count; page-by-page recovery). PDF pages with fewer
  than ``ingest.ocr_min_chars`` extractable characters would get OCR; OCR models
  are not installed, so those pages fall back to ``pypdf`` text when it has more
  characters (logged per page in ``notes.ocr``).
* **Canonical text.** NFC, ``\\n`` newlines, ``\\f`` between pages, one block per
  line (PDF line items that wrap a paragraph are re-flowed into one line with a
  single space), tables row-wise with cells joined by `` | ``.
  ``page_map = [{page, char_start, char_end}]``; ``text_version = 1``.
* **Sentences.** ``pysbd`` inside each text block, plus rules: a heading, a table
  row and a list item are one sentence each. Spans are stripped of surrounding
  whitespace; ``text[char_start:char_end]`` is the sentence. A sentence never
  crosses a block or a page. ``section_path`` is the heading path
  (``"2 Northbank facility"``, nested levels joined by `` > ``). Lines repeated
  at the top or bottom of ≥ ``ingest.header_footer_share`` of the text pages
  (documents with ≥ 3 text pages; digits ignored, so page numbers match) are
  ``filtered``.
* **Document date.** PDF metadata creation date, else the first full date
  (day, month, year) in the first ``ingest.doc_date_window_chars`` characters
  (span in ``notes.doc_date_span``). ``doc_date_time_id`` stays null (U3).
* **Chunks and sections.** See :mod:`synth.modules.u01_chunking`.

Stores written: S1 ``documents`` (change-logged), ``doc_texts``, ``sentences``,
``micro_chunks``, ``sections``; L3 ``coverage_events`` (per run). Token caps:
``chunking.micro`` (target 400, cap 500) and ``chunking.section`` (1,500 /
2,000), counted with the ``t1_micro`` tokenizer when its file exists, else the
gateway heuristic. Spec: P1 §3 U1, §2 "Two chunking cuts", §4 S1, §7.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from importlib import metadata as _metadata
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import and_, func, select

from app.ingest.adapter import SUPPORTED_SUFFIXES, discover, hash_file
from synth import states as S
from synth.config import SynthConfig
from synth.gateway.tokens import TokenCounter
from synth.ids import new_id
from synth.modules.common import StageContext, emit_coverage
from synth.modules.u01_chunking import group_sections, micro_chunks, profile_hash
from synth.store import Store
from synth.store.tables import coverage_events, doc_texts, sentences as sentences_table

log = logging.getLogger("synth.u01")

MODULE = "U1"
PARSER_RULES = "u01.v1"
TEXT_VERSION = 1
SEGMENTED, EMPTY, FAILED = "SEGMENTED", "EMPTY", "FAILED"
SECTION_SEPARATOR = " > "
#: Header/footer candidates: this many blocks at the top and at the bottom of each page.
EDGE_BLOCKS = 3
TOKEN_ENDPOINT = "t1_micro"


class ParseFailed(Exception):
    """The file could not be turned into text."""


# ======================================================================= text rules
_TERMINAL = re.compile(r"[.!?:][\"'”’)\]]*$")
_CONT_PUNCT = re.compile(r"[,;\-–—(&/]$")
_NUMBERED = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,2})+\.?|\d{1,2}\.|[IVXLC]{1,6}\.)\s+(?P<rest>\S.*)$")
_BULLET = re.compile(r"^(?:[-*•‣◦▪●∙·–]|\(?[a-z]\)|\(?[ivx]{1,4}\)|\(?\d{1,2}\))\s+\S")
_FIELD = re.compile(r"^[A-Z][A-Za-z&/'’.-]*(?:\s+[A-Za-z&/'’.-]+){0,3}:\s+\S")
_FUNCTION_WORDS = frozenset("""a an the of to in on at by for with from into onto upon under over
    between among and or but nor than as that which who whom whose where when while if is are was
    were be been being has have had not no its his her their our your my this these those such
    any each every per via""".split())


def _has_alnum(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def _heading_like(text: str) -> tuple[int, str] | None:
    """(level, title) when a single line reads as a heading: a numbered heading
    (``2.``, ``2.1``, ``IV.`` + capitalised words) or an all-capitals line."""
    s = text.strip()
    if not s or "\n" in s or len(s) > 100 or len(s.split()) > 12:
        return None
    if s[-1] in ".!?;,":
        return None
    m = _NUMBERED.match(s)
    if m and m.group("rest")[0].isupper():
        num = m.group("num").rstrip(".")
        level = num.count(".") + 1 if num[0].isdigit() else 1
        return level, f"{num} {' '.join(m.group('rest').split())}"
    letters = [c for c in s if c.isalpha()]
    if len(letters) >= 3 and all(c.isupper() for c in letters):
        return 1, " ".join(s.split())
    return None


def _list_like(text: str) -> bool:
    return bool(_BULLET.match(text.strip()))


def _field_like(text: str) -> bool:
    return bool(_FIELD.match(text.strip()))


def _table_like(text: str) -> bool:
    return text.count(" | ") >= 2 or (text.strip().startswith("|") and text.strip().endswith("|"))


def _structural(text: str) -> bool:
    return (_heading_like(text) is not None or _list_like(text) or _field_like(text)
            or _table_like(text))


def _starts_lower(text: str) -> bool:
    s = text.lstrip()
    return bool(s) and s[0].isalpha() and s[0].islower()


def _continuation_hint(a: str) -> bool:
    """``a`` reads as unfinished: ends in a comma/dash/bracket or a function word."""
    s = a.rstrip()
    if _CONT_PUNCT.search(s):
        return True
    words = s.split()
    return bool(words) and words[-1].casefold() in _FUNCTION_WORDS


def _clean(text: str) -> str:
    """NFC; ``\\n`` newlines; no stray page breaks or control characters inside an item."""
    text = unicodedata.normalize("NFC", text.replace("﻿", ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", "\n").replace(" ", "\n").replace("\x0b", "\n")
    text = text.replace("\f", "\n").replace("\x00", "")
    return text


# ======================================================================= structures
@dataclass
class Line:
    """One source item before block building (a PDF line, a DOCX paragraph, a table row,
    a ``.txt`` line)."""

    text: str
    page: int
    label: str = "text"
    level: int = 0
    bbox: dict[str, Any] | None = None
    start: int | None = None          # .txt only: offsets into the canonical text
    end: int | None = None
    gap_before: bool = False          # .txt only: a blank line precedes it


@dataclass
class Block:
    page: int
    kind: str                         # heading | list_item | table_row | text
    level: int
    lines: list[Line]
    title: str = ""
    start: int = 0
    end: int = 0
    filtered: bool = False
    section_path: str = ""


@dataclass
class Parsed:
    text: str
    page_map: list[dict[str, int]]
    blocks: list[Block]
    pages: int
    parser: str
    parser_version: str
    meta_date: str | None = None
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass
class SentenceSpec:
    start: int
    end: int
    page: int
    kind: str
    section_path: str
    filtered: bool


# ======================================================================= block building
def _bbox_adjacent(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool | None:
    """Are two PDF lines consecutive lines of one paragraph? ``None`` = no geometry."""
    if not a or not b:
        return None
    try:
        ha, hb = abs(a["t"] - a["b"]), abs(b["t"] - b["b"])
        h = min(ha, hb) or max(ha, hb) or 10.0
        if str(a.get("coord_origin", "")).upper().endswith("BOTTOMLEFT"):
            gap = a["b"] - b["t"]
        else:
            gap = b["t"] - a["b"]
        return -0.5 * h <= gap <= 1.0 * h and abs(b["l"] - a["l"]) <= 3 * h
    except (KeyError, TypeError):
        return None


def _joins(a: Line, b: Line) -> bool:
    """Should line ``b`` continue the paragraph that line ``a`` ends?"""
    if b.gap_before or a.page != b.page or _structural(b.text):
        return False
    if _starts_lower(b.text):
        return True
    if _TERMINAL.search(a.text.rstrip()):
        return False
    hint = _continuation_hint(a.text)
    geo = _bbox_adjacent(a.bbox, b.bbox)
    if geo is None:
        return hint
    if not geo:
        return False
    return hint or not _field_like(a.text)


def _classify(line: Line, text_headings: bool) -> tuple[str, int, str]:
    label = (line.label or "text").lower()
    if label == "table_row":
        return "table_row", 0, ""
    if label == "title":
        return "heading", 0, " ".join(line.text.split())
    if label == "section_header":
        return "heading", max(int(line.level or 1), 1), " ".join(line.text.split())
    if label == "list_item":
        return "list_item", 0, ""
    if text_headings:
        heading = _heading_like(line.text)
        if heading is not None:
            return "heading", heading[0], heading[1]
        if _list_like(line.text):
            return "list_item", 0, ""
        if _table_like(line.text):
            return "table_row", 0, ""
    return "text", 0, ""


def build_blocks(lines: Sequence[Line], *, join: bool, text_headings: bool) -> list[Block]:
    """Group lines into blocks. With ``join``, wrapped lines become one paragraph."""
    blocks: list[Block] = []
    for line in lines:
        kind, level, title = _classify(line, text_headings)
        if (join and blocks and kind == "text" and blocks[-1].kind in ("text", "list_item")
                and _joins(blocks[-1].lines[-1], line)):
            blocks[-1].lines.append(line)
            continue
        blocks.append(Block(page=line.page, kind=kind, level=level, lines=[line], title=title))
    return blocks


def _block_text(block: Block) -> str:
    """Re-flowed text of a block built from separate PDF lines."""
    out = block.lines[0].text
    for line in block.lines[1:]:
        if out.endswith("-") and not out.endswith(" -") and _starts_lower(line.text):
            out += line.text                       # hyphen kept; no character invented
        else:
            out += " " + line.text
    return out


def _assemble(blocks: list[Block], pages: int) -> tuple[str, list[dict[str, int]]]:
    """Canonical text from blocks: ``\\n`` between blocks, ``\\f`` between pages."""
    by_page: dict[int, list[Block]] = {}
    for block in blocks:
        by_page.setdefault(block.page, []).append(block)
    parts: list[str] = []
    page_map: list[dict[str, int]] = []
    cursor = 0
    for page in range(1, pages + 1):
        if page > 1:
            parts.append("\f")
            cursor += 1
        page_start = cursor
        for i, block in enumerate(by_page.get(page, [])):
            if i:
                parts.append("\n")
                cursor += 1
            text = _block_text(block)
            block.start, block.end = cursor, cursor + len(text)
            parts.append(text)
            cursor += len(text)
        page_map.append({"page": page, "char_start": page_start, "char_end": cursor})
    return "".join(parts), page_map


# ======================================================================= parsers
def _decode(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding), encoding.replace("-sig", "")
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def parse_text(text: str) -> Parsed:
    """Plain text: the canonical text is the (normalized) file text itself."""
    text = unicodedata.normalize("NFC", text.replace("﻿", ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines: list[Line] = []
    page_map: list[dict[str, int]] = []
    cursor = 0
    for page_no, page_text in enumerate(text.split("\f"), start=1):
        page_map.append({"page": page_no, "char_start": cursor, "char_end": cursor + len(page_text)})
        pos = cursor
        gap = False
        for raw_line in page_text.split("\n"):
            stripped = raw_line.strip()
            if stripped:
                lead = len(raw_line) - len(raw_line.lstrip())
                start = pos + lead
                lines.append(Line(text=stripped, page=page_no, start=start, end=start + len(stripped),
                                  gap_before=gap))
                gap = False
            else:
                gap = True
            pos += len(raw_line) + 1
        cursor += len(page_text) + 1
    blocks = build_blocks(lines, join=True, text_headings=True)
    for block in blocks:
        block.start, block.end = block.lines[0].start, block.lines[-1].end
    return Parsed(text=text, page_map=page_map, blocks=blocks, pages=len(page_map),
                  parser="plaintext", parser_version=PARSER_RULES)


def _parse_txt(path: Path) -> Parsed:
    raw_text, encoding = _decode(path.read_bytes())
    parsed = parse_text(raw_text)
    parsed.notes["encoding"] = encoding
    return parsed


def _version(dist: str) -> str:
    try:
        return _metadata.version(dist)
    except _metadata.PackageNotFoundError:
        return "unknown"


def _nonspace(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def _pdf_metadata_date(path: Path) -> str | None:
    try:
        import pypdf

        meta = pypdf.PdfReader(str(path)).metadata
        created = meta.creation_date if meta is not None else None
    except Exception:  # noqa: BLE001 - a malformed date is simply no date
        return None
    return created.date().isoformat() if created else None


def _pypdf_pages(path: Path, pages: Iterable[int]) -> dict[int, tuple[str, str | None]]:
    """page -> (text, error) from pypdf, for the requested 1-based pages."""
    out: dict[int, tuple[str, str | None]] = {}
    try:
        import pypdf

        reader = pypdf.PdfReader(str(path))
        count = len(reader.pages)
    except Exception as exc:  # noqa: BLE001
        return {p: ("", f"{type(exc).__name__}: {exc}") for p in pages}
    for page in pages:
        if page > count:
            out[page] = ("", "page beyond pypdf page count")
            continue
        try:
            out[page] = (reader.pages[page - 1].extract_text() or "", None)
        except Exception as exc:  # noqa: BLE001
            out[page] = ("", f"{type(exc).__name__}: {exc}")
    return out


def _parse_docling(path: Path, config: SynthConfig) -> Parsed:
    from app.config import IngestConfig as DatanautIngestConfig
    from app.ingest.adapter import Ingestor

    ingestor = Ingestor(DatanautIngestConfig(pdf_pipeline=config.ingest.pdf_pipeline))
    source = ingestor.ingest_file(path)
    is_pdf = path.suffix.lower() == ".pdf"
    notes: dict[str, Any] = {}
    docling_failed = source.state == "failed"
    if docling_failed:
        notes["docling_error"] = source.error or "conversion failed"
        if not is_pdf:
            raise ParseFailed(source.error or "Docling conversion failed")
    page_errors = [{"page": o.page_no, "error": o.error or o.reason}
                   for o in source.page_outcomes if o.state == "failed"]
    if page_errors:
        notes["page_errors"] = page_errors
    flags = getattr(source, "order_flags", None)
    if flags is not None and flags.suspicious:          # Datanaut's reading-order check flags, never repairs
        notes["reading_order"] = flags.to_dict()

    lines: list[Line] = []
    last_page = 1
    for passage in source.passages:
        page = passage.page_no or last_page
        last_page = page
        text = _clean(passage.text or "")
        label = (passage.label or "text").lower()
        if label == "table":
            for row in text.split("\n"):
                if _has_alnum(row):
                    lines.append(Line(text=row.strip(), page=page, label="table_row"))
            continue
        if text.strip():
            lines.append(Line(text=text.strip(), page=page, label=label, level=passage.level or 0,
                              bbox=passage.bbox))

    seen_pages = max((line.page for line in lines), default=0)
    pages = max(source.page_count or 0, seen_pages, 1)
    parser, parser_version = "docling", _version("docling-slim")

    if is_pdf:                                       # OCR rule -> pypdf fallback (no OCR models)
        chars: dict[int, int] = {}
        for line in lines:
            chars[line.page] = chars.get(line.page, 0) + _nonspace(line.text)
        low = [p for p in range(1, pages + 1) if chars.get(p, 0) < config.ingest.ocr_min_chars]
        if low:
            fallback = _pypdf_pages(path, low)
            ocr_pages = []
            used_pypdf = False
            for page in low:
                text, error = fallback.get(page, ("", "not extracted"))
                text = _clean(text)
                entry: dict[str, Any] = {"page": page, "docling_chars": chars.get(page, 0),
                                         "pypdf_chars": _nonspace(text), "used": "docling"}
                if error:
                    entry["error"] = error
                if _nonspace(text) > chars.get(page, 0):
                    entry["used"] = "pypdf"
                    used_pypdf = True
                    lines = [line for line in lines if line.page != page]
                    lines.extend(Line(text=t.strip(), page=page, label="text")
                                 for t in text.split("\n") if t.strip())
                ocr_pages.append(entry)
            lines.sort(key=lambda line: line.page)   # stable: reading order kept within a page
            notes["ocr"] = {"min_chars": config.ingest.ocr_min_chars, "engine": None,
                            "reason": "OCR models not installed; pypdf text used where it has more",
                            "pages": ocr_pages}
            if used_pypdf:
                pypdf_version = _version("pypdf")
                if docling_failed:
                    parser, parser_version = "pypdf", pypdf_version
                else:
                    parser, parser_version = "docling+pypdf", f"{parser_version}+pypdf {pypdf_version}"
        if docling_failed and not any(_has_alnum(line.text) for line in lines):
            raise ParseFailed(source.error or "Docling conversion failed; pypdf found no text")
    else:
        notes["pageless"] = source.page_count is None and seen_pages <= 1

    text_headings = not any(line.label in ("section_header", "title") for line in lines)
    blocks = build_blocks(lines, join=is_pdf, text_headings=text_headings)
    text, page_map = _assemble(blocks, pages)
    return Parsed(text=text, page_map=page_map, blocks=blocks, pages=pages, parser=parser,
                  parser_version=parser_version,
                  meta_date=_pdf_metadata_date(path) if is_pdf else None, notes=notes)


def parse_file(path: Path, config: SynthConfig) -> Parsed:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return _parse_txt(path)
    if suffix not in SUPPORTED_SUFFIXES:
        raise ParseFailed(f"unsupported format {suffix or '(none)'}")
    return _parse_docling(path, config)


# ======================================================================= sentence index
#: Lines up to this many words compare with digits masked (``Page 3 of 9`` == ``Page 4 of 9``).
HF_MASK_MAX_WORDS = 8


def _hf_key(text: str, numbered_heading: bool) -> str:
    key = " ".join(text.casefold().split())
    if len(key.split()) <= HF_MASK_MAX_WORDS and not numbered_heading:
        key = re.sub(r"\d+", "#", key)
    return key


def mark_header_footer(blocks: list[Block], text: str, share: float) -> list[str]:
    """Filter blocks repeated at the top/bottom of ≥ ``share`` of the text pages.

    Candidates are the first and last ``EDGE_BLOCKS`` blocks of each page. Short
    lines compare with digits masked (page numbers), except numbered headings
    (``1. Overview`` and ``2. Overview`` are content, not furniture)."""
    by_page: dict[int, list[Block]] = {}
    for block in blocks:
        if _has_alnum(text[block.start:block.end]):
            by_page.setdefault(block.page, []).append(block)
    if len(by_page) < 3:
        return []
    pages_with: dict[str, set[int]] = {}
    edges: dict[str, list[Block]] = {}
    for page, page_blocks in by_page.items():
        candidates = page_blocks[:EDGE_BLOCKS] + page_blocks[-EDGE_BLOCKS:]
        for block in {id(b): b for b in candidates}.values():
            piece = text[block.start:block.end]
            key = _hf_key(piece, block.kind == "heading" and bool(_NUMBERED.match(piece.strip())))
            pages_with.setdefault(key, set()).add(page)
            edges.setdefault(key, []).append(block)
    filtered = []
    for key, pages in pages_with.items():
        if len(pages) / len(by_page) >= share:
            for block in edges[key]:
                block.filtered = True
            filtered.append(key)
    return filtered


def assign_section_paths(blocks: list[Block]) -> None:
    stack: list[tuple[int, str]] = []
    for block in blocks:
        if block.kind == "heading" and not block.filtered and block.title:
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, block.title))
        block.section_path = SECTION_SEPARATOR.join(title for _, title in stack)


_SEGMENTER = None


def _segmenter():
    global _SEGMENTER
    if _SEGMENTER is None:
        import pysbd

        _SEGMENTER = pysbd.Segmenter(language="en", clean=False)
    return _SEGMENTER


def _split_block(text: str, start: int, end: int) -> tuple[list[tuple[int, int]], int]:
    """pysbd inside one text block -> stripped (start, end) spans; and the number of
    fallbacks (pysbd output that could not be located verbatim)."""
    source = text[start:end].replace("\n", " ").replace("\t", " ")    # same length, same offsets
    spans: list[tuple[int, int]] = []
    fallbacks = 0
    cursor = 0
    for piece in _segmenter().segment(source):
        piece = piece.strip()
        if not piece:
            continue
        at = source.find(piece, cursor)
        if at < 0:
            fallbacks += 1
            break
        spans.append((at, at + len(piece)))
        cursor = at + len(piece)
    rest = source[cursor:]
    if rest.strip():                                   # never lose text pysbd did not hand back
        lead = len(rest) - len(rest.lstrip())
        spans.append((cursor + lead, cursor + len(rest.rstrip())))
    return [(start + a, start + b) for a, b in spans], fallbacks


def segment(parsed: Parsed) -> tuple[list[SentenceSpec], dict[str, Any]]:
    out: list[SentenceSpec] = []
    fallbacks = 0
    for block in parsed.blocks:
        if block.kind == "text":
            spans, fb = _split_block(parsed.text, block.start, block.end)
            fallbacks += fb
            kind = "sentence"
        else:
            spans, kind = [(block.start, block.end)], block.kind
        for a, b in spans:
            piece = parsed.text[a:b]
            lead = len(piece) - len(piece.lstrip())
            a, b = a + lead, a + len(piece.rstrip())
            if b <= a or not _has_alnum(parsed.text[a:b]):
                continue
            out.append(SentenceSpec(a, b, block.page, kind, block.section_path, block.filtered))
    stats = {"sentence_fallbacks": fallbacks} if fallbacks else {}
    return out, stats


# ======================================================================= document date
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sept": 9,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MON = r"(?P<mon>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"(?P<day>[0-3]?\d)(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>(?:1[89]|20)\d{2})"
_DATE_PATTERNS = (
    re.compile(rf"\b{_DAY}(?:\s+day)?(?:\s+of)?\s+{_MON},?\s+{_YEAR}\b", re.I),     # 14 March 2024
    re.compile(rf"\b{_MON}\s+{_DAY},?\s+{_YEAR}\b", re.I),                          # March 14, 2024
    re.compile(r"\b(?P<year>(?:1[89]|20)\d{2})-(?P<month>[01]\d)-(?P<day>[0-3]\d)\b"),  # 2024-03-14
    re.compile(r"\b(?P<a>[0-3]?\d)[/.](?P<b>[01]?\d)[/.](?P<year>(?:1[89]|20)\d{2})\b"),  # 14/03/2024
)


def _match_date(m: re.Match) -> str | None:
    groups = m.groupdict()
    year = int(groups["year"])
    if groups.get("mon"):
        month, day = _MONTHS[groups["mon"].lower().rstrip(".")], int(groups["day"])
    elif groups.get("month"):
        month, day = int(groups["month"]), int(groups["day"])
    else:                                             # numeric: day first unless impossible
        a, b = int(groups["a"]), int(groups["b"])
        day, month = (b, a) if b > 12 >= a else (a, b)
    try:
        return _dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def find_doc_date(text: str, window: int) -> tuple[str, int, int] | None:
    """The first full date (day, month and year) in ``text[:window]``: (iso, start, end)."""
    head = text[:window]
    found = []
    for pattern in _DATE_PATTERNS:
        for m in pattern.finditer(head):
            iso = _match_date(m)
            if iso:
                found.append((m.start(), m.end(), iso))
                break                                  # the first valid match per pattern
    if not found:
        return None
    start, end, iso = min(found)
    return iso, start, end


# ======================================================================= ingest
def _token_counter(config: SynthConfig) -> tuple[TokenCounter, str]:
    endpoint = config.model_settings.endpoints.get(TOKEN_ENDPOINT)
    path = config.resolve(endpoint.tokenizer) if endpoint and endpoint.tokenizer else None
    if path is not None and Path(path).exists():
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
        return TokenCounter({TOKEN_ENDPOINT: path}), f"tokenizer:{TOKEN_ENDPOINT}:{digest}"
    return TokenCounter(), "heuristic"


def _counts(r, doc_id: str) -> dict[str, int]:
    version = r.execute(select(func.max(doc_texts.c.text_version))
                        .where(doc_texts.c.doc_id == doc_id)).scalar()
    rows = r.execute(select(sentences_table.c.filtered, func.count())
                     .where(and_(sentences_table.c.doc_id == doc_id,
                                 sentences_table.c.text_version == (version or TEXT_VERSION)))
                     .group_by(sentences_table.c.filtered)).all()
    by_flag = {bool(flag): n for flag, n in rows}
    return {"sentences": sum(by_flag.values()), "filtered": by_flag.get(True, 0),
            "chunks": len(r.select("micro_chunks", doc_id=doc_id)),
            "sections": len(r.select("sections", doc_id=doc_id))}


def _summary(doc: dict[str, Any], path: Path, counts: dict[str, int], *, existing: bool) -> dict[str, Any]:
    return {"doc_id": doc["doc_id"], "path": str(path), "filename": doc.get("filename"),
            "status": doc["status"], "existing": existing, "pages": doc.get("pages"),
            "doc_date": doc.get("doc_date"), "sovereign": bool(doc.get("sovereign")),
            **counts, "notes": doc.get("notes") or {}}


def _record_failed(store: Store, path: Path, rel_path: str, sha: str | None, sovereign: bool,
                   error: str, notes: dict[str, Any] | None = None) -> dict[str, Any]:
    doc_id = new_id("doc")
    row = dict(doc_id=doc_id, sha256=sha, filename=path.name, rel_path=rel_path,
               format=path.suffix.lower().lstrip("."), pages=None, sovereign=sovereign,
               parser=None, parser_version=None, status=FAILED, doc_date_source="none",
               notes={**(notes or {}), "error": error})
    with store.begin(stage=S.INGEST, module=MODULE) as tx:
        tx.insert("documents", row, rule_id="u01_failed")
    return _summary(row, path, {"sentences": 0, "filtered": 0, "chunks": 0, "sections": 0},
                    existing=False)


def _existing(store: Store, sha: str, path: Path, sovereign: bool) -> dict[str, Any] | None:
    with store.reader() as r:
        doc = r.get("documents", sha256=sha)
        if doc is None:
            return None
        counts = _counts(r, doc["doc_id"])
    if sovereign and not doc.get("sovereign"):         # never downgrade; upgrading is a safety fix
        with store.begin(stage=S.INGEST, module=MODULE) as tx:
            doc = tx.update("documents", {"doc_id": doc["doc_id"]}, {"sovereign": True},
                            rule_id="u01_sovereign_upgrade")
    return _summary(doc, path, counts, existing=True)


def _ingest_file(store: Store, config: SynthConfig, path: Path, rel_path: str, sovereign: bool,
                 counter: TokenCounter, counter_id: str, phash: str) -> dict[str, Any]:
    try:
        sha = hash_file(path)
    except OSError as exc:
        return _record_failed(store, path, rel_path, None, sovereign, f"{type(exc).__name__}: {exc}")
    found = _existing(store, sha, path, sovereign)
    if found is not None:
        return found
    try:
        parsed = parse_file(path, config)
        return _write_document(store, config, path, rel_path, sha, sovereign, parsed,
                               counter, counter_id, phash)
    except Exception as exc:  # noqa: BLE001 - a failure is a state, not a crash
        log.warning("U1: %s failed: %s", path, exc)
        return _record_failed(store, path, rel_path, sha, sovereign, f"{type(exc).__name__}: {exc}")


def _write_document(store: Store, config: SynthConfig, path: Path, rel_path: str, sha: str,
                    sovereign: bool, parsed: Parsed, counter: TokenCounter, counter_id: str,
                    phash: str) -> dict[str, Any]:
    text = parsed.text
    filtered_keys = mark_header_footer(parsed.blocks, text, config.ingest.header_footer_share)
    assign_section_paths(parsed.blocks)
    specs, seg_stats = segment(parsed)

    notes: dict[str, Any] = {**parsed.notes, **seg_stats, "rules": PARSER_RULES,
                             "segmenter": f"pysbd {_version('pysbd')}", "token_counter": counter_id}
    if filtered_keys:
        notes["filtered_lines"] = filtered_keys

    # the date the document states beats file metadata (often just when the PDF was generated)
    doc_date, date_source = None, "none"
    hit = find_doc_date(text, config.ingest.doc_date_window_chars)
    if hit:
        doc_date, date_source = hit[0], "text"
        notes["doc_date_span"] = [hit[1], hit[2]]
        notes["doc_date_surface"] = text[hit[1]:hit[2]]
    elif parsed.meta_date:
        doc_date, date_source = parsed.meta_date, "metadata"
    if parsed.meta_date and parsed.meta_date != doc_date:
        notes["metadata_date"] = parsed.meta_date

    doc_id = new_id("doc")
    sentence_rows = []
    for idx, spec in enumerate(specs):
        sentence_rows.append(dict(sentence_id=new_id("snt"), doc_id=doc_id, text_version=TEXT_VERSION,
                                  idx=idx, char_start=spec.start, char_end=spec.end, page=spec.page,
                                  section_path=spec.section_path, kind=spec.kind,
                                  filtered=spec.filtered))
    tokens = {row["idx"]: counter.n(text[row["char_start"]:row["char_end"]], TOKEN_ENDPOINT)
              for row in sentence_rows}
    micro, cfg_s = config.chunking.micro, config.chunking.section
    chunks, oversize = micro_chunks(sentence_rows, tokens, micro.target_tokens, micro.cap_tokens)
    if oversize:
        notes["oversize_sentences"] = oversize
        notes["oversize_rule"] = "a sentence over the micro cap is its own chunk"
    kinds = {row["idx"]: row["kind"] for row in sentence_rows}
    sections = group_sections(chunks, [kinds[c.first_sentence_idx] == "heading" for c in chunks],
                              cfg_s.target_tokens, cfg_s.cap_tokens)
    status = SEGMENTED if sentence_rows else EMPTY

    doc_row = dict(doc_id=doc_id, sha256=sha, filename=path.name, rel_path=rel_path,
                   format=path.suffix.lower().lstrip("."), pages=parsed.pages, doc_date=doc_date,
                   doc_date_source=date_source, sovereign=sovereign, parser=parsed.parser,
                   parser_version=parsed.parser_version, status=status, notes=notes)
    with store.begin(stage=S.INGEST, module=MODULE) as tx:
        tx.insert("documents", doc_row, rule_id="u01_ingest")
        tx.insert("doc_texts", dict(doc_id=doc_id, text_version=TEXT_VERSION, canonical_text=text,
                                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                    page_map=parsed.page_map))
        for row in sentence_rows:
            tx.insert("sentences", row)
        chunk_ids = {}
        for chunk in chunks:
            chunk_ids[chunk.idx] = new_id("chk")
            tx.insert("micro_chunks", dict(chunk_id=chunk_ids[chunk.idx], doc_id=doc_id, idx=chunk.idx,
                                           first_sentence_idx=chunk.first_sentence_idx,
                                           last_sentence_idx=chunk.last_sentence_idx,
                                           source_tokens=chunk.source_tokens, profile_hash=phash))
        for section in sections:
            tx.insert("sections", dict(section_id=new_id("sec"), doc_id=doc_id, idx=section.idx,
                                       first_chunk_idx=section.first_chunk_idx,
                                       last_chunk_idx=section.last_chunk_idx,
                                       source_tokens=section.source_tokens, profile_hash=phash))
    counts = {"sentences": len(sentence_rows),
              "filtered": sum(1 for row in sentence_rows if row["filtered"]),
              "chunks": len(chunks), "sections": len(sections)}
    return _summary(doc_row, path, counts, existing=False)


def ingest_path(store: Store, config: SynthConfig, path: str | Path, *,
                sovereign: bool = False) -> list[dict[str, Any]]:
    """Ingest a file or a folder (recursive). Returns one summary dict per file."""
    path = Path(path)
    if path.is_dir():
        files, root = discover(path), path
    elif path.is_file():
        files, root = [path], path.parent
    else:
        raise FileNotFoundError(f"input path does not exist: {path}")
    counter, counter_id = _token_counter(config)
    phash = profile_hash(config.chunking, counter_id)
    results = []
    for file in files:
        try:
            rel_path = file.relative_to(root).as_posix()
        except ValueError:
            rel_path = file.name
        results.append(_ingest_file(store, config, file, rel_path, sovereign, counter,
                                    counter_id, phash))
    return results


# ======================================================================= coverage
def emit_ingest_coverage(ctx: StageContext, doc_id: str) -> dict[str, Any]:
    """Per run: ``SEGMENTED`` per non-filtered sentence, ``SKIPPED_FILTER`` per filtered one."""
    with ctx.tx(S.INGEST, MODULE) as tx:
        if tx.get("documents", doc_id=doc_id) is None:
            raise KeyError(f"no document {doc_id}")
        version = tx.execute(select(func.max(doc_texts.c.text_version))
                             .where(doc_texts.c.doc_id == doc_id)).scalar()
        rows = tx.select("sentences", order_by="idx", doc_id=doc_id,
                         text_version=version or TEXT_VERSION)
        live = [r["sentence_id"] for r in rows if not r["filtered"]]
        dropped = [r["sentence_id"] for r in rows if r["filtered"]]
        if rows:
            already = tx.execute(select(coverage_events.c.sentence_id).where(and_(
                coverage_events.c.run_id == ctx.run_id, coverage_events.c.stage == S.INGEST,
                coverage_events.c.sentence_id == rows[0]["sentence_id"])).limit(1)).first()
            if already is not None:
                return {"doc_id": doc_id, "segmented": 0, "filtered": 0, "already_emitted": True}
        emit_coverage(tx, live, S.INGEST, S.SEGMENTED, ref_id=doc_id)
        emit_coverage(tx, dropped, S.INGEST, S.SKIPPED_FILTER, ref_id=doc_id)
    return {"doc_id": doc_id, "segmented": len(live), "filtered": len(dropped)}
