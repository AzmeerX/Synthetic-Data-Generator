"""Database access: engine, schema creation, append-only guards, test stores.

Purpose: open the one Synth database (PostgreSQL in real runs, SQLite in tests)
and install the triggers that make ``[A]`` tables append-only. Contract:
:func:`open_store` -> :class:`Store`; ``Store.begin()`` yields a
:class:`synth.store.repo.Tx` whose writes and change-log rows commit together.
Spec: P1 §4, CLAUDE.md invariant 4.
"""

from __future__ import annotations

import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from synth.store.tables import APPEND_ONLY_TABLES, metadata

if TYPE_CHECKING:
    from synth.store.repo import Tx

DEFAULT_URL = "postgresql://synth:synth@localhost:5432/synth"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class AppendOnlyViolation(Exception):
    """Raised (by the database) when something tries to DELETE from an [A] table."""


def resolve_url(url: str | None = None) -> str:
    """``postgresql://`` -> ``postgresql+psycopg://`` (psycopg 3 driver)."""
    url = url or os.environ.get("SYNTH_DB_URL") or DEFAULT_URL
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    elif url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    return url


def make_engine(url: str | None = None, schema: str | None = None, **kwargs: Any) -> Engine:
    url = resolve_url(url)
    if url.startswith("sqlite"):
        engine_kwargs: dict[str, Any] = {"future": True}
        if url in ("sqlite://", "sqlite:///:memory:"):
            engine_kwargs.update(poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
        engine = create_engine(url, **engine_kwargs, **kwargs)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()
        return engine
    connect_args = {}
    if schema:
        connect_args["options"] = f"-csearch_path={schema}"
    return create_engine(url, future=True, connect_args=connect_args, pool_pre_ping=True, **kwargs)


# ------------------------------------------------------------------ guards
_PG_GUARD_FN = """
CREATE OR REPLACE FUNCTION synth_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'append-only table %: % is not allowed', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'restrict_violation';
END
$$ LANGUAGE plpgsql;
"""


def install_guards(conn) -> None:
    """Install the DELETE/TRUNCATE rejection triggers on every [A] table."""
    dialect = conn.dialect.name
    if dialect == "postgresql":
        conn.execute(text(_PG_GUARD_FN))
        for table in APPEND_ONLY_TABLES:
            conn.execute(text(f"DROP TRIGGER IF EXISTS {table}_no_delete ON {table}"))
            conn.execute(text(
                f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION synth_append_only()"))
            conn.execute(text(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}"))
            conn.execute(text(
                f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION synth_append_only()"))
    elif dialect == "sqlite":
        for table in APPEND_ONLY_TABLES:
            conn.execute(text(f"DROP TRIGGER IF EXISTS {table}_no_delete"))
            conn.execute(text(
                f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, 'append-only table {table}: DELETE is not allowed'); END"))
    else:  # pragma: no cover
        raise RuntimeError(f"no append-only guard for dialect {dialect!r}")


def create_schema(engine: Engine) -> None:
    """Create every table and guard directly (tests, and Alembic revision 0001)."""
    with engine.begin() as conn:
        metadata.create_all(conn)
        install_guards(conn)


def upgrade(url: str | None = None, revision: str = "head") -> str:
    """Run Alembic migrations against ``url``. Returns the resolved URL."""
    from alembic import command
    from alembic.config import Config

    resolved = resolve_url(url)
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", resolved.replace("%", "%%"))
    command.upgrade(cfg, revision)
    return resolved


# ------------------------------------------------------------------ the store
class Store:
    """The database, with transactional write helpers."""

    def __init__(self, engine: Engine, schema: str | None = None) -> None:
        self.engine = engine
        self.schema = schema

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    @contextmanager
    def begin(self, run_id: str | None = None, stage: str | None = None,
              module: str | None = None) -> Iterator["Tx"]:
        from synth.store.repo import Tx

        with self.engine.begin() as conn:
            yield Tx(conn, run_id=run_id, stage=stage, module=module)

    def read(self) -> "Tx":
        """A non-transactional reader (autocommit connection). Close with ``.close()``."""
        from synth.store.repo import Tx

        return Tx(self.engine.connect(), read_only=True)

    @contextmanager
    def reader(self) -> Iterator["Tx"]:
        from synth.store.repo import Tx

        with self.engine.connect() as conn:
            yield Tx(conn, read_only=True)

    def table_names(self) -> list[str]:
        return sorted(inspect(self.engine).get_table_names(schema=self.schema))

    def dispose(self) -> None:
        if self.schema and self.dialect == "postgresql":
            with self.engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
        self.engine.dispose()


def open_store(url: str | None = None, create: bool = False) -> Store:
    """Open the configured database. ``create`` builds missing tables directly
    (development convenience; real installs use ``synth db upgrade``)."""
    engine = make_engine(url)
    store = Store(engine)
    if create:
        create_schema(engine)
    return store


def test_store() -> Store:
    """A fresh, empty, fully-guarded store.

    ``SYNTH_TEST_DB_URL`` set -> a throwaway schema in that PostgreSQL database
    (dropped by :meth:`Store.dispose`). Unset -> SQLite in memory.
    """
    url = os.environ.get("SYNTH_TEST_DB_URL")
    if not url:
        engine = make_engine("sqlite://")
        create_schema(engine)
        return Store(engine)
    schema = "t_" + re.sub(r"[^a-z0-9]", "", uuid.uuid4().hex)[:16]
    bootstrap = make_engine(url)
    with bootstrap.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    bootstrap.dispose()
    engine = make_engine(url, schema=schema)
    create_schema(engine)
    return Store(engine, schema=schema)
