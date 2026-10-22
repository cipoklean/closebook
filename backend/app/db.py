"""Database engine + session factory.

SQLite for dev; the schema in models.py is Postgres-compatible, so swapping
DATABASE_URL is the only production change. Tables are created idempotently
(`create_all`); Alembic is wired in `alembic/` for real migrations.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
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


@contextmanager
def session_scope():
    """`with session_scope() as s:` — commit on success, rollback on error.

    Decorated with @contextmanager: without it this is a bare generator and
    `with session_scope()` raises "object does not support the context manager
    protocol" — while the docstring cheerfully promises that exact usage.
    """
    with get_default_db().session() as s:
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise


def reset_default_db(*, allow_demo_file: bool = False) -> None:
    """Drop the cached DB so a new CLOSEBOOK_DATABASE_URL takes effect.

    REFUSES to drop a handle onto the real demo database unless you say so.

    Why: this function does NOT isolate anything. It only clears the cache, so
    the next `get_default_db()` reopens whatever URL is configured — which,
    outside the test suite, is `data/closebook.sqlite3`, the seeded demo data.
    Calling it and then writing rows silently pollutes the demo (this actually
    happened: two phantom payables and two "X" suppliers appeared in the demo
    book and were only noticed when a tile read USD 10,500.00 where the
    arithmetic said 7,000).

    Isolated work belongs in a temp database, which is what the test suite does:

        CLOSEBOOK_DATABASE_URL=sqlite:////tmp/scratch.sqlite3 python ...

    To deliberately reopen the demo file, pass `allow_demo_file=True` and mean
    it.
    """
    global _default_db
    url = os.environ.get("CLOSEBOOK_DATABASE_URL") or ""
    if not allow_demo_file:
        points_at_demo = (not url) or (str(_DATA_DIR / "closebook.sqlite3") in url)
        if points_at_demo:
            raise RuntimeError(
                "reset_default_db() refused: CLOSEBOOK_DATABASE_URL is not set, "
                "so this would reopen the DEMO database "
                f"({_DATA_DIR / 'closebook.sqlite3'}). Point at a temp database "
                "first, e.g. CLOSEBOOK_DATABASE_URL=sqlite:////tmp/scratch.sqlite3, "
                "or pass allow_demo_file=True if you really mean the demo file.")
    _default_db = None
