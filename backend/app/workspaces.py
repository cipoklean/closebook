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

That is the whole entry protocol. The suffix is the workspace; the first request
creates it empty, and the response sets `closebook_ws=qa-7f31` as a SESSION
cookie so every later request in that browser — the tab it clicks, the form it
submits, the 303 that answers the form, the `/state/*` polls, the landing page's
link back to `/app` — resolves to the same book WITHOUT repeating the query
string. The query param still wins when both are present, which is how a second
browser context moves off a cookie it is carrying. `POST /workspaces/new` mints
one and redirects to it, which is what the "Start a fresh book" button does.

A COOKIE ALONE got a book for one request and then leaked everything after it
into the shared book: that was the whole bug this replaces.

PROD NEVER HAS THIS. It is gated on `CLOSEBOOK_WORKSPACES=1`, which only
`scripts/testsprite.sh` sets. A request carrying `?ws=` against an instance
without that flag is refused, and a cookie is ignored outright, so the
operator's workspace cannot be multiplied or selected by a query string.
"""
from __future__ import annotations

import os
import re
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

_SUFFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")

#: The cookie that makes a book STICK for the whole browser session.
#:
#: `?ws=qa-7f31` alone survives exactly one request. Everything after it — the
#: tab the test clicks, the form it submits, the 303 that comes back, the
#: `/state/*` polls, `/money-in/accounts` — arrives without the query string and
#: silently resolved to the SHARED book, so per-test isolation lasted one page
#: and then leaked. The query param is still the way IN (and still wins when
#: both are present); the cookie is how the book is remembered.
COOKIE = "closebook_ws"

#: The workspace this request belongs to, if any. A context var rather than an
#: argument because every existing caller already asks `get_default_db()`.
_current: ContextVar[Optional[object]] = ContextVar("closebook_workspace", default=None)
#: Its NAME, so a renderer can say which book it is showing and carry it on
#: every link without being handed the suffix down every call.
_current_suffix: ContextVar[Optional[str]] = ContextVar(
    "closebook_workspace_suffix", default=None)


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


def current_suffix() -> str:
    """The active book's name, or "" in the shared book.

    Read by the renderers so the page can SAY which book it is showing and carry
    it on every link, form action and fetch.
    """
    return _current_suffix.get() or ""


def set_current(handle, suffix: str = "") -> tuple:
    """Bind this request to a book. Returns the token `reset` needs."""
    return (_current.set(handle), _current_suffix.set(suffix or None))


def reset(token) -> None:
    t_handle, t_suffix = token
    _current.reset(t_handle)
    _current_suffix.reset(t_suffix)


def cookie_header(suffix: str) -> str:
    """The Set-Cookie value that keeps this book for the session.

    `Path=/` so it covers every route rather than only the path it was issued
    from; `SameSite=Lax` so a cross-site POST cannot select someone's book; no
    `Max-Age`, so a session cookie that dies with the test's browser; no
    `HttpOnly` because it is a label for a test book, not a credential, and a
    tester inspecting it is doing the right thing.
    """
    return f"{COOKIE}={suffix}; Path=/; SameSite=Lax"


def resolve(*, query: str = "", cookie: str = "") -> str:
    """Which book this request is for: the query param FIRST, then the cookie.

    The query param wins so an explicit `?ws=` can always move a client off a
    cookie it is carrying, which is what a second test context does. Returns ""
    when neither names a usable book, which means the shared one.

    A MALFORMED cookie is ignored rather than refused: a bad value on a live
    session must fall back to the shared book, not 400 every page in it.
    """
    asked = (query or "").strip()
    if asked:
        return asked if valid(asked) else ""
    carried = (cookie or "").strip()
    return carried if valid(carried) else ""


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


def forget_cached_books() -> None:
    """Drop every cached handle.

    For a caller that moves the workspace ROOT under a running process — a test
    with a fresh temp root, mainly. Without this the cache hands out a handle to
    the previous run's file, and one test's rows (and its approvals) reappear as
    the next test's mystery. Not called by the app: an instance keeps its
    handles for its whole life, which is the point of the cache.
    """
    with _lock:
        _books.clear()
