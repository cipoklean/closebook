"""The dashboard context builder must never show a blank row.

It silently shipped twice: once rendering every FX conversion as 0.00, and once
filling the plan table with legacy rows that had no amount at all.
"""
from __future__ import annotations

from app.dashboard import build_context
from app.db import DB, get_default_db, reset_default_db
from app.models import PlanStep, StepStatus, StepType


def _db():
    reset_default_db()
    return get_default_db()


def _step(db, payload, type_=StepType.TRANSFER, status=StepStatus.SUBMITTED):
    import json
    s = db.session()
    st = PlanStep(type=type_,
                  payload_json=json.dumps(payload) if payload is not None else None,
                  status=status)
    s.add(st)
    s.commit()
    return st


def test_fx_step_exposes_both_legs():
    db = _db()
    _step(db, {"action": "FX", "payable_number": "PO-1", "currency": "USD",
               "sell_currency": "USD", "buy_currency": "EUR",
               "amount_minor": 0, "sell_amount_minor": 1_031_500},
          type_=StepType.FX)
    ctx = build_context(db)
    assert len(ctx["plan"]) == 1
    p = ctx["plan"][0]
    assert p["sell_amount_minor"] == 1_031_500
    assert p["buy_currency"] == "EUR"


def test_legacy_row_without_an_amount_is_dropped():
    """These really exist in the dev DB — payloads of only a payable number."""
    db = _db()
    _step(db, {"payable_number": "PO-7003", "supplier": "Atlanta Packaging"})
    assert build_context(db)["plan"] == []


def test_row_with_no_payload_at_all_is_dropped():
    db = _db()
    _step(db, None)
    assert build_context(db)["plan"] == []


def test_real_payment_step_survives():
    db = _db()
    _step(db, {"action": "PAY", "payable_number": "PO-7003", "currency": "USD",
               "amount_minor": 180_000})
    ctx = build_context(db)
    assert len(ctx["plan"]) == 1
    assert ctx["plan"][0]["amount_minor"] == 180_000


def test_context_always_reports_the_budget():
    """The headline numbers must be present even with an empty database."""
    ctx = build_context(_db())
    assert ctx["budget"]["committed_minor"] > 0
    assert ctx["budget"]["reserve_floor_minor"] > 0
    assert "ledger_balanced" in ctx


def test_context_never_raises_on_empty_db():
    ctx = build_context(_db())
    for key in ("plan", "receivables", "payables", "incidents",
                "pending_approvals", "cash", "suspense"):
        assert key in ctx, key
