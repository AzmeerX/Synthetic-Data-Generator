"""Configuration loading.

Settings come from ``config.yaml``; secrets come from the environment and are
never written to disk, never echoed back over the API, and never logged. The
one place an API key exists in memory is :attr:`ProviderConfig.api_key`, and
:meth:`Settings.redacted` is what the UI and the logs are allowed to see.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CONFIG_PATH = ROOT / "config.yaml"

#: Environment variable that holds each provider's key. The key itself is
#: never stored in ``config.yaml`` — only the name of the variable to read.
DEFAULT_KEY_ENVVARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "replay": "",
    "echo": "",
}


class ConfigError(Exception):
    """Raised when configuration is malformed. Always names the offending key."""


@dataclass(frozen=True)
class ModelConfig:
    """One selectable model.

    ``cost_per_mtok_in``/``out`` are in whole currency units per million tokens
    and are what the cost ceiling is enforced against. They are declared, not
    discovered: a provider that changes its prices will not change these until
    someone edits them, which is deliberate — a run's cost must be predictable
    before it starts.
    """

    id: str
    provider: str
    model: str
    temperature: float = 0.0
    max_tokens: int = 4096
    cost_per_mtok_in: float = 0.0
    cost_per_mtok_out: float = 0.0
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "cost_per_mtok_in": self.cost_per_mtok_in,
            "cost_per_mtok_out": self.cost_per_mtok_out,
            "label": self.label or self.id,
        }


#: Wire protocols Datanaut can speak. A provider is selected by the *protocol*
#: it speaks, not by who runs it — which is what lets any OpenAI-compatible
#: endpoint work without new code.
API_PROTOCOLS = ("openai", "anthropic", "replay")

#: How a provider accepts a JSON schema constraint. Support varies widely
#: outside the two big vendors, so it is configurable rather than assumed:
#:
#:   schema  response_format: {type: json_schema, ...}   (OpenAI, vLLM, some others)
#:   object  response_format: {type: json_object}        (widely supported)
#:   none    no constraint sent; rely on the prompt
#:
#: Datanaut validates every response against the schema itself regardless of
#: this setting, so `none` is safe — it just means more rejected responses when
#: a model returns malformed JSON.
JSON_MODES = ("schema", "object", "none")


@dataclass(frozen=True)
class ProviderConfig:
    """Credentials and endpoint for one provider.

    ``api_key`` is resolved from the environment at load time. It is excluded
    from :meth:`to_dict`, which is the only serialisation the API layer uses.

    ``api`` names the wire protocol, not the vendor. Anything that speaks the
    OpenAI Chat Completions format — Together, Groq, Fireworks, OpenRouter, a
    self-hosted vLLM or Ollama — is reachable by setting ``api: openai`` and a
    ``base_url``, with no code change.
    """

    name: str
    api_key_env: str = ""
    base_url: str = ""
    api: str = ""
    json_mode: str = ""
    api_key: str = field(default="", repr=False, compare=False)
    timeout_seconds: float = 120.0

    @property
    def protocol(self) -> str:
        """The wire format to speak.

        Defaults by name for the providers Datanaut ships with, and to
        ``openai`` for everything else — because the OpenAI Chat Completions
        shape is the de-facto standard that open-source hosts implement.
        """
        if self.api:
            return self.api
        if self.name in ("anthropic", "replay", "echo"):
            return self.name if self.name != "echo" else "replay"
        return "openai"

    @property
    def is_local(self) -> bool:
        """Whether this endpoint is on the machine running Datanaut.

        A local model server needs no API key, so its absence must not be
        treated as a misconfiguration.
        """
        return any(host in (self.base_url or "") for host in
                   ("localhost", "127.0.0.1", "0.0.0.0", "[::1]",
                    "host.docker.internal"))

    @property
    def json_constraint(self) -> str:
        """How to ask this provider for JSON.

        Defaults to strict schema for OpenAI itself, and to the widely
        supported ``object`` mode for any other OpenAI-compatible host, since
        `json_schema` support outside OpenAI is patchy and a 400 from an
        unsupported field is a confusing first failure.
        """
        if self.json_mode:
            return self.json_mode
        return "schema" if self.name == "openai" else "object"

    def to_dict(self) -> dict[str, Any]:
        """Serialise without the secret. Reports only whether a key is present."""
        return {
            "name": self.name,
            "api_key_env": self.api_key_env,
            "base_url": self.base_url,
            "api": self.protocol,
            "json_mode": self.json_constraint,
            "is_local": self.is_local,
            "timeout_seconds": self.timeout_seconds,
            "key_present": bool(self.api_key),
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (f"ProviderConfig(name={self.name!r}, api_key_env={self.api_key_env!r}, "
                f"base_url={self.base_url!r}, key_present={bool(self.api_key)})")


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 5
    initial_backoff_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 30.0
    jitter: float = 0.1


@dataclass(frozen=True)
class IngestConfig:
    #: ``native`` runs no models at all (Stage 0 decision). ``standard`` loads
    #: Docling's layout, table and OCR models and needs network access to
    #: HuggingFace.
    pdf_pipeline: str = "native"
    #: Passages shorter than this contribute to the "suspicious" signal but are
    #: still ingested.
    min_passage_chars: int = 2
    #: Reading-order flag thresholds. Exceeding one flags the document; nothing
    #: is ever repaired.
    max_unterminated_ratio: float = 0.75
    max_midword_ratio: float = 0.15
    max_length_cv: float = 2.5


#: How extraction asks the model.
#:
#:   single   one call per window against the full frame and a nested schema.
#:            Right for large frontier models.
#:   focused  three small passes per window - things, then events, then links -
#:            each with a flat output shape and only the part of the frame it
#:            needs. Right for small local models, which fail the single call.
#:
#: Anchor verification, rejection and consolidation are identical either way.
#: The mode changes what is asked for, not what is trusted.
EXTRACT_MODES = ("single", "focused")


@dataclass(frozen=True)
class ExtractConfig:
    mode: str = "single"
    window_chars: int = 6000
    window_overlap_chars: int = 600
    respect_section_boundaries: bool = True
    #: rapidfuzz similarity, 0-100. Below this a quote is unanchored and the
    #: element is rejected. Deliberately high.
    fuzzy_threshold: float = 88.0
    workers: int = 2
    #: Hard ceiling in whole currency units. The run halts when crossed.
    cost_ceiling: float = 5.0


@dataclass(frozen=True)
class ConsolidateConfig:
    #: Score at or above which two nodes merge without asking a model.
    merge_high: float = 0.92
    #: Score below which two nodes never merge. Between the two, a model
    #: adjudicates and the verdict goes to the decision ledger.
    merge_low: float = 0.55
    #: Ask the model about ambiguous pairs at all.
    adjudicate: bool = True
    #: Cap on adjudication calls per run, so consolidation cannot run away.
    max_adjudications: int = 200


@dataclass(frozen=True)
class LedgerConfig:
    #: Feed prior decisions with the same class signature into the prompt.
    #: Switchable off so its effect can be measured (Stage 6 / Stage 10).
    use_precedent: bool = True
    max_precedents_in_prompt: int = 3


@dataclass(frozen=True)
class AblationConfig:
    """Stage 10 switches. Every one of these must be reachable from config
    alone, with no code change."""

    precedent_off: bool = False
    anchor_enforcement_off: bool = False
    adjudication_off: bool = False


@dataclass(frozen=True)
class Settings:
    config_path: Path
    secrets_path: Path
    root: Path
    db_path: Path
    corpus_dir: Path
    runs_dir: Path
    frames_dir: Path
    active_model: str
    models: dict[str, ModelConfig]
    providers: dict[str, ProviderConfig]
    retry: RetryConfig = field(default_factory=RetryConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    extract: ExtractConfig = field(default_factory=ExtractConfig)
    consolidate: ConsolidateConfig = field(default_factory=ConsolidateConfig)
    ledger: LedgerConfig = field(default_factory=LedgerConfig)
    ablation: AblationConfig = field(default_factory=AblationConfig)
    host: str = "127.0.0.1"
    port: int = 8420

    # -- accessors ---------------------------------------------------------

    def model(self, model_id: str | None = None) -> ModelConfig:
        chosen = model_id or self.active_model
        if chosen not in self.models:
            known = ", ".join(sorted(self.models)) or "<none configured>"
            raise ConfigError(f"unknown model {chosen!r}; configured models: {known}")
        return self.models[chosen]

    def provider(self, name: str) -> ProviderConfig:
        if name not in self.providers:
            known = ", ".join(sorted(self.providers)) or "<none configured>"
            raise ConfigError(f"unknown provider {name!r}; configured providers: {known}")
        return self.providers[name]

    def secrets(self) -> list[str]:
        """Every secret string currently held. The log scrubber redacts these."""
        return [p.api_key for p in self.providers.values() if p.api_key]

    def redacted(self) -> dict[str, Any]:
        """The whole configuration, safe to log or send to the browser."""
        return {
            "config_path": str(self.config_path),
            "secrets_path": str(self.secrets_path),
            "db_path": str(self.db_path),
            "corpus_dir": str(self.corpus_dir),
            "runs_dir": str(self.runs_dir),
            "frames_dir": str(self.frames_dir),
            "active_model": self.active_model,
            "models": {k: v.to_dict() for k, v in self.models.items()},
            "providers": {k: v.to_dict() for k, v in self.providers.items()},
            "retry": vars(self.retry),
            "ingest": vars(self.ingest),
            "extract": vars(self.extract),
            "consolidate": vars(self.consolidate),
            "ledger": vars(self.ledger),
            "ablation": vars(self.ablation),
            "host": self.host,
            "port": self.port,
        }

    def with_active_model(self, model_id: str) -> Settings:
        """Return a copy pointing at a different model.

        Model switching must not need a restart, so this is a cheap immutable
        swap rather than a mutation of shared state.
        """
        if model_id not in self.models:
            raise ConfigError(f"unknown model {model_id!r}")
        return replace(self, active_model=model_id)


# --------------------------------------------------------------------------


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"config key {key!r} must be a mapping, got {type(value).__name__}")
    return value


def validate_extract(config: "ExtractConfig") -> "ExtractConfig":
    if config.mode not in EXTRACT_MODES:
        raise ConfigError(
            f"extract.mode is {config.mode!r}; must be one of "
            f"{', '.join(EXTRACT_MODES)}")
    return config


def _build(cls, raw: dict[str, Any], key: str):
    """Build a frozen dataclass from a config section, rejecting unknown keys.

    Unknown keys are an error rather than a silent no-op: a typo in a config
    file that quietly does nothing is exactly the kind of silent wrong answer
    this system is supposed to make impossible.
    """
    section = _section(raw, key)
    allowed = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(
            f"unknown key(s) in config section {key!r}: {', '.join(sorted(unknown))}; "
            f"allowed: {', '.join(sorted(allowed))}"
        )
    return cls(**section)


#: Secrets live here, never in ``config.yaml``. The distinction matters: the
#: main config is meant to be read, diffed and shared, and this file is not.
#: It is gitignored, written 0600 where the OS supports it, and never returned
#: over the HTTP API — the API reports ``key_present`` and nothing more.
#:
#: A real environment variable always beats a stored one, so CI and container
#: deployments keep working exactly as before.
SECRETS_FILENAME = "config.local.yaml"


def load_secrets(path: Path) -> dict[str, str]:
    """Read the local secret store. A missing or malformed file yields none."""
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    secrets = loaded.get("secrets") or {}
    if not isinstance(secrets, dict):
        raise ConfigError(f"{path}: 'secrets' must be a mapping of NAME: value")
    return {str(k): str(v) for k, v in secrets.items() if v}


def write_secret(path: Path, name: str, value: str | None) -> None:
    """Store or clear one secret, leaving the rest of the file alone.

    ``value`` of None or "" deletes the entry rather than storing an empty
    string, so clearing a key in the UI really clears it.
    """
    existing = load_secrets(path)
    if value:
        existing[name] = value
    else:
        existing.pop(name, None)

    path.parent.mkdir(parents=True, exist_ok=True)
    body = ("# Datanaut secrets. Gitignored, and never sent to the browser.\n"
            "# A real environment variable of the same name overrides what is here.\n")
    path.write_text(body + yaml.safe_dump({"secrets": existing}, sort_keys=True))
    try:
        path.chmod(0o600)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows/odd FS
        pass


def load_settings(path: Path | str | None = None,
                  env: dict[str, str] | None = None,
                  root: Path | None = None) -> Settings:
    """Load settings from YAML plus environment.

    Missing config file is not an error — the defaults are usable, which keeps
    ``datanaut serve`` working on a fresh checkout.
    """
    env = dict(os.environ if env is None else env)
    root = Path(root) if root else ROOT
    config_path = Path(path) if path else Path(env.get("DATANAUT_CONFIG", DEFAULT_CONFIG_PATH))
    secrets_path = config_path.parent / SECRETS_FILENAME
    stored_secrets = load_secrets(secrets_path)

    raw: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")
        raw = loaded

    paths = _section(raw, "paths")
    db_path = Path(env.get("DATANAUT_DB") or paths.get("db") or (root / "runs" / "datanaut.sqlite3"))
    corpus_dir = Path(paths.get("corpus") or (root / "corpus"))
    runs_dir = Path(env.get("DATANAUT_RUNS") or paths.get("runs") or (root / "runs"))
    frames_dir = Path(paths.get("frames") or (root / "frames"))

    providers: dict[str, ProviderConfig] = {}
    for name, spec in _section(raw, "providers").items():
        spec = spec or {}
        allowed = {"api_key_env", "base_url", "api", "json_mode", "timeout_seconds"}
        unknown = set(spec) - allowed
        if unknown:
            raise ConfigError(
                f"unknown key(s) in provider {name!r}: {', '.join(sorted(unknown))}; "
                f"allowed: {', '.join(sorted(allowed))}")

        api = spec.get("api", "")
        if api and api not in API_PROTOCOLS:
            raise ConfigError(
                f"provider {name!r} has api {api!r}; must be one of "
                f"{', '.join(API_PROTOCOLS)}")
        json_mode = spec.get("json_mode", "")
        if json_mode and json_mode not in JSON_MODES:
            raise ConfigError(
                f"provider {name!r} has json_mode {json_mode!r}; must be one of "
                f"{', '.join(JSON_MODES)}")

        key_env = spec.get("api_key_env") or DEFAULT_KEY_ENVVARS.get(name, "")
        providers[name] = ProviderConfig(
            name=name,
            api_key_env=key_env,
            base_url=spec.get("base_url", ""),
            api=api,
            json_mode=json_mode,
            api_key=(env.get(key_env) or stored_secrets.get(key_env, "")) if key_env else "",
            timeout_seconds=float(spec.get("timeout_seconds", 120.0)),
        )

    # The replay provider is always available: it is what the test suite, the
    # golden run and the shipped demo use, and it never touches the network.
    providers.setdefault("replay", ProviderConfig(name="replay"))

    models: dict[str, ModelConfig] = {}
    for spec in raw.get("models") or []:
        if not isinstance(spec, dict) or "id" not in spec:
            raise ConfigError("each entry under 'models' must be a mapping with an 'id'")
        allowed = set(ModelConfig.__dataclass_fields__)
        unknown = set(spec) - allowed
        if unknown:
            raise ConfigError(
                f"unknown key(s) in model {spec['id']!r}: {', '.join(sorted(unknown))}")
        models[spec["id"]] = ModelConfig(**spec)

    active = env.get("DATANAUT_MODEL") or raw.get("active_model") or ""

    # A configured model whose provider has no key cannot answer. Rather than
    # let the first run fail with an authentication error, fall back to the
    # offline replay model when one is configured and no key is present. This
    # is what makes a fresh clone usable before the user has an API key.
    if active and active in models:
        chosen = models[active]
        provider = providers.get(chosen.provider)
        # A self-hosted endpoint needs no key, so its absence is not a reason
        # to fall back to the offline model.
        needs_key = (chosen.provider not in ("replay", "echo")
                     and not (provider is not None and provider.is_local))
        if needs_key and (provider is None or not provider.api_key):
            offline = next((m.id for m in models.values()
                            if m.provider == "replay"), "")
            if offline:
                active = offline
    if not active and models:
        active = next(iter(models))

    server = _section(raw, "server")

    settings = Settings(
        config_path=config_path,
        secrets_path=secrets_path,
        root=root,
        db_path=db_path,
        corpus_dir=corpus_dir,
        runs_dir=runs_dir,
        frames_dir=frames_dir,
        active_model=active,
        models=models,
        providers=providers,
        retry=_build(RetryConfig, raw, "retry"),
        ingest=_build(IngestConfig, raw, "ingest"),
        extract=validate_extract(_build(ExtractConfig, raw, "extract")),
        consolidate=_build(ConsolidateConfig, raw, "consolidate"),
        ledger=_build(LedgerConfig, raw, "ledger"),
        ablation=_build(AblationConfig, raw, "ablation"),
        host=str(server.get("host", "127.0.0.1")),
        port=int(server.get("port", 8420)),
    )

    # Ablations override the positive settings, so a comparison run needs one
    # config key and no code change (Stage 10 acceptance criterion).
    if settings.ablation.precedent_off:
        settings = replace(settings, ledger=replace(settings.ledger, use_precedent=False))
    if settings.ablation.adjudication_off:
        settings = replace(settings,
                           consolidate=replace(settings.consolidate, adjudicate=False))
    return settings
