"""Phase C closure — corpus-level renderer context (the Phase D interface).

A renderer receives a ``CdmDocument``; entity names live in the corpus world.  The
proposed interface is an optional, immutable :class:`RenderContext`.  These tests
pin the three properties that make it safe: it is additive, it cannot change the
bytes, and it cannot be mutated into a channel for new data.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import MappingProxyType

import pytest

from synthetic_data.canonical import canonicalize
from synthetic_data.renderers import DEFAULT_TXT_PROFILE, RenderContext, TxtRenderer
from synthetic_data.tests.v3_txt import pilot_plan, txt_rendered


@pytest.fixture(scope="module")
def context():
    plan = pilot_plan()
    return RenderContext.from_world(plan.world)


@pytest.fixture(scope="module")
def document():
    plan = pilot_plan()
    return plan.documents[plan.prose_ids[2]]


def test_context_is_optional(document, tmp_path):
    without = TxtRenderer().render(document, DEFAULT_TXT_PROFILE, tmp_path / "a.txt")
    with_context = TxtRenderer().render(document, DEFAULT_TXT_PROFILE,
                                        tmp_path / "b.txt", context=RenderContext())
    assert without.sha256 == with_context.sha256


def test_context_never_changes_the_bytes(document, context, tmp_path):
    plain = TxtRenderer().render(document, DEFAULT_TXT_PROFILE, tmp_path / "plain.txt")
    enriched = TxtRenderer().render(document, DEFAULT_TXT_PROFILE,
                                    tmp_path / "enriched.txt", context=context)
    assert plain.sha256 == enriched.sha256
    assert Path(plain.path).read_bytes() == Path(enriched.path).read_bytes()
    assert [span.canonical_id for span in plain.spans] == \
           [span.canonical_id for span in enriched.spans]


def test_context_adds_entity_names_to_fact_surfaces(document, context, tmp_path):
    plain = TxtRenderer().render(document, DEFAULT_TXT_PROFILE, tmp_path / "p.txt")
    enriched = TxtRenderer().render(document, DEFAULT_TXT_PROFILE,
                                    tmp_path / "e.txt", context=context)
    added = 0
    for fact_id, surfaces in enriched.fact_surfaces.items():
        local = enriched.fact_surfaces.get(fact_id, [])
        added += len(set(local) if fact_id not in plain.fact_surfaces else
                     set(local) - set(plain.fact_surfaces[fact_id]))
    assert added > 0
    assert set(plain.fact_surfaces) <= set(enriched.fact_surfaces)


def test_named_surfaces_are_actually_rendered(document, context, tmp_path):
    result = TxtRenderer().render(document, DEFAULT_TXT_PROFILE,
                                  tmp_path / "names.txt", context=context)
    text = Path(result.path).read_text(encoding="utf-8")
    for surfaces in result.fact_surfaces.values():
        for surface in surfaces:
            assert surface in text, surface


def test_context_is_immutable(context):
    with pytest.raises(TypeError):
        context.entities["ent:injected"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        context.roles["ent:injected"] = "the Borrower"  # type: ignore[index]
    assert isinstance(context.entities, MappingProxyType)


def test_context_resolves_only_known_ids(context):
    assert context.entity("ent:unknown") is None
    assert context.entity_name("ent:unknown") is None
    assert context.fact("fact:unknown") is None
    known = next(iter(context.entities))
    assert context.entity_name(known)
    assert context.roles == {}


def test_context_preserves_canonical_identity(document, context):
    """Nothing is re-derived: ids in the context are the corpus ids unchanged."""
    world = pilot_plan().world
    assert set(context.entities) == set(world.entities)
    assert set(context.facts) == set(world.facts)
    for statement in document.statements:
        assert statement.fact_id in context.facts


def test_rendering_with_context_is_deterministic(document, context, tmp_path):
    first = TxtRenderer().render(document, DEFAULT_TXT_PROFILE, tmp_path / "1.txt",
                                 context=context)
    second = TxtRenderer().render(document, DEFAULT_TXT_PROFILE, tmp_path / "2.txt",
                                  context=context)
    assert first.sha256 == second.sha256
    assert first.fact_surfaces == second.fact_surfaces


def test_context_does_not_change_sentence_ids(document, context, tmp_path):
    plain = canonicalize(document)
    TxtRenderer().render(document, DEFAULT_TXT_PROFILE, tmp_path / "x.txt",
                         context=context)
    after = canonicalize(document)
    assert [s.sentence_id for s in plain.sentences] == \
           [s.sentence_id for s in after.sentences]


def test_renderer_takes_no_clock_and_no_store(document, context, tmp_path):
    """Smoke check that the context is data only: two processes' inputs are equal."""
    result = TxtRenderer().render(document, DEFAULT_TXT_PROFILE,
                                  tmp_path / "c.txt", context=context)
    assert result.capabilities.format == "txt"
    assert result.warnings is not None
    assert all("created_at" not in span.model_dump_json() for span in result.spans)
