"""Console model servers: plug-and-play endpoints for the workshop.

Purpose: let the operator connect a model server by kind + host + port (or a base
URL), find local servers on their usual ports, start/stop the ones whose tool is
installed (LM Studio ``lms``, Ollama, llama.cpp ``llama-server``), load models,
and see live status (reachable, latency, models) — then assign a server's model
to an engine slot in one click.
Contract: :func:`list_servers`, :func:`add_server`, :func:`update_server`,
:func:`remove_server`, :func:`detect`, :func:`launchers`, :class:`Launchers`
(``start`` / ``stop`` / ``managed``), :func:`load_command`, :func:`normalise`.
Saved servers live in ``<profiles.local>/_servers.yaml`` (gitignored). Probes are
``GET {base}/models`` only — model calls still go through the gateway.
Stores written: files only. Token caps: none.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import yaml

from synth.config import local_profiles_dir, resolve_secret
from synth.console import settings

LOCAL_KINDS = {"lmstudio": ("LM Studio", 1234), "ollama": ("Ollama", 11434), "vllm": ("vLLM", 8000),
               "llamacpp": ("llama.cpp", 8080), "jan": ("Jan", 1337)}
CLOUD_KINDS = {p["id"]: p for p in settings.PRESETS if not p["local"] and p["base_url"]}
KINDS = set(LOCAL_KINDS) | set(CLOUD_KINDS) | {"custom"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class ServerError(ValueError):
    """Bad server input (HTTP 400)."""


def _file() -> Path:
    return local_profiles_dir() / "_servers.yaml"


def _read() -> list[dict[str, Any]]:
    try:
        data = yaml.safe_load(_file().read_text(encoding="utf-8")) or {}
    except OSError:
        return []
    return [s for s in (data.get("servers") or []) if isinstance(s, dict) and s.get("base_url")]


def _write(servers: list[dict[str, Any]]) -> None:
    _file().parent.mkdir(parents=True, exist_ok=True)
    _file().write_text("# Model servers added in the Synth console.\n"
                       + yaml.safe_dump({"servers": servers}, sort_keys=False), encoding="utf-8")


def normalise(kind: str, base_url: str | None = None, host: str | None = None, port: int | None = None) -> str:
    """A base URL from kind + host/port or a typed URL: scheme added, '/v1' for local servers."""
    if kind in CLOUD_KINDS and not (base_url or host):
        return CLOUD_KINDS[kind]["base_url"]
    if not base_url:
        if not host and kind in LOCAL_KINDS:
            host = "localhost"
        if not host:
            raise ServerError("enter a host and port, or a base URL")
        port = port or LOCAL_KINDS.get(kind, ("", None))[1]
        base_url = f"{host}:{port}" if port else host
    url = base_url.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    parsed = httpx.URL(url)
    if not parsed.host:
        raise ServerError(f"not a valid address: {base_url!r}")
    if (kind in LOCAL_KINDS or kind == "custom") and not re.search(r"/v\d+$", url):
        url += "/v1"
    return url


def _is_local(url: str) -> bool:
    return (httpx.URL(url).host or "") in LOCAL_HOSTS


def _server_id(kind: str, url: str) -> str:
    parsed = httpx.URL(url)
    slug = re.sub(r"[^a-z0-9]+", "_", f"{parsed.host}_{parsed.port or ''}".lower()).strip("_")
    return f"srv_{kind}_{slug}"


def probe(base_url: str, api_key_env: str | None = None, timeout: float = 3.0) -> dict[str, Any]:
    started = time.monotonic()
    result = settings.fetch_models(base_url, api_key_env, timeout=timeout)
    return {"reachable": bool(result.get("ok")), "latency_ms": int((time.monotonic() - started) * 1000),
            "models": result.get("models") or [], "error": result.get("error")}


def _same(a: str, b: str) -> bool:
    return (a or "").rstrip("/") == (b or "").rstrip("/")


def list_servers(launch: Launchers | None = None, with_status: bool = True) -> list[dict[str, Any]]:
    saved = _read()
    profiles = settings.list_profiles()
    for p in profiles:                                       # a URL a build uses is a server too
        for view in p["slots"].values():
            url = view.get("base_url") or ""
            if view.get("configured") and not any(_same(url, s["base_url"]) for s in saved):
                kind = view.get("provider") or settings._preset_for(url) or "custom"
                saved.append({"id": _server_id(kind, url), "name": settings_label(kind), "kind": kind,
                              "base_url": url, "api_key_env": view.get("api_key_env"), "auto": True})
    out = []
    for s in saved:
        used = [f"{p['name']}/{slot}" for p in profiles for slot, v in p["slots"].items()
                if v.get("configured") and _same(v.get("base_url"), s["base_url"])]
        managed = launch.managed_for(s["base_url"]) if launch else None
        out.append({"id": s["id"], "name": s.get("name") or settings_label(s["kind"]), "kind": s["kind"],
                    "base_url": s["base_url"], "local": _is_local(s["base_url"]),
                    "api_key_env": s.get("api_key_env"), "key_present": bool(resolve_secret(s.get("api_key_env"))),
                    "auto": bool(s.get("auto")), "used_by": used, "managed": managed, "status": None})
    if with_status and out:
        with ThreadPoolExecutor(max_workers=min(8, len(out))) as pool:
            for s, status in zip(out, pool.map(lambda x: probe(x["base_url"], x["api_key_env"]), out)):
                s["status"] = status
    return out


def settings_label(kind: str) -> str:
    if kind in LOCAL_KINDS:
        return LOCAL_KINDS[kind][0]
    if kind in CLOUD_KINDS:
        return CLOUD_KINDS[kind]["label"]
    return "Custom server"


def get_server(server_id: str) -> dict[str, Any]:
    for s in list_servers(with_status=False):
        if s["id"] == server_id:
            return s
    raise KeyError(server_id)


def add_server(kind: str, name: str | None = None, base_url: str | None = None, host: str | None = None,
               port: int | None = None, api_key_env: str | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ServerError(f"kind must be one of {sorted(KINDS)}")
    if api_key_env and not settings.ENV_NAME.match(api_key_env):
        raise ServerError("key variable names use A-Z, 0-9 and '_' (e.g. SYNTH_OPENROUTER_KEY)")
    url = normalise(kind, base_url, host, port)
    servers = [s for s in _read() if not _same(s["base_url"], url)]
    entry = {"id": _server_id(kind, url), "name": (name or settings_label(kind))[:60], "kind": kind,
             "base_url": url, "api_key_env": api_key_env or None}
    servers.append(entry)
    _write(servers)
    return get_server(entry["id"])


def update_server(server_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    servers = _read()
    current = next((s for s in servers if s["id"] == server_id), None)
    if current is None:
        auto = get_server(server_id)                         # an auto-listed server becomes a saved one
        current = {k: auto[k] for k in ("id", "name", "kind", "base_url", "api_key_env")}
        servers.append(current)
    if changes.get("name"):
        current["name"] = str(changes["name"])[:60]
    if changes.get("base_url"):
        current["base_url"] = normalise(current["kind"], changes["base_url"])
    if "api_key_env" in changes:
        env = changes["api_key_env"] or None
        if env and not settings.ENV_NAME.match(env):
            raise ServerError("key variable names use A-Z, 0-9 and '_'")
        current["api_key_env"] = env
    _write(servers)
    return get_server(server_id)


def remove_server(server_id: str) -> None:
    servers = _read()
    kept = [s for s in servers if s["id"] != server_id]
    if len(kept) == len(servers):
        raise KeyError(server_id)
    _write(kept)


def detect(timeout: float = 1.5) -> list[dict[str, Any]]:
    saved = {s["base_url"].rstrip("/") for s in _read()}
    candidates = [(kind, label, port, f"http://localhost:{port}/v1") for kind, (label, port) in LOCAL_KINDS.items()]
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        results = list(pool.map(lambda c: probe(c[3], timeout=timeout), candidates))
    return [{"kind": kind, "name": label, "port": port, "base_url": url, "models": res["models"],
             "saved": url in saved}
            for (kind, label, port, url), res in zip(candidates, results) if res["reachable"]]


# ------------------------------------------------------------------ launchers (start/stop local servers)
LAUNCHERS = {
    "lmstudio": {"label": "LM Studio", "tool": "lms", "default_port": 1234, "needs_model_path": False,
                 "can_load": True, "hint": "install LM Studio and run it once; it puts `lms` on your PATH"},
    "ollama": {"label": "Ollama", "tool": "ollama", "default_port": 11434, "needs_model_path": False,
               "can_load": True, "hint": "install Ollama from ollama.com"},
    "llamacpp": {"label": "llama.cpp server", "tool": "llama-server", "default_port": 8080,
                 "needs_model_path": True, "can_load": False, "hint": "install llama.cpp; give a .gguf model file"},
}


def _command(kind: str, port: int, model_path: str | None) -> list[str]:
    tool = shutil.which(LAUNCHERS[kind]["tool"]) or LAUNCHERS[kind]["tool"]
    if kind == "lmstudio":
        return [tool, "server", "start", "--port", str(port)]
    if kind == "ollama":
        return [tool, "serve"]
    if not model_path:
        raise ServerError("llama.cpp needs a .gguf model file path")
    return [tool, "-m", model_path, "--port", str(port)]


def launchers() -> list[dict[str, Any]]:
    out = []
    for kind, info in LAUNCHERS.items():
        port = info["default_port"]
        cmd = " ".join(Path(x).name if i == 0 else x for i, x in
                       enumerate(_command(kind, port, "<model.gguf>")))
        out.append({"kind": kind, "label": info["label"], "available": shutil.which(info["tool"]) is not None,
                    "command": cmd, "default_port": port, "needs_model_path": info["needs_model_path"],
                    "can_load": info["can_load"], "hint": info["hint"]})
    return out


def load_command(server: dict[str, Any], model: str) -> list[str]:
    """The command that loads/pulls ``model`` on a server (LM Studio, Ollama)."""
    if not model or not re.match(r"^[\w./:@-]{1,200}$", model):
        raise ServerError("enter a model id")
    if server["kind"] == "lmstudio" and shutil.which("lms"):
        return [shutil.which("lms"), "load", model, "--yes"]
    if server["kind"] == "ollama" and shutil.which("ollama"):
        return [shutil.which("ollama"), "pull", model]
    raise ServerError(f"loading models is supported for LM Studio (lms) and Ollama when installed, not {server['kind']}")


class Launchers:
    """Processes the console started. Long-running servers (Ollama, llama.cpp) are kept and
    can be stopped; ``lms server start`` returns at once and is stopped with ``lms server stop``."""

    def __init__(self) -> None:
        self.procs: dict[str, dict[str, Any]] = {}

    def managed_for(self, base_url: str) -> dict[str, Any] | None:
        for info in self.procs.values():
            if _same(info["base_url"], base_url):
                proc = info.get("proc")
                running = info.get("daemon", False) or (proc is not None and proc.poll() is None)
                return {"running": running, "pid": proc.pid if proc else None, "log": list(info["log"])[-50:]}
        return None

    def start(self, kind: str, port: int | None = None, model_path: str | None = None) -> dict[str, Any]:
        if kind not in LAUNCHERS:
            raise ServerError(f"no launcher for {kind!r}")
        if not shutil.which(LAUNCHERS[kind]["tool"]):
            raise ServerError(f"{LAUNCHERS[kind]['label']} is not installed here: {LAUNCHERS[kind]['hint']}")
        port = int(port or LAUNCHERS[kind]["default_port"])
        cmd = _command(kind, port, model_path)
        base_url = f"http://localhost:{port}/v1"
        log: deque = deque(maxlen=200)
        info: dict[str, Any] = {"kind": kind, "base_url": base_url, "log": log, "proc": None}
        if kind == "lmstudio":
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
            log.extend((done.stdout + done.stderr).splitlines())
            if done.returncode != 0:
                raise ServerError(f"lms server start failed: {(done.stderr or done.stdout)[-300:]}")
            info["daemon"] = True
        else:
            env = None
            if kind == "ollama":
                import os
                env = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{port}")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
            info["proc"] = proc
            threading.Thread(target=lambda: [log.append(line.rstrip()) for line in proc.stdout], daemon=True).start()
        self.procs[kind] = info
        server = add_server(kind, base_url=base_url)
        return {"server": server, "managed": self.managed_for(base_url)}

    def stop(self, kind: str) -> dict[str, Any]:
        info = self.procs.get(kind)
        if kind == "lmstudio" and shutil.which("lms"):
            subprocess.run([shutil.which("lms"), "server", "stop"], capture_output=True, timeout=30, check=False)
        elif not info or info.get("proc") is None:
            raise ServerError("this server was not started from the console")
        else:
            info["proc"].terminate()
        self.procs.pop(kind, None)
        return {"stopped": True}
