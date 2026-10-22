"""Workspace policy, owned by the operator and stored in the DATABASE.

Why the database and not `.env`:
  * `.env` holds credentials and endpoints. These are business decisions — how
    much cash to keep back, how much may leave without a human — and an
    operator who can only change them by editing a file on the server does not
    really own them.
  * the dashboard and the planner must read the SAME numbers, and a row in the
    database is one place they can both read.

`policy.load_budget()` still reads `settings/committed_cash.json` and is still
the pure-library entry point (used by tests and by `build_plan` callers that
have no session). This module is the APP's entry point: `workspace_budget()`
returns the database row when it has a session, and falls back to the file only
for a database that has never been configured.

ONE RULE, ONE PLACE. Everything that asks "what is the floor?" goes through
here, so the plan and the tiles cannot disagree about the limits either.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..models import WorkspaceSettings
from ..timeutils import utcnow
from ..money import money_str, to_minor
from .policy import BudgetSettings, load_budget

#: What a workspace starts with, before anything is saved. Documented defaults,
#: not a vendor file: the running app reads its policy from the database.
DEFAULT_BUDGET_CURRENCY = "USD"
DEFAULT_RESERVE_FLOOR = "5000"
DEFAULT_AUTO_PAY_LIMIT = "2500"
DEFAULT_FX_TOLERANCE_PCT = "0.50"


def _defaults() -> WorkspaceSettings:
    """The policy a workspace starts with.

    Taken from the module constants in `policy.py`, NOT from
    `settings/committed_cash.json`: that file was the previous source of truth
    and it is precisely the "demo config the running app reads" this round
    removes. A brand-new workspace starts at the documented defaults and the
    first Save on the Settings screen puts a row in the database, after which
    the database is the only source.

    Returns a TRANSIENT row — deliberately not added to any session. Reading a
    page must not write to the database: the first version of this called
    `get_or_create` from the dashboard, which INSERTed a row and never
    committed, and on a file-backed SQLite database that left a write lock
    behind. The very first page load then failed with "database is locked" and
    rendered the fallback error page — a read path that broke the write path.
    """
    cur = DEFAULT_BUDGET_CURRENCY
    return WorkspaceSettings(
        id=1,
        budget_currency=cur,
        reserve_floor_minor=to_minor(DEFAULT_RESERVE_FLOOR, cur),
        auto_pay_limit_minor=to_minor(DEFAULT_AUTO_PAY_LIMIT, cur),
        fx_tolerance_pct=float(DEFAULT_FX_TOLERANCE_PCT),
        updated_at=utcnow(),
    )


def read(session) -> WorkspaceSettings:
    """The stored row, or the defaults if the workspace has never been saved.

    Pure: never writes. Everything that RENDERS or PLANS uses this.
    """
    row = session.get(WorkspaceSettings, 1)
    return row if row is not None else _defaults()


def get_or_create(session) -> WorkspaceSettings:
    """The stored row, CREATED from the file defaults if it is missing.

    Only for write paths, inside a committing session — a read path must use
    `read()` and leave the database alone.
    """
    row = session.get(WorkspaceSettings, 1)
    if row is not None:
        return row
    row = _defaults()
    session.add(row)
    session.flush()
    return row


def as_budget(row: WorkspaceSettings) -> BudgetSettings:
    """The stored row, in the shape the planner already understands.

    Minor units -> major here, once, because `BudgetSettings` speaks major
    units and the database speaks minor. `to_major_str` does the zero-stripping
    so "5000.00" never reaches policy as a float.
    """
    from ..money import to_major_str

    return BudgetSettings(
        currency=row.budget_currency,
        reserve_floor_usd=Decimal(to_major_str(row.reserve_floor_minor, row.budget_currency)),
        auto_pay_limit_usd=Decimal(to_major_str(row.auto_pay_limit_minor, row.budget_currency)),
        fx_tolerance_pct=Decimal(str(row.fx_tolerance_pct)),
    )


def workspace_budget(session=None) -> BudgetSettings:
    """The app's entry point. DB when a session is given, file otherwise.

    Uses `read()`, so PLANNING never writes. The row is created by the first
    Save on the Settings screen, not by the first page view.
    """
    if session is None:
        return load_budget()
    return as_budget(read(session))


def describe(row: WorkspaceSettings) -> dict:
    """For the UI: the numbers as the operator set them, formatted once."""
    ccy = row.budget_currency
    return {
        "budget_currency": ccy,
        "reserve_floor_minor": row.reserve_floor_minor,
        "auto_pay_limit_minor": row.auto_pay_limit_minor,
        "fx_tolerance_pct": row.fx_tolerance_pct,
        "reserve_floor": money_str(row.reserve_floor_minor, ccy),
        "auto_pay_limit": money_str(row.auto_pay_limit_minor, ccy),
        "updated_at": row.updated_at,
    }


class SettingsRefused(ValueError):
    """A value the workspace will not accept, with the reason in plain words."""


def save(session, *, budget_currency: str, reserve_floor: str,
         auto_pay_limit: str, fx_tolerance_pct: str) -> WorkspaceSettings:
    """Validate and store. Refuses rather than storing something unusable.

    The floor and the limit are given in MAJOR units as typed ("5000"), because
    that is what the operator sees on the invoice. They are converted once,
    here, and any parse failure is a refusal — not a silent zero, which would
    disable the floor entirely and is the worst possible default for a control.
    """
    ccy = (budget_currency or "").strip().upper()
    if not ccy or not ccy.isalpha() or len(ccy) != 3:
        raise SettingsRefused("Budget currency must be a 3-letter code, e.g. USD.")
    try:
        floor = to_minor(reserve_floor, ccy)
    except Exception:
        raise SettingsRefused(
            f"The reserve floor has to be a number, like 5000. "
            f"Got {reserve_floor!r}.") from None
    try:
        limit = to_minor(auto_pay_limit, ccy)
    except Exception:
        raise SettingsRefused(
            f"The automatic limit has to be a number, like 2500. "
            f"Got {auto_pay_limit!r}.") from None
    if floor < 0 or limit < 0:
        raise SettingsRefused("Neither the floor nor the limit can be negative.")
    try:
        tol = Decimal(str(fx_tolerance_pct).strip().rstrip("%") or "0.50")
    except Exception:
        raise SettingsRefused(
            f"FX tolerance has to be a percentage, like 0.5. "
            f"Got {fx_tolerance_pct!r}.") from None
    if tol < 0 or tol > 50:
        raise SettingsRefused("FX tolerance has to be between 0 and 50 percent.")

    row = get_or_create(session)
    row.budget_currency = ccy
    row.reserve_floor_minor = floor
    row.auto_pay_limit_minor = limit
    row.fx_tolerance_pct = float(tol)
    session.flush()
    return row


__all__ = ["SettingsRefused", "as_budget", "describe", "get_or_create",
           "read", "save", "workspace_budget"]
