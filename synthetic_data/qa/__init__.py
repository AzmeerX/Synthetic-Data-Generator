"""Phase H: QA benchmark generation and independent semantic validation."""

from synthetic_data.qa.build import CorpusTexts, QASource, generate, \
    generate_unanswerable
from synthetic_data.qa.model import QASet, QAQuestion, QAEvidence, UnanswerableProof
from synthetic_data.qa.taxonomy import QUESTION_TYPES, UNANSWERABLE_KINDS
from synthetic_data.qa.validate import ValidationInput, ValidationReport, \
    validate_corpus, validate_set

__all__ = ["CorpusTexts", "QASource", "generate", "generate_unanswerable", "QASet",
           "QAQuestion", "QAEvidence", "UnanswerableProof", "QUESTION_TYPES",
           "UNANSWERABLE_KINDS", "ValidationInput", "ValidationReport",
           "validate_corpus", "validate_set"]
