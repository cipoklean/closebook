"""A stored approval must actually release the payment it authorised.

THE BUG THIS PINS. Approving an over-limit bill recorded the decision and
changed nothing. `approve_step` wrote an APPROVED row, the step went PROPOSED ->
APPROVED, and it stopped there: the planner regenerated `needs_approval` from the
limit on every cycle and never looked at the stored approval, so no cycle ever
considered the step clear and no transfer was ever created. The operator
approved a payment that could not be paid, and the screen said the decision was
recorded.

The rule these tests enforce:

  * a valid stored APPROVED approval clears the LIMIT CHECK ONLY;
  * every other gate still applies -- reserve floor, available cash, dependency,
    sanctions fence -- and if one blocks, the step is Held with that reason and
    the approval is KEPT, never re-asked;
  * the approval is void only when the amount, currency, beneficiary or payable
    changes, and then it asks again;
  * exactly ONE transfer is created, whatever happens concurrently.
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import threading
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import reset_default_db, session_scope  # noqa: E402
from app.domain.ledger import post  # noqa: E402

PIN = "2468"


# ---- fakes ---------------------------------------------------------------
class FakeTransfers:
    """Records every create; the lookup returns what was created under that id.

    That is what a real Airwallex does for a duplicate request_id inside its
    dedup window, which is the behaviour the idempotency rests on.
    """
    ALL = {}

    def __init__(self):
        self.calls: list[dict] = []      # transfers actually created
        self.attempts: list[dict] = []   # create calls made

    def create(self, db_session, **kw):
        """`calls` records CREATIONS, not attempts.

        That is what the real service does: a repeated request_id inside the
        dedup window returns the transfer it already has. Counting attempts
        would count a correct no-op as a duplicate payment.
        """
        self.attempts.append(kw)
        rid = kw.get("request_id")
        created = FakeTransfers.ALL.get(rid)
        if created is not None:
            return created
        obj = {"id": f"tr_{len(FakeTransfers.ALL) + 1}", "status": "SENT"}
        FakeTransfers.ALL[rid] = obj
        self.calls.append(kw)
        return obj

    def find_by_request_id(self, request_id):
        return FakeTransfers.ALL.get(request_id)

    def status_of(self, transfer_id):
        return "PROCESSING"


class FakeFX:
    def create_quote(self, **kw):
        return type("Q", (), {"quote_id": "q_1", "client_rate": Decimal("1")})()

    def create_conversion(self, db_session, **kw):
        return {"id": "cnv_1"}


class FakeClient:
    def __init__(self):
        self.token = "t"

    def __getattr__(self, name):
        raise AssertionError(f"the fake client was used for {name}")


# ---- world ---------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated():
    reset_default_db()
    FakeTransfers.ALL.clear()
    yield
    reset_default_db()
    FakeTransfers.ALL.clear()


_N = itertools.count(1)


def _cash(ccy, minor):
    """Fund the book. The transaction id must be UNIQUE per call: `post()` is
    idempotent per (txn, account, currency), so a fixed id made the second
    deposit a silent no-op and the test then measured the first one."""
    with session_scope() as s:
        n = next(_N)
        post(s, f"opening:{ccy}:{n}", [(f"cash:{ccy}", minor, 0, ccy),
                                       ("opening_balance", 0, minor, ccy)])


def _spend(ccy, minor):
    """Money leaving the book, posted the only way it can be: debits equal
    credits in the same currency, cash on the credit side."""
    with session_scope() as s:
        post(s, f"spend:{ccy}:{next(_N)}", [("payable_clearing", minor, 0, ccy),
                                            (f"cash:{ccy}", 0, minor, ccy)])


def _bill(number="QA-OVL-1", minor=3_500_00, ccy="USD", *, supplier="Atlanta Packaging"):
    from app.models import InvoicePayable, PayStatus, PayoutMethod, Supplier

    with session_scope() as s:
        sup = (s.query(Supplier).filter(Supplier.name == supplier).first()
               or Supplier(name=supplier, payout_method=PayoutMethod.LOCAL,
                           currency="USD",
                           airwallex_beneficiary_id="bn_test_1"))
        if sup.id is None:
            s.add(sup)
            s.flush()
        s.add(InvoicePayable(number=number, supplier_id=sup.id, currency=ccy,
                             amount_minor=minor, status=PayStatus.READY,
                             criticality=3))


def _factory(transfers):
    """The conductor calls the factory with keyword arguments; swap the two
    that would talk to Airwallex and leave the rest alone."""
    def build(**kw):
        from app.domain.executor import Executor
        kw["client"] = FakeClient()
        kw["transfers"] = transfers
        kw["fx"] = FakeFX()
        return Executor(**kw)
    return build


def _cycle(db, transfers, *, execute=True):
    from app.domain.conductor import on_event
    return on_event(db, source="test", budget_s=30, execute=execute,
                    dry_run=not execute, client=FakeClient(),
                    executor_factory=_factory(transfers))


def _approve(step_id, *, pin=PIN):
    from app.db import get_default_db
    from app.domain.approval_api import approve_step
    return approve_step(get_default_db(), step_id, pin=pin,
                        approved_by="tester", configured_pin=PIN)


def _approvable():
    from app.dashboard import build_context
    return build_context([])["approvable"]


def _step_for(db, number="QA-OVL-1"):
    from app.models import PlanStep
    for row in db.session().query(PlanStep).all():
        pl = json.loads(row.payload_json or "{}")
        if pl.get("payable_number") == number:
            return row
    return None


class TestASingleCycle:
    """The whole bug, end to end."""

    def test_an_over_limit_bill_gets_an_approval_card(self):
        from app.db import get_default_db
        _cash("USD", 9_000_00)
        _bill()
        _cycle(get_default_db(), FakeTransfers())
        assert len(_approvable()) == 1

    def test_approving_it_creates_exactly_one_transfer(self):
        """The failing line against master: no transfer was ever created."""
        from app.db import get_default_db

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        row = _step_for(db)
        _approve(row.id)

        transfers = FakeTransfers()
        _cycle(db, transfers, execute=True)
        assert len(transfers.calls) == 1, transfers.calls
        assert _step_for(db).airwallex_object_id, "the transfer id was not stored"

    def test_and_the_verdict_is_not_held(self):
        from app.db import get_default_db

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        _approve(_step_for(db).id)
        run = _cycle(db, FakeTransfers(), execute=True)
        held = [v for v in run.verdicts if v.outcome == "HELD"]
        assert not held, [(v.payable_number, v.detail) for v in held]

    def test_approving_twice_still_creates_one_transfer(self):
        from app.db import get_default_db

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        row = _step_for(db)
        _approve(row.id)
        with pytest.raises(Exception):
            _approve(row.id)          # the second approve is refused

        transfers = FakeTransfers()
        _cycle(db, transfers, execute=True)
        _cycle(db, transfers, execute=True)
        assert len(transfers.calls) == 1, transfers.calls

    def test_two_concurrent_cycles_create_one_transfer(self):
        """Both cycles see the same row and race to send it."""
        from app.db import get_default_db

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        _approve(_step_for(db).id)

        transfers = FakeTransfers()
        errors: list = []

        def go():
            try:
                _cycle(get_default_db(), transfers, execute=True)
            except Exception as exc:                     # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # A losing cycle may refuse; it must not create a second transfer.
        rids = [c.get("request_id") for c in transfers.calls]
        from app.models import PlanStep
        rows = get_default_db().session().query(PlanStep).all()
        assert len(transfers.calls) == 1, (
            "request_ids=%r rows=%r errors=%r" % (
                rids, [(r.id, r.step_key, r.request_id) for r in rows], errors))

    def test_a_rerun_does_not_re_ask(self):
        """The card is gone once approved, and stays gone."""
        from app.db import get_default_db

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        _approve(_step_for(db).id)
        assert not _approvable(), "the approval card came back after approval"
        _cycle(db, FakeTransfers(), execute=True)
        assert not _approvable()


class TestTheOtherGatesStillApply:
    """The approval clears the LIMIT CHECK and nothing else."""

    def test_with_cash_short_it_is_held_and_not_sent(self):
        from app.db import get_default_db

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        row = _step_for(db)
        _approve(row.id)

        _spend("USD", 7_000_00)           # the money is spent on something else
        transfers = FakeTransfers()
        run = _cycle(db, transfers, execute=True)
        assert not transfers.calls, "nothing should be sent without the cash"
        assert _step_for(db).airwallex_object_id is None
        assert any(v.outcome == "HELD" for v in run.verdicts), run.verdicts

    def test_the_approval_is_kept_and_not_re_asked_while_held(self):
        from app.db import get_default_db

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        _approve(_step_for(db).id)
        _cash("USD", -7_000_00)
        _cycle(db, FakeTransfers(), execute=True)
        assert not _approvable(), "a held-but-approved step must not re-ask"

    def test_and_it_sends_once_the_cash_arrives_without_a_new_ask(self):
        from app.db import get_default_db

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        _approve(_step_for(db).id)
        _spend("USD", 7_000_00)           # the money goes out elsewhere
        _cycle(db, FakeTransfers(), execute=True)
        assert not FakeTransfers.ALL

        _cash("USD", 7_000_00)            # and comes back
        transfers = FakeTransfers()
        _cycle(db, transfers, execute=True)
        assert len(transfers.calls) == 1, transfers.calls
        assert not _approvable(), "it was already approved; do not ask again"

    def test_a_dependency_hold_still_blocks_an_approved_step(self):
        """A dependency is not a human decision, so an approval cannot lift it.

        The order matters and it is the operator's order: approve while nothing
        blocks it, THEN the block appears. A step that is blocked from the start
        is never offered a card at all, which is correct -- a button that cannot
        honestly do anything is worse than no button.
        """
        from app.db import get_default_db
        from app.models import Customer, InvoicePayable, InvoiceReceivable, RecStatus

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        _approve(_step_for(db).id)

        # Now the money it depends on has not arrived.
        with session_scope() as s:
            c = Customer(name="Harrow & Co")
            s.add(c)
            s.flush()
            rec = InvoiceReceivable(number="INV-DEP-1", customer_id=c.id,
                                    currency="USD", amount_minor=500_00,
                                    status=RecStatus.OPEN)
            s.add(rec)
            s.flush()
            dep = rec.id
        with session_scope() as s:
            s.query(InvoicePayable).filter_by(number="QA-OVL-1").one() \
                .depends_on_receivable_id = dep

        transfers = FakeTransfers()
        run = _cycle(db, transfers, execute=True)
        assert not transfers.calls, "an approval must not lift a dependency hold"
        assert any("waiting on INV-DEP-1" in (v.detail or "") for v in run.verdicts), \
            [(v.outcome, v.detail) for v in run.verdicts]

    def test_the_approval_survives_that_block_and_is_not_re_asked(self):
        from app.db import get_default_db
        from app.models import Customer, InvoicePayable, InvoiceReceivable, RecStatus

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        _approve(_step_for(db).id)
        with session_scope() as s:
            c = Customer(name="Harrow & Co")
            s.add(c)
            s.flush()
            rec = InvoiceReceivable(number="INV-DEP-1", customer_id=c.id,
                                    currency="USD", amount_minor=500_00,
                                    status=RecStatus.OPEN)
            s.add(rec)
            s.flush()
            dep = rec.id
        with session_scope() as s:
            s.query(InvoicePayable).filter_by(number="QA-OVL-1").one() \
                .depends_on_receivable_id = dep

        _cycle(db, FakeTransfers(), execute=True)
        assert not _approvable(), "a blocked, approved step must not re-ask"


class TestTheApprovalIsVoidWhenTheTermsChange:
    def test_editing_the_amount_voids_it_and_the_card_comes_back(self):
        from app.db import get_default_db
        from app.models import InvoicePayable

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        _approve(_step_for(db).id)
        assert not _approvable()

        with session_scope() as s:
            s.query(InvoicePayable).filter_by(number="QA-OVL-1").one() \
                .amount_minor = 3_900_00        # edited: still over the limit, still affordable

        transfers = FakeTransfers()
        _cycle(db, transfers, execute=True)
        assert not transfers.calls, "the old approval must not authorise a new amount"
        assert _approvable(), "the card must come back for the new amount"

    def test_an_approval_does_not_clear_a_different_currency(self):
        """Asserted where the rule lives: `valid_approval` is the one reader of
        the stored approval, and it has to reject a currency that differs."""
        from app.db import get_default_db
        from app.domain.plan_store import valid_approval

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        _approve(_step_for(db).id)

        s = db.session()
        assert valid_approval(s, payable_number="QA-OVL-1", action="PAY",
                              amount_minor=3_500_00, currency="USD",
                              counterparty="Atlanta Packaging") is not None
        assert valid_approval(s, payable_number="QA-OVL-1", action="PAY",
                              amount_minor=3_500_00, currency="EUR",
                              counterparty="Atlanta Packaging") is None
        assert valid_approval(s, payable_number="QA-OVL-1", action="PAY",
                              amount_minor=3_900_00, currency="USD",
                              counterparty="Atlanta Packaging") is None
        assert valid_approval(s, payable_number="QA-OVL-1", action="PAY",
                              amount_minor=3_500_00, currency="USD",
                              counterparty="Someone Else Ltd") is None

    def test_the_approval_is_matched_to_the_payable(self):
        """Two bills, one approved: only that one is released."""
        from app.db import get_default_db

        db = get_default_db()
        _cash("USD", 20_000_00)
        _bill(number="QA-OVL-1")
        _bill(number="QA-OVL-2")
        _cycle(db, FakeTransfers())
        assert len(_approvable()) == 2, _approvable()

        _approve(_step_for(db, "QA-OVL-1").id)
        still = _approvable()
        assert len(still) == 1, still

        transfers = FakeTransfers()
        _cycle(db, transfers, execute=True)
        assert len(transfers.calls) == 1, transfers.calls
        assert _step_for(db, "QA-OVL-1").airwallex_object_id
        assert _step_for(db, "QA-OVL-2").airwallex_object_id is None


class TestTheIdempotencyKeyIsStored:
    def test_the_key_is_written_before_anything_leaves(self):
        """A restart mid-send must find the same key on disk."""
        from app.db import get_default_db
        from app.domain.executor import Executor
        from app.models import PlanStep

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        row = _step_for(db)

        with session_scope() as s:
            fresh = s.get(PlanStep, row.id)
            rid = Executor.reserve(fresh)
            s.commit()
        assert rid
        with session_scope() as s:
            assert s.get(PlanStep, row.id).request_id == rid

    def test_reserving_twice_returns_the_same_key(self):
        from app.db import get_default_db
        from app.domain.executor import Executor
        from app.models import PlanStep

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        row = _step_for(db)
        with session_scope() as s:
            first = Executor.reserve(s.get(PlanStep, row.id))
            s.commit()
        with session_scope() as s:
            second = Executor.reserve(s.get(PlanStep, row.id))
            s.commit()
        assert first == second

    def test_two_sessions_racing_agree_on_one_key(self):
        """The key is claimed by a conditional update, so a loser re-reads the
        winner's value instead of minting a second one."""
        from app.db import get_default_db
        from app.db import get_default_db as _g
        from app.domain.executor import Executor
        from app.models import PlanStep

        db = get_default_db()
        _cash("USD", 9_000_00)
        _bill()
        _cycle(db, FakeTransfers())
        row = _step_for(db)

        s1, s2 = _g().session(), _g().session()
        a = Executor.reserve(s1.get(PlanStep, row.id))
        s1.commit()
        b = Executor.reserve(s2.get(PlanStep, row.id))
        s2.commit()
        assert a == b, (a, b)


