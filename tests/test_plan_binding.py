"""Plan -> PlanStep persistence, the binding that makes duplicate protection real.

Found by running `scripts/live_payout.py` twice: it minted a FRESH `plan_step`
per invocation, so the executor's `ALREADY_DONE` check had nothing to recognise
and a SECOND real USD 1,800 transfer was created. The executor was correct; the
plan layer was handing it a new identity every time.

The rule enforced here: **one `plan_step` per (payable, step kind) per plan
run, and a payable that already carries a live transfer is never re-planned.**
That second half is what stops a duplicate across separate runs entirely.
"""
from __future__ import annotations

import json

import pytest

from app.db import reset_default_db, session_scope
from app.models import PlanStep, StepStatus, StepType


@pytest.fixture(autouse=True)
def _isolated_db():
    """Each test in this module needs a genuinely empty database.

    Without this the tests SHARE the module-level default DB, so a plan_step
    carrying an airwallex_object_id from an earlier test makes the next test's
    persist_plan() skip instead of write — which looks exactly like the bug
    under test. Every test that asserts on persistence must reset first.
    """
    reset_default_db()
    yield


def _row(number="PO-7003", kind=StepType.TRANSFER, **kw):
    """Write a row in the REAL persisted shape (payable_number + action).

    Hand-rolling payload_json here is exactly what hid the FX/PAY identity
    bug, so fixtures go through plan_store.persist_plan instead.
    """
    from app.domain.plan_store import new_run_id, persist_plan
    from app.domain.planner import ACT_FX, ACT_PAY, Step

    action = ACT_FX if kind == StepType.FX else ACT_PAY
    step = Step(action=action, reason="fixture", payable_number=number,
                supplier="Atlanta Packaging", amount_minor=180_000, currency="USD")
    with session_scope() as s:
        rows, _ = persist_plan(s, new_run_id(), [step])
        row = rows[0]
        for k, v in kw.items():
            setattr(row, k, v)
        s.commit()
        return row.id
    defaults = dict(type=kind, status=StepStatus.PROPOSED)
    defaults.update(kw)
    with session_scope() as s:
        r = PlanStep(**defaults)
        s.add(r)
        s.commit()
        return r.id


def _find(number="PO-7003", kind=StepType.TRANSFER):
    from app.domain.executor import Executor

    class _FakeStep:
        payable_number = number
        action = "PAY"

    with session_scope() as s:
        ex = Executor.__new__(Executor)
        ex.db = s
        return Executor._row_for(ex, _FakeStep())


def test_a_persisted_step_is_found_by_its_payable():
    reset_default_db()
    _row()
    assert _find() is not None


def test_re_persisting_the_same_payable_reuses_the_same_row():
    """Persisting twice must CONVERGE, not append a second identity.

    An earlier version of this test asserted `first != second`, encoding the
    very bug that let a duplicate payout through: a fresh row per run meant the
    executor had nothing to recognise. The correct invariant is one row.
    """
    reset_default_db()
    first = _row()
    second = _row()
    assert first == second, "a second seed created a second plan_step identity"
    assert _find().id == first


def test_a_step_with_a_live_object_is_recognised_as_done():
    reset_default_db()
    step_id = _row(airwallex_object_id="tr_live_1")
    with session_scope() as s:
        from app.models import PlanStep as PS
        assert s.get(PS, step_id).airwallex_object_id == "tr_live_1"


def test_persist_plan_converges_on_one_row_across_runs():
    """THE FIX for the live duplicate: same payable + action -> same row."""
    from app.domain.plan_store import new_run_id, persist_plan
    from app.domain.planner import ACT_PAY, Step

    step = Step(action=ACT_PAY, reason="r1", payable_number="PO-7003",
                supplier="Atlanta Packaging", amount_minor=180_000,
                currency="USD")
    with session_scope() as s:
        rows1, skipped1 = persist_plan(s, new_run_id(), [step])
        first_id = rows1[0].id
    with session_scope() as s:
        rows2, skipped2 = persist_plan(s, new_run_id(), [step])
        second_id = rows2[0].id
    assert first_id == second_id, "a second run created a NEW plan_step"
    assert skipped2 == [], "nothing was paid yet, so nothing should be skipped"


def test_a_paid_payable_is_skipped_on_the_next_run():
    """Once a transfer exists, re-planning must not queue it again."""
    from app.domain.plan_store import is_already_paid, new_run_id, persist_plan
    from app.domain.planner import ACT_PAY, Step

    step = Step(action=ACT_PAY, reason="r", payable_number="PO-7003",
                supplier="Atlanta Packaging", amount_minor=180_000,
                currency="USD")
    with session_scope() as s:
        rows, _ = persist_plan(s, new_run_id(), [step])
        rows[0].airwallex_object_id = "tr_live_1"
        s.commit()
    with session_scope() as s:
        rows2, skipped = persist_plan(s, new_run_id(), [step])
        assert skipped and skipped[0].payable_number == "PO-7003"
        assert is_already_paid(s, step) is True


