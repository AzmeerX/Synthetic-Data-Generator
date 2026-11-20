"""Docling adapter.

Everything Stage 0 learned is enforced here:

* Page counts come from **pypdfium2**, never from Docling. Docling silently
  truncated a damaged two-page PDF to one page and reported ``SUCCESS`` with an
  empty error list; a coverage ledger that trusts it is a ledger that lies.
* PDFs go through ``NativePdfPipeline``, which runs **no models at all**
  (Rule 3). The model-loading ``StandardPdfPipeline`` is reachable by config.
* Provenance is optional. Office formats have none, and a ``None`` page number
  is a legitimate value.
* ``doc.furniture`` is never read — it is deprecated and warns on access.
* Docling items are never mutated. They cannot be (``extra="forbid"``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any, Iterable

from app.config import IngestConfig, Settings
from app.ingest import order_check
from app.ingest.models import (
    EMPTY, FAILED, YIELDED, OrderFlags, PageOutcome, Passage, SourceDocument,
)

log = logging.getLogger(__name__)

#: What Docling can convert and we are willing to try.
SUPPORTED_SUFFIXES = {
    ".pdf", ".docx", ".xlsx", ".pptx", ".md", ".markdown", ".html", ".htm",
    ".csv", ".txt", ".adoc", ".asciidoc", ".xml", ".json",
}

#: Formats that legitimately carry no page or position provenance. Stage 0
#: proved DOCX and XLSX items have an empty `prov` and an empty `pages` dict.
PAGELESS_SUFFIXES = {
    ".docx", ".xlsx", ".pptx", ".md", ".markdown", ".html", ".htm", ".csv",
    ".txt", ".adoc", ".asciidoc", ".xml", ".json",
}


class IngestError(Exception):
    pass


# --------------------------------------------------------------------------
# Hashing and discovery
# --------------------------------------------------------------------------

def hash_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def document_id(content_hash: str) -> str:
    """Content-derived, so an id means the same thing across runs.

    Deliberately not derived from ``self_ref`` or from position: Stage 0 showed
    ``self_ref`` is positional and would silently re-point on re-conversion.
    """
    return f"doc_{content_hash[:16]}"


def discover(input_path: Path, recursive: bool = True) -> list[Path]:
    """Every candidate file under ``input_path``, sorted for determinism."""
    input_path = Path(input_path)
    if input_path.is_file():
        return [input_path]
    if not input_path.exists():
        raise IngestError(f"input path does not exist: {input_path}")
    pattern = "**/*" if recursive else "*"
    return sorted(
        p for p in input_path.glob(pattern)
        if p.is_file() and not p.name.startswith(".")
        and p.suffix.lower() in SUPPORTED_SUFFIXES
    )


# --------------------------------------------------------------------------
# Independent page counting — the Stage 0 lesson
# --------------------------------------------------------------------------

def count_pages(path: Path) -> int | None:
    """Count a PDF's pages without Docling.

    Returns ``None`` for formats that have no page concept, which is different
    from zero and must stay different: a DOCX has no pages, a PDF with zero
    pages is broken.
    """
    if path.suffix.lower() != ".pdf":
        return None
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover - pypdfium2 ships with docling
        log.warning("pypdfium2 unavailable; cannot verify page count for %s", path)
        return None
    try:
        pdf = pdfium.PdfDocument(str(path))
        try:
            return len(pdf)
        finally:
            pdf.close()
    except Exception as exc:  # noqa: BLE001 - any failure means "unknown"
        log.warning("independent page count failed for %s: %s", path, exc)
        return None


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------

_CONVERTER_CACHE: dict[str, Any] = {}


def build_converter(pipeline: str = "native"):
    """Build (and cache) a Docling converter.

    ``native`` is the default and runs no models. See
    ``docs/STAGE_0_FINDINGS.md`` for why.
    """
    if pipeline in _CONVERTER_CACHE:
        return _CONVERTER_CACHE[pipeline]

    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat

    if pipeline == "standard":
        converter = DocumentConverter()
    else:
        from docling.datamodel.pipeline_options import NativePdfPipelineOptions
        from docling.pipeline.native_pdf_pipeline import NativePdfPipeline

        converter = DocumentConverter(format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=NativePdfPipeline,
                pipeline_options=NativePdfPipelineOptions(),
            )
        })
    _CONVERTER_CACHE[pipeline] = converter
    return converter


def _iter_items(doc) -> Iterable[tuple[Any, int]]:
    """Reading order. Never touches ``doc.furniture`` (deprecated, warns)."""
    yield from doc.iterate_items()


def _item_text(item) -> str:
    """Text for a text item or a table.

    Tables are serialised row by row with a delimiter, preserving cell order, so
    a quoted cell value is still findable and its coordinates are recoverable.
    """
    text = getattr(item, "text", None)
    if text:
        return text

    data = getattr(item, "data", None)
    cells = getattr(data, "table_cells", None) if data is not None else None
    if not cells:
        return ""

    rows: dict[int, list[tuple[int, str]]] = {}
    for cell in cells:
        row = getattr(cell, "start_row_offset_idx", 0) or 0
        col = getattr(cell, "start_col_offset_idx", 0) or 0
        rows.setdefault(row, []).append((col, (getattr(cell, "text", "") or "").strip()))
    lines = []
    for row_index in sorted(rows):
        ordered = [text for _, text in sorted(rows[row_index])]
        lines.append(" | ".join(ordered))
    return "\n".join(lines)


def _first_prov(item) -> tuple[int | None, dict | None]:
    provs = getattr(item, "prov", None) or []
    if not provs:
        return None, None
    prov = provs[0]
    page_no = getattr(prov, "page_no", None)
    bbox = getattr(prov, "bbox", None)
    bbox_dict = None
    if bbox is not None:
        try:
            bbox_dict = {k: v for k, v in bbox.model_dump(mode="json").items()
                         if isinstance(v, (int, float, str))}
        except Exception:  # pragma: no cover
            bbox_dict = None
    return page_no, bbox_dict


def build_passages(doc, doc_id: str) -> tuple[list[Passage], str]:
    """Walk reading order and build our own passage stream and document text.

    This is where Datanaut's anchor precision comes from. Docling's ``charspan``
    is item-local and useless as a document coordinate (Stage 0), so we
    concatenate item text in reading order and record exact offsets into the
    result. The concatenated text is stored, so those offsets are replayable and
    independently verifiable by the audit.
    """
    passages: list[Passage] = []
    parts: list[str] = []
    cursor = 0
    ordinal = 0

    for item, level in _iter_items(doc):
        text = _item_text(item)
        if not text or not text.strip():
            continue
        page_no, bbox = _first_prov(item)
        start = cursor
        end = start + len(text)
        passages.append(Passage(
            id=f"{doc_id}_p{ordinal:05d}",
            document_id=doc_id,
            ordinal=ordinal,
            text=text,
            char_start=start,
            char_end=end,
            item_ref=getattr(item, "self_ref", None),
            label=str(getattr(item, "label", "") or "") or None,
            level=int(level or 0),
            page_no=page_no,
            bbox=bbox,
        ))
        parts.append(text)
        cursor = end + 1  # the newline join adds one character
        ordinal += 1

    return passages, "\n".join(parts)


def _convert(path: Path, pipeline: str, page_range: tuple[int, int] | None = None):
    converter = build_converter(pipeline)
    kwargs: dict[str, Any] = {"raises_on_error": False}
    if page_range is not None:
        kwargs["page_range"] = page_range
    return converter.convert(str(path), **kwargs)


# --------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------

class Ingestor:
    """Converts a folder of documents into :class:`SourceDocument` objects."""

    def __init__(self, config: IngestConfig | None = None,
                 archive_dir: Path | None = None) -> None:
        self.config = config or IngestConfig()
        self.archive_dir = Path(archive_dir) if archive_dir else None
        #: content hash -> the source path first seen with it. Keyed by path,
        #: not by id, because a document id is derived from the hash: two
        #: different files with identical content share an id, and re-reading
        #: the *same* file is not a duplicate at all.
        self._seen: dict[str, str] = {}

    @classmethod
    def from_settings(cls, settings: Settings, archive_dir: Path | None = None) -> Ingestor:
        return cls(settings.ingest, archive_dir)

    # -- public ------------------------------------------------------------

    def ingest_folder(self, input_path: Path,
                      progress=None) -> list[SourceDocument]:
        paths = discover(Path(input_path))
        documents: list[SourceDocument] = []
        for index, path in enumerate(paths, start=1):
            if progress:
                progress({"stage": "ingest", "done": index - 1, "total": len(paths),
                          "message": path.name})
            documents.append(self.ingest_file(path))
        if progress:
            progress({"stage": "ingest", "done": len(paths), "total": len(paths),
                      "message": "converted"})
        return documents

    def ingest_file(self, path: Path) -> SourceDocument:
        path = Path(path)
        content_hash = hash_file(path)
        doc_id = document_id(content_hash)

        document = SourceDocument(
            id=doc_id,
            source_path=str(path),
            filename=path.name,
            content_hash=content_hash,
            mimetype=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            byte_size=path.stat().st_size,
        )

        # Duplicate detection by content hash. A duplicate is recorded, never
        # silently dropped — the coverage ledger must account for every input.
        # Re-reading the same path is a re-ingest, not a duplicate.
        first_seen = self._seen.get(content_hash)
        if first_seen is not None and first_seen != str(path):
            document.duplicate_of = doc_id
            document.state = EMPTY
            document.reason = "duplicate_content"
            return document
        self._seen[content_hash] = str(path)

        # Independent page count FIRST. This is the number everything is
        # reconciled against.
        expected_pages = count_pages(path)
        document.page_count = expected_pages

        try:
            self._convert_document(document, path, expected_pages)
        except Exception as exc:  # noqa: BLE001 - a failure is a state, not a crash
            log.warning("conversion failed for %s: %s", path.name, exc)
            document.state = FAILED
            document.error = f"{type(exc).__name__}: {exc}"
            document.page_outcomes = [
                PageOutcome(page_no=n, state=FAILED, error=document.error)
                for n in range(1, (expected_pages or 0) + 1)
            ]
            return document

        document.order_flags = order_check.assess(document.passages, self.config)
        return document

    # -- internals ---------------------------------------------------------

    def _convert_document(self, document: SourceDocument, path: Path,
                          expected_pages: int | None) -> None:
        result = _convert(path, self.config.pdf_pipeline)
        status = str(getattr(result, "status", "")).lower()

        if "failure" in status or getattr(result, "document", None) is None:
            errors = "; ".join(str(getattr(e, "error_message", e))
                               for e in (getattr(result, "errors", None) or []))
            # Whole-document failure. Try page by page and keep what works.
            if expected_pages and expected_pages > 1:
                self._recover_page_by_page(document, path, expected_pages,
                                           whole_doc_error=errors)
                return
            document.state = FAILED
            document.error = errors or "conversion failed with no error detail"
            document.page_outcomes = [
                PageOutcome(page_no=n, state=FAILED, error=document.error)
                for n in range(1, (expected_pages or 0) + 1)
            ]
            return

        doc = result.document
        document.docling_page_count = len(getattr(doc, "pages", {}) or {}) or None
        document.passages, document.text = build_passages(doc, document.id)
        self._archive(document, doc)

        # Reconcile against the independent count. This is the check that
        # catches Docling's silent truncation.
        pages_present = self._pages_with_content(document.passages)

        if expected_pages is None:
            # Pageless format: one document-level outcome, no page rows.
            document.state = YIELDED if document.passages else EMPTY
            document.reason = None if document.passages else "no_extractable_text"
            return

        missing = [n for n in range(1, expected_pages + 1) if n not in pages_present]
        if missing:
            log.warning(
                "%s: Docling returned %s of %d pages; recovering %d dropped page(s)",
                path.name, sorted(pages_present) or "none", expected_pages, len(missing))
            self._recover_pages(document, path, missing)

        document.page_outcomes = self._page_outcomes(document, expected_pages)
        document.state = YIELDED if document.passages else EMPTY
        if not document.passages:
            document.reason = "no_extractable_text"

    def _pages_with_content(self, passages: list[Passage]) -> set[int]:
        return {p.page_no for p in passages if p.page_no is not None}

    def _recover_pages(self, document: SourceDocument, path: Path,
                       missing: list[int]) -> None:
        """Convert each dropped page on its own and keep whatever succeeds.

        A page that fails here is recorded as ``failed`` — a visible, countable
        loss. What must never happen is a document quietly losing half its
        content while the ledger says ``yielded``.
        """
        for page_no in missing:
            try:
                result = _convert(path, self.config.pdf_pipeline,
                                  page_range=(page_no, page_no))
                status = str(getattr(result, "status", "")).lower()
                if "failure" in status or getattr(result, "document", None) is None:
                    raise IngestError(
                        "; ".join(str(getattr(e, "error_message", e))
                                  for e in (getattr(result, "errors", None) or []))
                        or "page conversion failed")
                recovered, _ = build_passages(result.document, document.id)
                self._append_passages(document, recovered, page_no)
            except Exception as exc:  # noqa: BLE001
                document.page_outcomes.append(PageOutcome(
                    page_no=page_no, state=FAILED,
                    reason="page_dropped_by_converter",
                    error=f"{type(exc).__name__}: {exc}"))

    def _recover_page_by_page(self, document: SourceDocument, path: Path,
                              expected_pages: int, whole_doc_error: str) -> None:
        """Whole document failed. Convert page ranges and keep what succeeds."""
        document.docling_page_count = 0
        for page_no in range(1, expected_pages + 1):
            try:
                result = _convert(path, self.config.pdf_pipeline,
                                  page_range=(page_no, page_no))
                status = str(getattr(result, "status", "")).lower()
                if "failure" in status or getattr(result, "document", None) is None:
                    raise IngestError("page conversion failed")
                recovered, _ = build_passages(result.document, document.id)
                self._append_passages(document, recovered, page_no)
            except Exception as exc:  # noqa: BLE001
                document.page_outcomes.append(PageOutcome(
                    page_no=page_no, state=FAILED,
                    reason="page_conversion_failed",
                    error=f"{type(exc).__name__}: {exc}"))

        document.text = "\n".join(p.text for p in document.passages)
        document.page_outcomes = self._page_outcomes(document, expected_pages)

        if document.passages:
            document.state = YIELDED
            document.error = (f"whole-document conversion failed "
                              f"({whole_doc_error}); recovered page by page")
        else:
            document.state = FAILED
            document.error = whole_doc_error or "no page could be converted"

    def _append_passages(self, document: SourceDocument, recovered: list[Passage],
                         page_no: int) -> None:
        """Add recovered passages, renumbering and re-offsetting into our text."""
        cursor = max((p.char_end for p in document.passages), default=-1) + 1
        ordinal = len(document.passages)
        for passage in recovered:
            text = passage.text
            document.passages.append(Passage(
                id=f"{document.id}_p{ordinal:05d}",
                document_id=document.id,
                ordinal=ordinal,
                text=text,
                char_start=cursor,
                char_end=cursor + len(text),
                item_ref=passage.item_ref,
                label=passage.label,
                level=passage.level,
                # A page-range conversion reports page 1; the real page number
                # is the one we asked for.
                page_no=page_no,
                bbox=passage.bbox,
            ))
            cursor += len(text) + 1
            ordinal += 1
        document.text = "\n".join(p.text for p in document.passages)

    def _page_outcomes(self, document: SourceDocument,
                       expected_pages: int) -> list[PageOutcome]:
        """One outcome per page that structurally exists. Rule 2."""
        failed = {o.page_no: o for o in document.page_outcomes if o.state == FAILED}
        counts: dict[int, int] = {}
        for passage in document.passages:
            if passage.page_no is not None:
                counts[passage.page_no] = counts.get(passage.page_no, 0) + 1

        outcomes: list[PageOutcome] = []
        for page_no in range(1, expected_pages + 1):
            if page_no in failed:
                outcomes.append(failed[page_no])
            elif counts.get(page_no):
                outcomes.append(PageOutcome(page_no=page_no, state=YIELDED,
                                            passage_count=counts[page_no]))
            else:
                outcomes.append(PageOutcome(
                    page_no=page_no, state=EMPTY,
                    reason="no_text_layer",
                ))
        return outcomes

    def _archive(self, document: SourceDocument, doc) -> None:
        """Persist Docling's own dict so a run can be re-audited without
        re-converting."""
        if self.archive_dir is None:
            return
        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            target = self.archive_dir / f"{document.id}.docling.json"
            target.write_text(json.dumps(doc.export_to_dict(), default=str))
            document.raw_path = str(target)
        except Exception:  # pragma: no cover - archiving must not break a run
            log.exception("failed to archive %s", document.id)
