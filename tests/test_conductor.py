"""The loop: re-plan after every event, execute only what is unambiguously clear.

These tests pin the boundary, not the plumbing. The interesting failures are all
one shape — something that should have waited, paying. So most of them assert
that the conductor DID NOTHING, which is the hard direction to get right.

Nothing here touches a network: the executor factory is injected, so the fake
`Transfers` records what would have been sent and nothing is.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import get_default_db, reset_default_db, session_scope
from app.domain.conductor import clear_steps, held_steps, on_event
from app.domain.executor import ExecutionRefused, Executor
from app.domain.planner import ACT_DEFER, ACT_FX, ACT_PAY, Step
from app.domain.workspace import (
    create_customer, create_invoice, create_payable, create_supplier,
    record_beneficiary,
)
from app.models import PlanStep, StepStatus, StepType


@pytest.fixture(autouse=True)
def _fresh_db():
    """A genuinely empty book per test, including the app's own handle."""
    reset_default_db()
    yield


class FakeTransfers:
    """Records everything it would have sent, and sends nothing."""

    def __init__(self, _client=None, **_kw):
        self.calls: list[dict] = []
        self.created = 0

    def create(self, db_session, **kw):
        self.calls.append(kw)
        self.created += 1
        return {"id": f"tr_{self.created}", "status": "SENT"}

    def find_by_request_id(self, request_id):
        self.calls.append({"lookup": request_id})
        return None

    def status_of(self, transfer_id):
        return "PROCESSING"


class FakeFX:
    def __init__(self, _client=None, **_kw):
        self.conversions: list[dict] = []

    def create_quote(self, **kw):
        return type("Q", (), {"quote_id": "q_1",
                              "client_rate": Decimal("0.878174")})()

    def create_conversion(self, db_session, **kw):
        self.conversions.append(kw)
        return {"id": "cnv_1"}


class FakeClient:
    """Stands in for AirwallexClient so no test ever needs credentials."""

    token = "test"
    calls: list = []


def _wire(db, *, amount="1800.00", currency="USD", limit="2500", cash="20000.00",
          beneficiary=True, method="LOCAL"):
    """A minimal but REAL book, built through the app's own CRUD."""
    with session_scope() as s:
        cust = create_customer(s, name="Brightline Retail", country="NG")
        create_invoice(s, customer_id=cust.id, number="INV-1001",
                       currency=currency, amount="12000.00",
                       due_date="2026-12-01")
        sup = create_supplier(s, name="Northwind Logistics", country="US",
                              currency=currency, payout_method=method)
        # The beneficiary comes FIRST: `create_payable` refuses a supplier that
        # cannot be paid, because a bill with nowhere to send money is not a
        # payable, it is a wish.
        if beneficiary:
            record_beneficiary(s, sup.id, request={"test": True},
                               beneficiary_id="ben_123", status="CREATED")
        create_payable(s, supplier_id=sup.id, currency=currency,
                       amount=amount, number="PO-2001")

        from app.domain.ledger import post
        from app.money import to_minor
        post(s, "cash", [("cash:USD", to_minor(cash, "USD"), 0, "USD"),
                         ("opening_balance", 0, to_minor(cash, "USD"), "USD")])

        from app.domain.settings_store import save
        save(s, budget_currency="USD", reserve_floor="5000",
             auto_pay_limit=limit, fx_tolerance_pct="0.50")
    return db


def _run(db, **kw):
    """Run the loop with a FAKE client, always.

    Passing `client` is not cosmetic: without it `on_event` consults
    `settings.sandbox_ready()` and refuses when no credentials are configured —
    which is exactly what happens in CI. These tests passed locally only because
    the developer's `.env` held real sandbox keys. Supplying a client is what
    makes them hermetic, and it is the same seam a real caller uses.
    """
    factory = kw.pop("factory", None)

    def build(**kwargs):
        kwargs["transfers"] = FakeTransfers()
        kwargs["fx"] = FakeFX()
        return Executor(**kwargs)

    kw.setdefault("client", FakeClient())
    return on_event(db, executor_factory=factory or build, **kw)


