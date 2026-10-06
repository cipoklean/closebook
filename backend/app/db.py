"""Database engine + session factory.

SQLite for dev; the schema in models.py is Postgres-compatible, so swapping
DATABASE_URL is the only production change. Tables are created idempotently
(`create_all`); Alembic is wired in `alembic/` for real migrations.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base  # ensure all tables are registered on Base

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _default_db_path() -> str:
    return str(_DATA_DIR / "closebook.sqlite3")


def make_engine(database_url: str | None = None):
    url = database_url or os.environ.get("CLOSEBOOK_DATABASE_URL") or f"sqlite:///{_default_db_path()}"
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            # Every new connection to ":memory:" gets a brand-new empty
            # database, so create_all and the test session would see different
            # schemas. StaticPool pins one connection for the whole process.
            kwargs["poolclass"] = StaticPool
    return create_engine(url, **kwargs)


class DB:
    """Small context: create_all + one sessionmaker bound to the engine."""

    def __init__(self, database_url: str | None = None):
        self.engine = make_engine(database_url)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def session(self) -> Session:
        return self.SessionLocal()


def get_db(db: DB):
    """FastAPI dependency factory returning a request-scoped session."""
    def _dep():
        with db.session() as s:
            yield s
    return _dep


# Module-level default, so `from app.db import SessionLocal` works in scripts,
# tests and the FastAPI app without threading a DB object everywhere.
_default_db: DB | None = None


def get_default_db() -> DB:
    global _default_db
    if _default_db is None:
        _default_db = DB()
    return _default_db


def session_scope():
    """`with session_scope() as s:` — commit on success, rollback on error."""
    with get_default_db().session() as s:
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise


def reset_default_db() -> None:
    """Drop the cached DB so a new CLOSEBOOK_DATABASE_URL takes effect."""
    global _default_db
    _default_db = None