def test_an_fx_step_and_its_transfer_step_coexist():
    """One payable legitimately has an FX step AND a transfer step."""
    from app.domain.plan_store import new_run_id, persist_plan
    from app.domain.planner import ACT_FX, ACT_PAY, Step

    fx = Step(action=ACT_FX, reason="convert", payable_number="PO-7001",
              sell_currency="USD", sell_amount_minor=1_029_334,
              buy_currency="EUR", buy_amount_minor=901_285, currency="EUR")
    pay = Step(action=ACT_PAY, reason="pay", payable_number="PO-7001",
               supplier="Rotterdam Freight BV", amount_minor=901_285,
               currency="EUR")
    with session_scope() as s:
        rows, _ = persist_plan(s, new_run_id(), [fx, pay])
        kinds = sorted(r.type.value for r in rows)
        assert kinds == ["FX", "TRANSFER"]


def test_non_money_steps_are_never_persisted():
    from app.domain.plan_store import new_run_id, persist_plan
    from app.domain.planner import ACT_DEFER, Step

    with session_scope() as s:
        rows, _ = persist_plan(s, new_run_id(),
                               [Step(action=ACT_DEFER, reason="waiting",
                                     payable_number="PO-7002")])
        assert rows == []


def test_reason_text_changing_does_not_create_a_new_identity():
    """Prose changes between runs; the payment does not."""
    from app.domain.plan_store import new_run_id, persist_plan
    from app.domain.planner import ACT_PAY, Step

    a = Step(action=ACT_PAY, reason="first wording", payable_number="PO-7003",
             supplier="Atlanta Packaging", amount_minor=180_000, currency="USD")
    b = Step(action=ACT_PAY, reason="totally different wording",
             payable_number="PO-7003", supplier="Atlanta Packaging",
             amount_minor=180_000, currency="USD")
    with session_scope() as s:
        r1, _ = persist_plan(s, new_run_id(), [a])
        i1 = r1[0].id
    with session_scope() as s:
        r2, _ = persist_plan(s, new_run_id(), [b])
        assert r2[0].id == i1


def test_payable_number_with_sql_wildcards_is_matched_exactly():
    """LIKE on the payload would match this to the wrong row.

    '%' and '_' are LIKE wildcards, so a LIKE-based lookup binds PO_7003 and
    PO-7003X to the same row. This pins the JSON-parse lookup instead.
    """
    reset_default_db()
    _row(number="PO_7003", airwallex_object_id="tr_underscore")
    _row(number="PO_7004", airwallex_object_id="tr_plain")
    assert _find(number="PO_7003").airwallex_object_id == "tr_underscore"
    assert _find(number="PO_7004").airwallex_object_id == "tr_plain"

# ---- FX vs PAY identity (a real bug, caught live 2026-10-07) -------------
def test_fx_and_transfer_steps_for_one_payable_do_not_collide():
    """THE bug: the FX conversion's object id was adopted by the payment.

    Both steps carry payable_number PO-7001. Keying on the payable alone made
    the transfer report ALREADY_DONE after the conversion ran, so the supplier
    was never paid and the run still looked successful.
    """
    from app.domain.executor import Executor
    from app.domain.plan_store import find_step, new_run_id, persist_plan
    from app.domain.planner import ACT_FX, ACT_PAY, Step

    fx = Step(action=ACT_FX, reason="convert", payable_number="PO-7001",
              sell_currency="USD", sell_amount_minor=1_026_317,
              buy_currency="EUR", buy_amount_minor=901_285, currency="EUR")
    pay = Step(action=ACT_PAY, reason="pay", payable_number="PO-7001",
               supplier="Rotterdam Freight BV", amount_minor=901_285,
               currency="EUR")
    with session_scope() as s:
        rows, _ = persist_plan(s, new_run_id(), [fx, pay])
        fx_row = [r for r in rows if r.type == StepType.FX][0]
        pay_row = [r for r in rows if r.type == StepType.TRANSFER][0]
        # Simulate: the FX conversion succeeded and wrote its object id back.
        fx_row.airwallex_object_id = "cnv_live_1"
        s.commit()

    with session_scope() as s:
        assert find_step(s, fx).airwallex_object_id == "cnv_live_1"
        # The transfer step must NOT see that object id...
        assert find_step(s, pay).airwallex_object_id is None

        ex = Executor.__new__(Executor)
        ex.db = s

        class _Fake:
            pass
        f = _Fake(); f.payable_number = "PO-7001"; f.action = ACT_PAY
        assert Executor._row_for(ex, f).airwallex_object_id is None, \
            "the transfer adopted the FX conversion's object id"


def test_an_executor_would_pay_after_the_fx_conversion_completed():
    """End-to-end shape of the bug: conversion done => payment must proceed."""
    from app.domain.plan_store import find_step, new_run_id, persist_plan
    from app.domain.planner import ACT_FX, ACT_PAY, Step

    fx = Step(action=ACT_FX, reason="c", payable_number="PO-7001",
              sell_currency="USD", sell_amount_minor=1_026_317,
              buy_currency="EUR", buy_amount_minor=901_285, currency="EUR")
    pay = Step(action=ACT_PAY, reason="p", payable_number="PO-7001",
               supplier="Rotterdam Freight BV", amount_minor=901_285,
               currency="EUR")
    with session_scope() as s:
        rows, _ = persist_plan(s, new_run_id(), [fx, pay])
        [r for r in rows if r.type == StepType.FX][0].airwallex_object_id = "cnv_1"
        s.commit()
    with session_scope() as s:
        is_already_paid_now = find_step(s, pay).airwallex_object_id is not None
        assert is_already_paid_now is False, \
            "PO-7001 would look already paid and be silently skipped"