# ---- what counts as clear ------------------------------------------------
class TestWhatMayMoveWithoutAHuman:
    def test_a_planned_payment_with_no_blockers_is_clear(self):
        db = _wire(get_default_db())
        run = _run(db, source="t", dry_run=True)
        assert any(s.action == ACT_PAY for s in clear_steps(run.plan)), \
            "an affordable, in-limit, unblocked bill is exactly what may be sent"

    def test_a_blocked_payment_is_not_clear_and_says_why(self):
        # USD 1,800 out of a 6,000 balance leaves 4,200 — under the 5,000 floor,
        # and still under the 2,500 limit, so the FLOOR is the reason.
        db = _wire(get_default_db(), amount="1800.00", cash="6000.00")
        run = _run(db, source="t", dry_run=True)
        assert not [s for s in clear_steps(run.plan) if s.action == ACT_PAY]
        # The planner's answer for a floor breach is a DEFER, not a blocked PAY,
        # so match on the payable rather than on the action.
        held = [v for v in run.verdicts if v.outcome == "HELD"
                and v.payable_number == "PO-2001"]
        assert held, [v.as_dict() for v in run.verdicts]
        assert "floor" in held[0].detail, held[0].detail
        assert "4,200.00" in held[0].detail, \
            "the reason must carry the number that actually decides it"

    def test_a_payment_over_the_limit_needs_a_human_not_a_decision(self):
        db = _wire(get_default_db(), amount="9000.00", limit="2500")
        run = _run(db, source="t", dry_run=True)
        assert not clear_steps(run.plan)
        v = next(v for v in run.verdicts if v.action == ACT_PAY)
        assert v.outcome == "HELD"

    def test_defer_and_escalate_never_move_money(self):
        steps = [Step(action=ACT_DEFER, reason="wait", payable_number="PO-1"),
                 Step(action="ESCALATE", reason="ask", payable_number="PO-2")]
        assert clear_steps(_plan(steps)) == []

    def test_a_deferral_is_reported_even_though_it_moves_nothing(self):
        """A filter on `action in MONEY_ACTIONS` alone drops the planner's
        DEFER answer, and the loop then reports "nothing to do" while a supplier
        waits. It must be said, not sent."""
        steps = [Step(action=ACT_DEFER, reason="would leave cash below the floor",
                      payable_number="PO-1")]
        assert clear_steps(_plan(steps)) == []
        held = held_steps(_plan(steps))
        assert held and held[0][1].startswith("would leave cash")

    def test_a_step_with_a_blocker_is_never_clear_even_without_approval(self):
        steps = [Step(action=ACT_PAY, reason="x", payable_number="PO-1",
                      amount_minor=100, currency="USD",
                      blockers=["insufficient_balance"])]
        assert clear_steps(_plan(steps)) == []
        assert held_steps(_plan(steps))[0][1] == "insufficient_balance"


def _plan(steps):
    from app.domain.planner import Plan
    return Plan(steps=list(steps))


# ---- dry run is the safe default ------------------------------------------
class TestDryRunSendsNothing:
    def test_the_default_call_moves_no_money(self):
        db = _wire(get_default_db())
        run = _run(db, source="t", dry_run=True)
        assert not run.executed
        assert all(v.outcome != "EXECUTED" for v in run.verdicts)
        # and nothing was even persisted to send later
        with session_scope() as s:
            assert s.query(PlanStep).count() == 0

    def test_execute_false_moves_no_money_either(self):
        db = _wire(get_default_db())
        run = _run(db, source="t", execute=False, dry_run=False)
        assert not run.executed


# ---- the real thing -------------------------------------------------------
class TestAClearPaymentGoesThroughTheExecutor:
    def test_it_is_sent_and_its_id_is_recorded(self):
        db = _wire(get_default_db())
        run = _run(db, source="deposit", dry_run=False)
        sent = run.executed
        assert sent, [v.as_dict() for v in run.verdicts]
        assert sent[0].action == ACT_PAY and sent[0].object_id == "tr_1"
        with session_scope() as s:
            row = s.query(PlanStep).filter(
                PlanStep.airwallex_object_id == "tr_1").first()
            assert row is not None, "the transfer id must be written back"
            assert row.request_id, "a request_id was committed before the POST"

    def test_a_second_event_does_not_pay_again(self):
        """THE guarantee. A repeated event is not a second payment."""
        db = _wire(get_default_db())
        first = _run(db, source="deposit", dry_run=False)
        assert first.executed

        second = _run(db, source="poll", dry_run=False)
        assert not second.executed, "the same payable must not be paid twice"
        assert second.of("ALREADY_DONE"), [v.as_dict() for v in second.verdicts]

        # exactly ONE transfer exists, however many events arrived
        with session_scope() as s:
            rows = s.query(PlanStep).filter(
                PlanStep.airwallex_object_id.isnot(None)).all()
            assert len(rows) == 1, f"{len(rows)} transfers for one payable"

    def test_a_payable_with_no_beneficiary_is_held_not_sent(self):
        """`create_payable` refuses a supplier with no beneficiary, so a state
        like this comes from the beneficiary being removed afterwards. Either
        way the conductor must not hand Airwallex the literal string "unknown"."""
        db = _wire(get_default_db())
        with session_scope() as s:
            from app.models import Supplier
            s.query(Supplier).one().airwallex_beneficiary_id = None

        run = _run(db, source="deposit", dry_run=False)
        assert not run.executed
        held = run.of("HELD")
        assert held and "no Airwallex beneficiary" in held[0].detail, \
            [v.as_dict() for v in run.verdicts]
        # and nothing was persisted to send later
        with session_scope() as s:
            assert s.query(PlanStep).filter(
                PlanStep.airwallex_object_id.isnot(None)).count() == 0


