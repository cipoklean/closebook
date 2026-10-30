"""An approval authorises ONE payment, and a held bill says what holds it.

WHAT WENT WRONG IN RUN 4. An approval was matched by BILL NUMBER, so any step
for that bill would accept it. A bill that had been through a retired attempt,
or a re-plan that wrote a fresh step, could ride an approval a person issued for
a different attempt at a different figure. An approval is a human agreeing to one
amount, in one currency, to one beneficiary, for one attempt. This file holds
that line and the two consequences of it: a held bill names its real blocker, and
settling a payment does not move the number it was already reserved against.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import reset_default_db, session_scope  # noqa: E402
from app.domain.ledger import post  # noqa: E402

PIN = "2468"


class FakeTransfers:
    ALL: dict = {}

    def __init__(self):
        self.calls: list[dict] = []

    def create(self, db_session, **kw):
        rid = kw.get("request_id")
        if rid in FakeTransfers.ALL:
            return FakeTransfers.ALL[rid]
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
        from decimal import Decimal
        return type("Q", (), {"quote_id": "q_1", "client_rate": Decimal("1")})()

    def create_conversion(self, db_session, **kw):
        return {"id": "cnv_1"}


class FakeClient:
    def __getattr__(self, name):
        raise AssertionError(f"the fake client was used for {name}")


@pytest.fixture(autouse=True)
def _isolated():
    reset_default_db()
    FakeTransfers.ALL.clear()
    yield
    reset_default_db()
    FakeTransfers.ALL.clear()


def _cash(ccy, minor, tag="a"):
    with session_scope() as s:
        post(s, f"open:{ccy}:{tag}", [(f"cash:{ccy}", minor, 0, ccy),
                                      ("opening_balance", 0, minor, ccy)])


def _supplier(name="Atlanta Packaging", beneficiary="bn_x"):
    from app.models import PayoutMethod, Supplier

    with session_scope() as s:
        row = Supplier(name=name, payout_method=PayoutMethod.LOCAL,
                       currency="USD", airwallex_beneficiary_id=beneficiary)
        s.add(row)
        s.flush()
        return row.id


def _bill(number, minor, supplier_id):
    from app.models import InvoicePayable, PayStatus

    with session_scope() as s:
        s.add(InvoicePayable(number=number, supplier_id=supplier_id,
                             currency="USD", amount_minor=minor,
                             status=PayStatus.READY, criticality=3))


def _cycle(transfers, *, execute=True):
    from app.db import get_default_db
    from app.domain.conductor import on_event
    from app.domain.executor import Executor

    def build(**kw):
        kw["client"] = FakeClient()
        kw["transfers"] = transfers
        kw["fx"] = FakeFX()
        return Executor(**kw)

    return on_event(get_default_db(), source="test", budget_s=30,
                    execute=execute, dry_run=not execute,
                    client=FakeClient(), executor_factory=build)


def _step(number):
    from app.db import get_default_db
    from app.models import PlanStep

    return (get_default_db().session().query(PlanStep)
            .filter(PlanStep.step_key == f"PAY:{number}").first())


def _approve(step_id):
    from app.db import get_default_db
    from app.domain.approval_api import approve_step

    return approve_step(get_default_db(), step_id, pin=PIN,
                        approved_by="tester", configured_pin=PIN)


def _cards():
    from app.dashboard import build_context

    return build_context([])["approvable"]


class TestAnApprovalIsOnePayment:
    def test_an_over_limit_bill_is_not_sent_by_a_deposit(self):
        """The headline. Over the limit, no approval, so NOTHING goes out, and
        the card exists so a person can say yes."""
        _cash("USD", 20_000_00)
        sid = _supplier()
        _bill("PO-B9", 4_000_00, sid)          # limit is 3,000
        transfers = FakeTransfers()
        _cycle(transfers)
        assert not transfers.calls, "an over-limit bill was sent without an approval"
        assert _cards(), "the over-limit bill produced no approval card"

    def test_two_bills_same_supplier_same_amount_do_not_share_an_approval(self):
        _cash("USD", 30_000_00)
        sid = _supplier()
        _bill("PO-SAME-1", 4_000_00, sid)
        _bill("PO-SAME-2", 4_000_00, sid)
        _cycle(FakeTransfers())
        assert len(_cards()) == 2, _cards()

        _approve(_step("PO-SAME-1").id)
        assert len(_cards()) == 1, "approving one bill released the other"

        transfers = FakeTransfers()
        _cycle(transfers)
        assert len(transfers.calls) == 1, transfers.calls
        assert _step("PO-SAME-1").airwallex_object_id
        assert _step("PO-SAME-2").airwallex_object_id is None, \
            "the second bill rode the first bill's approval"

    def test_an_approval_does_not_survive_a_change_of_beneficiary(self):
        """The reachable form of the same hole.

        A bill has one step (the key is unique, so a re-plan converges rather
        than adding one), but the SUPPLIER's beneficiary can be edited after the
        approval. The approval said "send this to that person"; a different
        beneficiary is a different payment, whatever the amount says.
        """
        from app.models import Supplier

        _cash("USD", 20_000_00)
        sid = _supplier(beneficiary="bn_original")
        _bill("PO-BEN", 4_000_00, sid)
        _cycle(FakeTransfers())
        _approve(_step("PO-BEN").id)

        with session_scope() as s:
            s.get(Supplier, sid).airwallex_beneficiary_id = "bn_someone_else"

        transfers = FakeTransfers()
        _cycle(transfers)
        assert not transfers.calls, \
            "the approval authorised a beneficiary that is no longer the payee"
        assert _cards(), "and the person must be asked again"

    def test_the_limit_comes_from_the_current_setting(self):
        """Raise the limit and the same bill stops needing an approval."""
        from app.models import WorkspaceSettings

        _cash("USD", 20_000_00)
        sid = _supplier()
        _bill("PO-LIM", 4_000_00, sid)
        _cycle(FakeTransfers())
        assert _cards(), "4,000 should be over a 3,000 limit"

        from app.domain import settings_store

        with session_scope() as s:
            row = settings_store.get_or_create(s)
            row.auto_pay_limit_minor = 5_000_00

        transfers = FakeTransfers()
        _cycle(transfers)
        assert transfers.calls, "a raised limit did not take effect"
        assert not _cards()


class TestAHeldBillSaysWhatHoldsIt:
    """The badge is copy; the blocker is the fact. Assert on the fact."""

    def _step(self, *, cash_minor, amount_minor=4_000_00, floor_minor=500_000):
        from decimal import Decimal

        from app.domain.planner import CashPosition, Payable, build_plan
        from app.domain.policy import load_budget

        pos = CashPosition(balances={"USD": cash_minor}, rates={"USD": Decimal(1)},
                           budget_currency="USD", floor_minor=floor_minor)
        p = Payable(number="PO-H", supplier="Atlanta Packaging", currency="USD",
                    amount_minor=amount_minor)
        return build_plan(payables=[p], pos=pos, budget=load_budget()).steps[0]

    def test_cash_short_names_the_shortfall_with_numbers(self):
        step = self._step(cash_minor=2_000_00)
        assert step.blockers, "a bill that cannot be paid must say why"
        joined = " ".join(step.blockers).lower()
        assert any(ch.isdigit() for ch in joined), \
            f"the blocker names no number: {step.blockers}"
        assert (("cash" in joined or "balance" in joined or "account" in joined
                or "floor" in joined or "holds" in joined)), step.blockers

    def test_the_reserve_floor_names_the_floor(self):
        # 5,400 cash, a 4,000 bill, a 5,000 floor: the bill breaches the floor
        step = self._step(cash_minor=5_400_00)
        joined = " ".join(step.blockers).lower()
        assert "safety cushion" in joined, step.blockers
        assert any(ch.isdigit() for ch in joined), step.blockers

    def test_an_over_limit_bill_says_the_limit_and_its_number(self):
        step = self._step(cash_minor=90_000_00)
        assert step.needs_approval, "4,000 is over a 3,000 limit"
        reason = (step.approval_reason or "").lower()
        assert "limit" in reason, step.approval_reason
        assert any(ch.isdigit() for ch in reason), step.approval_reason


class TestSettlingDoesNotMoveFreeToSpend:
    """Free to spend must not fall when a payment settles.

    The money left the figure the moment it was committed, not when the bank
    confirmed. If it only leaves at settlement, the operator is shown cash they
    have already promised.
    """

    def test_an_in_flight_payment_is_already_subtracted(self):
        from app.domain.policy import available_cash_minor

        base = dict(cash_minor=22_300_00, floor_minor=0)
        without = available_cash_minor(in_flight_minor=0, **base)
        with_flight = available_cash_minor(in_flight_minor=10_000_00, **base)
        assert with_flight == without - 10_000_00

    def test_committing_a_payment_changes_free_to_spend_once(self):
        from app.domain.policy import available_cash_minor

        # committed, not yet settled: cash is still on the books, in flight
        committed = available_cash_minor(cash_minor=22_300_00, in_flight_minor=10_000_00,
                                         floor_minor=0)
        # settled: the ledger cash has fallen, and nothing is in flight
        settled = available_cash_minor(cash_minor=12_300_00, in_flight_minor=0,
                                       floor_minor=0)
        assert committed == settled, (
            "settling moved free to spend from "
            f"{committed} to {settled}: the payment was subtracted twice")


class TestParallelWrites:
    """A parallel run must not lose a row or answer 5xx.

    Run 4 had Cloudflare 502s on invoice and bill creation. The origin never
    logged a traceback, which is what a stalled event loop looks like from
    outside: the request is fine, it just never gets a turn. These tests hold the
    behaviour that makes that impossible, and the route that does the most work
    now runs off the loop.
    """

    @pytest.fixture()
    def file_book(self, tmp_path, monkeypatch):
        """A FILE database, because that is what production runs.

        The in-memory book every other test uses shares one connection across
        threads, which is not how the app behaves and would have made this test
        fail for a reason the product does not have.
        """
        import app.db as dbmod

        url = f"sqlite:///{tmp_path / 'parallel.sqlite3'}"
        monkeypatch.setenv("CLOSEBOOK_DATABASE_URL", url)
        dbmod.reset_default_db()          # reads the env var we just set
        yield url

    def test_twenty_parallel_setup_posts_lose_nothing(self, file_book):
        import threading

        from fastapi.testclient import TestClient

        from app.main import app
        from app.models import Customer

        client = TestClient(app)
        names = [f"Parallel Co {i}" for i in range(20)]
        codes: list[int] = []
        lock = threading.Lock()

        errors: list = []

        def post(name):
            try:
                r = client.post("/setup/customers", json={"name": name},
                                headers={"Accept": "application/json"})
                with lock:
                    codes.append(r.status_code)
            except Exception as exc:               # noqa: BLE001
                with lock:
                    errors.append(repr(exc)[:160])

        threads = [threading.Thread(target=post, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors[:4]
        assert len(codes) == 20
        assert all(c < 500 for c in codes), f"a 5xx under parallel load: {codes}"
        from app.db import get_default_db
        s = get_default_db().session()
        stored = {c.name for c in s.query(Customer).all()}
        missing = [n for n in names if n not in stored]
        assert not missing, f"rows lost under parallel load: {missing}"

    def test_a_parallel_cycle_does_not_block_writes(self, file_book):
        import threading

        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        _cash("USD", 20_000_00, "par")

        results: list[int] = []

        def writer(i):
            r = client.post("/setup/customers", json={"name": f"Cycle Co {i}"},
                            headers={"Accept": "application/json"})
            results.append(r.status_code)

        def cycle():
            r = client.post("/money-out/run", headers={"Accept": "application/json"})
            results.append(r.status_code)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        threads.append(threading.Thread(target=cycle))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(c < 500 for c in results), results
