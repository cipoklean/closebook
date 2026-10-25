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
from app.domain.planner import (  # noqa: E402
    ACT_CHASE, ACT_DEFER, ACT_FX, ACT_PAY, build_plan,
)
from app.domain.policy import available_cash_minor, load_budget  # noqa: E402
from app.domain.treasury import cash_position, current_plan, payables_from_db  # noqa: E402
from app.models import (  # noqa: E402
    Customer, InvoicePayable, InvoiceReceivable, PayStatus, PayoutMethod,
    RecStatus, Supplier,
)
from app.ui import render_app  # noqa: E402


def assert_held(step) -> None:
    """A payment that is NOT being sent.

    Its action is PAY when a person can approve it (over the auto-pay limit) and
    DEFER when nothing can be done yet. In both cases it carries the blocker that
    says why it is waiting, and being held is what these tests are about -- the
    action word only says whether a person is the missing thing.
    """
    assert step.action in (ACT_DEFER, ACT_PAY), step.action
    assert step.blockers or step.needs_approval or step.approval_reason, \
        f"{step.action} step is not held and has no reason"

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
        assert_held(step), "it must NOT be paid on expected money"
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
        assert_held(step)
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
        assert_held(step)
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
        assert_held(steps["PO-7001"])
        assert_held(steps["PO-7002"])
        assert any("waits for INV-0041 to land" in b
                   for b in steps["PO-7001"].blockers)
        # PO-7002 needs USD and the only open invoice behind it is GBP, so it is
        # NOT told to wait for a GBP payment to arrive — that was the wrong
        # currency for the bill and pointed at the wrong customer. Its
        # dependency is still named, because THAT is what structurally blocks it.
        assert any("INV-0042" in b for b in steps["PO-7002"].blockers)
        assert not any("waits for INV-0042 to land" in b
                       for b in steps["PO-7002"].blockers), \
            "a GBP invoice does not unblock a USD payment"

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
        assert_held(step), "the dependency is met; cash still is not"
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
        assert_held(step)
        assert "reserve_floor" in step.blocker_codes

    def test_the_second_payment_sees_the_first_one_already_spent(self, db):
        """The floor is checked against the RUNNING position, so two payments
        that each fit do not both go out when together they breach it."""
        _cash("USD", 1_000_000)                 # 10,000, floor 5,000
        _payable("PO-BIG", "USD", 300_000, criticality=5)
        _payable("PO-SMALL", "USD", 300_000, criticality=2)
        steps = {s.payable_number: s for s in current_plan(db).steps}
        assert steps["PO-BIG"].action == ACT_PAY     # 10,000 - 3,000 = 7,000
        assert_held(steps["PO-SMALL"])  # 7,000 - 3,000 = 4,000 < 5,000
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
        assert_held(step)


# ------------------------------------------------------------- the page says it
class TestThePageShowsIt:
    def test_the_forecast_is_labelled_as_not_spendable(self, db):
        _cash("USD", 900_000)
        _receivable("INV-BIG", "USD", 1_200_000)
        html = render_app(build_context([]), tab="home")
        assert "Forecast:" in html
        assert "not landed" in html or "Not spendable" in html

    def test_the_waiting_reason_is_stated_once_and_linked_elsewhere(self, db):
        """The reason lives on Home, ONCE, and every other mention links to it.

        It used to print in full on every tab, so a reader met the same
        paragraph on Home, again in the plan row and again on Needs your OK.
        Stating it once and linking is what makes the link worth following --
        and a link must point at an anchor that is really on the page, which is
        why Home is the one that keeps the text.
        """
        _cash("USD", 600_000)
        _payable("PO-X", "USD", 300_000)
        _receivable("INV-BIG", "USD", 1_200_000)
        ctx = build_context([])
        for tab, expect_text in (("home", True), ("money-out", True), ("ok", False)):
            html = render_app(ctx, tab=tab)
            if expect_text:
                # RUN 5: the card now carries its blocker. A held payment whose
                # row says only "Held by policy" gave an operator nothing to act
                # on, so money-out states the sentence and still links to Home.
                assert "waits for INV-BIG to land" in html, tab
                assert "held-PO-X" in html, f"{tab} must link to Home"
            else:
                assert "waits for INV-BIG to land" not in html, tab
                assert "held-PO-X" in html, f"{tab} must link to it"

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
        assert_held(step)
        assert len(step.blockers) == 1, f"duplicated: {step.blockers}"
        assert "USD 1,800.00" in step.blockers[0]
        # NOT the drift-inflated number the conversion path produced
        assert "1,809" not in step.blockers[0]

    def test_a_foreign_bill_DOES_get_its_conversion_checked(self, db):
        """The guard must not switch the FX path off for the case that needs it."""
        _payable("PO-Y", "EUR", 900_000)
        plan = current_plan(db)
        step = next(s for s in plan.steps if s.payable_number == "PO-Y")
        assert_held(step)
        # costed in the FUNDING currency, which is USD
        assert "USD" in step.blockers[-1]


