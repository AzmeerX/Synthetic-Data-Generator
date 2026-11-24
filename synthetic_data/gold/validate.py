"""Validation for the three gold artifacts (§8.7 rules 5 and 6).

The v1 file is checked by the **vendored** validator — that is the whole point of
keeping it byte-shape-compatible, and re-implementing its rules here would only
create a second, weaker opinion.  The v2 file and the alignment record are checked
here, because they are V3 additions:

* every quote is an exact substring of the parser text and lies inside the sentence
  it cites;
* every canonical id resolves against the document and the world;
* a render location exists and respects the page rule (mandatory for PDF, null for
  pageless formats);
* a currency claimed in the model is only claimed as *extracted* when the parser
  text actually contains it;
* a derived value names its derivation inputs and they resolve.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from synth.eval.gold import GoldSet, load_gold_file, validate as vendored_validate

from synthetic_data.alignment import EVIDENCE_KINDS
from synthetic_data.gold.model import AlignmentArtifact, GoldV2
from synthetic_data.workbook_alignment import currency_in_quote

__all__ = [
    "GoldValidationError",
    "GoldReport",
    "ontology_path",
    "load_ontology_v3",
    "validate_v1",
    "validate_v2",
    "validate_alignment",
    "validate_pair",
]

PAGE_KINDS = {"physical", "logical"}
PAGELESS_FORMATS = {"docx", "xlsx", "txt"}


class GoldValidationError(RuntimeError):
    """A gold artifact claims something its own evidence does not support."""


class GoldReport(dict):
    """``{"ok": bool, "errors": [...], "counts": {...}}`` with attribute access."""

    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))

    @property
    def errors(self) -> list[str]:
        return list(self.get("errors") or [])


def ontology_path() -> Path:
    from synth.config import REPO_ROOT
    return Path(REPO_ROOT) / "configs" / "synth" / "ontology" / "corporate_legal.v3.yaml"


def load_ontology_v3():
    from synth.ontology import load_ontology
    return load_ontology(ontology_path())


# ---------------------------------------------------------------------------
def validate_v1(store, path: str | Path, ontology=None) -> GoldReport:
    """The vendored validator, against the V3 ontology pack (DEC-3, B3)."""
    report = vendored_validate(store, Path(path),
                               ontology=ontology if ontology is not None else load_ontology_v3())
    return GoldReport(ok=bool(report.get("ok")), errors=list(report.get("errors") or [])
                      + [f"NOT_FOUND {item['item']}: {item['quote']!r}"
                         for item in report.get("not_found") or []]
                      + [f"SENTENCE_MISMATCH {item['item']} -> {item['found_in']}"
                         for item in report.get("sentence_mismatch") or []],
                      counts=dict(report.get("grounding") or {}),
                      items=report.get("items", 0), doc_id=report.get("doc_id"))


# ---------------------------------------------------------------------------
def validate_v2(artifact: GoldV2, *, view, render=None, canonical=None,
                document=None, world=None, row_quotes=None) -> list[str]:
    """Every claim the v2 artifact makes, checked against the parser and the model."""
    errors: list[str] = []
    text = view.text
    sentences = {row["sentence_id"]: row for row in view.sentences}
    node_ids = set(document.node_ids()) if document is not None else set()
    canonical_ids = ({sentence.sentence_id for sentence in canonical.sentences}
                     if canonical is not None else set())
    known_facts = set(getattr(world, "facts", {}) or {})
    derived_ids = set()
    if document is not None and getattr(document, "workbook", None) is not None:
        derived_ids = set(document.workbook.derived_fact_ids())

    entity_ids = [entity.id for entity in artifact.entities]
    edge_ids = [edge.id for edge in artifact.edges]
    prop_ids = [prop.id for prop in artifact.propositions]
    for label, ids in (("entity", entity_ids), ("edge", edge_ids),
                       ("proposition", prop_ids)):
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        if duplicates:
            errors.append(f"duplicate {label} ids: {duplicates}")
    entities = set(entity_ids)

    for edge in artifact.edges:
        for end in (edge.subject, edge.object):
            if end not in entities:
                errors.append(f"edge {edge.id}: unknown entity {end}")
        if not edge.evidence:
            errors.append(f"edge {edge.id}: no evidence")
        if edge.canonical_fact_id and known_facts and \
                edge.canonical_fact_id not in known_facts:
            errors.append(f"edge {edge.id}: canonical fact does not resolve")
    for prop in artifact.propositions:
        if prop.speaker and prop.speaker not in entities:
            errors.append(f"proposition {prop.id}: unknown speaker {prop.speaker}")
        if not prop.evidence:
            errors.append(f"proposition {prop.id}: no evidence")
        canonical_fact = prop.canonical_fact_id
        if canonical_fact and known_facts and canonical_fact not in known_facts \
                and canonical_fact not in derived_ids:
            errors.append(f"proposition {prop.id}: canonical fact does not resolve")
        if prop.derived_from:
            if document is None or getattr(document, "workbook", None) is None:
                errors.append(f"proposition {prop.id}: derived value outside a workbook")
            else:
                known_cells = {cell.cell_id for cell in _cells(document)}
                unknown = [cell for cell in prop.derived_from if cell not in known_cells]
                if unknown:
                    errors.append(
                        f"proposition {prop.id}: derivation inputs do not resolve: {unknown}")

    for label, items in _evidence_items(artifact):
        errors.extend(_check_evidence(label, items, text, sentences, render,
                                      node_ids, canonical_ids, row_quotes))

    for time_ref in artifact.time_refs:
        errors.extend(_check_evidence(f"time_ref {time_ref.id}", [time_ref.evidence],
                                      text, sentences, render, node_ids, canonical_ids,
                                      row_quotes))

    if artifact.first_idx > artifact.last_idx:
        errors.append("first_idx > last_idx")
    if artifact.doc.text_sha256:
        import hashlib
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest != artifact.doc.text_sha256:
            errors.append("text_sha256 does not match the parser text")
    return errors


def _cells(document):
    from synthetic_data.model import workbook_cells
    return workbook_cells(document.workbook)


def _evidence_items(artifact: GoldV2):
    for entity in artifact.entities:
        yield f"entity {entity.id}", entity.mentions
    for edge in artifact.edges:
        yield f"edge {edge.id}", edge.evidence
    for prop in artifact.propositions:
        yield f"proposition {prop.id}", prop.evidence


def _check_evidence(label: str, items, text: str, sentences: dict, render,
                    node_ids: set[str], canonical_ids: set[str],
                    row_quotes: dict[str, str] | None = None) -> list[str]:
    errors: list[str] = []
    for position, item in enumerate(items):
        where = f"{label} evidence {position}"
        if not item.quote:
            errors.append(f"{where}: empty quote")
            continue
        if item.quote not in text:
            errors.append(f"{where}: quote is not an exact parser substring")
            continue
        row = sentences.get(item.sentence_id)
        if row is None:
            errors.append(f"{where}: unknown parser sentence {item.sentence_id!r}")
            continue
        body = text[row["char_start"]:row["char_end"]]
        if item.quote not in body:
            errors.append(f"{where}: quote is not inside {item.sentence_id}")
        if item.alignment_kind and item.alignment_kind not in EVIDENCE_KINDS:
            errors.append(f"{where}: alignment {item.alignment_kind!r} cannot ground a fact")
        if node_ids and item.canonical_node_id and item.canonical_node_id not in node_ids:
            errors.append(f"{where}: canonical node does not resolve")
        if canonical_ids:
            unknown = [value for value in item.canonical_sentence_ids
                       if value not in canonical_ids]
            if unknown:
                errors.append(f"{where}: canonical sentences do not resolve: {unknown}")
        errors.extend(_check_loc(where, item.render_loc, render))
        if item.currency and item.currency_in_quote is not None:
            actual = currency_in_quote(item.currency, item.quote)
            if actual != item.currency_in_quote:
                errors.append(
                    f"{where}: claims currency_in_quote={item.currency_in_quote} "
                    f"but the parser text says {actual}")
        if item.currency_in_row is not None and item.canonical_node_id:
            if row_quotes is None:
                errors.append(f"{where}: currency_in_row cannot be checked without rows")
            else:
                row_quote = row_quotes.get(item.canonical_node_id, "")
                actual = currency_in_quote(item.currency or "", row_quote)
                if actual != item.currency_in_row:
                    errors.append(
                        f"{where}: claims currency_in_row={item.currency_in_row} "
                        f"but the row text says {actual}")
    return errors


def _check_loc(where: str, loc: dict, render) -> list[str]:
    errors: list[str] = []
    if not loc:
        return [f"{where}: no render location"]
    fmt = getattr(render, "format", None)
    page_kind = getattr(getattr(render, "capabilities", None), "page_kind", None)
    page = loc.get("page")
    if page_kind in PAGE_KINDS:
        if page is None:
            errors.append(f"{where}: a {fmt} evidence item must carry a physical page")
    else:
        if page is not None:
            errors.append(f"{where}: a {fmt} evidence item must not invent a page")
        if loc.get("pageless") is not True:
            errors.append(f"{where}: a pageless format must record pageless=True")
    if fmt == "xlsx" and not loc.get("sheet_name"):
        errors.append(f"{where}: an XLSX evidence item must name its sheet")
    if fmt == "xlsx" and loc.get("bbox") is not None:
        errors.append(f"{where}: a workbook has no bounding boxes")
    return errors


# ---------------------------------------------------------------------------
def validate_alignment(artifact: AlignmentArtifact, *, view, render=None
                       ) -> list[str]:
    errors: list[str] = []
    sentences = {row["sentence_id"]: row for row in view.sentences}
    text = view.text
    counts: dict[str, int] = {}
    for unit in artifact.units:
        counts[unit.kind] = counts.get(unit.kind, 0) + 1
        if unit.kind != "missing" and not unit.parser_sentence_ids:
            errors.append(f"{unit.canonical_id}: {unit.kind} without a parser sentence")
        for sentence_id in unit.parser_sentence_ids:
            if sentence_id not in sentences:
                errors.append(f"{unit.canonical_id}: unknown parser sentence {sentence_id}")
        if (unit.char_start is not None and unit.char_end is not None
                and unit.char_end > len(text)):
            errors.append(f"{unit.canonical_id}: character range outside the document")
    if counts != artifact.counts:
        errors.append(f"counts {artifact.counts} do not match the units {counts}")
    if render is not None and artifact.render_sha256:
        if artifact.render_sha256 != getattr(render, "sha256", None):
            errors.append("render_sha256 does not match the rendered artifact")
    return errors


# ---------------------------------------------------------------------------
def validate_pair(v1_path: str | Path, v2_path: str | Path,
                  alignment_path: str | Path, *, store, view, render=None,
                  canonical=None, document=None, world=None, ontology=None,
                  row_quotes=None) -> GoldReport:
    """Validate all three artifacts; ``ok`` only when every one of them passes."""
    errors: list[str] = []
    v1 = validate_v1(store, v1_path, ontology)
    errors.extend(v1.errors)
    v2 = GoldV2.model_validate(json_load(v2_path))
    errors.extend(validate_v2(v2, view=view, render=render, canonical=canonical,
                              document=document, world=world, row_quotes=row_quotes))
    alignment = AlignmentArtifact.model_validate(json_load(alignment_path))
    errors.extend(validate_alignment(alignment, view=view, render=render))
    return GoldReport(ok=not errors, errors=errors, counts=v1.get("counts", {}),
                      items=v1.get("items", 0))


def json_load(path: str | Path) -> Any:
    import json
    return json.loads(Path(path).read_text(encoding="utf-8"))
