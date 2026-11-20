"""Transactional write helpers that keep the change log (L4) honest.

Purpose: the only way modules write stores. Every insert into a logged table
writes a ``CREATE`` row to ``change_log`` and every update writes the before and
after images, in the same transaction (CLAUDE.md invariant 4). There is no
delete method: ``[A]`` tables reject DELETE at the database level anyway.
Contract: :class:`Tx` — ``insert``, ``insert_many``, ``update``, ``get``,
``select``, ``log_change``. Spec: P1 §4, §5 (lineage), §11 (state machines).
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Iterable, Sequence

from sqlalchemy import and_, select as sa_select
from sqlalchemy.engine import Connection

from synth.ids import new_id
from synth.store.tables import LOGGED_OBJECTS, TABLES, change_log


def _jsonable(value: Any) -> Any:
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _clean_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _jsonable(v) for k, v in row.items() if k != "ts"}


class Tx:
    """A connection inside one transaction, with run/stage/module context."""

    def __init__(self, conn: Connection, run_id: str | None = None, stage: str | None = None,
                 module: str | None = None, read_only: bool = False) -> None:
        self.conn = conn
        self.run_id = run_id
        self.stage = stage
        self.module = module
        self.read_only = read_only

    # -- context ---------------------------------------------------------------
    def with_context(self, *, run_id: str | None = None, stage: str | None = None,
                     module: str | None = None) -> "Tx":
        return Tx(self.conn, run_id=run_id or self.run_id, stage=stage or self.stage,
                  module=module or self.module, read_only=self.read_only)

    def close(self) -> None:
        self.conn.close()

    # -- reads -----------------------------------------------------------------
    @staticmethod
    def table(name: str):
        try:
            return TABLES[name]
        except KeyError as exc:
            raise KeyError(f"unknown table {name!r}") from exc

    def get(self, table: str, **key: Any) -> dict[str, Any] | None:
        rows = self.select(table, **key)
        return rows[0] if rows else None

    def select(self, table: str, order_by: str | Sequence[str] | None = None,
               where: Any = None, **equals: Any) -> list[dict[str, Any]]:
        t = self.table(table)
        stmt = sa_select(t)
        clauses = [t.c[k] == v for k, v in equals.items()]
        if where is not None:
            clauses.append(where)
        if clauses:
            stmt = stmt.where(and_(*clauses))
        if order_by:
            names = [order_by] if isinstance(order_by, str) else list(order_by)
            stmt = stmt.order_by(*[t.c[n] for n in names])
        return [dict(r._mapping) for r in self.conn.execute(stmt)]

    def execute(self, stmt):
        return self.conn.execute(stmt)

    def rows(self, stmt) -> list[dict[str, Any]]:
        return [dict(r._mapping) for r in self.conn.execute(stmt)]

    # -- writes ----------------------------------------------------------------
    def _check_writable(self) -> None:
        if self.read_only:
            raise RuntimeError("this Tx is read-only")

    def insert(self, table: str, row: dict[str, Any], *, call_id: str | None = None,
               rule_id: str | None = None, log: bool | None = None) -> dict[str, Any]:
        """Insert one row; logged tables also get a ``CREATE`` change-log row."""
        self._check_writable()
        t = self.table(table)
        row = dict(row)
        if "run_id" in t.c and "run_id" not in row and self.run_id is not None:
            row["run_id"] = self.run_id
        unknown = set(row) - set(t.c.keys())
        if unknown:
            raise KeyError(f"{table}: unknown columns {sorted(unknown)}")
        self.conn.execute(t.insert().values(**row))
        if (log is None and table in LOGGED_OBJECTS) or log:
            object_type, pk = LOGGED_OBJECTS[table]
            self.log_change(object_type, row[pk], "CREATE", None, _clean_row(row),
                            call_id=call_id, rule_id=rule_id)
        return row

    def insert_many(self, table: str, rows: Iterable[dict[str, Any]], *,
                    call_id: str | None = None, rule_id: str | None = None) -> list[dict[str, Any]]:
        return [self.insert(table, r, call_id=call_id, rule_id=rule_id) for r in rows]

    def update(self, table: str, key: dict[str, Any], changes: dict[str, Any], *,
               op: str = "UPDATE", call_id: str | None = None,
               rule_id: str | None = None) -> dict[str, Any]:
        """Update one row by key; logs the full before and after images."""
        self._check_writable()
        t = self.table(table)
        before = self.get(table, **key)
        if before is None:
            raise KeyError(f"{table}: no row for {key}")
        clause = and_(*[t.c[k] == v for k, v in key.items()])
        if table in LOGGED_OBJECTS and "version" in t.c and "version" not in changes:
            changes = {**changes, "version": (before.get("version") or 1) + 1}
        self.conn.execute(t.update().where(clause).values(**changes))
        after = {**before, **changes}
        if table in LOGGED_OBJECTS:
            object_type, pk = LOGGED_OBJECTS[table]
            self.log_change(object_type, before[pk], op, _clean_row(before), _clean_row(after),
                            call_id=call_id, rule_id=rule_id)
        return after

    def log_change(self, object_type: str, object_id: str, op: str, before: Any, after: Any,
                   *, call_id: str | None = None, rule_id: str | None = None) -> str:
        self._check_writable()
        change_id = new_id("chg")
        self.conn.execute(change_log.insert().values(
            change_id=change_id, run_id=self.run_id, object_type=object_type,
            object_id=object_id, op=op,
            before=json.loads(json.dumps(before, default=str)) if before is not None else None,
            after=json.loads(json.dumps(after, default=str)) if after is not None else None,
            stage=self.stage, module=self.module, call_id=call_id, rule_id=rule_id,
        ))
        return change_id
