"""Configuration: run defaults, model endpoints, and the hash that pins a run.

Purpose: load ``configs/synth/p1.default.yaml`` + ``models.yaml`` into typed
objects. Contract: :func:`load_config` returns a :class:`SynthConfig`; its
:meth:`SynthConfig.config_hash` changes whenever any value that can change a
result changes. Stores written: none (J0 writes the manifest to L1).
Spec: P1 §9 (models.yaml), §10 (caps), CLAUDE.md "Environment".
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "configs" / "synth" / "p1.default.yaml"
DEFAULT_DB_URL = "postgresql://synth:synth@localhost:5432/synth"


class _Open(BaseModel):
    """Typed where we know the field, tolerant of additions (parallel builds)."""

    model_config = ConfigDict(extra="allow")


class EndpointConfig(_Open):
    base_url: str
    model: str
    context_length: int = 8192
    tokenizer: str | None = None
    structured_mode: str = "strict_schema"      # strict_schema | json_then_validate
    temperature: float = 0.0
    api_key_env: str | None = None
    local: bool | None = None                   # None -> inferred from base_url
    price_per_mtok_in: float = 0.0
    price_per_mtok_out: float = 0.0
    timeout_s: float = 600.0
    max_parallel: int = 1
    reasoning_budget: int = 0                   # extra max_tokens for thinking (reasoning models)

    def is_local(self) -> bool:
        if self.local is not None:
            return self.local
        host = self.base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".local")


class GlinerConfig(_Open):
    model: str = "fastino/gliner2.5-base-v1"
    device: str = "cuda"
    threshold: float = 0.5
    window_words: int = 384
    overlap_words: int = 64


class Tier1Config(_Open):
    relation_mode: str = "micro"                # micro | gliner | gliner+micro (E1)


class ModelsConfig(_Open):
    endpoints: dict[str, EndpointConfig] = Field(default_factory=dict)
    gliner: GlinerConfig = Field(default_factory=GlinerConfig)
    tier1: Tier1Config = Field(default_factory=Tier1Config)


class ChunkSize(_Open):
    target_tokens: int
    cap_tokens: int


class ChunkingConfig(_Open):
    micro: ChunkSize = ChunkSize(target_tokens=400, cap_tokens=500)
    section: ChunkSize = ChunkSize(target_tokens=1500, cap_tokens=2000)


class IngestConfig(_Open):
    pdf_pipeline: str = "native"
    ocr_min_chars: int = 50
    header_footer_share: float = 0.6
    doc_date_window_chars: int = 1500


class GroundingConfig(_Open):
    fuzzy_accept: float = 85.0
    fuzzy_pass: float = 92.0
    min_quote_words: int = 5


class ResolverConfig(_Open):
    merge_threshold: float = 0.90
    apart_threshold: float = 0.20
    max_edit_distance: int = 2
    rival_margin: float = 0.05
    bias: float = -4.0
    weights: dict[str, float] = Field(default_factory=dict)


class GateConfig(_Open):
    escalate_max_share: float = 0.10
    escalate_target_share: float = 0.05
    low_conf: float = 0.6


class SynthConfig(_Open):
    version: int = 1
    seed: int = 7
    paths: dict[str, str] = Field(default_factory=dict)
    ontology: str = "configs/synth/ontology/corporate_legal.v1.yaml"
    models: str = "configs/synth/models.yaml"
    db: dict[str, str] = Field(default_factory=dict)
    modules: dict[str, str] = Field(default_factory=dict)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    caps: dict[str, int] = Field(default_factory=dict)
    grounding: GroundingConfig = Field(default_factory=GroundingConfig)
    contract: dict[str, float] = Field(default_factory=lambda: {"confidence_floor": 0.35})
    t2: dict[str, Any] = Field(default_factory=lambda: {"mode": "diff"})
    resolver: ResolverConfig = Field(default_factory=ResolverConfig)
    gate: GateConfig = Field(default_factory=GateConfig)
    eval: dict[str, Any] = Field(default_factory=dict)
    model_settings: ModelsConfig = Field(default_factory=ModelsConfig)

    #: Directory the relative paths above are resolved against.
    root: str = str(REPO_ROOT)

    # -- paths ----------------------------------------------------------------
    def path(self, key: str) -> Path:
        value = self.paths.get(key)
        if value is None:
            raise KeyError(f"no path configured for {key!r}")
        return self.resolve(value)

    def resolve(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else Path(self.root) / path

    # -- convenience ------------------------------------------------------------
    def cap(self, name: str) -> int:
        if name not in self.caps:
            raise KeyError(f"no token cap named {name!r} in config.caps")
        return int(self.caps[name])

    def endpoint(self, name: str) -> EndpointConfig:
        try:
            return self.model_settings.endpoints[name]
        except KeyError as exc:
            raise KeyError(f"no endpoint {name!r} in models.yaml") from exc

    def db_url(self) -> str:
        env = self.db.get("url_env", "SYNTH_DB_URL")
        return os.environ.get(env) or self.db.get("default_url") or DEFAULT_DB_URL

    def as_manifest(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data.pop("root", None)
        return data

    def config_hash(self, extra: dict[str, Any] | None = None) -> str:
        """sha256 over the manifest plus anything else that pins a result
        (ontology pack, prompt hashes)."""
        payload = {"config": self.as_manifest(), "extra": extra or {}}
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold a mapping at the top level")
    return data


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


PROFILES_DIR = REPO_ROOT / "configs" / "synth" / "profiles"            # built-in line-ups (committed)
LOCAL_PROFILES_DIR = REPO_ROOT / "configs" / "synth" / "profiles.local"  # made in the console (gitignored)


def local_profiles_dir() -> Path:
    """Where the console keeps the user's profiles; ``SYNTH_LOCAL_PROFILES`` overrides (tests)."""
    return Path(os.environ.get("SYNTH_LOCAL_PROFILES") or LOCAL_PROFILES_DIR)