class TestThePageDoesNotContradictTheSettleResponse:
    """A settle response says PAID. The page said PROCESSING, because the pill
    reads a cache that a background refresh fills and settle never wrote to.
    Reporting one thing and displaying another is worse than either."""

    def test_settling_writes_the_answer_it_just_read(self):
        from app.dashboard import _LIVE_CACHE, live_transfer_statuses, note_live_status
        from app.db import get_default_db
        from app.domain.ledger import post  # noqa: F401
        from app.models import PlanStep, StepStatus, StepType

        db = get_default_db()
        with session_scope() as s:
            s.add(PlanStep(type=StepType.TRANSFER, status=StepStatus.SUBMITTED,
                           step_key="PAY:QA-PILL-1",
                           payload_json='{"payable_number": "QA-PILL-1", "action": "PAY"}',
                           airwallex_object_id="tr_pill_1"))
        _LIVE_CACHE.pop("tr_pill_1", None)

        s = db.session()
        assert live_transfer_statuses(s).get("tr_pill_1") == "UNCHECKED"

        note_live_status("tr_pill_1", "paid")
        assert live_transfer_statuses(s).get("tr_pill_1") == "PAID"

    def test_it_ignores_an_empty_answer(self):
        from app.dashboard import _LIVE_CACHE, note_live_status

        _LIVE_CACHE.pop("tr_pill_2", None)
        note_live_status("tr_pill_2", "")
        assert "tr_pill_2" not in _LIVE_CACHE