class TestAnUnfundableBillIsNotAShortfallInItsOwnCurrency:
    """A EUR bill we hold no EUR for and have no conversion for must NOT be
    reported as "Short by EUR 9,012.85".

    That number was invented: nobody was going to spend EUR out of a GBP/USD
    account, and it made a page with USD 15,250 in it read as overdrawn.
    """

    def test_a_foreign_bill_is_costed_in_its_FUNDING_currency(self, db):
        """A EUR bill paid with USD is a USD obligation.

        It must never be reported as "Short by EUR" — nobody was going to spend
        EUR out of a USD account — and the number has to be the PLAN's, not one
        the tile invented, so both read the same computation.
        """
        _cash("USD", 1_500_000)
        _payable("PO-EUR", "EUR", 900_000)          # a currency we do not hold
        ctx = build_context([])
        m = ctx["money"]
        assert "EUR" not in m["shortfall"], m["shortfall"]
        assert "EUR" not in m["promised"], m["promised"]
        line = next(l for l in m["lines"] if l["payable_number"] == "PO-EUR")
        assert line["currency"] == "USD", "costed in the currency that leaves"
        assert line["amount_minor"] > 900_000, "the conversion includes headroom"

        # and it is the SAME number the planner put on the step
        from app.domain.treasury import current_plan, funding_for
        plan = current_plan(db)
        step = next(s for s in plan.steps if s.payable_number == "PO-EUR")
        assert step.funding_currency == line["currency"]
        assert step.funding_amount_minor == line["amount_minor"]

    def test_a_FUNDED_foreign_bill_IS_costed_in_its_funding_currency(self, db):
        """The guard must not switch off the case the funding lookup exists for."""
        _cash("USD", 1_500_000)
        _payable("PO-EUR", "EUR", 90_000)
        m = build_context([])["money"]
        assert m["promised"].get("USD"), "costed in the currency that buys it"
        assert "EUR" not in m["promised"]

    def test_the_page_never_reports_a_shortfall_in_a_currency_nobody_spends(self, db):
        _cash("USD", 1_500_000)
        _payable("PO-EUR", "EUR", 900_000)
        html = render_app(build_context([]), tab="home")
        assert "Short by EUR" not in html

    def test_the_tile_and_the_planner_agree_on_the_cost(self, db):
        """The whole point: one costing, called by both."""
        from app.domain.treasury import current_plan
        _cash("USD", 1_500_000)
        _payable("PO-EUR", "EUR", 900_000)
        m = build_context([])["money"]
        program = {s.payable_number: s for s in current_plan(db).steps}
        for line in m["lines"]:
            st = program.get(line["payable_number"])
            if st is None or not st.funding_currency:
                continue
            assert (line["currency"], line["amount_minor"]) == \
                   (st.funding_currency, st.funding_amount_minor), line