# ---- an executor refusal is a result, not a crash --------------------------
class TestARefusalIsReportedNotRaised:
    def test_execution_refused_becomes_a_verdict(self):
        db = _wire(get_default_db())

        class Angry(Executor):
            def run_step(self, step):
                raise ExecutionRefused("no spine for this step")

        def build(**kwargs):
            kwargs["transfers"] = FakeTransfers()
            kwargs["fx"] = FakeFX()
            return Angry(**kwargs)

        run = _run(db, source="t", dry_run=False, factory=build)
        assert not run.executed
        assert run.of("REFUSED")[0].detail == "no spine for this step"

    def test_a_dead_api_does_not_take_the_loop_down(self):
        db = _wire(get_default_db())

        def build(**kwargs):
            kwargs["transfers"] = FakeTransfers()
            kwargs["fx"] = FakeFX()
            ex = Executor(**kwargs)

            def boom(step):
                raise RuntimeError("connection reset")

            ex.run_step = boom
            return ex

        run = _run(db, source="t", dry_run=False, factory=build)
        assert run.of("REFUSED"), "the failure is reported, not propagated"
        assert "connection reset" in run.of("REFUSED")[0].detail


def test_the_summary_never_claims_more_than_happened():
    db = _wire(get_default_db())
    run = _run(db, source="deposit", dry_run=True)
    assert "executed" not in run.summary()
    assert "held" in run.summary()


# ---- a failed attempt must not become a dead end --------------------------
class TestAReplacementIsANewOperation:
    """A transfer that ends FAILED or CANCELLED will never settle.

    Treating its row as the current attempt made the payable permanently
    unplannable: the payment fails once and the supplier can never be paid
    again. But a replacement is not a retry either — the original request_id is
    spent and the terms were approved for a payment that did not happen — so it
    must need a human, not go out on its own.
    """

    def _kill_the_attempt(self):
        from app.models import PlanStep, StepStatus
        with session_scope() as s:
            row = s.query(PlanStep).order_by(PlanStep.id.desc()).first()
            row.status = StepStatus.CANCELLED
            return row.id

    def test_a_retired_attempt_is_not_the_current_attempt(self):
        from app.domain.plan_store import find_step
        db = _wire(get_default_db())
        _run(db, source="deposit", dry_run=False)
        with session_scope() as s:
            from app.domain.planner import Step as _S
            st = _S(action=ACT_PAY, reason="x", payable_number="PO-2001",
                    amount_minor=180_000, currency="USD")
            assert find_step(s, st) is not None, "while SUBMITTED it is current"
        self._kill_the_attempt()
        with session_scope() as s:
            from app.domain.planner import Step as _S
            st = _S(action=ACT_PAY, reason="x", payable_number="PO-2001",
                    amount_minor=180_000, currency="USD")
            assert find_step(s, st) is None, \
                "a dead attempt must not block a replacement"

    def test_the_replacement_is_held_for_a_human_not_sent(self):
        db = _wire(get_default_db())
        _run(db, source="deposit", dry_run=False)
        self._kill_the_attempt()

        def build(**kwargs):
            kwargs["transfers"] = FakeTransfers()
            kwargs["fx"] = FakeFX()
            ex = Executor(**kwargs)
            return ex

        run = _run(db, source="after-failure", dry_run=False, factory=build)
        assert not run.executed, [v.as_dict() for v in run.verdicts]
        held = [v for v in run.verdicts if v.payable_number == "PO-2001"
                and v.outcome == "HELD"]
        assert held and "replacement" in held[0].detail, held

        # and no NEW transfer was created for it
        from app.models import PlanStep
        with session_scope() as s:
            objs = [r.airwallex_object_id for r in s.query(PlanStep).all()
                    if r.airwallex_object_id]
            assert len(objs) == 1, f"{len(objs)} transfers for one payable"

    def test_retiring_keeps_the_record_of_what_was_sent(self):
        """The object id is the only link between this book and the transfer the
        bank has. Clearing it would erase the audit trail."""
        from app.models import PlanStep
        db = _wire(get_default_db())
        _run(db, source="deposit", dry_run=False)
        row_id = self._kill_the_attempt()
        with session_scope() as s:
            row = s.get(PlanStep, row_id)
            assert row.airwallex_object_id == "tr_1"
            assert row.status.value == "CANCELLED"
