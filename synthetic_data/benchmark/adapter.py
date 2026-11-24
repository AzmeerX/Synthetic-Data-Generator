"""Adapters that turn an extraction system's output into a prediction bundle.

An adapter is any object with ``load() -> PredictionBundle``.  The evaluator is
given an adapter, never a generator object, so a system that is not this repository
can be scored by writing one small class or by emitting the JSON directly.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from synthetic_data.benchmark.schema import (PREDICTION_SCHEMA_VERSION,
                                             PredictionBundle)

__all__ = ["PredictionAdapter", "JsonPredictionAdapter", "MappingAdapter",
           "load_bundle", "bundle_from_documents"]


@runtime_checkable
class PredictionAdapter(Protocol):
    name: str

    def load(self) -> PredictionBundle:  # pragma: no cover - protocol
        ...


class JsonPredictionAdapter:
    """Reads one JSON file written to the documented schema."""

    def __init__(self, path: str | Path, name: str | None = None) -> None:
        self.path = Path(path)
        self.name = name or f"json:{self.path.name}"

    def load(self) -> PredictionBundle:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return PredictionBundle.model_validate(payload)


class MappingAdapter:
    """Wraps an in-memory payload; used by tests and by notebook-style callers."""

    def __init__(self, payload: dict[str, Any], name: str = "mapping") -> None:
        self.payload = payload
        self.name = name

    def load(self) -> PredictionBundle:
        return PredictionBundle.model_validate(self.payload)


def bundle_from_documents(documents: list[Any], *, corpus: str = "",
                          adapter: str = "documents") -> PredictionBundle:
    """Build a bundle from already-validated documents (fixtures, adapters)."""
    return PredictionBundle(schema_version=PREDICTION_SCHEMA_VERSION, corpus=corpus,
                            adapter=adapter, documents=documents)


def load_bundle(source: Any) -> PredictionBundle:
    """Accept an adapter, a path, or a raw payload and return a bundle."""
    if hasattr(source, "load") and not isinstance(source, (str, Path, dict)):
        return source.load()
    if isinstance(source, (str, Path)):
        return JsonPredictionAdapter(source).load()
    if isinstance(source, dict):
        return MappingAdapter(source).load()
    raise TypeError(f"cannot load predictions from {type(source).__name__}")
