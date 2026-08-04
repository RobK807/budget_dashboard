"""Engine and session construction."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from budget import config
from budget.models import Base


def _configure_sqlite(dbapi_connection, connection_record) -> None:
    cur = dbapi_connection.cursor()
    # Off by default in SQLite, and this schema leans on foreign keys for correctness.
    cur.execute("PRAGMA foreign_keys=ON")
    # WAL gives better crash resilience. It is safe here because the database always lives
    # on a local disk -- the NAS only ever holds pushed copies (DESIGN.md 6.1).
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def make_engine(db_path: Path | None = None, echo: bool = False) -> Engine:
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", echo=echo, future=True)
    event.listen(engine, "connect", _configure_sqlite)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def create_all(engine: Engine) -> list[str]:
    """Create anything missing, then bring an existing database up to date.

    Returns the migrations applied, so a caller can report them.
    """
    from budget.schema import apply_migrations

    applied = apply_migrations(engine)  # before create_all: it cannot alter a CHECK
    Base.metadata.create_all(engine)
    return applied
