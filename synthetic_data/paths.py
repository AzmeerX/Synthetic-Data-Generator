"""Portable artifact names for generated corpora.

A canonical id such as ``doc:00007:ndyafme64g7ly`` or a document type such as
``workbook:shareholder_register`` cannot appear in a filename unchanged: on Windows
the colon turns the name into an NTFS alternate data stream, on every platform it is
either illegal or confusing, and a copy to a non-NTFS filesystem silently loses the
data.  That is exactly what happened to two Phase G artifacts.

This module owns the mapping from canonical ids to filenames.  Rules:

* only ``[A-Za-z0-9._-]`` survives; every other character becomes ``_``;
* a component may not end with a space or a dot, may not be empty, and may not be a
  reserved device name (``CON``, ``NUL``, ``COM1`` …), with or without an extension;
* components are length-bounded, and a long name is shortened with a hash so two
  different ids never collapse onto one name;
* the mapping is deterministic and collision-checked, including *case-insensitive*
  collisions, because macOS and Windows would otherwise overwrite one file with
  another.

The canonical id itself is never rewritten — it lives inside the JSON.
"""
from __future__ import annotations

import hashlib
import re

__all__ = ["PathCollision", "InvalidComponent", "safe_component", "artifact_name",
           "PortablePaths", "RESERVED_NAMES"]

RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
    | {f"com{index}" for index in ("¹", "²", "³")}
    | {f"lpt{index}" for index in ("¹", "²", "³")}
)

_ALLOWED = re.compile(r"[^A-Za-z0-9._-]")
_MAX_COMPONENT = 180


class InvalidComponent(ValueError):
    """A name that cannot be made portable without losing information."""


class PathCollision(RuntimeError):
    """Two different artifacts would share one filename."""


def safe_component(text: str, *, max_length: int = _MAX_COMPONENT) -> str:
    """A single portable path component derived from ``text``."""
    if not text:
        raise InvalidComponent("empty component")
    # strip trailing spaces/dots from the *source* first: a trailing space replaced by
    # "_" would be legal but meaningless, and Windows rejects the space itself
    cleaned = _ALLOWED.sub("_", text.strip(" ."))
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        raise InvalidComponent(f"nothing portable left of {text!r}")
    stem = cleaned.split(".", 1)[0].lower()
    if stem in RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    if len(cleaned) > max_length:
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:10]
        keep = max_length - len(digest) - 1
        cleaned = f"{cleaned[:keep]}.{digest}"
    if not cleaned or cleaned in {".", ".."}:
        raise InvalidComponent(f"nothing portable left of {text!r}")
    return cleaned


def artifact_name(document_id: str, doc_type: str, fmt: str, role: str) -> str:
    """``<document>__<doc_type>__<format>[__<role>].json`` with every part sanitised.

    The role is omitted for gold v1/v2 (the directory carries it) and included for
    the alignment artifact, which has always had a distinct name.
    """
    document = safe_component(document_id.replace(":", "_"))
    kind = safe_component(doc_type.replace(":", "_"))
    suffix = safe_component(fmt)
    if role in {"v1", "v2"}:
        return f"{document}__{kind}__{suffix}.json"
    return f"{document}__{kind}__{suffix}__{safe_component(role)}.json"


class PortablePaths:
    """Deterministic, collision-checked mapping from artifact keys to filenames."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str, str, str], str] = {}
        self._by_folded: dict[str, tuple[str, str, str, str]] = {}

    def name(self, document_id: str, doc_type: str, fmt: str, role: str,
             directory: str = "") -> str:
        """The filename for one artifact; ``directory`` scopes the collision check.

        Gold v1 and v2 legitimately share a filename in different directories, so the
        check is on the full relative path, not the basename alone.
        """
        key = (document_id, doc_type, fmt, role)
        if key in self._by_key:
            return self._by_key[key]
        candidate = artifact_name(document_id, doc_type, fmt, role)
        folded = f"{directory}/{candidate}".casefold()
        owner = self._by_folded.get(folded)
        if owner is not None and owner != key:
            raise PathCollision(
                f"{document_id}/{doc_type}/{fmt}/{role} and {owner} both map to "
                f"{directory}/{candidate} (case-insensitive)")
        self._by_folded[folded] = key
        self._by_key[key] = candidate
        return candidate

    def mapping(self) -> dict[tuple[str, str, str, str], str]:
        return dict(self._by_key)

    def __len__(self) -> int:
        return len(self._by_key)
