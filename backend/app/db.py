"""Database engine + session factory.

SQLite for dev; the schema in models.py is Postgres-compatible, so swapping
DATABASE_URL is the only production change. Tables are created idempotently
(`create_all`); Alembic is wired in `alembic/` for real migrations.
"""
from __future__ import annotations

import threading

import os
from contextlib import contextmanager
from typing import Optional
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

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
        else:
            # NO POOL, ON PURPOSE. A pool is a fixed number of connections with
            # a timeout, and one leaked session is then enough to take the whole
            # server down: every later request waits out the timeout and fails.
            # That is exactly what happened — two helpers opened a session and
            # never closed it, so after fifteen requests `/state` returned 500
            # after a 30.00s wait while `/healthz` stayed instant. SQLite opens
            # a file connection in microseconds, so a pool buys nothing here and
            # can only ever be exhausted.
            kwargs["poolclass"] = NullPool
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):
            """WAL and a busy timeout, on every connection, always.

            WAL lets readers work while a writer holds the database, so a page
            render cannot be blocked by a settlement. `busy_timeout` makes a
            writer WAIT for the lock instead of immediately raising "database is
            locked" — without it, two concurrent setup POSTs on a fresh book
            fail each other instead of queueing.

            Set here rather than in connect_args because a PRAGMA is per
            CONNECTION and the engine may open many.
            """
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA synchronous=NORMAL")
                # NOT turning on foreign_keys here. It is the right long-term
                # setting, but switching it on changes what the EXISTING code is
                # allowed to write, and it broke seven tests that depend on
                # insert order. That is a separate change with its own review,
                # not a side effect of fixing an outage.
            finally:
                cursor.close()

    return engine


class DB:
    """Small context: create_all + one sessionmaker bound to the engine."""

    def __init__(self, database_url: str | None = None):
        self.engine = make_engine(database_url)
        # CREATE_ALL IS NOT SAFE TO RACE. Two threads that both find no cached
        # DB will both build one and both issue CREATE TABLE; the loser's DDL
        # fails with "table customer already exists" and its request 500s. That
        # is what produced the 502s under parallel load: the table was always
        # there, and the request still failed. `checkfirst` does not close it,
        # because the check and the create are two separate statements.
        try:
            Base.metadata.create_all(self.engine)
        except OperationalError as exc:
            if "already exists" not in str(exc).lower():
                raise
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def session(self) -> Session:
        return self.SessionLocal()

    @contextmanager
    def session_scope(self):
        """`with db.session_scope() as s:` — commit on success, rollback on error.

        Bound to THIS instance's engine. The module-level `session_scope()` is
        the same thing bound to `get_default_db()`, and BOTH must exist: the
        app holds a `DB` object and the tests hold the module default, and a
        helper that only exists on one of them is a helper whose first caller
        crashes. (It did: the setup routes and the workspace reset both wrote
        `_db.session_scope()` before this method existed.)
        """
        s = self.session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


@contextmanager
def session_for(db: DB, session: Optional[Session] = None):
    """The caller's session, or one WE open and are therefore responsible for.

    THE LEAK THIS EXISTS TO PREVENT. Helpers took an optional session and did
    `s = session or db.session()`, then never closed the one they had created.
    Every request to a read path that called such a helper checked a connection
    out of the pool and never returned it, so after fifteen requests the pool
    was empty and every later request waited out the 30-second timeout and
    returned a 500 — while `/healthz`, which touches no database, stayed
    instant. Two helpers had the pattern, and both are read paths the dashboard
    uses on every render.

    A helper that might create a session must therefore own closing it, and the
    only way to guarantee that is to make the closing unconditional here rather
    than remembered at each call site.
    """
    if session is not None:
        yield session                      # the caller's to close, not ours
        return
    opened = db.session()
    try:
        yield opened
    finally:
        opened.close()


def get_db(db: DB):
    """FastAPI dependency factory returning a request-scoped session."""
    def _dep():
        with db.session() as s:
            yield s
    return _dep


# Module-level default, so `from app.db import SessionLocal` works in scripts,
# tests and the FastAPI app without threading a DB object everywhere.
_default_db: DB | None = None


_DEFAULT_DB_LOCK = threading.Lock()


def get_default_db() -> DB:
    """The process-wide handle, built ONCE.

    The lock is not decoration: twenty parallel requests all read
    `_default_db is None` before any of them wrote it, so every one of them
    built its own engine and ran its own DDL. A guard that is only correct when
    nobody is looking is not a guard.
    """
    # A REQUEST'S WORKSPACE WINS. Resolved here rather than threaded through
    # every caller, so `session_scope()`, the routes, the dashboard and the
    # planner all follow `?ws=` without knowing it exists. A thread with no
    # workspace (a background job, a script) keeps getting the default book.
    try:
        from .workspaces import current

        chosen = current()
        if chosen is not None:
            return chosen
    except Exception:                      # noqa: BLE001
        pass

    global _default_db
    if _default_db is None:
        with _DEFAULT_DB_LOCK:
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
