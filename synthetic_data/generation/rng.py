"""Scope-seeded randomness (architecture §5.3).

Every choice is a pure function of the corpus key and a readable scope.  There is
no module-level RNG state, so two processes cannot disagree and adding a document
cannot perturb an earlier one.
"""
from __future__ import annotations

import hashlib
import random
from typing import Iterable, Sequence, TypeVar

__all__ = [
    "scope_rng",
    "scope_seed",
    "pick",
    "pick_weighted",
    "shuffled",
    "sample",
    "sample_distinct",
    "coin",
    "int_between",
]

T = TypeVar("T")


def scope_seed(key: bytes, *scope: str) -> bytes:
    payload = "|".join(scope).encode("utf-8")
    return hashlib.blake2b(key + b"|" + payload, digest_size=16).digest()


def scope_rng(key: bytes, *scope: str) -> random.Random:
    return random.Random(scope_seed(key, *scope))


def pick(rng: random.Random, items: Sequence[T]) -> T:
    if not items:
        raise ValueError("cannot pick from an empty sequence")
    return items[rng.randrange(len(items))]


def pick_weighted(rng: random.Random, items: Sequence[T], weights: Sequence[int]) -> T:
    if len(items) != len(weights) or not items:
        raise ValueError("items and weights must be non-empty and the same length")
    if any(weight < 0 for weight in weights) or sum(weights) == 0:
        raise ValueError("weights must be non-negative with a positive total")
    total = sum(weights)
    point = rng.randrange(total)
    running = 0
    for item, weight in zip(items, weights):
        running += weight
        if point < running:
            return item
    return items[-1]                       # pragma: no cover - arithmetic guard


def shuffled(rng: random.Random, items: Iterable[T]) -> list[T]:
    out = list(items)
    rng.shuffle(out)
    return out


def sample(rng: random.Random, items: Sequence[T], count: int) -> list[T]:
    """Ordered sample without replacement, clamped to the population size."""
    if count < 0:
        raise ValueError("count must be >= 0")
    population = list(items)
    count = min(count, len(population))
    return rng.sample(population, count)


def sample_distinct(rng: random.Random, items: Sequence[T], count: int) -> list[T]:
    """Sorted-by-identity sample, so callers can rely on a stable order."""
    return sample(rng, items, count)


def coin(rng: random.Random, probability: float = 0.5) -> bool:
    if not 0.0 <= probability <= 1.0:
        raise ValueError("probability must be in [0, 1]")
    return rng.random() < probability


def int_between(rng: random.Random, low: int, high: int) -> int:
    if low > high:
        raise ValueError("low must be <= high")
    return rng.randint(low, high)
