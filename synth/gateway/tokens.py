"""Token counting: exact with a Hugging Face tokenizer file, else a logged heuristic.

Contract: :meth:`TokenCounter.count` -> ``(tokens, exact)``. The heuristic is
``ceil(chars / 3.5)`` and is always reported as inexact (P1 §9).
"""

from __future__ import annotations

import math
from pathlib import Path

HEURISTIC_CHARS_PER_TOKEN = 3.5


def heuristic_tokens(text: str) -> int:
    return math.ceil(len(text) / HEURISTIC_CHARS_PER_TOKEN) if text else 0


class TokenCounter:
    def __init__(self, tokenizer_paths: dict[str, Path | None] | None = None) -> None:
        self._paths = tokenizer_paths or {}
        self._cache: dict[str, object | None] = {}

    def _tokenizer(self, endpoint: str | None):
        if endpoint is None:
            return None
        if endpoint not in self._cache:
            path = self._paths.get(endpoint)
            tok = None
            if path is not None and Path(path).exists():
                try:
                    from tokenizers import Tokenizer

                    tok = Tokenizer.from_file(str(path))
                except Exception:  # pragma: no cover - corrupt tokenizer file
                    tok = None
            self._cache[endpoint] = tok
        return self._cache[endpoint]

    def count(self, text: str, endpoint: str | None = None) -> tuple[int, bool]:
        tok = self._tokenizer(endpoint)
        if tok is None:
            return heuristic_tokens(text), False
        return len(tok.encode(text).ids), True

    def n(self, text: str, endpoint: str | None = None) -> int:
        return self.count(text, endpoint)[0]


_default = TokenCounter()


def count_tokens(text: str, endpoint: str | None = None) -> int:
    """Module-level convenience (heuristic unless a counter was configured)."""
    return _default.n(text, endpoint)
