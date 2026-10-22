"""Executor + approval guarantees.

These tests exist because the failures they cover are expensive: a duplicate
payout, a payout nobody approved, or a payout that silently exceeded what a
human signed. Each test names the guarantee it pins.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.airwallex.client import AmbiguousSubmit, ApiError
from app.db import reset_default_db, session_scope
from app.domain import approvals as ap
from app.domain.executor import ExecutionRefused, Executor, StepResult
from app.domain.planner import ACT_FX, ACT_PAY, CashPosition, Payable, Plan, Step
from app.models import PlanStep, StepStatus, StepType
from app.domain.policy import Forecast


@pytest.fixture(autouse=True)
def _fresh_db():
    reset_default_db()
    yield


@pytest.fixture(autouse=True)
def _spine():
    """Every executor test starts with a persisted plan_step for PO-7003.

    The executor refuses to move money without a durable row to hold the
    request_id, so tests that exercise real submission need one. Created per
    test so the anti-duplicate assertions stay independent.
    """
    with session_scope() as s:
        s.add(PlanStep(
            type=StepType.TRANSFER,
            payload_json=json.dumps({"payable_number": "PO-7003",
                                     "action": "PAY"}),
            status=StepStatus.PROPOSED))
        s.commit()
    yield


# ---- fakes ---------------------------------------------------------------
class FakeTransfers:
    def __init__(self, *, ambiguous=False, error=None, existing=None):
        self.calls: list[dict] = []
        self._ambiguous = ambiguous
        self._error = error
        self._existing = existing          # object returned by the lookup

    def create(self, db_session, **kw):
        self.calls.append(kw)
        if self._ambiguous:
            raise AmbiguousSubmit("timed out mid-flight")
        if self._error:
            raise self._error
        return {"id": "tr_new_1", "status": "SENT"}

    def find_by_request_id(self, request_id):
        self.calls.append({"lookup": request_id})
        return self._existing


class FakeFX:
    def __init__(self, *, ambiguous=False):
        self.quotes: list[dict] = []
        self.conversions: list[dict] = []
        self._ambiguous = ambiguous

    def create_quote(self, **kw):
        self.quotes.append(kw)
        return type("Q", (), {"quote_id": "q_1", "client_rate": Decimal("0.878174")})()

    def create_conversion(self, db_session, **kw):
        self.conversions.append(kw)
        if self._ambiguous:
            raise AmbiguousSubmit("5xx from /fx/conversions/create")
        return {"id": "cnv_1"}


class FakeClient:
    def __init__(self):
        self.token = "t"


def _pay_step(**kw) -> Step:
    base = dict(action=ACT_PAY, reason="ok", payable_number="PO-7003",
                supplier="Atlanta Packaging", amount_minor=180_000, currency="USD")
    base.update(kw)
    return Step(**base)


def _row(number="PO-7003", **kw) -> PlanStep:
    with session_scope() as s:
        row = PlanStep(type=StepType.TRANSFER,
                       payload_json=json.dumps({"payable_number": number,
                                                "action": "PAY"}),
                       status=StepStatus.PROPOSED, **kw)
        s.add(row)
        s.commit()
        return row


def _executor(transfers=None, fx=None, *, dry_run=False, approval=None):
    from app.airwallex.fx import FX
    from app.airwallex.transfers import Transfers
    pos = CashPosition(balances={"USD": 4_000_000}, forecast=Forecast(), rates={})
    with session_scope() as s:
        return Executor(
            db=s, client=FakeClient(),
            transfers=transfers or (transfers if transfers is not None else FakeTransfers()),
            fx=fx or FX(FakeClient()), resolve_supplier=lambda n: "bn_1",
            position=lambda: pos, rates={"USD": Decimal("1"), "EUR": Decimal("0.878174")},
            dry_run=dry_run, approval=approval)


# ---- the anti-duplicate guarantee ---------------------------------------
def test_a_step_with_an_existing_object_is_never_re_sent():
    """The core promise: money already moved is not moved again."""
    t = FakeTransfers()
    row = _row(airwallex_object_id="tr_already")
    ex = _executor(t)
    with session_scope() as s:
        ex.db = s
        res = ex.run_step(_pay_step())
    assert res.status == "ALREADY_DONE"
    assert not [c for c in t.calls if "lookup" not in c], "no create was attempted"
    assert res.airwallex_object_id == "tr_already"


def test_re_running_the_same_plan_twice_moves_money_once():
    t = FakeTransfers()
    ex = _executor(t)
    step = _pay_step()
    with session_scope() as s:
        ex.db = s
        first = ex.run_step(step)
    with session_scope() as s:
        ex.db = s
        second = ex.run_step(step)
    creates = [c for c in t.calls if "lookup" not in c]
    assert len(creates) == 1, f"expected exactly one create, got {len(creates)}"
    assert first.status == "SUBMITTED" and second.status == "ALREADY_DONE"


def test_request_id_is_persisted_before_the_call_and_reused():
    t = FakeTransfers()
    ex = _executor(t)
    with session_scope() as s:
        ex.db = s
        ex.run_step(_pay_step())
        rid_used = [c for c in t.calls if "lookup" not in c][0]["request_id"]
    with session_scope() as s:
        stored = s.query(PlanStep).order_by(PlanStep.id.desc()).first()
        assert stored.request_id == rid_used, "request_id must be on disk"
        assert stored.status == StepStatus.SUBMITTED


# ---- ambiguity -----------------------------------------------------------
def test_ambiguous_submit_resolves_by_lookup_and_does_not_resend():
    t = FakeTransfers(ambiguous=True, existing={"id": "tr_found", "status": "SENT"})
    ex = _executor(t)
    with session_scope() as s:
        ex.db = s
        res = ex.run_step(_pay_step())
    creates = [c for c in t.calls if "lookup" not in c]
    assert len(creates) == 1, "must NOT resend after an ambiguous failure"
    assert res.resolved_from == "lookup"
    assert res.airwallex_object_id == "tr_found"


def test_ambiguous_with_nothing_found_is_flagged_for_a_human():
    t = FakeTransfers(ambiguous=True, existing=None)
    ex = _executor(t)
    with session_scope() as s:
        ex.db = s
        res = ex.run_step(_pay_step())
    assert res.ambiguous is True
    assert res.status == "AMBIGUOUS"
    assert res.airwallex_object_id is None


def test_ambiguous_fx_does_not_resend_the_conversion():
    f = FakeFX(ambiguous=True)
    ex = _executor(FakeTransfers(existing=None), f)
    # Real FX steps carry the payable_number they are funding (see planner.py),
    # which is how the executor finds their persisted row.
    with session_scope() as s:
        s.add(PlanStep(type=StepType.FX,
                       payload_json=json.dumps({"payable_number": "PO-7003",
                                                "action": "FX"}),
                       status=StepStatus.PROPOSED))
        s.commit()
    step = Step(action=ACT_FX, reason="convert", sell_currency="USD",
                buy_currency="EUR", sell_amount_minor=901_285, currency="EUR",
                payable_number="PO-7003")
    with session_scope() as s:
        ex.db = s
        res = ex.run_step(step)
    assert len(f.conversions) == 1
    assert res.ambiguous is True


# ---- approvals -----------------------------------------------------------
def test_step_needing_approval_is_refused_without_one():
    ex = _executor(FakeTransfers())
    with session_scope() as s:
        ex.db = s
        with pytest.raises(ExecutionRefused):
            ex.run_step(_pay_step(needs_approval=True))


def test_dry_run_moves_no_money_even_for_auto_pay():
    t = FakeTransfers()
    ex = _executor(t, dry_run=True)
    with session_scope() as s:
        ex.db = s
        res = ex.run_step(_pay_step())
    assert res.status == "DRY_RUN"
    assert t.calls == [], "a dry run must not touch the API"
    assert res.airwallex_object_id is None


def test_matching_approval_permits_execution():
    t = FakeTransfers()
    terms = ap.ApprovalTerms(step_id=1, payable_number="PO-7003",
                            amount_minor=180_000, currency="USD",
                            counterparty="Atlanta Packaging", supplier_id="bn_1")
    ex = _executor(t, approval=ap.issue(terms, approved_by="david"))
    with session_scope() as s:
        ex.db = s
        ex.run_step(_pay_step(needs_approval=True))
    assert len(t.calls) == 1


def test_approval_for_a_different_amount_is_rejected():
    """The hash binds the amount. Approving 1800 must not pay 9000."""
    t = FakeTransfers()
    terms = ap.ApprovalTerms(step_id=1, payable_number="PO-7003",
                            amount_minor=180_000, currency="USD",
                            counterparty="Atlanta Packaging", supplier_id="bn_1")
    ex = _executor(t, approval=ap.issue(terms, approved_by="david"))
    with session_scope() as s:
        ex.db = s
        with pytest.raises(ap.ApprovalStale):
            ex.run_step(_pay_step(needs_approval=True, amount_minor=900_000))


def test_approval_for_a_different_supplier_is_rejected():
    t = FakeTransfers()
    terms = ap.ApprovalTerms(step_id=1, payable_number="PO-7003",
                            amount_minor=180_000, currency="USD",
                            counterparty="Atlanta Packaging", supplier_id="bn_1")
    ex = _executor(t, approval=ap.issue(terms, approved_by="david"))
    with session_scope() as s:
        ex.db = s
        with pytest.raises(ap.ApprovalStale):
            ex.run_step(_pay_step(needs_approval=True, supplier="Someone Else"))


def test_expired_approval_blocks_execution():
    t = FakeTransfers()
    terms = ap.ApprovalTerms(step_id=1, payable_number="PO-7003",
                            amount_minor=180_000, currency="USD",
                            counterparty="Atlanta Packaging", supplier_id="bn_1")
    old = ap.issue(terms, approved_by="david",
                   now=datetime.now(timezone.utc) - timedelta(hours=2))
    ex = _executor(t, approval=old)
    with session_scope() as s:
        ex.db = s
        with pytest.raises(ap.ApprovalExpired):
            ex.run_step(_pay_step(needs_approval=True))
    assert t.calls == []


def test_void_approval_blocks_execution():
    t = FakeTransfers()
    terms = ap.ApprovalTerms(step_id=1, payable_number="PO-7003",
                            amount_minor=180_000, currency="USD",
                            counterparty="Atlanta Packaging", supplier_id="bn_1")
    approval = ap.void(ap.issue(terms, approved_by="david"), "fx moved too far")
    ex = _executor(t, approval=approval)
    with session_scope() as s:
        ex.db = s
        with pytest.raises(ap.ApprovalStale):
            ex.run_step(_pay_step(needs_approval=True))


# ---- approval hashing ----------------------------------------------------
def test_fx_drift_beyond_tolerance_voids_the_approval():
    reason = ap.check_fx_drift(Decimal("0.878174"), Decimal("0.80"), Decimal("0.50"))
    assert reason and "beyond the" in reason
    assert ap.check_fx_drift(Decimal("0.878174"), Decimal("0.879"), Decimal("0.50")) is None


def test_bound_hash_is_stable_and_term_sensitive():
    terms = ap.ApprovalTerms(step_id=1, payable_number="PO-1", amount_minor=100,
                            currency="USD", counterparty="X", supplier_id="b")
    same = ap.ApprovalTerms(step_id=1, payable_number="PO-1", amount_minor=100,
                            currency="USD", counterparty="X", supplier_id="b")
    other = ap.ApprovalTerms(step_id=1, payable_number="PO-1", amount_minor=101,
                             currency="USD", counterparty="X", supplier_id="b")
    assert ap.bound_hash(terms) == ap.bound_hash(same)
    assert ap.bound_hash(terms) != ap.bound_hash(other)


def test_decimal_1_10_and_1_1_hash_identically():
    """'1.10' vs '1.1' must not invalidate an approval over formatting."""
    a = ap.ApprovalTerms(step_id=1, payable_number="P", amount_minor=1,
                         currency="USD", counterparty="X", supplier_id="b",
                         fx_rate=Decimal("1.10"))
    b = ap.ApprovalTerms(step_id=1, payable_number="P", amount_minor=1,
                         currency="USD", counterparty="X", supplier_id="b",
                         fx_rate=Decimal("1.1"))
    assert ap.bound_hash(a) == ap.bound_hash(b)


def test_invoice_ids_are_bound():
    a = ap.ApprovalTerms(step_id=1, payable_number="P", amount_minor=1,
                         currency="USD", counterparty="X", supplier_id="b",
                         invoice_ids=(1, 2))
    b = ap.ApprovalTerms(step_id=1, payable_number="P", amount_minor=1,
                         currency="USD", counterparty="X", supplier_id="b",
                         invoice_ids=(1, 3))
    assert ap.bound_hash(a) != ap.bound_hash(b)


# ---- non-money steps ------------------------------------------------------
def test_defer_and_escalate_never_call_the_api():
    t = FakeTransfers()
    ex = _executor(t)
    with session_scope() as s:
        ex.db = s
        res = ex.run_step(Step(action="DEFER", reason="waiting on receivable"))
    assert res.status == "NO_ACTION" and res.attempted is False
    assert t.calls == []


def test_api_error_cancels_the_step_rather_than_retrying():
    t = FakeTransfers(error=ApiError(400, "invalid", "bad beneficiary"))
    ex = _executor(t)
    with session_scope() as s:
        ex.db = s
        res = ex.run_step(_pay_step())
        row = s.query(PlanStep).order_by(PlanStep.id.desc()).first()
    assert res.status == "FAILED"
    assert row.status == StepStatus.CANCELLED
    assert row.airwallex_object_id is None


def test_money_moves_refuse_when_there_is_no_idempotency_spine():
    """No plan_step row means nowhere to persist a request_id — so refuse.

    Better to skip a payment than to send one we cannot de-duplicate after a
    crash. This is the guard that makes the rest of the spine trustworthy.
    """
    t = FakeTransfers()
    ex = _executor(t)
    with session_scope() as s:
        ex.db = s
        with pytest.raises(ExecutionRefused) as err:
            ex.run_step(Step(action=ACT_PAY, reason="x", payable_number="PO-NEW",
                             supplier="Nobody", amount_minor=100_000, currency="USD"))
    assert "idempotency spine" in str(err.value)
    assert t.calls == []