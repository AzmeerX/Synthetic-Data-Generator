"""Generator components: world, documents, gold annotations, QA and batches."""

from synthetic_data.generator.annotations import (
    GoldBuildResult,
    GoldGenerationError,
    build_gold,
    write_and_validate_gold,
)
from synthetic_data.generator.documents import (
    DocumentPlan,
    build_document_plan,
    page_counts,
)
from synthetic_data.generator.pipeline import CorpusResult, generate_corpus
from synthetic_data.generator.queries import (
    CorpusIndex,
    DocumentContext,
    QueryGenerationError,
    generate_qa_dataset,
)
from synthetic_data.generator.world import World, build_world

__all__ = [
    "CorpusIndex",
    "CorpusResult",
    "DocumentContext",
    "DocumentPlan",
    "GoldBuildResult",
    "GoldGenerationError",
    "QueryGenerationError",
    "World",
    "build_document_plan",
    "build_gold",
    "build_world",
    "generate_corpus",
    "generate_qa_dataset",
    "page_counts",
    "write_and_validate_gold",
]
