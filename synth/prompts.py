"""Prompt registry: versioned prompt files, hashed into the run registry.

Prompts live in ``configs/synth/prompts/<name>.vN.md`` (CLAUDE.md conventions).
A prompt file holds the **system** text; modules build the user message.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from synth.config import REPO_ROOT

PROMPTS_DIR = REPO_ROOT / "configs" / "synth" / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str            # e.g. "u5_micro.v1"
    text: str
    sha256: str

    @property
    def version(self) -> str:
        return self.name.rsplit(".", 1)[-1]


@lru_cache(maxsize=64)
def load_prompt(name: str, directory: str | None = None) -> Prompt:
    base = Path(directory) if directory else PROMPTS_DIR
    path = base / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt {name!r} not found at {path}")
    text = path.read_text(encoding="utf-8").strip()
    return Prompt(name=name, text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


def prompt_hashes(directory: str | None = None) -> dict[str, str]:
    base = Path(directory) if directory else PROMPTS_DIR
    return {p.stem: hashlib.sha256(p.read_text(encoding="utf-8").strip().encode("utf-8")).hexdigest()
            for p in sorted(base.glob("*.md"))}
