"""ONE basis for affordability, shared by the planner and the tiles.

The plan and the dashboard used to answer "can we afford this?" differently: the
plan measured the reserve floor against a committed-cash budget of EXPECTED
INFLOWS (USD 40,000 invoiced, not landed) while the tiles measured it against
ledger cash. So the plan approved PO-7003 while the page reported the position
USD 7,715.00 short. These tests pin the single rule that replaced both.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import get_default_db, reset_default_db, session_scope  # noqa: E402
from app.dashboard import build_context  # noqa: E402
from app.domain.ledger import post, reconcile  # noqa: E402
from app.domain.planner import ACT_DEFER, ACT_FX, ACT_PAY, build_plan  # noqa: E402
from app.domain.policy import available_cash_minor, load_budget  # noqa: E402
from app.domain.treasury import cash_position, current_plan, payables_from_db  # noqa: E402
from app.models import (  # noqa: E402
    Customer, InvoicePayable, InvoiceReceivable, PayStatus, PayoutMethod,
    RecStatus, Supplier,
)
from app.ui import render_app  # noqa: E402

FLOOR = 500_000


@pytest.fixture(autouse=True)
def _isolated_db():
    reset_default_db()
    import app.dashboard as dash
    prev = dash.LIVE_STATUS_ENABLED
    dash.LIVE_STATUS_ENABLED = False
    yield
    dash.LIVE_STATUS_ENABLED = prev


@pytest.fixture
def db():
    return get_default_db()


def _cash(ccy: str, minor: int) -> None:
    with session_scope() as s:
        post(s, f"seed:{ccy}", [(f"cash:{ccy}", minor, 0, ccy),
                                ("opening_balance", 0, minor, ccy)])


def _payable(number: str, ccy: str, minor: int, *, supplier="Supplies Co",
             method=PayoutMethod.LOCAL, criticality=3, depends_on=None) -> None:
    with session_scope() as s:
        sup = Supplier(name=f"{supplier} {number}", payout_method=method)
        s.add(sup)
        s.flush()
        s.add(InvoicePayable(number=number, supplier_id=sup.id, currency=ccy,
                             amount_minor=minor, status=PayStatus.HELD,
                             criticality=criticality,
                             depends_on_receivable_id=depends_on))


def _receivable(number: str, ccy: str, minor: int, paid: int = 0) -> int:
    with session_scope() as s:
        c = Customer(name=f"Cust {number}")
        s.add(c)
        s.flush()
        r = InvoiceReceivable(number=number, customer_id=c.id, currency=ccy,
                              amount_minor=minor, amount_paid_minor=paid,
                              status=RecStatus.PAID if paid >= minor
                              else (RecStatus.PARTIAL if paid else RecStatus.OPEN))
        s.add(r)
        s.flush()
        return r.id


# ------------------------------------------------ the rule, and its one home
class TestTheRule:
    def test_the_planner_and_the_tile_compute_the_same_available_cash(self, db):
        """The invariant, stated directly: whatever the tile calls free plus
        whatever it calls promised is exactly what the planner calls available.

        If these ever drift the plan and the page disagree again, which is the
        entire defect this replaced.
        """
        _cash("USD", 2_100_000)
        _payable("PO-A", "USD", 300_000)
        _payable("PO-B", "USD", 450_000)

        ctx = build_context([])
        m = ctx["money"]
        pos = cash_position(db, load_budget())

        assert pos.available_minor() == (m["free"]["USD"]
                                         + int(m["promised"].get("USD", 0)))
        assert pos.available_minor() == available_cash_minor(
            cash_minor=2_100_000, in_flight_minor=0,
            committed_this_run_minor=0, floor_minor=FLOOR)

    def test_the_planner_reads_ledger_cash_not_a_constant(self, db):
        """It used to read SEED_DEPOSITS — hardcoded demo numbers — so the plan
        could spend money the ledger did not have."""
        _cash("USD", 123_456)
        pos = cash_position(db, load_budget())
        assert pos.balances["USD"] == 123_456

    def test_there_is_no_second_basis_left_to_call(self):
        """The committed-cash budget is gone, not merely unused."""
        from app.domain import policy as pol
        assert not hasattr(pol, "CommittedCash")
        assert not hasattr(load_budget(), "committed")


# ------------------------------- defers, and names what it waits for
class TestWaitsForInflow:
    def test_a_payment_that_only_fits_with_expected_inflows_is_deferred(self, db):
        """USD 6,000 of REAL cash, a USD 3,000 payable, and a USD 12,000 invoice
        already invoiced but not landed. The forecast covers it; the cash does
        not. It must be DEFERRED and named, never paid early."""
        _cash("USD", 600_000)
        _payable("PO-X", "USD", 300_000)
        _receivable("INV-BIG", "USD", 1_200_000)      # invoiced, not landed

        plan = current_plan(db)
        step = next(s for s in plan.steps if s.payable_number == "PO-X")
        assert step.action == ACT_DEFER, "it must NOT be paid on expected money"
        assert step.blocker_codes[0] == "waits_for_inflow"
        assert "waits for INV-BIG to land" in step.blockers[0]

    def test_the_deferral_is_not_a_shortfall_of_the_wrong_kind(self, db):
        """A timing problem and an affordability problem read differently,
        because the fix differs."""
        _cash("USD", 600_000)
        _payable("PO-X", "USD", 300_000)
        _receivable("INV-BIG", "USD", 1_200_000)
        step = next(s for s in current_plan(db).steps
                    if s.payable_number == "PO-X")
        assert "below the" in step.blockers[0]        # it says what it would leave

    def test_no_forecast_means_a_plain_floor_reason_not_a_promise(self, db):
        """Nothing is invoiced, so there is nothing to wait for and the reason
        must not invent one."""
        _cash("USD", 600_000)
        _payable("PO-X", "USD", 300_000)
        step = next(s for s in current_plan(db).steps
                    if s.payable_number == "PO-X")
        assert step.action == ACT_DEFER
        assert step.blocker_codes[0] == "reserve_floor"
        assert "waits for" not in step.blockers[0]

    def test_a_forecast_that_would_not_cover_it_does_not_get_named_alone(self, db):
        """A USD 12,000 payment against a USD 6,000 cash position and a USD 1,000
        invoice: the invoice does not fix it, so it must not be offered as the
        fix."""
        _cash("USD", 600_000)
        _payable("PO-X", "USD", 1_200_000)
        _receivable("INV-SMALL", "USD", 100_000)
        step = next(s for s in current_plan(db).steps
                    if s.payable_number == "PO-X")
        assert step.action == ACT_DEFER
        assert "waits for" not in step.blockers[0]

    def test_expected_inflows_never_make_a_payment_happen(self, db):
        """The strongest form: cash USD 0, a huge forecast, a payable. Nothing
        may be planned as payable."""
        _cash("USD", 0)
        _payable("PO-X", "USD", 100_000)
        _receivable("INV-BIG", "USD", 9_000_000)
        plan = current_plan(db)
        assert not [s for s in plan.steps if s.action == ACT_PAY], \
            "money that has not landed must never fund a payment"


# ------------------------------------------------------------ position sanity
class TestOpenPosition:
    def test_the_forecast_is_derived_from_the_open_receivables(self, db):
        _receivable("INV-1", "USD", 1_200_000)
        _receivable("INV-2", "USD", 450_000, paid=100_000)   # 3,500 open
        _receivable("INV-3", "USD", 700_000, paid=700_000)   # closed, excluded
        pos = cash_position(db, load_budget())
        by_num = {l["number"]: l["min"] if "min" in l else l["amount_minor"]
                  for l in pos.forecast.lines}
        assert set(by_num) == {"INV-1", "INV-2"}
        assert by_num == {"INV-1": 1_200_000, "INV-2": 350_000}


# ------------------------------------------------- beat 1 from the new seed
class TestBeatOneFromTheSeed:
    """The scripted demo's first beat, on the real seed."""

    def _seed(self):
        from app.seed import run_seed
        run_seed._seed_local(get_default_db())

    def test_the_seed_puts_real_cash_in_the_ledger(self, db):
        self._seed()
        assert reconcile(db.session()).cash_by_currency == {"USD": 900_000,
                                                           "GBP": 600_000}

    def test_po7003_auto_pays_out_of_real_cash_above_the_floor(self, db):
        self._seed()
        step = next(s for s in current_plan(db).steps
                    if s.payable_number == "PO-7003")
        assert step.action == ACT_PAY
        assert step.needs_approval is False, "1,800 < the 2,500 limit"
        # the arithmetic the seed was chosen for
        assert available_cash_minor(cash_minor=900_000 - 180_000,
                                    floor_minor=FLOOR) == 220_000

    def test_po7001_waits_for_inv0041_and_po7002_for_inv0042(self, db):
        self._seed()
        steps = {s.payable_number: s for s in current_plan(db).steps}
        assert steps["PO-7001"].action == ACT_DEFER
        assert steps["PO-7002"].action == ACT_DEFER
        assert any("waits for INV-0041 to land" in b
                   for b in steps["PO-7001"].blockers)
        assert any("waits for INV-0042 to land" in b
                   for b in steps["PO-7002"].blockers)

    def test_each_held_payment_states_both_reasons(self, db):
        """Not just the first: the dependency AND the cash."""
        self._seed()
        steps = {s.payable_number: s for s in current_plan(db).steps}
        for number in ("PO-7001", "PO-7002"):
            assert len(steps[number].blockers) >= 2, number
            assert "dependency_not_met" in steps[number].blocker_codes
            assert len(set(steps[number].blocker_codes)) == len(
                steps[number].blocker_codes), "no duplicated reason"

    def test_once_inv0041_lands_po7001_converts_and_pays(self, db):
        self._seed()
        with session_scope() as s:
            post(s, "test:inv41", [("cash:USD", 1_200_000, 0, "USD"),
                                   ("receivable_clearing", 0, 1_200_000, "USD")])
            r = s.query(InvoiceReceivable).filter_by(number="INV-0041").one()
            r.amount_paid_minor = r.amount_minor
            r.status = RecStatus.PAID
        steps = {s.payable_number: s for s in current_plan(db).steps}
        assert steps["PO-7001"].action in (ACT_FX, ACT_PAY)
        pay = next(s for s in current_plan(db).steps
                   if s.payable_number == "PO-7001" and s.action == ACT_PAY)
        assert pay.needs_approval is True, "EUR 9,012.85 > the USD 2,500 limit"

    def test_po7002_is_still_checked_against_real_cash_once_inv0042_lands(self, db):
        """Its dependency is met, and the floor still refuses it."""
        self._seed()
        # INV-0041 lands, and PO-7001 has already gone out
        with session_scope() as s:
            post(s, "test:inv41", [("cash:USD", 1_200_000, 0, "USD"),
                                   ("receivable_clearing", 0, 1_200_000, "USD")])
            post(s, "test:fx", [("fx_clearing", 1_031_500, 0, "USD"),
                                ("cash:USD", 0, 1_031_500, "USD"),
                                ("cash:EUR", 901_285, 0, "EUR"),
                                ("fx_clearing", 0, 901_285, "EUR")])
            post(s, "test:pay7001", [("payable:PO-7001", 901_285, 0, "EUR"),
                                     ("cash:EUR", 0, 901_285, "EUR")])
            s.query(InvoicePayable).filter_by(number="PO-7001").one().status = \
                PayStatus.PAID
            r = s.query(InvoiceReceivable).filter_by(number="INV-0042").one()
            r.amount_paid_minor = r.amount_minor
            r.status = RecStatus.PAID
        step = next(s for s in current_plan(db).steps
                    if s.payable_number == "PO-7002")
        assert step.action == ACT_DEFER, "the dependency is met; cash still is not"
        assert step.blocker_codes[0] in ("reserve_floor", "waits_for_inflow")


