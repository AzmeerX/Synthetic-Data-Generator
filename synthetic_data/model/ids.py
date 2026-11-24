"""Deterministic canonical identifier allocation (architecture §5).

The architecture fixes the derivation exactly::

    id(prefix, scope_path) = prefix + ":" + b32(blake2b(seed_key || "|" || scope_path, digest_size=8))
    seed_key = blake2b(str(seed) || ":" || corpus_name, digest_size=16)

This module implements that formula, plus an explicit collision policy.

Design notes
------------
* Identity is a pure function of ``(seed, corpus, prefix, scope_path)``. It never
  depends on renderer output, parser output, iteration order, wall-clock time or
  ``hash()`` (which is salted per process).
* Scope paths are human-readable and hierarchical (``00042/03.02``), so an id is
  debuggable without a lookup table.
* Collisions are detected by :class:`IdRegistry` over the *whole* set of specs it
  is asked to allocate, then resolved deterministically by sorting the colliding
  scope paths. Resolution therefore does not depend on the order in which specs
  were registered.
* Renderer and parser identifiers are deliberately **not** produced here. They
  are artifact-scoped and are modelled in :mod:`synthetic_data.model.render`.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Mapping

__all__ = [
    "IdCollision",
    "IdCollisionError",
    "IdRegistry",
    "derive_id",
    "seed_key",
    "document_scope",
    "section_scope",
    "block_scope",
    "table_scope",
    "sentence_scope",
    "entity_scope",
    "fact_scope",
    "thread_scope",
]

MAX_ATTEMPT = 64


class IdCollisionError(RuntimeError):
    """Raised when a collision cannot be resolved deterministically."""


def seed_key(seed: int, corpus_name: str) -> bytes:
    """Corpus-level key: ``blake2b(str(seed) + ":" + corpus_name, digest_size=16)``."""
    if not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if not corpus_name:
        raise ValueError("corpus_name must be non-empty")
    return hashlib.blake2b(f"{seed}:{corpus_name}".encode("utf-8"), digest_size=16).digest()


def _encode(digest: bytes) -> str:
    """Lowercase base32 without padding (RFC 4648 alphabet)."""
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def derive_id(prefix: str, scope_path: str, key: bytes, *, attempt: int = 0,
              digest_size: int = 8) -> str:
    """Single-id derivation with no registry and therefore no collision check.

    ``prefix`` is the readable object family (``ent:company``, ``doc``, ``blk``);
    ``scope_path`` is the stable readable path.  ``attempt`` is only non-zero when
    a caller has already detected a collision and wants the next candidate.
    """
    if not prefix:
        raise ValueError("prefix must be non-empty")
    if not scope_path:
        raise ValueError("scope_path must be non-empty")
    if attempt < 0 or attempt >= MAX_ATTEMPT:
        raise ValueError(f"attempt must be in [0, {MAX_ATTEMPT})")
    if digest_size < 1:
        raise ValueError("digest_size must be >= 1")
    payload = f"{prefix}|{scope_path}|{attempt}".encode("utf-8")
    tag = _encode(hashlib.blake2b(key + b"|" + payload, digest_size=digest_size).digest())
    return f"{prefix}:{scope_path}:{tag}"


@dataclass(frozen=True)
class IdCollision:
    """A detected hash-tag collision and the deterministic resolution applied.

    Canonical ids embed a readable scope path, so two different objects can never
    produce the same *full* id.  The collision that matters is on the hash **tag**:
    two objects sharing a tag weakens the tag as a fingerprint, so the registry
    bumps the attempt counter for the later scope (sorted) until every tag in the
    corpus is unique.
    """

    base_tag: str
    scopes: tuple[str, ...]
    resolved: Mapping[str, str]

    def as_dict(self) -> dict:
        return {"base_tag": self.base_tag, "scopes": list(self.scopes),
                "resolved": dict(self.resolved)}


@dataclass
class IdRegistry:
    """Allocates canonical ids for one corpus.

    Usage::

        reg = IdRegistry(seed=7, corpus_name="v3_pilot")
        reg.register("doc", "00042")
        reg.register("ent:company", "company:0017")
        ids = reg.build()                      # deterministic mapping
        ids[("doc", "00042")]
    """

    seed: int
    corpus_name: str
    digest_size: int = 8
    _specs: set[tuple[str, str]] = field(default_factory=set, repr=False)
    _ids: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)
    _collisions: list[IdCollision] = field(default_factory=list, repr=False)
    _built: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self._key = seed_key(self.seed, self.corpus_name)
        if self.digest_size < 1:
            raise ValueError("digest_size must be >= 1")

    # -- registration -------------------------------------------------------
    def register(self, prefix: str, scope_path: str) -> None:
        if self._built:
            raise RuntimeError("cannot register ids after build()")
        if not prefix or not scope_path:
            raise ValueError("prefix and scope_path must be non-empty")
        self._specs.add((prefix, scope_path))

    def register_many(self, specs: Iterable[tuple[str, str]]) -> None:
        for prefix, scope in specs:
            self.register(prefix, scope)

    # -- allocation ---------------------------------------------------------
    def build(self) -> dict[tuple[str, str], str]:
        """Assign every registered id, resolving collisions deterministically.

        Algorithm: compute base ids for all specs; group by base id; for a group
        with more than one distinct scope, sort the scopes and assign attempt
        0..n-1 in that order.  Sorting by scope (not registration order) is what
        makes the result independent of how the caller discovered the objects.
        """
        if self._built:
            return dict(self._ids)
        self._ids.clear()
        self._collisions.clear()

        # Group by hash tag, then assign unique tags deterministically.
        by_tag: dict[str, list[tuple[str, str]]] = {}
        for prefix, scope in sorted(self._specs):
            base = derive_id(prefix, scope, self._key, attempt=0,
                             digest_size=self.digest_size)
            tag = base.rsplit(":", 1)[1]
            by_tag.setdefault(tag, []).append((prefix, scope))

        used_tags: set[str] = set()
        for base_tag, specs in sorted(by_tag.items()):
            specs = sorted(specs)
            resolved: dict[str, str] = {}
            bumped = False
            for prefix, scope in specs:
                attempt = 0
                while True:
                    if attempt >= MAX_ATTEMPT:  # pragma: no cover - absurd digest only
                        raise IdCollisionError(f"unresolvable collision for {prefix}:{scope}")
                    candidate = derive_id(prefix, scope, self._key, attempt=attempt,
                                          digest_size=self.digest_size)
                    tag = candidate.rsplit(":", 1)[1]
                    if tag not in used_tags:
                        break
                    attempt += 1
                bumped = bumped or attempt > 0
                used_tags.add(tag)
                self._ids[(prefix, scope)] = candidate
                resolved[f"{prefix}:{scope}"] = candidate
            if len(specs) > 1 or bumped:
                self._collisions.append(IdCollision(
                    base_tag=base_tag,
                    scopes=tuple(f"{p}:{s}" for p, s in specs),
                    resolved=resolved,
                ))

        self._built = True
        return dict(self._ids)

    # -- access -------------------------------------------------------------
    def id_for(self, prefix: str, scope_path: str) -> str:
        key = (prefix, scope_path)
        if not self._built:
            self.build()
        try:
            return self._ids[key]
        except KeyError as exc:  # pragma: no cover - programming error
            raise KeyError(f"unregistered scope {prefix}:{scope_path}") from exc

    @property
    def collisions(self) -> tuple[IdCollision, ...]:
        if not self._built:
            self.build()
        return tuple(self._collisions)

    def mapping(self) -> dict[str, str]:
        """Readable ``"prefix:scope" -> id`` mapping, for manifests and tests."""
        if not self._built:
            self.build()
        return {f"{p}:{s}": v for (p, s), v in sorted(self._ids.items())}


# --------------------------------------------------------------------------
# readable scope-path builders (inputs to the registry, never ids by themselves)
# --------------------------------------------------------------------------
def document_scope(ordinal: int) -> str:
    if ordinal < 0:
        raise ValueError("ordinal must be >= 0")
    return f"{ordinal:05d}"


def section_scope(doc_scope: str, number_path: str) -> str:
    return f"{doc_scope}/{number_path}" if number_path else doc_scope


def block_scope(doc_scope: str, section_path: str, index: int) -> str:
    return f"{doc_scope}/{section_path}.{index:02d}"


def table_scope(doc_scope: str, section_path: str, index: int) -> str:
    return f"{doc_scope}/{section_path}.t{index:02d}"


def sentence_scope(block: str, index: int) -> str:
    return f"{block}.s{index:02d}"


def entity_scope(kind: str, ordinal: int) -> str:
    return f"{kind}:{ordinal:04d}"


def fact_scope(kind: str, doc_scope: str, ordinal: int) -> str:
    return f"{kind}:{doc_scope}:{ordinal:02d}"


def thread_scope(corpus_name: str, ordinal: int) -> str:
    return f"{corpus_name}:{ordinal:04d}"
