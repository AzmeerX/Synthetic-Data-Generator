"""Console settings: model profiles per stage slot, API keys, provider presets.

Purpose: let the console swap the model behind each stage the way Datanaut's UI
does — pick a provider preset, a model id and a key per slot (``t1_micro``,
``t2``, ``t3``, plus the in-process ``gliner`` model).
Contract: :func:`list_profiles`, :func:`get_profile`, :func:`save_profile`,
:func:`copy_profile`, :func:`delete_profile`, :func:`active_profile`,
:func:`set_active`, :func:`list_secrets`, :func:`fetch_models`; :data:`PRESETS`.
Built-in profiles (``configs/synth/profiles``) are read-only; the console writes
the user's own to ``configs/synth/profiles.local`` (gitignored). Keys go to the
shared local secret store (:func:`synth.config.write_secret`) and are never
returned — only whether one is present.
Stores written: files only (profiles, secret store). Token caps: none.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import yaml

from synth.config import PROFILES_DIR, load_secrets, local_profiles_dir, profile_path, resolve_secret

SLOTS = ("t1_micro", "t2", "t3")
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
DEFAULT_PROFILE = "granite-mistral"
SLOT_FIELDS = ("provider", "base_url", "model", "api_key_env", "local", "context_length", "structured_mode",
               "temperature", "reasoning_budget", "price_per_mtok_in", "price_per_mtok_out", "timeout_s",
               "tokenizer")

PRESETS = [
    {"id": "lmstudio", "label": "LM Studio", "base_url": "http://localhost:1234/v1", "local": True, "needs_key": False},
    {"id": "ollama", "label": "Ollama", "base_url": "http://localhost:11434/v1", "local": True, "needs_key": False},
    {"id": "vllm", "label": "vLLM", "base_url": "http://localhost:8000/v1", "local": True, "needs_key": False},
    {"id": "llamacpp", "label": "llama.cpp server", "base_url": "http://localhost:8080/v1", "local": True,
     "needs_key": False},
    {"id": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1", "local": False, "needs_key": True},
    {"id": "openrouter", "label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "local": False,
     "needs_key": True},
    {"id": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "local": False,
     "needs_key": True},
    {"id": "mistral", "label": "Mistral", "base_url": "https://api.mistral.ai/v1", "local": False, "needs_key": True},
    {"id": "groq", "label": "Groq", "base_url": "https://api.groq.com/openai/v1", "local": False, "needs_key": True},
    {"id": "together", "label": "Together", "base_url": "https://api.together.xyz/v1", "local": False,
     "needs_key": True},
    {"id": "opencode_go", "label": "OpenCode Go", "base_url": "https://opencode.ai/inference/go/openai/v1",
     "local": False, "needs_key": True},
    {"id": "anthropic", "label": "Anthropic (OpenAI-compatible)", "base_url": "https://api.anthropic.com/v1",
     "local": False, "needs_key": True},
    {"id": "custom", "label": "Custom (OpenAI-compatible)", "base_url": "", "local": None, "needs_key": False},
]


def _active_file() -> Path:
    return local_profiles_dir() / "_active.txt"


class SettingsError(ValueError):
    """Bad input for a settings change (HTTP 400)."""


def _check_name(name: str) -> str:
    if not NAME.match(name or ""):
        raise SettingsError("profile names use a-z, 0-9, '.', '_' or '-' (max 48 characters)")
    return name


def _preset_for(base_url: str | None) -> str | None:
    for p in PRESETS:
        if p["base_url"] and base_url and base_url.rstrip("/").startswith(p["base_url"].rstrip("/")):
            return p["id"]
    return "custom" if base_url else None


def _is_local(base_url: str | None, flag: Any) -> bool:
    if flag is not None:
        return bool(flag)
    host = httpx.URL(base_url).host if base_url and "://" in base_url else ""
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")


def _read(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _slot_view(raw: dict[str, Any]) -> dict[str, Any]:
    base, model = raw.get("base_url") or "", raw.get("model") or ""
    local = _is_local(base, raw.get("local"))
    env = raw.get("api_key_env")
    return {"provider": raw.get("provider") or _preset_for(base), "base_url": base, "model": model,
            "api_key_env": env, "key_present": bool(resolve_secret(env)), "local": local,
            "context_length": raw.get("context_length"), "structured_mode": raw.get("structured_mode"),
            "temperature": raw.get("temperature"), "reasoning_budget": raw.get("reasoning_budget", 0),
            "price_per_mtok_in": raw.get("price_per_mtok_in", 0.0),
            "price_per_mtok_out": raw.get("price_per_mtok_out", 0.0),
            "configured": bool(base and model and "<" not in base and "<" not in model)}


def _view(name: str, path: Path, active: str) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    first = text.splitlines()[0] if text else ""
    builtin = path.parent == PROFILES_DIR
    endpoints = data.get("endpoints") or {}
    return {"name": name, "description": data.get("description") or first.lstrip("# ").strip(),
            "builtin": builtin, "editable": not builtin, "active": name == active,
            "slots": {s: _slot_view(endpoints.get(s) or {}) for s in SLOTS},
            "gliner": dict(data.get("gliner") or {})}


def _names() -> dict[str, Path]:
    found = {p.stem: p for p in sorted(PROFILES_DIR.glob("*.yaml"))}
    found.update({p.stem: p for p in sorted(local_profiles_dir().glob("*.yaml"))})
    return found


def active_profile() -> str:
    try:
        name = _active_file().read_text(encoding="utf-8").strip()
    except OSError:
        name = ""
    return name if name in _names() else DEFAULT_PROFILE


def set_active(name: str) -> str:
    if name not in _names():
        raise KeyError(name)
    local_profiles_dir().mkdir(parents=True, exist_ok=True)
    _active_file().write_text(name + "\n", encoding="utf-8")
    return name


def list_profiles() -> list[dict[str, Any]]:
    active = active_profile()
    return [_view(n, p, active) for n, p in _names().items()]


def get_profile(name: str) -> dict[str, Any]:
    if name not in _names():
        raise KeyError(name)
    return _view(name, profile_path(name), active_profile())


def save_profile(name: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Apply partial changes to an editable profile and write it back."""
    path = local_profiles_dir() / f"{_check_name(name)}.yaml"
    if not path.exists():
        if (PROFILES_DIR / f"{name}.yaml").exists():
            raise SettingsError(f"'{name}' is a built-in profile; copy it to make an editable line-up")
        raise KeyError(name)
    data = _read(path)
    endpoints = data.setdefault("endpoints", {})
    for slot, values in (changes.get("slots") or {}).items():
        if slot not in SLOTS:
            raise SettingsError(f"unknown slot {slot!r} (slots: {', '.join(SLOTS)})")
        current = endpoints.setdefault(slot, {})
        for key, value in (values or {}).items():
            if key not in SLOT_FIELDS:
                continue
            if key == "api_key_env" and value and not ENV_NAME.match(value):
                raise SettingsError("key variable names use A-Z, 0-9 and '_' (e.g. SYNTH_T3_API_KEY)")
            if value is None or value == "":
                current.pop(key, None)
            else:
                current[key] = value
        current.setdefault("structured_mode", "strict_schema")
    if "gliner" in changes:
        data["gliner"] = {**(data.get("gliner") or {}), **{k: v for k, v in (changes["gliner"] or {}).items()
                                                           if k in ("model", "device", "threshold", "window_words",
                                                                    "overlap_words")}}
    if changes.get("description") is not None:
        data["description"] = str(changes["description"])[:200]
    header = f"# {data.get('description') or name}\n"
    path.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return get_profile(name)