# ------------------------------------------------------- the floor, on cash
class TestTheFloorBindsOnRealCash:
    def test_a_payment_just_inside_the_floor_goes_through(self, db):
        # cash USD 5,440, payment USD 440 -> leaves EXACTLY the USD 5,000 floor
        _cash("USD", 544_000)
        _payable("PO-Y", "USD", 44_000)
        step = next(s for s in current_plan(db).steps
                    if s.payable_number == "PO-Y")
        assert step.action == ACT_PAY
        assert available_cash_minor(cash_minor=544_000 - 44_000,
                                    floor_minor=FLOOR) == 0

    def test_one_unit_more_and_the_floor_refuses_it(self, db):
        # one minor unit past the floor, and it is refused
        _cash("USD", 544_000)
        _payable("PO-Y", "USD", 44_001)
        step = next(s for s in current_plan(db).steps
                    if s.payable_number == "PO-Y")
        assert step.action == ACT_DEFER
        assert "reserve_floor" in step.blocker_codes

    def test_the_second_payment_sees_the_first_one_already_spent(self, db):
        """The floor is checked against the RUNNING position, so two payments
        that each fit do not both go out when together they breach it."""
        _cash("USD", 1_000_000)                 # 10,000, floor 5,000
        _payable("PO-BIG", "USD", 300_000, criticality=5)
        _payable("PO-SMALL", "USD", 300_000, criticality=2)
        steps = {s.payable_number: s for s in current_plan(db).steps}
        assert steps["PO-BIG"].action == ACT_PAY     # 10,000 - 3,000 = 7,000
        assert steps["PO-SMALL"].action == ACT_DEFER  # 7,000 - 3,000 = 4,000 < 5,000
        assert steps["PO-SMALL"].blocker_codes[0] in ("reserve_floor",
                                                      "waits_for_inflow")

    def test_the_tile_and_the_plan_agree_when_something_is_affordable(self, db):
        """No contradiction in either direction: if the tile says the position is
        positive, the plan may pay; if the plan pays, the tile is not negative."""
        _cash("USD", 1_000_000)
        _payable("PO-SMALL", "USD", 300_000)
        money = build_context([])["money"]
        step = next(s for s in current_plan(db).steps
                    if s.payable_number == "PO-SMALL")
        assert money["free"]["USD"] >= 0
        assert step.action == ACT_PAY

    def test_the_tile_and_the_plan_agree_when_nothing_is_affordable(self, db):
        _cash("USD", 100_000)
        _payable("PO-SMALL", "USD", 300_000)
        money = build_context([])["money"]
        step = next(s for s in current_plan(db).steps
                    if s.payable_number == "PO-SMALL")
        assert money["free"]["USD"] < 0
        assert step.action == ACT_DEFER


