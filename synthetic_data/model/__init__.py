"""Public surface of the V3 canonical document model (Phase A).

Layers
------
* :mod:`synthetic_data.model.semantic` — Layer 1, format-independent facts.
* :mod:`synthetic_data.model.content`  — Layer 2, format-independent structure.
* :mod:`synthetic_data.model.workbook` — specialised spreadsheet document kind.
* :mod:`synthetic_data.model.render`   — Layer 3, provenance and render maps.
* :mod:`synthetic_data.model.corpus`   — the aggregate and cross-layer invariants.

Nothing here generates, renders, parses or annotates.  Phase A is the model and
its validation only.
"""
from .content import (
    Block,
    BlockKind,
    CdmDocument,
    ColumnSpec,
    ColumnType,
    DocKind,
    DocumentMetadata,
    Exhibit,
    FactStatement,
    Footnote,
    HeaderFooter,
    InlineRun,
    ListItem,
    MessageMeta,
    Section,
    SignatureRecord,
    Table,
    TableCell,
    TableRow,
)
from .corpus import Corpus, CorpusViolation, collect_violations
from .ids import (
    IdCollision,
    IdCollisionError,
    IdRegistry,
    block_scope,
    derive_id,
    document_scope,
    entity_scope,
    fact_scope,
    section_scope,
    seed_key,
    sentence_scope,
    table_scope,
    thread_scope,
)
from .render import (
    CanonicalSourceLoc,
    EvidenceRef,
    ParseLoc,
    RendererCapabilities,
    RenderLoc,
    RenderMap,
    RenderOccurrence,
    RenderProfile,
    RenderResult,
    RenderSpan,
    SourceFormat,
    assert_render_coverage,
)
from .semantic import (
    Condition,
    Contradiction,
    CorpusWorld,
    CrossReference,
    Entity,
    Event,
    SemanticFact,
    Thread,
    TimeScope,
    Transaction,
)
from .values import (
    AlignmentKind,
    CanonicalId,
    CellValue,
    ConditionKind,
    ConditionSatisfied,
    ContradictionDimension,
    DocumentRef,
    Emphasis,
    EntityRef,
    FactKind,
    FactStatus,
    IsoDate,
    Money,
    Percentage,
    Register,
)
from .workbook import (CdmWorkbook, CellFormula, CellRole, DerivedValue,
                       NamedRange, Sheet, Workbook, cell_role, workbook_cells)

__all__ = [
    # values
    "Money", "Percentage", "IsoDate", "EntityRef", "DocumentRef", "CellValue",
    "CanonicalId", "Emphasis", "Register", "FactKind", "FactStatus",
    "ConditionKind", "ConditionSatisfied", "ContradictionDimension", "AlignmentKind",
    # semantic
    "CorpusWorld", "Entity", "SemanticFact", "Condition", "Event", "Transaction",
    "Contradiction", "Thread", "TimeScope", "CrossReference",
    # content
    "CdmDocument", "DocumentMetadata", "MessageMeta", "Section", "Block", "BlockKind",
    "InlineRun", "ListItem", "Table", "TableRow", "TableCell", "ColumnSpec",
    "ColumnType", "FactStatement", "HeaderFooter", "Exhibit", "SignatureRecord",
    "Footnote", "DocKind",
    # workbook
    "CdmWorkbook", "Workbook", "Sheet", "CellFormula", "NamedRange", "DerivedValue",
    "CellRole", "cell_role", "workbook_cells",
    # render / provenance
    "RenderProfile", "RendererCapabilities", "CanonicalSourceLoc", "RenderLoc",
    "ParseLoc", "EvidenceRef", "RenderOccurrence", "RenderSpan", "RenderMap",
    "RenderResult", "SourceFormat", "assert_render_coverage",
    # corpus
    "Corpus", "CorpusViolation", "collect_violations",
    # ids
    "IdRegistry", "IdCollision", "IdCollisionError", "derive_id", "seed_key",
    "document_scope", "section_scope", "block_scope", "table_scope",
    "sentence_scope", "entity_scope", "fact_scope", "thread_scope",
]

__version__ = "0.1.0"
