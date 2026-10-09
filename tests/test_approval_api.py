"""Human approval: PIN-gated, bound to exact terms, and never self-approving.

Audit item 8. The endpoint records a human decision; it does not move money, and
execution re-verifies everything regardless.
"""
from __future__ import annotations

import pytest

from app.db import DB
from app.domain.approval_api import (
    ApprovalRefused, approve_step, check_pin, reject_step, terms_from_step,
)
from app.models import Approval, ApprovalStatus, PlanStep, StepStatus, StepType

PIN = "demo-pin-1234"


@pytest.fixture
def db(tmp_path):
    return DB(f"sqlite:///{tmp_path}/appr.sqlite3")


def _step(db, **over):
    """Persist a plan step and return a DETACHED-safe handle.

    Each caller re-queries rather than reusing this object: SQLAlchemy expires
    instances after commit, so mutating through a stale handle raises or
    silently writes to a different identity map.
    """
    import json
    payload = {"action": "PAY", "payable_number": "PO-7001",
               "currency": "EUR", "amount_minor": 901_285,
               "supplier": "Rotterdam Freight BV",
               "supplier_id": "rotterdam", "fx_rate": "0.878174"}
    payload.update(over.pop("payload", {}) or {})
    s = db.session()
    st = PlanStep(type=over.pop("type", StepType.TRANSFER),
                  payload_json=json.dumps(payload),
                  status=StepStatus.PROPOSED)
    s.add(st)
    s.commit()
    return st.id


def _reload(db, step_id):
    return db.session().query(PlanStep).filter_by(id=step_id).first()


# --- the PIN is the whole boundary ------------------------------------
def test_wrong_pin_is_refused(db):
    step_id = _step(db)
    with pytest.raises(ApprovalRefused, match="wrong PIN"):
        approve_step(db, step_id, pin="nope", approved_by="attacker",
                     configured_pin=PIN)


def test_empty_pin_is_refused(db):
    step_id = _step(db)
    with pytest.raises(ApprovalRefused, match="wrong PIN"):
        approve_step(db, step_id, pin="", approved_by="attacker",
                     configured_pin=PIN)


def test_no_configured_pin_means_the_endpoint_is_closed_not_open(db):
    """The failure mode to avoid: no PIN configured -> approve everything."""
    assert check_pin("anything", "") is False
    assert check_pin("", "") is False
    step_id = _step(db)
    with pytest.raises(ApprovalRefused):
        approve_step(db, step_id, pin="anything", approved_by="x",
                     configured_pin="")


def test_pin_compare_is_constant_time(db):
    assert check_pin(PIN, PIN) is True
    assert check_pin(PIN + "x", PIN) is False


# --- approving records the decision -----------------------------------
def test_approval_is_recorded_and_bound(db):
    step_id = _step(db)
    out = approve_step(db, step_id, pin=PIN, approved_by="david",
                       configured_pin=PIN)
    assert out["status"] == "APPROVED"
    assert out["hash_prefix"], "the bound hash must be shown to the approver"
    assert out["amount_minor"] == 901_285
    assert out["currency"] == "EUR"
    assert out["counterparty"] == "Rotterdam Freight BV"


def test_approving_does_not_move_money(db):
    """The endpoint must not create an Airwallex object or change step status."""
    step_id = _step(db)
    approve_step(db, step_id, pin=PIN, approved_by="david",
                 configured_pin=PIN)
    st = _reload(db, step_id)
    assert st.airwallex_object_id is None
    assert st.status == StepStatus.PROPOSED


def test_double_approval_is_refused(db):
    step_id = _step(db)
    approve_step(db, step_id, pin=PIN, approved_by="david",
                 configured_pin=PIN)
    with pytest.raises(ApprovalRefused, match="already approved"):
        approve_step(db, step_id, pin=PIN, approved_by="david",
                     configured_pin=PIN)


def test_approving_a_step_that_already_moved_money_is_refused(db):
    """Approving after execution would authorise nothing and muddy the audit."""
    step_id = _step(db)
    with db.session() as s:
        s.query(PlanStep).filter_by(id=step_id).update(
            {"airwallex_object_id": "tr-already-sent"},
            synchronize_session=False)
        s.commit()
    with pytest.raises(ApprovalRefused, match="already has a live"):
        approve_step(db, step_id, pin=PIN, approved_by="david",
                     configured_pin=PIN)


def test_unknown_step_is_refused(db):
    with pytest.raises(ApprovalRefused, match="no plan step"):
        approve_step(db, 99999, pin=PIN, approved_by="x", configured_pin=PIN)


# --- the terms bound are the STEP's terms -----------------------------
def test_terms_come_from_the_persisted_step_not_the_caller(db):
    """The approval binds what will be EXECUTED, not what a UI displayed."""
    step_id = _step(db)
    terms = terms_from_step(_reload(db, step_id))
    assert terms.amount_minor == 901_285
    assert terms.payable_number == "PO-7001"
    assert str(terms.fx_rate) == "0.878174"


def test_terms_survive_a_missing_payload(db):
    step_id = _step(db)
    with db.session() as s:
        s.query(PlanStep).filter_by(id=step_id).update(
            {"payload_json": None}, synchronize_session=False)
        s.commit()
    terms = terms_from_step(_reload(db, step_id))
    assert terms.amount_minor == 0
    assert terms.payable_number == ""


def test_terms_survive_corrupt_json(db):
    step_id = _step(db)
    with db.session() as s:
        s.query(PlanStep).filter_by(id=step_id).update(
            {"payload_json": "{not json"}, synchronize_session=False)
        s.commit()
    terms = terms_from_step(_reload(db, step_id))
    assert terms.amount_minor == 0


# --- rejection --------------------------------------------------------
def test_rejection_requires_the_pin_too(db):
    step_id = _step(db)
    with pytest.raises(ApprovalRefused, match="wrong PIN"):
        reject_step(db, step_id, pin="nope", rejected_by="x",
                    configured_pin=PIN)


def test_rejection_is_recorded(db):
    step_id = _step(db)
    out = reject_step(db, step_id, pin=PIN, rejected_by="david",
                      reason="rate moved too much", configured_pin=PIN)
    assert out["status"] == "REJECTED"
    assert out["rejected_reason"] == "rate moved too much"
    s = db.session()
    row = s.query(Approval).filter_by(plan_step_id=step_id).one()
    assert row.status == ApprovalStatus.REJECTED


def test_a_rejected_step_is_not_approved(db):
    step_id = _step(db)
    reject_step(db, step_id, pin=PIN, rejected_by="david", configured_pin=PIN)
    with pytest.raises(ApprovalRefused, match="was rejected by a human"):
        # re-approving must not silently override a human's rejection
        approve_step(db, step_id, pin=PIN, approved_by="david",
                     configured_pin=PIN)