# ------------------------------------------------------------- the page says it
class TestThePageShowsIt:
    def test_the_forecast_is_labelled_as_not_spendable(self, db):
        _cash("USD", 900_000)
        _receivable("INV-BIG", "USD", 1_200_000)
        html = render_app(build_context([]), tab="home")
        assert "Forecast:" in html
        assert "not landed" in html or "Not spendable" in html

    def test_a_deferred_payment_shows_its_waiting_reason_on_every_tab(self, db):
        _cash("USD", 600_000)
        _payable("PO-X", "USD", 300_000)
        _receivable("INV-BIG", "USD", 1_200_000)
        ctx = build_context([])
        for tab in ("home", "money-out", "ok"):
            html = render_app(ctx, tab=tab)
            assert "waits for INV-BIG to land" in html, tab


# ------------------------------------------- a same-currency bill is not an FX
class TestASameCurrencyBillIsNotAConversion:
    """A USD bill paid out of a USD budget needs no conversion.

    The first version of this modelled one anyway, which inflated the
    requirement by the FX drift headroom (a USD 1,800 bill reported as needing
    USD 1,809.00) and stated the same fact twice, once with each number.
    """

    def test_a_usd_bill_is_never_given_an_fx_step(self, db):
        _payable("PO-X", "USD", 180_000)
        plan = current_plan(db)
        assert not [s for s in plan.steps
                    if s.action == ACT_FX and s.payable_number == "PO-X"]

    def test_it_reports_ONE_reason_with_the_real_amount(self, db):
        _payable("PO-X", "USD", 180_000)
        _receivable("INV-BIG", "USD", 1_200_000)
        step = next(s for s in current_plan(db).steps
                    if s.payable_number == "PO-X")
        assert step.action == ACT_DEFER
        assert len(step.blockers) == 1, f"duplicated: {step.blockers}"
        assert "USD 1,800.00" in step.blockers[0]
        # NOT the drift-inflated number the conversion path produced
        assert "1,809" not in step.blockers[0]

    def test_a_foreign_bill_DOES_get_its_conversion_checked(self, db):
        """The guard must not switch the FX path off for the case that needs it."""
        _payable("PO-Y", "EUR", 900_000)
        plan = current_plan(db)
        step = next(s for s in plan.steps if s.payable_number == "PO-Y")
        assert step.action == ACT_DEFER
        # costed in the FUNDING currency, which is USD
        assert "USD" in step.blockers[-1]
