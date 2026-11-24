"""Fact surfaces shared by renderers that have no text stream of their own.

``PDF`` keeps a private copy of this logic inside its layout; DOCX (and, later,
XLSX) have no page buffer, so the same rule lives here: a fact's surfaces are the
typed values and entity names that the *rendered* sentences actually contain.
Only values that appear in a rendered sentence are recorded — a surface the
document never printed is not a surface.
"""
from __future__ import annotations

from synthetic_data.canonical import CanonicalDocument
from synthetic_data.model import CdmDocument
from synthetic_data.renderers.context import RenderContext

__all__ = ["fact_surfaces"]


def fact_surfaces(document: CdmDocument, canonical: CanonicalDocument,
                  context: RenderContext | None = None) -> dict[str, list[str]]:
    by_sentence = {sentence.sentence_id: sentence for sentence in canonical.sentences}
    surfaces: dict[str, set[str]] = {}
    for statement in document.statements:
        rendered = [by_sentence[sid].text for sid in statement.canonical_sentence_ids
                    if sid in by_sentence]
        if not rendered:
            continue
        blob = " ".join(rendered)
        values = {slot for slot in statement.value_slots.values() if slot and slot in blob}
        if context is not None:
            for mention in statement.mention_ids:
                name = context.entity_name(mention)
                if name and name in blob:
                    values.add(name)
                role = context.role(mention)
                if role and role in blob:
                    values.add(role)
        if values:
            surfaces.setdefault(statement.fact_id, set()).update(values)
    return {fact: sorted(values) for fact, values in surfaces.items()}
