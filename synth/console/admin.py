"""Console v2 routes: model profiles and keys, the pipeline tree, the corpus, and
the data browser (stores, database, output files).

Purpose: the management side of the console — swap the model behind each stage
(Datanaut-style providers, keys and model tests), see the pipeline as an
inverted tree with live progress, manage the documents to ingest, and reach
every store, ledger and generated file from the browser.
Contract: :func:`add_routes` registers the "Addendum v2" routes of
``docs/CONSOLE_API.md`` on the console app. Keys are write-only; files are
served only from the run-output folder. Stores written: none (database is
read-only here; files: profiles, secret store, ``data/raw`` uploads).
Token caps: none. Spec: P1 §3 J0/J1/J2.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import String, cast, func, select, text

from synth import engines
from synth import states as S
from synth.api import query
from synth.config import SynthConfig, load_config, write_secret
from synth.console import gearbox, servers, settings
from synth.store import Store
from synth.store.tables import APPEND_ONLY_TABLES, TABLES

UPLOAD_TYPES = {".pdf", ".docx", ".txt", ".md", ".html", ".htm"}
MAX_UPLOAD = 100 * 1024 * 1024
CELL_CHARS = 2000

GROUPS = [
    ("S1 Documents", ["documents", "doc_texts", "sentences", "spans", "micro_chunks", "sections"]),
    ("S2 Entities", ["entities", "entity_aliases", "entity_roles", "mentions"]),
    ("S3 Time", ["time_refs", "edge_times"]),
    ("S4 Micro graph", ["micro_edges", "edge_spans", "edge_lineage"]),
    ("S5 Macro graph", ["macro_edges", "propositions", "prop_links"]),
    ("L1 Runs", ["runs", "stage_metrics"]),
    ("L2 Model calls", ["model_calls"]),
    ("L3 Coverage", ["coverage_events", "coverage_final"]),
    ("L4 Change log", ["change_log"]),
    ("L5 Resolutions", ["resolutions"]),
    ("L6 Flags", ["flags"]),
    ("L7 Escalations", ["escalations", "calibration_points"]),
    ("L8 Eval", ["eval_scores", "gold_sets"]),
    ("J0 Units", ["unit_status"]),
]

def _iso_mtime(path: Path) -> str:
    return _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.timezone.utc).isoformat(timespec="seconds")


def _cell(value: Any) -> Any:
    value = query.clean(value)
    if isinstance(value, str) and len(value) > CELL_CHARS:
        return value[:CELL_CHARS] + "…"
    return value


def _safe_name(name: str) -> str:
    base = Path(name or "").name
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .")
    return base[:150]


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def add_routes(app: FastAPI, store: Store, start: Callable[..., Any], run_row: Callable[..., Any],
               read_only: bool, config: SynthConfig | None = None) -> None:
    config = config or load_config()
    launch = servers.Launchers()
    app.state.launchers = launch
    raw_dir = Path(config.path("raw"))
    runs_dir = Path(config.path("runs"))

    def guard() -> None:
        if read_only:
            raise HTTPException(status_code=403, detail="demo mode: changes are disabled")

    def known(name: str) -> dict[str, Any]:
        try:
            return settings.get_profile(name)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no profile {name!r}")

    # ------------------------------------------------------------ profiles, keys, providers
    @app.get("/api/providers/presets")
    def presets() -> list[dict[str, Any]]:
        return settings.PRESETS

    @app.get("/api/profiles")
    def profiles() -> list[dict[str, Any]]:
        out = settings.list_profiles()
        for p in out:                                            # v1 fields, kept for older views
            p["models"] = {**{k: v["model"] for k, v in p["slots"].items()}, "gliner": p["gliner"].get("model")}
            p["default"] = p["active"]
        return out

    @app.get("/api/profiles/{name}")
    def profile(name: str) -> dict[str, Any]:
        return known(name)

    @app.put("/api/profiles/{name}")
    def save(name: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        guard()
        try:
            return settings.save_profile(name, body)
        except settings.SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no profile {name!r}")

    @app.post("/api/profiles/{name}/copy", status_code=201)
    def copy(name: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        guard()
        try:
            return settings.copy_profile(name, str(body.get("name") or ""))
        except settings.SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no profile {name!r}")

    @app.delete("/api/profiles/{name}")
    def delete(name: str) -> dict[str, Any]:
        guard()
        try:
            settings.delete_profile(name)
        except settings.SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no profile {name!r}")
        return {"deleted": name}

    @app.post("/api/profiles/{name}/activate")
    def activate(name: str) -> dict[str, Any]:
        guard()
        known(name)
        return {"active": settings.set_active(name)}

    @app.get("/api/secrets")
    def secrets() -> list[dict[str, Any]]:
        return settings.list_secrets()

    @app.put("/api/secrets/{env_name}")
    def put_secret(env_name: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        guard()
        if not settings.ENV_NAME.match(env_name):
            raise HTTPException(status_code=400, detail="key names use A-Z, 0-9 and '_' (e.g. SYNTH_T3_API_KEY)")
        value = str(body.get("value") or "").strip()
        if not value:
            raise HTTPException(status_code=400, detail="enter a key (use DELETE to clear one)")
        write_secret(env_name, value)
        return {"name": env_name, "present": True}

    @app.delete("/api/secrets/{env_name}")
    def delete_secret(env_name: str) -> dict[str, Any]:
        guard()
        if not settings.ENV_NAME.match(env_name):
            raise HTTPException(status_code=400, detail="bad key name")
        write_secret(env_name, None)
        return {"name": env_name, "present": bool(settings.resolve_secret(env_name))}

    @app.post("/api/providers/models")
    def provider_models(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return settings.fetch_models(str(body.get("base_url") or ""), body.get("api_key_env"))

    @app.post("/api/profiles/{name}/test")
    def test_slot(name: str, body: dict[str, Any] = Body(default_factory=dict)):
        known(name)
        slot = body.get("slot")
        if slot not in settings.SLOTS:
            raise HTTPException(status_code=400, detail=f"slot must be one of {list(settings.SLOTS)}")
        return start("smoke", ["smoke", "--endpoint", slot], name)

    @app.post("/api/preflight")
    def preflight(body: dict[str, Any] = Body(default_factory=dict)):
        name = body.get("profile") or settings.active_profile()
        prof = known(name)
        engine = engine_of(body.get("engine") or body.get("arm"))
        need = [s.endpoint for s in engine.slots if s.kind == "llm"
                and (s.required or prof["slots"][s.endpoint]["configured"])]
        if not need:
            raise HTTPException(status_code=400, detail=f"{engine.name} uses no model endpoints to test")
        return start("smoke", ["smoke", *[x for ep in dict.fromkeys(need) for x in ("--endpoint", ep)]], name)

    # ------------------------------------------------------------ engines and the pipeline tree
    def engine_of(engine_id: str | None) -> engines.Engine:
        if not engine_id:
            return engines.default_engine()
        try:
            return engines.get(engine_id)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/engines")
    def list_engines() -> list[dict[str, Any]]:
        return [e.as_dict() for e in engines.ENGINES.values()]

    @app.get("/api/pipeline")
    def pipeline(profile: str | None = None, run_id: str | None = None, engine: str | None = None) -> dict[str, Any]:
        prof = known(profile or settings.active_profile())
        with store.reader() as r:
            if run_id:
                run = run_row(r, run_id)
            elif engine:
                run = None
            else:
                latest = r.execute(select(func.max(TABLES["runs"].c.run_id))).scalar()
                run = r.get("runs", run_id=latest) if latest else None
            eng = engine_of(engine or (run["arm"] if run and run["arm"] in engines.ENGINES else None))
            if run is not None and run["arm"] != eng.id:
                run = None                                   # the run belongs to another engine
            rid = run["run_id"] if run else None
            progress: dict[str, Counter] = defaultdict(Counter)
            tokens: dict[str, Counter] = defaultdict(Counter)
            if rid:
                for u in r.select("unit_status", run_id=rid):
                    progress[u["stage"]][u["status"]] += 1
                for c in r.select("model_calls", run_id=rid):
                    t = tokens[c["stage"]]
                    t["in"] += c.get("tokens_in") or 0
                    t["out"] += c.get("tokens_out") or 0
                    t["calls"] += 1
            n_docs = len(r.select("documents"))
        known_servers = servers.list_servers(with_status=False)
        nodes = []
        for n in eng.nodes:
            info: dict[str, Any] = {**vars(n), "children": list(n.children), "tier": n.lane, "endpoint": None,
                                    "model": None, "provider": None, "local": None, "key_present": None,
                                    "configured": None, "server_id": None, "progress": None, "tokens": None}
            if n.id == "DOCS":
                info["detail"] = f"{n_docs} documents ingested"
            slot = eng.slot(n.slot) if n.slot else None
            if slot and slot.kind == "gliner":
                info.update(endpoint="gliner", model=prof["gliner"].get("model"), local=True,
                            configured=bool(prof["gliner"].get("model")))
            elif slot:
                sv = prof["slots"][slot.endpoint]
                match = next((x for x in known_servers if x["base_url"].rstrip("/") == (sv["base_url"] or "").rstrip("/")),
                             None)
                info.update(endpoint=slot.endpoint, model=sv["model"], provider=sv["provider"], local=sv["local"],
                            key_present=sv["key_present"], configured=sv["configured"],
                            server_id=match["id"] if match else None)
            if n.stage and n.stage in progress:
                p = progress[n.stage]
                info["progress"] = {"done": p[S.DONE], "total": sum(p.values()), "failed": p[S.FAILED],
                                    "pending": p[S.PENDING], "dead_letter": p[S.DEAD_LETTER]}
            if n.stage and n.stage in tokens:
                info["tokens"] = dict(tokens[n.stage])
            nodes.append(info)
        return {"engine": eng.id, "arm": eng.id, "profile": prof["name"], "run_id": rid, "nodes": nodes}

    # ------------------------------------------------------------ servers (plug and play)
    def server_or_404(server_id: str) -> dict[str, Any]:
        try:
            return servers.get_server(server_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no server {server_id}")

    @app.get("/api/servers")
    def list_servers() -> list[dict[str, Any]]:
        return servers.list_servers(launch)

    @app.post("/api/servers", status_code=201)
    def add_server(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        guard()
        try:
            port = int(body["port"]) if body.get("port") not in (None, "") else None
            return servers.add_server(str(body.get("kind") or "custom"), body.get("name"), body.get("base_url"),
                                      body.get("host"), port, body.get("api_key_env"))
        except (servers.ServerError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.put("/api/servers/{server_id}")
    def edit_server(server_id: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        guard()
        server_or_404(server_id)
        try:
            return servers.update_server(server_id, body)
        except servers.ServerError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.delete("/api/servers/{server_id}")
    def delete_server(server_id: str) -> dict[str, Any]:
        guard()
        try:
            servers.remove_server(server_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no saved server {server_id} (servers a build uses are listed "
                                                        "automatically; change the build to drop it)")
        return {"deleted": server_id}

    @app.post("/api/servers/detect")
    def detect() -> list[dict[str, Any]]:
        return servers.detect()

    @app.post("/api/servers/{server_id}/speedtest")
    def speedtest(server_id: str, body: dict[str, Any] = Body(default_factory=dict)):
        server = server_or_404(server_id)
        model = str(body.get("model") or "")
        if not model:
            raise HTTPException(status_code=400, detail="choose a model to test")
        args = ["speedtest", "--base-url", server["base_url"], "--model", model]
        if server.get("api_key_env"):
            args += ["--api-key-env", server["api_key_env"]]
        return start("speedtest", args)

    @app.post("/api/servers/{server_id}/load")
    def load_model(server_id: str, body: dict[str, Any] = Body(default_factory=dict)):
        server = server_or_404(server_id)
        try:
            command = servers.load_command(server, str(body.get("model") or ""))
        except servers.ServerError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return start("load", [], command=command)

    @app.get("/api/launchers")
    def list_launchers() -> list[dict[str, Any]]:
        return servers.launchers()

    @app.post("/api/launchers/{kind}/start")
    def start_server(kind: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        guard()
        try:
            return launch.start(kind, body.get("port"), body.get("model_path"))
        except servers.ServerError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/launchers/{kind}/stop")
    def stop_server(kind: str) -> dict[str, Any]:
        guard()
        try:
            return launch.stop(kind)
        except servers.ServerError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------ dyno (engine performance)
    @app.get("/api/bench")
    def bench(limit: int = Query(20, ge=1, le=200)) -> list[dict[str, Any]]:
        out = []
        with store.reader() as r:
            docs = {d["doc_id"]: d for d in r.select("documents")}
            sentence_counts = Counter(x["doc_id"] for x in r.select("sentences"))
            rows = sorted((x for x in r.select("runs") if x["arm"] in engines.ENGINES),
                          key=lambda x: x["run_id"], reverse=True)[:limit]
            for row in rows:
                rid, manifest = row["run_id"], row.get("manifest") or {}
                doc_ids = manifest.get("doc_ids") or []
                pages = sum(int(docs[d].get("pages") or 0) for d in doc_ids if d in docs)
                calls = r.select("model_calls", run_id=rid)
                slots: dict[tuple, Counter] = defaultdict(Counter)
                local = frontier = 0
                cost = 0.0
                for c in calls:
                    g = slots[(c["endpoint"], c.get("model_id"), c["stage"])]
                    g["calls"] += 1
                    g["tokens_in"] += c.get("tokens_in") or 0
                    g["tokens_out"] += c.get("tokens_out") or 0
                    g["latency_ms"] += c.get("latency_ms") or 0
                    used = (c.get("tokens_in") or 0) + (c.get("tokens_out") or 0)
                    if c["endpoint"] == "t3":
                        frontier += used
                    else:
                        local += used
                    cost += c.get("cost_usd") or 0.0
                facts = sum(1 for e in r.select("macro_edges", run_id=rid) if e["status"] == S.COMMITTED)
                started, finished = row.get("started_at"), row.get("finished_at")
                wall = (finished - started).total_seconds() if started and finished and hasattr(started, "year") else None
                scores = {(x["metric"], x["slice"]): x["value"] for x in r.select("eval_scores", run_id=rid, arm=row["arm"])}
                quality = ({"evaluated": True, "fact_f1": scores.get(("fact.f1", "all")),
                            "entity_f1": scores.get(("entity.cluster.f1", "all"))} if scores else {"evaluated": False})
                out.append({
                    "run_id": rid, "engine": row["arm"], "archived": rid in gearbox.archived_runs(), "engine_name": engines.ENGINES[row["arm"]].name,
                    "profile": manifest.get("profile"), "status": row["status"],
                    "started_at": started.isoformat() if hasattr(started, "isoformat") else started,
                    "documents": len(doc_ids), "pages": pages,
                    "sentences": sum(sentence_counts.get(d, 0) for d in doc_ids),
                    "wall_seconds": wall, "seconds_per_page": round(wall / pages, 1) if wall and pages else None,
                    "facts_committed": facts,
                    "facts_per_minute": round(facts / (wall / 60), 2) if wall else None,
                    "entities": len(r.select("entities", run_id=rid, status=S.ACTIVE)),
                    "tokens_local": local, "tokens_frontier": frontier,
                    "ftvf": round(frontier / facts, 1) if facts else None, "cost_usd": round(cost, 4),
                    "slots": [{"endpoint": k[0], "model": k[1], "stage": k[2], **v,
                               "tokens_per_s": round(v["tokens_out"] / (v["latency_ms"] / 1000), 1)
                               if v["latency_ms"] else None} for k, v in slots.items()],
                    "quality": quality})
        return out

    @app.post("/api/bench/eval")
    def bench_eval(body: dict[str, Any] = Body(default_factory=dict)):
        ids = [str(i) for i in (body.get("run_ids") or [])]
        if len(ids) != 1:
            raise HTTPException(status_code=400, detail="evaluate one run at a time")
        with store.reader() as r:
            run_row(r, ids[0])
        return start("eval", ["eval", "--run", ids[0]])

    # ------------------------------------------------------------ corpus
    @app.get("/api/corpus")
    def corpus() -> list[dict[str, Any]]:
        with store.reader() as r:
            by_sha = {d["sha256"]: d["doc_id"] for d in r.select("documents")}
        out = []
        if raw_dir.exists():
            for path in sorted(p for p in raw_dir.iterdir() if p.is_file() and not p.name.startswith(".")):
                doc = by_sha.get(_file_sha(path))
                out.append({"name": path.name, "size": path.stat().st_size, "modified": _iso_mtime(path),
                            "ingested": doc is not None, "doc_id": doc})
        return out

    @app.post("/api/corpus/upload")
    async def upload(files: list[UploadFile] = File(...)) -> dict[str, Any]:
        guard()
        raw_dir.mkdir(parents=True, exist_ok=True)
        saved, skipped = [], []
        for f in files:
            name = _safe_name(f.filename or "")
            if not name or Path(name).suffix.lower() not in UPLOAD_TYPES:
                skipped.append({"name": f.filename, "reason": f"allowed types: {' '.join(sorted(UPLOAD_TYPES))}"})
                continue
            data = await f.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                skipped.append({"name": name, "reason": "larger than 100 MB"})
                continue
            (raw_dir / name).write_bytes(data)
            saved.append(name)
        return {"saved": saved, "skipped": skipped}

    @app.delete("/api/corpus/{name}")
    def delete_file(name: str) -> dict[str, Any]:
        guard()
        path = raw_dir / _safe_name(name)
        if path.name != name or not path.is_file():
            raise HTTPException(status_code=404, detail=f"no file {name!r} in {raw_dir}")
        path.unlink()
        return {"deleted": name}

    # ------------------------------------------------------------ data: database, stores
    @app.get("/api/data/db")
    def db_info() -> dict[str, Any]:
        engine = store.engine
        out: dict[str, Any] = {"dialect": engine.dialect.name,
                               "url": str(engine.url).split("@")[-1] if "@" in str(engine.url) else str(engine.url),
                               "revision": None, "size_bytes": None}
        with engine.connect() as conn:
            try:
                out["revision"] = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except Exception:  # noqa: BLE001 - no alembic table (tests / SQLite)
                conn.rollback()
            if engine.dialect.name == "postgresql":
                out["size_bytes"] = conn.execute(text("SELECT pg_database_size(current_database())")).scalar()
        return out

    @app.get("/api/data/tables")
    def tables() -> list[dict[str, Any]]:
        out = []
        with store.reader() as r:
            listed = set()
            for group, names in GROUPS:
                for name in names:
                    if name in TABLES:
                        listed.add(name)
                        out.append({"name": name, "group": group, "append_only": name in APPEND_ONLY_TABLES,
                                    "rows": r.execute(select(func.count()).select_from(TABLES[name])).scalar()})
            for name in sorted(set(TABLES) - listed):
                out.append({"name": name, "group": "Other", "append_only": name in APPEND_ONLY_TABLES,
                            "rows": r.execute(select(func.count()).select_from(TABLES[name])).scalar()})
        return out

    @app.get("/api/data/tables/{name}")
    def table_rows(name: str, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                   run_id: str | None = None, column: str | None = None, value: str | None = None,
                   order: str = "desc") -> dict[str, Any]:
        table = TABLES.get(name)
        if table is None:
            raise HTTPException(status_code=404, detail=f"no table {name!r}")
        where = []
        if run_id and "run_id" in table.c:
            where.append(table.c.run_id == run_id)
        if column:
            if column not in table.c:
                raise HTTPException(status_code=400, detail=f"{name} has no column {column!r}")
            where.append(cast(table.c[column], String) == value)
        key = list(table.primary_key.columns) or [list(table.c)[0]]
        ordering = [c.desc() if order == "desc" else c.asc() for c in key]
        with store.reader() as r:
            total = r.execute(select(func.count()).select_from(table).where(*where)).scalar()
            rows = r.execute(select(table).where(*where).order_by(*ordering).limit(limit).offset(offset)).mappings().all()
        return {"name": name, "columns": [c.name for c in table.c], "total": total, "limit": limit,
                "offset": offset, "rows": [{k: _cell(v) for k, v in row.items()} for row in rows]}

    # ------------------------------------------------------------ outputs
    @app.get("/api/outputs")
    def outputs() -> list[dict[str, Any]]:
        out = []
        if runs_dir.exists():
            for path in runs_dir.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(runs_dir)
                first = rel.parts[0]
                kind = ("export" if "export" in rel.parts else "viewer" if "viewer" in rel.parts
                        else "report" if "report" in rel.parts else "log" if path.suffix in (".json", ".log")
                        and first.startswith("session_") else "other")
                out.append({"path": rel.as_posix(), "run_id": first if first.startswith("run_") else None,
                            "kind": kind, "size": path.stat().st_size, "modified": _iso_mtime(path)})
        out.sort(key=lambda x: x["modified"], reverse=True)
        return out

    @app.get("/api/outputs/file")
    def output_file(path: str):
        target = (runs_dir / path).resolve()
        root = runs_dir.resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(status_code=400, detail="files are served only from the run-output folder")
        inline = target.suffix.lower() in (".html", ".htm", ".json", ".md", ".txt", ".svg")
        return FileResponse(target, filename=None if inline else target.name)

    @app.post("/api/runs/{run_id}/export")
    def export(run_id: str):
        with store.reader() as r:
            run_row(r, run_id)
        return start("export", ["export", "--run", run_id])

    @app.post("/api/runs/{run_id}/view")
    def view(run_id: str):
        with store.reader() as r:
            run_row(r, run_id)
        return start("view", ["view", "--run", run_id])

    @app.post("/api/reports")
    def report(body: dict[str, Any] = Body(default_factory=dict)):
        ids = [str(i) for i in (body.get("run_ids") or [])]
        if not ids:
            raise HTTPException(status_code=400, detail="choose at least one run")
        with store.reader() as r:
            for rid in ids:
                run_row(r, rid)
        return start("report", ["report", *[x for rid in ids for x in ("--runs", rid)]])