ONTOLOGY_DIR = REPO_ROOT / "configs" / "synth" / "ontology"              # built-in packs (committed, read-only)
LOCAL_ONTOLOGY_DIR = REPO_ROOT / "configs" / "synth" / "ontology.local"  # made in the console (gitignored)


def local_ontology_dir() -> Path:
    """Where the console keeps the user's ontologies; ``SYNTH_LOCAL_ONTOLOGIES`` overrides (tests)."""
    return Path(os.environ.get("SYNTH_LOCAL_ONTOLOGIES") or LOCAL_ONTOLOGY_DIR)


def ontology_path(name: str) -> Path:
    """An ontology pack's file by name (its file stem): the user's own wins over a built-in."""
    local = local_ontology_dir() / f"{name}.yaml"
    if local.exists():
        return local
    builtin = ONTOLOGY_DIR / f"{name}.yaml"
    if not builtin.exists():
        raise FileNotFoundError(f"no ontology named {name!r} (in {ONTOLOGY_DIR} or {local_ontology_dir()})")
    return builtin


def profile_path(name: str) -> Path:
    """A profile's file: the user's own (profiles.local) wins over a built-in of the same name."""
    local = local_profiles_dir() / f"{name}.yaml"
    return local if local.exists() else PROFILES_DIR / f"{name}.yaml"


def _models_file(data: dict[str, Any]) -> str:
    """Model endpoints: ``SYNTH_MODELS`` (a path) > ``SYNTH_PROFILE`` (a profile name,
    see :func:`profile_path`) > the run config's ``models:`` entry."""
    if os.environ.get("SYNTH_MODELS"):
        return os.environ["SYNTH_MODELS"]
    if os.environ.get("SYNTH_PROFILE"):
        return str(profile_path(os.environ["SYNTH_PROFILE"]))
    return data.get("models", "configs/synth/models.yaml")


# ------------------------------------------------------------------ secrets
def secrets_file() -> Path:
    """The local secret store shared with Datanaut (``config.local.yaml``, gitignored,
    0600). ``SYNTH_SECRETS_FILE`` points elsewhere (tests)."""
    return Path(os.environ.get("SYNTH_SECRETS_FILE") or REPO_ROOT / "config.local.yaml")


def load_secrets() -> dict[str, str]:
    path = secrets_file()
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    stored = loaded.get("secrets") if isinstance(loaded, dict) else None
    return {str(k): str(v) for k, v in (stored or {}).items() if v} if isinstance(stored, dict) else {}


def write_secret(name: str, value: str | None) -> None:
    """Store or clear one secret, keeping the others (same format as Datanaut's store)."""
    path = secrets_file()
    existing = load_secrets()
    if value:
        existing[name] = value
    else:
        existing.pop(name, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ("# Datanaut / Synth secrets. Gitignored, and never sent to the browser.\n"
            "# A real environment variable of the same name overrides what is here.\n")
    path.write_text(body + yaml.safe_dump({"secrets": existing}, sort_keys=True), encoding="utf-8")
    try:
        path.chmod(0o600)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows / odd file systems
        pass


def resolve_secret(name: str | None) -> str | None:
    """An API key by its variable name: the environment first, then the local store."""
    if not name:
        return None
    return os.environ.get(name) or load_secrets().get(name)


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None,
                root: str | Path | None = None) -> SynthConfig:
    """Load the run config and the model endpoints it points to."""
    config_path = Path(path) if path else DEFAULT_CONFIG
    data = _read_yaml(config_path)
    if overrides:
        data = _deep_merge(data, overrides)
    root_dir = Path(root) if root else REPO_ROOT
    data["root"] = str(root_dir)
    models_path = Path(_models_file(data))
    if not models_path.is_absolute():
        models_path = root_dir / models_path
    if not models_path.exists() and (os.environ.get("SYNTH_PROFILE") or os.environ.get("SYNTH_MODELS")):
        raise FileNotFoundError(f"model profile not found: {models_path}")
    models = _read_yaml(models_path) if models_path.exists() else {}
    if overrides and "model_settings" in overrides:
        models = _deep_merge(models, overrides["model_settings"])
    for name, endpoint in (models.get("endpoints") or {}).items():
        model_id = os.environ.get(f"SYNTH_MODEL_{name.upper()}")   # e.g. SYNTH_MODEL_T2
        if model_id:
            endpoint["model"] = model_id
    data["model_settings"] = models
    if os.environ.get("SYNTH_ONTOLOGY"):                          # the console's per-run ontology choice
        data["ontology"] = str(ontology_path(os.environ["SYNTH_ONTOLOGY"]))
    return SynthConfig.model_validate(data)