def copy_profile(source: str, new_name: str) -> dict[str, Any]:
    if source not in _names():
        raise KeyError(source)
    _check_name(new_name)
    if new_name in _names():
        raise SettingsError(f"a profile named '{new_name}' already exists")
    data = _read(profile_path(source))
    data["description"] = f"{data.get('description') or source} (copy)"
    local_profiles_dir().mkdir(parents=True, exist_ok=True)
    (local_profiles_dir() / f"{new_name}.yaml").write_text(
        f"# {data['description']}\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return get_profile(new_name)


def delete_profile(name: str) -> None:
    path = local_profiles_dir() / f"{_check_name(name)}.yaml"
    if not path.exists():
        if name in _names():
            raise SettingsError(f"'{name}' is a built-in profile and cannot be deleted")
        raise KeyError(name)
    was_active = active_profile() == name
    path.unlink()
    if was_active:
        _active_file().unlink(missing_ok=True)


def list_secrets() -> list[dict[str, Any]]:
    import os

    stored = load_secrets()
    used: dict[str, list[str]] = {}
    for profile in list_profiles():
        for slot, view in profile["slots"].items():
            if view.get("api_key_env"):
                used.setdefault(view["api_key_env"], []).append(f"{profile['name']}/{slot}")
    names = sorted(set(used) | set(stored))
    return [{"name": n, "present": bool(os.environ.get(n) or stored.get(n)),
             "source": "env" if os.environ.get(n) else ("file" if stored.get(n) else None),
             "used_by": used.get(n, [])} for n in names]


def fetch_models(base_url: str, api_key_env: str | None = None, timeout: float = 8.0) -> dict[str, Any]:
    """``GET {base_url}/models`` from the server side, so a key never reaches the browser."""
    if not base_url or "://" not in base_url:
        return {"ok": False, "error": "enter a base URL such as http://localhost:1234/v1"}
    headers = {}
    key = resolve_secret(api_key_env)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = base_url.rstrip("/") + "/models"
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    if resp.status_code >= 400:
        return {"ok": False, "error": f"HTTP {resp.status_code} from {url}: {resp.text[:200]}"}
    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "error": f"{url} did not answer JSON"}
    items = data.get("data") if isinstance(data, dict) else data
    ids = sorted({str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id")})
    return {"ok": True, "models": ids}