class TestSuspenseCashCannotFundAPayment:
    """Money received but not attributed is in the bank and is NOT spendable.

    A payment that lands below the auto band sits in `unapplied_cash` until a
    human decides. Leaving it in the spendable total meant the planner could pay
    a supplier with money that may have to be refunded tomorrow — and the refund
    then comes out of cash that is already gone.
    """

    def test_a_proposal_parks_the_cash_and_the_planner_cannot_spend_it(self, db):
        from app.domain import intake as I
        from app.domain.treasury import cash_position, current_plan
        from app.domain.policy import load_budget

        _receivable("INV-1002", "GBP", 800_000)
        _payable("PO-BIG", "GBP", 480_000)
        # A part payment that reads at 0.70: below the band, so it is PROPOSED.
        r = I.ingest_deposit(db, {"id": "s1", "currency": "GBP", "amount": 4800,
                                  "payer_name": "Harrow and Company",
                                  "reference": "chq 1OO2 part", "status": "SETTLED"})
        assert r.disposition == "PROPOSED"

        pos = cash_position(db, load_budget())
        assert pos.balances.get("GBP") == 480_000, "the cash IS there"
        assert pos.suspense.get("GBP") == 480_000, "and it is held, not spendable"

        step = next(s for s in current_plan(db).steps
                    if s.payable_number == "PO-BIG")
        assert_held(step), \
            "a supplier must not be paid with money nobody has attributed yet"
        assert step.blocker_codes[0] in ("insufficient_balance", "reserve_floor",
                                         "waits_for_inflow")

    def test_the_tile_excludes_it_and_still_shows_it(self, db):
        from app.domain import intake as I
        _cash("USD", 1_000_000)
        _receivable("INV-1002", "USD", 800_000)
        I.ingest_deposit(db, {"id": "s2", "currency": "USD", "amount": 4000,
                              "payer_name": "Nobody", "reference": "junk ref",
                              "status": "SETTLED"})
        m = build_context([])["money"]
        assert m["suspense"].get("USD") == 400_000
        # The deposit ADDS to cash: 10,000 seeded + 4,000 received = 14,000.
        # Then suspense comes off, then the floor: 14,000 - 4,000 - 5,000 = 5,000.
        # Without the suspense term this would read 9,000 — 4,000 of spendable
        # money that belongs to nobody yet.
        from app.domain.policy import available_cash_minor
        assert m["free"].get("USD") == 500_000
        assert m["free"]["USD"] == available_cash_minor(
            cash_minor=1_400_000, suspense_minor=400_000, floor_minor=500_000)
        assert m["free"]["USD"] != available_cash_minor(
            cash_minor=1_400_000, floor_minor=500_000)
        html = render_app(build_context([]), tab="home")
        assert "unmatched" in html, "the money is still visible, just not spendable"

    def test_an_approved_proposal_stops_being_suspense(self, db):
        """Once a human approves it the money is attributed, so it is spendable."""
        from app.domain import intake as I
        from app.domain.ledger import approve_inflow
        from app.domain.treasury import cash_position
        from app.domain.policy import load_budget
        from app.models import Inflow
        from app.db import session_scope

        _receivable("INV-1002", "GBP", 800_000)
        I.ingest_deposit(db, {"id": "s3", "currency": "GBP", "amount": 4800,
                              "payer_name": "Harrow and Company",
                              "reference": "chq 1OO2 part", "status": "SETTLED"})
        with session_scope() as s:
            approve_inflow(s, s.query(Inflow).one(), approved_by="test")
        pos = cash_position(db, load_budget())
        assert pos.suspense.get("GBP", 0) == 0
        assert pos.balances.get("GBP") == 480_000


def test_suspense_is_counted_once_in_the_plan_not_twice(db):
    """SUSPENSE COMES OFF `working`, SO THE RULE MUST NOT TAKE IT OFF AGAIN.

    `working` is the planner's per-currency capacity and it already excludes
    unattributed cash. Subtracting it a second time inside the available-cash
    rule counted the same money twice — a USD 7,000 deposit that changed nothing
    about what was spendable flipped PO-2001 from PAY to DEFER, which is the same
    "two answers to one question" defect this file exists to prevent.

    The property that makes it checkable: a deposit that lands ENTIRELY in
    suspense must not change the plan at all. It is invisible to spending.
    """
    from app.domain.treasury import cash_position, current_plan, payables_from_db
    from app.domain.policy import load_budget
    from app.domain.planner import build_plan, CashPosition

    _cash("USD", 1_200_000)
    _payable("PO-A", "USD", 180_000)
    payables = payables_from_db(db.session())
    budget = load_budget()
    rates = cash_position(db, budget).rates
    floor = int(budget.reserve_floor_usd * 100)

    def _plan(balances, suspense):
        return build_plan(payables,
                          CashPosition(balances=balances, suspense=suspense,
                                       floor_minor=floor, rates=rates),
                          budget)

    before = _plan({"USD": 1_200_000}, {})
    after = _plan({"USD": 1_900_000}, {"USD": 700_000})
    assert [s.action for s in before.steps] == [s.action for s in after.steps], \
        "money we cannot attribute must not change what we do"
    assert before.steps[0].action == ACT_PAY
    assert after.steps[0].action == ACT_PAY

    # and it is genuinely not spendable: with the same cash 100% unattributed,
    # the bill cannot be paid at all
    tight = _plan({"USD": 700_000}, {"USD": 700_000})
    assert_held(tight.steps[0])
    # the account "holds" 7,000 but none of it is attributable, so the honest
    # reading is that it holds nothing we may spend
    assert "insufficient_balance" in tight.steps[0].blocker_codes


def test_a_chase_names_the_invoices_own_currency(db):
    """A GBP gap must not be reported as USD.

    `chase_shortfalls` returned bare amounts and the planner labelled every one
    "USD", so an INV-1002 gap of GBP 3,200 printed as "USD 3,200.00" — the wrong
    unit on the number the operator takes to the customer. The currency now
    travels with the amount.
    """
    from app.domain.treasury import chase_shortfalls, current_plan
    _receivable("INV-1002", "GBP", 800_000, paid=480_000)
    assert chase_shortfalls(db.session())["INV-1002"] == (320_000, "GBP")

    step = next(s for s in current_plan(db).steps if s.action == ACT_CHASE)
    assert step.currency == "GBP"
    assert "GBP 3,200.00" in step.reason
    assert "USD" not in step.reason
