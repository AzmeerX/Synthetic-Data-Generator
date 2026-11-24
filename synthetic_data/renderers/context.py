"""Optional corpus-level context for renderers (Phase C closure, Phase D input).

The renderer contract receives a ``CdmDocument`` and a ``RenderProfile``.  That is
enough to write bytes and spans, but not enough to name an entity: the document
holds only ``EntityRef.entity_id``, while the canonical name lives in the corpus
world.  Rather than widen the document model (which would duplicate corpus data in
every file, §4.4), a renderer may accept an optional, read-only
:class:`RenderContext`:

    result = TxtRenderer().render(document, profile, path, context=ctx)

Rules that keep this compatible and pure:

* it is **additive and optional** — ``render(document, profile, path)`` is
  unchanged, and a renderer ignores the context if it does not need it;
* it is **read-only** — frozen dataclasses over immutable Pydantic models, no
  clock, no I/O, no generator, no rng;
* it cannot change the bytes — the context only enriches provenance
  (``fact_surfaces`` entity names), never layout or wording, so
  ``sha256(render(doc, profile))`` is identical with and without a context;
* identity is untouched — canonical ids stay the ones the document and the corpus
  already carry; nothing is re-derived from the context.

This is the smallest interface that lets a renderer resolve an entity or fact
without giving it the key, the registry or the world's mutability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from synthetic_data.model import Entity, SemanticFact

__all__ = ["RenderContext", "EMPTY_CONTEXT"]


@dataclass(frozen=True)
class RenderContext:
    """Immutable view of the corpus facts and entities a renderer may display."""

    entities: Mapping[str, Entity] = field(
        default_factory=lambda: MappingProxyType({}))
    facts: Mapping[str, SemanticFact] = field(
        default_factory=lambda: MappingProxyType({}))
    roles: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}))

    def entity(self, entity_id: str) -> Entity | None:
        return self.entities.get(entity_id)

    def entity_name(self, entity_id: str) -> str | None:
        entity = self.entities.get(entity_id)
        return entity.canonical_name if entity is not None else None

    def role(self, entity_id: str) -> str | None:
        return self.roles.get(entity_id)

    def fact(self, fact_id: str) -> SemanticFact | None:
        return self.facts.get(fact_id)

    @classmethod
    def from_world(cls, world, *, roles: Mapping[str, str] | None = None
                   ) -> "RenderContext":
        """Build a context from a ``CorpusWorld``; only ids and names cross over."""
        return cls(
            entities=MappingProxyType(dict(world.entities)),
            facts=MappingProxyType(dict(world.facts)),
            roles=MappingProxyType(dict(roles or {})))


EMPTY_CONTEXT = RenderContext()
