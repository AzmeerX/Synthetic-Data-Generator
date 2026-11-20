"""J1 over HTTP: the Store Query API as read-only FastAPI GET routes.

Purpose: let the viewer and later tool-using agents ask the stores the same
five questions U9/U10 ask in process. Contract: :func:`create_app` returns a
FastAPI app with GET routes only (no POST/PUT/PATCH/DELETE anywhere):

    GET /sentences/{doc_id}?ids=s41&ids=s42&n=2      (ids may also be comma-separated)
    GET /entities/{entity_id}
    GET /entities/{entity_id}/edges?level=macro&status=VERIFIED
    GET /entities/{entity_id}/timeline
    GET /provenance/{object_id}

Every route takes an optional ``run_id`` query parameter; the default is the
``run_id`` given to :func:`create_app`, else the newest run in L1 (ULIDs sort by time).
Stores written: none. Token caps: none. Spec: P1 §3 J1.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import func, select

from synth.api import query
from synth.store import Store
from synth.store.tables import runs


def _split(values: list[str]) -> list[str]:
    return [v.strip() for value in values for v in value.split(",") if v.strip()]


def create_app(store: Store, run_id: str | None = None) -> FastAPI:
    app = FastAPI(title="Synth J1 — Store Query API (read-only)", version="1")

    def run_of(requested: str | None) -> str:
        if requested:
            return requested
        if run_id:
            return run_id
        with store.reader() as r:
            latest = r.execute(select(func.max(runs.c.run_id))).scalar()
        if not latest:
            raise HTTPException(status_code=400, detail="no run_id given and no runs in the store")
        return latest

    @app.get("/sentences/{doc_id}")
    def sentences(doc_id: str, ids: list[str] = Query(default=[]), n: int = 0,
                  run_id: str | None = None) -> dict[str, Any]:
        answer = query.get_sentences(store, doc_id, _split(ids), n=n, run_id=run_of(run_id))
        if not answer["found"]:
            raise HTTPException(status_code=404, detail=f"no document {doc_id}")
        return answer

    @app.get("/entities/{entity_id}")
    def entity(entity_id: str, run_id: str | None = None) -> dict[str, Any]:
        answer = query.get_entity(store, run_of(run_id), entity_id)
        if answer is None:
            raise HTTPException(status_code=404, detail=f"no entity {entity_id}")
        return answer

    @app.get("/entities/{entity_id}/edges")
    def edges(entity_id: str, level: str = "macro", status: list[str] = Query(default=[]),
              run_id: str | None = None) -> dict[str, Any]:
        if level not in ("macro", "micro"):
            raise HTTPException(status_code=422, detail="level must be macro or micro")
        wanted = _split(status)
        return query.get_edges(store, run_of(run_id), entity_id, level=level,
                               status=wanted[0] if len(wanted) == 1 else (wanted or None))

    @app.get("/entities/{entity_id}/timeline")
    def timeline(entity_id: str, run_id: str | None = None) -> dict[str, Any]:
        return query.get_timeline(store, run_of(run_id), entity_id)

    @app.get("/provenance/{object_id}")
    def provenance(object_id: str, run_id: str | None = None) -> dict[str, Any]:
        answer = query.get_provenance(store, run_of(run_id), object_id)
        if not answer["found"] and not answer["changes"]:
            raise HTTPException(status_code=404, detail=f"nothing known about {object_id}")
        return answer

    return app
