"""One definition of "now", used everywhere.

`datetime.utcnow()` is deprecated in Python 3.12+, so the codebase was moved to
`datetime.now(timezone.utc)`. Doing that naively, though, breaks every DB
comparison: SQLite has no timezone type, so `DateTime(timezone=True)` stores a
naive string. An aware value read back alongside a naive one raises
`TypeError: can't compare offset-naive and offset-aware datetimes` the first
time anything sorts or compares them — and existing rows are all naive.

So `utcnow()` returns a **naive UTC** datetime, derived from the aware one. That
satisfies the deprecation fix while keeping every stored value, ordering clause
and comparison exactly as compatible as before. Any future migration to
timezone-aware storage should happen in one place: here.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time, naive — the shape every existing DB row is stored in."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_naive_utc(value: datetime | None) -> datetime | None:
    """Normalise an incoming datetime to naive UTC for comparison/storage."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


__all__ = ["utcnow", "as_naive_utc"]
