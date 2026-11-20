"""Prefixed ULID identifiers.

Purpose: one place that mints every object id, so ids sort by creation time and
carry their kind in the prefix (``ent_01J…``). Contract: :func:`new_id` returns
``"<prefix>_<ULID>"``. Tests can switch to a seeded factory so ids are
reproducible. Spec: CLAUDE.md "Conventions".
"""

from __future__ import annotations

import random
import threading
import time

from ulid import ULID

#: Every prefix in use. Adding a kind means adding it here, so a typo fails loudly.
PREFIXES = {
    "doc", "snt", "spn", "men", "ent", "tim", "med", "mad", "prp", "lnk", "flg",
    "esc", "run", "cal", "chk", "sec", "chg", "mcl", "res", "gld",
}


class IdFactory:
    """Mints ULIDs. With a seed, ids are deterministic (tests only)."""

    def __init__(self, seed: int | None = None) -> None:
        self._seed = seed
        self._rng = random.Random(seed) if seed is not None else None
        self._clock = 1_700_000_000_000
        self._lock = threading.Lock()

    def new(self, prefix: str) -> str:
        if prefix not in PREFIXES:
            raise ValueError(f"unknown id prefix {prefix!r}")
        if self._rng is None:
            return f"{prefix}_{ULID()}"
        with self._lock:
            self._clock += 1
            stamp = self._clock.to_bytes(6, "big")
            noise = bytes(self._rng.getrandbits(8) for _ in range(10))
        return f"{prefix}_{ULID.from_bytes(stamp + noise)}"


_factory = IdFactory()


def new_id(prefix: str) -> str:
    """Return a fresh id such as ``ent_01JABCD…``."""
    return _factory.new(prefix)


def use_seeded_ids(seed: int) -> None:
    """Make every following id deterministic. For tests."""
    global _factory
    _factory = IdFactory(seed)


def use_random_ids() -> None:
    global _factory
    _factory = IdFactory()


def now_ms() -> int:
    return int(time.time() * 1000)
