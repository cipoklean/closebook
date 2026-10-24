"""Per-test books, on the tester instance only.

WHY. Run 5 was 63 tests against one workspace: one test's commitments held
another test's payment, one test's bill number collided with another's, and a
test that reset the book destroyed the evidence of the test beside it. Almost
every failure was that collision, not a defect in the product.

WHAT ISOLATION HERE MEANS. A workspace is a separate SQLite FILE. Not a column
on every table, not a filter every query has to remember: a different database,
so settings, balances, invoices, bills, approvals, plan steps and traces are
separate by construction and cannot leak through a query that forgot a WHERE.
The Airwallex sandbox is shared, exactly as the real world shares one bank.

HOW A TEST OPENS ONE.

    GET /app?ws=qa-7f31

That is the whole protocol. The suffix is the workspace; the first request
creates it empty. `POST /workspaces/new` mints one and redirects to it, which is
what the "Start a fresh book" button on Setup does.

PROD NEVER HAS THIS. It is gated on `CLOSEBOOK_WORKSPACES=1`, which only
`scripts/testsprite.sh` sets. A request carrying `?ws=` against an instance
without that flag is refused, so the operator's workspace cannot be multiplied
or selected by a query string.
"""
from __future__ import annotations

import os
import re
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

_SUFFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")

#: The workspace this request belongs to, if any. A context var rather than an
#: argument because every existing caller already asks `get_default_db()`.
_current: ContextVar[Optional[object]] = ContextVar("closebook_workspace", default=None)

_lock = threading.Lock()
_books: dict[str, object] = {}


def enabled() -> bool:
    return os.environ.get("CLOSEBOOK_WORKSPACES", "").strip() == "1"


def root() -> Path:
    raw = os.environ.get("CLOSEBOOK_WORKSPACE_ROOT", "").strip()
    base = Path(raw) if raw else (Path(__file__).resolve().parent.parent.parent
                                  / "data" / "workspaces")
    base.mkdir(parents=True, exist_ok=True)
    return base


def valid(suffix: str) -> bool:
    return bool(_SUFFIX.match(suffix or ""))


def book(suffix: str):
    """The DB handle for a workspace, created on first use.

    One handle per suffix, cached and guarded: the same reason `get_default_db`
    needs a lock. Two requests for a cold workspace must not each run DDL.
    """
    if not enabled():
        raise LookupError("workspaces are not enabled on this instance")
    if not valid(suffix):
        raise ValueError(f"not a workspace suffix: {suffix!r}")

    from .db import DB

    with _lock:
        handle = _books.get(suffix)
        if handle is None:
            path = root() / f"{suffix}.sqlite3"
            handle = DB(f"sqlite:///{path}")
            _books[suffix] = handle
        return handle


def current():
    return _current.get()


def set_current(handle) -> object:
    return _current.set(handle)


def reset(token) -> None:
    _current.reset(token)


def new_suffix() -> str:
    """A short, readable, collision-proof suffix for a fresh book."""
    import secrets

    return "qa-" + secrets.token_hex(4)


def known() -> list[str]:
    """Every workspace that exists on disk, newest first."""
    try:
        files = sorted(root().glob("*.sqlite3"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
    except OSError:
        return []
    return [p.stem for p in files]
