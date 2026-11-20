"""Ontology pack: labels, entity types, relations and their constraints.

Purpose: one typed view of ``configs/synth/ontology/<pack>.yaml`` so no module
parses the YAML itself. Contract: :func:`load_ontology` -> :class:`Ontology`.
Stores written: none. Spec: P1 §8.

Entity types are two-level: a **class** (person, organization, legal, value,
place, thing, event) and a **subtype** (company, bank, judge, …). Relation
constraints are written against classes; ``any`` matches every class.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: GLiNER labels that name a time expression rather than an entity. Their
#: mentions go to U3 (S3), not to the entity table.
TIME_LABELS = {"date", "time period", "duration"}
#: GLiNER labels that are kept as mentions but never become entities on their own.
ROLE_LABELS = {"role title"}


@dataclass(frozen=True)
class RelationType:
    name: str
    family: str
    source_class: str
    target_class: str

    def allows(self, source_class: str | None, target_class: str | None) -> bool:
        def ok(want: str, have: str | None) -> bool:
            return want == "any" or have is None or want == have
        return ok(self.source_class, source_class) and ok(self.target_class, target_class)


@dataclass
class Ontology:
    pack: str
    version: int
    gliner_labels: list[str]
    entity_types: dict[str, list[str]]          # class -> subtypes
    relations: dict[str, RelationType]
    relation_aliases: dict[str, str]
    functional_relations: set[str]
    critical_relations: set[str]
    proposition_status: list[str]
    link_types: list[str]
    fiscal_year_start_month: int
    time_roles: list[str]
    honorifics_strip: set[str]
    honorifics_keep: set[str]
    raw: dict[str, Any] = field(default_factory=dict)

    # -- types -----------------------------------------------------------------
    @property
    def classes(self) -> list[str]:
        return list(self.entity_types)

    def class_of(self, type_or_subtype: str | None) -> str | None:
        """Map a class or a subtype (``bank`` -> ``organization``) to its class."""
        if not type_or_subtype:
            return None
        key = _snake(type_or_subtype)
        if key in self.entity_types:
            return key
        for cls, subs in self.entity_types.items():
            if key in subs:
                return cls
        return None

    def label_to_type(self, label: str) -> tuple[str | None, str | None]:
        """GLiNER label -> ``(class, subtype)``. Time and role labels -> ``(None, None)``."""
        if label in TIME_LABELS or label in ROLE_LABELS:
            return None, None
        key = _snake(label)
        special = {"money_amount": "money", "role_title": None}
        key = special.get(key, key)
        if key is None:
            return None, None
        if key in self.entity_types:
            subtype = key if key in self.entity_types[key] else None
            return key, subtype
        cls = self.class_of(key)
        return cls, key if cls else None

    # -- relations -------------------------------------------------------------
    @property
    def relation_names(self) -> list[str]:
        return list(self.relations)

    def canonical_relation(self, name: str | None) -> str | None:
        """Resolve aliases and spelling (``board member of`` -> ``director_of``)."""
        if not name:
            return None
        raw = name.strip()
        if raw in self.relations:
            return raw
        lowered = raw.lower()
        if lowered in self.relation_aliases:
            return self.relation_aliases[lowered]
        snake = _snake(raw)
        if snake in self.relations:
            return snake
        if snake.replace("_", " ") in self.relation_aliases:
            return self.relation_aliases[snake.replace("_", " ")]
        return None

    def relation(self, name: str) -> RelationType | None:
        canonical = self.canonical_relation(name)
        return self.relations.get(canonical) if canonical else None

    def is_functional(self, name: str) -> bool:
        return self.canonical_relation(name) in self.functional_relations

    def is_critical(self, name: str) -> bool:
        return self.canonical_relation(name) in self.critical_relations

    # -- names -------------------------------------------------------------------
    def norm_key(self, name: str) -> str:
        """Normalized name key: NFKC, casefold, punctuation out, honorifics stripped
        (but name parts such as ``syed`` kept)."""
        text = unicodedata.normalize("NFKC", name).casefold()
        text = re.sub(r"[’'`]s\b", "", text)
        text = re.sub(r"[^\w\s]", " ", text)
        tokens = [t for t in text.split() if t]
        kept = [t for t in tokens if t in self.honorifics_keep or t not in self.honorifics_strip]
        if not kept:
            kept = tokens
        return " ".join(kept)

    def fingerprint(self) -> str:
        blob = json.dumps(self.raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()


def _snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def load_ontology(path: str | Path) -> Ontology:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    relations: dict[str, RelationType] = {}
    for family, members in (data.get("relations") or {}).items():
        for name, ends in (members or {}).items():
            source, target = (ends + ["any", "any"])[:2]
            relations[name] = RelationType(name=name, family=family,
                                           source_class=source, target_class=target)
    aliases = {str(k).lower(): v for k, v in (data.get("relation_aliases") or {}).items()}
    time_cfg = data.get("time") or {}
    return Ontology(
        pack=data.get("pack", Path(path).stem),
        version=int(data.get("version", 1)),
        gliner_labels=list(data.get("gliner_labels") or []),
        entity_types={k: list(v or []) for k, v in (data.get("entity_types") or {}).items()},
        relations=relations,
        relation_aliases=aliases,
        functional_relations=set(data.get("functional_relations") or []),
        critical_relations=set(data.get("critical_relations") or []),
        proposition_status=list(data.get("proposition_status") or []),
        link_types=list(data.get("link_types") or []),
        fiscal_year_start_month=int(time_cfg.get("fiscal_year_start_month", 7)),
        time_roles=list(time_cfg.get("time_roles") or []),
        honorifics_strip={h.lower() for h in data.get("honorifics_strip") or []},
        honorifics_keep={h.lower() for h in data.get("honorifics_keep") or []},
        raw=data,
    )
