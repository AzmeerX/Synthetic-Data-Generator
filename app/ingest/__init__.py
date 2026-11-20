"""Ingestion: Docling adapter and the internal document model."""

from app.ingest.adapter import (
    SUPPORTED_SUFFIXES, Ingestor, IngestError, count_pages, discover,
    document_id, hash_file,
)
from app.ingest.models import (
    EMPTY, FAILED, YIELDED, Anchor, OrderFlags, PageOutcome, Passage, SourceDocument,
)

__all__ = [
    "SUPPORTED_SUFFIXES", "Anchor", "EMPTY", "FAILED", "IngestError", "Ingestor", "OrderFlags",
    "PageOutcome", "Passage", "SourceDocument", "YIELDED", "count_pages",
    "discover", "document_id", "hash_file",
]
