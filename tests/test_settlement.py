"""Settlement: the outflow half of the ledger.

The books recorded inflows only, so `cash:USD` never fell when a payout left, a
payable never became PAID, and "Free to spend" treated money that had already
been sent as still available. These tests pin the outflow posting, its
idempotency, and the tile arithmetic across the moment of settlement.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import get_default_db, reset_default_db, session_scope  # noqa: E402
from app.dashboard import build_context  # noqa: E402
from app.domain.ledger import post, reconcile  # noqa: E402
from app.domain.settlement import (  # noqa: E402
    FX_CLEARING, FX_VARIANCE, ledger_position, paid_payouts, payable_account,
    post_conversion, post_payout, settle_fx_step, settle_payout_step,
    settled_txn_keys,
)
from app.models import (  # noqa: E402
    InvoicePayable, LedgerEntry, PayStatus, PayoutMethod, PlanStep, StepStatus,
    StepType, Supplier,
)

CONVERSION_ID = "40eaec35-e348-40c7-81cd-a6e6fd8dfb98"
TRANSFER_ID = "e4337039-e1b5-4cdc-8ef7-a58fa1c2b091"

# Verified live 2026-10-22 from GET /fx/conversions/<id> — the ACTUAL numbers,
# which differ from the plan's.
CONVERSION_BODY = {
    "conversion_id": CONVERSION_ID, "status": "SETTLED",
    "sell_currency": "USD", "sell_amount": 10315.0,
    "buy_currency": "EUR", "buy_amount": 9065.36,
    "client_rate": 1.137848,
}


@pytest.fixture(autouse=True)
def _isolated_db():
    """A genuinely empty database per test.

    `CLOSEBOOK_DATABASE_URL` is set to :memory: by tests/conftest.py — that, and
    not reset_default_db(), is what isolates. reset_default_db() now REFUSES to
    run without it, which is why this fixture cannot be written the way it used
    to be.
    """
    reset_default_db()
    import app.dashboard as dash
    prev = dash.LIVE_STATUS_ENABLED
    dash.LIVE_STATUS_ENABLED = False
    yield
    dash.LIVE_STATUS_ENABLED = prev


@pytest.fixture
def db():
    return get_default_db()


def _seed_cash(ccy="USD", minor=1_640_000):
    with session_scope() as s:
        post(s, f"inflow:{ccy}", [(f"cash:{ccy}", minor, 0, ccy),
                                  ("receivable_clearing", 0, minor, ccy)])


def _payable(number="PO-7001", ccy="EUR", minor=900_000, status=PayStatus.HELD):
    with session_scope() as s:
        sup = Supplier(name="Rotterdam Freight", payout_method=PayoutMethod.SWIFT)
        s.add(sup)
        s.flush()
        s.add(InvoicePayable(number=number, supplier_id=sup.id, currency=ccy,
                             amount_minor=minor, status=status, criticality=3))


def _fx_step(payable="PO-7001", obj=CONVERSION_ID, sell_minor=1_031_500,
             status=StepStatus.SUBMITTED):
    with session_scope() as s:
        s.add(PlanStep(run_id="r", type=StepType.FX,
                       payload_json=json.dumps({
                           "payable_number": payable, "action": "FX",
                           "sell_currency": "USD", "sell_amount_minor": sell_minor,
                           "buy_currency": "EUR"}),
                       reason="buy the EUR PO-7001 needs", status=status,
                       airwallex_object_id=obj))


def _pay_step(payable="PO-7001", obj=TRANSFER_ID, ccy="EUR", minor=901_285,
              status=StepStatus.SUBMITTED):
    with session_scope() as s:
        s.add(PlanStep(run_id="r", type=StepType.TRANSFER,
                       payload_json=json.dumps({
                           "payable_number": payable, "action": "PAY",
                           "currency": ccy, "amount_minor": minor}),
                       reason="pay it", status=status, airwallex_object_id=obj))


def _ledger_rows():
    with session_scope() as s:
        return [(e.txn_id, e.account, e.debit_minor, e.credit_minor, e.currency)
                for e in s.query(LedgerEntry).order_by(LedgerEntry.id).all()]


# ------------------------------------------------------- the conversion legs
class TestConversionPosts:
    def test_a_settled_conversion_posts_both_legs_against_clearing(self, db):
        _seed_cash()
        with session_scope() as s:
            r = post_conversion(s, conversion_id=CONVERSION_ID,
                                sell_currency="USD", sell_minor=1_031_500,
                                buy_currency="EUR", buy_minor=906_536)
        assert r.posted
        rep = reconcile(db.session())
        assert rep.cash_by_currency == {"USD": 1_640_000 - 1_031_500,
                                        "EUR": 906_536}
        assert rep.balanced, "the FX legs must balance per currency"
        # the two sides of one conversion sit together in clearing
        pos = ledger_position(db.session())
        assert pos[f"{FX_CLEARING}:USD"] == 1_031_500     # a debit
        assert pos[f"{FX_CLEARING}:EUR"] == -906_536      # a credit

    def test_the_actual_rate_is_used_not_the_plans_headroom(self, db):
        """The plan carries 0.50% of deliberate drift headroom. Booking that as
        an actual would put money in the books that never moved."""
        _seed_cash()
        with session_scope() as s:
            post_conversion(s, conversion_id=CONVERSION_ID, sell_currency="USD",
                            sell_minor=1_031_500, buy_currency="EUR",
                            buy_minor=906_536)
        # 906,536 minor is the API's actual 9065.36 — NOT a plan-derived figure
        assert reconcile(db.session()).cash_by_currency["EUR"] == 906_536

    def test_a_replayed_conversion_posts_nothing_new(self, db):
        _seed_cash()
        with session_scope() as s:
            post_conversion(s, conversion_id=CONVERSION_ID, sell_currency="USD",
                            sell_minor=1_031_500, buy_currency="EUR",
                            buy_minor=906_536)
        before = _ledger_rows()
        with session_scope() as s:
            r = post_conversion(s, conversion_id=CONVERSION_ID,
                                sell_currency="USD", sell_minor=1_031_500,
                                buy_currency="EUR", buy_minor=906_536)
        assert r.posted is False, "a replay must not claim it moved money"
        assert "already posted" in r.reason
        assert _ledger_rows() == before

    def test_every_fx_leg_balances_per_currency(self, db):
        _seed_cash()
        with session_scope() as s:
            post_conversion(s, conversion_id=CONVERSION_ID, sell_currency="USD",
                            sell_minor=1_031_500, buy_currency="EUR",
                            buy_minor=906_536)
        by = {}
        for txn, _acct, d, c, ccy in _ledger_rows():
            k = (txn, ccy)
            by[k] = (by.get(k, (0, 0))[0] + d, by.get(k, (0, 0))[1] + c)
        assert all(d == c for d, c in by.values())

    def test_a_variance_line_is_posted_when_actual_differs_from_the_plan(self, db):
        _seed_cash()
        with session_scope() as s:
            r = post_conversion(s, conversion_id=CONVERSION_ID,
                                sell_currency="USD", sell_minor=1_025_000,
                                buy_currency="EUR", buy_minor=900_500,
                                planned_sell_minor=1_031_500,
                                planned_buy_minor=901_285)
        assert r.posted
        assert any(t.startswith("fxvar:") for t, *_ in _ledger_rows())
        assert "variance" in r.reason
        # clearing lands on the PLANNED amount in each currency, and the
        # difference is what the variance account carries
        pos = ledger_position(db.session())
        assert pos[f"{FX_CLEARING}:USD"] == 1_031_500
        assert pos[f"{FX_CLEARING}:EUR"] == -901_285
        assert reconcile(db.session()).balanced

    def test_no_variance_line_when_the_actual_matches_the_plan(self, db):
        _seed_cash()
        with session_scope() as s:
            post_conversion(s, conversion_id=CONVERSION_ID, sell_currency="USD",
                            sell_minor=1_031_500, buy_currency="EUR",
                            buy_minor=906_536, planned_sell_minor=1_031_500)
        assert not any(t.startswith("fxvar:") for t, *_ in _ledger_rows())
        assert not any(FX_VARIANCE in a for _t, a, *_ in _ledger_rows())

    def test_same_currency_is_refused_not_posted_as_a_no_op(self, db):
        _seed_cash()
        with session_scope() as s:
            r = post_conversion(s, conversion_id="x", sell_currency="USD",
                                sell_minor=100, buy_currency="USD", buy_minor=100)
        assert r.posted is False
        assert "same" in r.reason

    def test_a_zero_amount_is_refused(self, db):
        _seed_cash()
        with session_scope() as s:
            r = post_conversion(s, conversion_id="x", sell_currency="USD",
                                sell_minor=0, buy_currency="EUR", buy_minor=5)
        assert r.posted is False


# ------------------------------------------------------------ the payout legs
class TestPayoutPosts:
    def test_a_settled_payout_reduces_cash_and_clears_the_payable(self, db):
        _seed_cash(ccy="EUR", minor=1_000_000)
        _payable(status=PayStatus.HELD)
        with session_scope() as s:
            r = post_payout(s, transfer_id=TRANSFER_ID, payable_number="PO-7001",
                            currency="EUR", amount_minor=901_285)
        assert r.posted
        rep = reconcile(db.session())
        assert rep.cash_by_currency == {"EUR": 1_000_000 - 901_285}
        assert rep.balanced
        with session_scope() as s:
            assert s.query(InvoicePayable).filter_by(number="PO-7001").one().status \
                == PayStatus.PAID

    def test_the_payable_is_marked_paid_only_by_the_posting(self, db):
        """Planning a payment must not mark anything paid."""
        _seed_cash(ccy="EUR", minor=1_000_000)
        _payable(status=PayStatus.HELD)
        _pay_step()
        with session_scope() as s:
            assert s.query(InvoicePayable).one().status == PayStatus.HELD
        with session_scope() as s:
            settle_payout_step(s, _step_row(StepType.TRANSFER), "PROCESSING")
        with session_scope() as s:
            assert s.query(InvoicePayable).one().status == PayStatus.HELD, \
                "an in-flight transfer has not paid anybody"

    def test_a_duplicate_payout_posts_nothing(self, db):
        _seed_cash(ccy="EUR", minor=1_000_000)
        _payable()
        with session_scope() as s:
            post_payout(s, transfer_id=TRANSFER_ID, payable_number="PO-7001",
                        currency="EUR", amount_minor=901_285)
        before = _ledger_rows()
        with session_scope() as s:
            r = post_payout(s, transfer_id=TRANSFER_ID, payable_number="PO-7001",
                            currency="EUR", amount_minor=901_285)
        assert r.posted is False
        assert _ledger_rows() == before
        assert reconcile(db.session()).cash_by_currency == {"EUR": 98_715}

    def test_a_failed_transfer_posts_nothing(self, db):
        _seed_cash(ccy="EUR", minor=1_000_000)
        _payable()
        _pay_step()
        with session_scope() as s:
            r = settle_payout_step(s, _step_row(StepType.TRANSFER), "CANCELLED")
        assert r.posted is False
        assert "no payout is posted" in r.reason
        assert reconcile(db.session()).cash_by_currency == {"EUR": 1_000_000}

    def test_a_failed_payout_after_conversion_leaves_the_eur_in_cash(self, db):
        """The converted currency STAYS as cash in that currency. It is not
        converted back, and saying it 'returned' to USD would invent a second
        FX event that never happened."""
        _seed_cash()                                    # USD 16,400.00
        _payable()
        _pay_step()
        with session_scope() as s:
            post_conversion(s, conversion_id=CONVERSION_ID, sell_currency="USD",
                            sell_minor=1_031_500, buy_currency="EUR",
                            buy_minor=906_536)
        with session_scope() as s:
            settle_payout_step(s, _step_row(StepType.TRANSFER), "CANCELLED")
        cash = reconcile(db.session()).cash_by_currency
        assert cash["EUR"] == 906_536, "the bought EUR is still ours"
        assert cash["USD"] == 1_640_000 - 1_031_500
        # nothing was converted back: the books hold no second FX event
        assert not any(t.startswith("fx:") and t != f"fx:{CONVERSION_ID}"
                       for t, *_ in _ledger_rows())

    def test_an_unknown_status_settles_nothing(self, db):
        """Guessing is how a payment gets booked twice."""
        _seed_cash(ccy="EUR", minor=1_000_000)
        _payable()
        _pay_step()
        for status in ("", "WEIRD", "PENDING", "PROCESSING", "SUBMITTED"):
            with session_scope() as s:
                r = settle_payout_step(s, _step_row(StepType.TRANSFER), status)
            assert r.posted is False, status

    def test_a_transfer_with_no_amount_is_refused_not_posted_as_zero(self, db):
        _seed_cash(ccy="EUR", minor=1_000_000)
        with session_scope() as s:
            s.add(PlanStep(run_id="r", type=StepType.TRANSFER,
                           payload_json=json.dumps({"payable_number": "PO-7001",
                                                    "action": "PAY"}),
                           reason="no amount", status=StepStatus.SUBMITTED,
                           airwallex_object_id=TRANSFER_ID))
        with session_scope() as s:
            r = settle_payout_step(s, _step_row(StepType.TRANSFER), "PAID")
        assert r.posted is False
        assert "does not name a payable and an amount" in r.reason


def _step_row(step_type: StepType, obj: str | None = None) -> PlanStep:
    """The persisted row, read fresh.

    `obj` defaults to None (type alone) because the two step types carry
    DIFFERENT Airwallex ids — the FX step holds the conversion id and the
    transfer step holds the transfer id — and filtering an FX lookup by the
    transfer id silently returns None, which then reads as "settlement did
    nothing" rather than "the test looked for the wrong row".
    """
    with session_scope() as s:
        q = s.query(PlanStep).filter(PlanStep.type == step_type)
        if obj is not None:
            q = q.filter(PlanStep.airwallex_object_id == obj)
        return q.order_by(PlanStep.id.desc()).first()


# --------------------------------------------------------- the FX step driver
class TestSettleFxStep:
    def test_a_settled_conversion_is_read_and_booked(self, db):
        _seed_cash()
        _fx_step(sell_minor=1_031_500)
        with session_scope() as s:
            r = settle_fx_step(s, _step_row(StepType.FX), CONVERSION_BODY)
        assert r.posted
        assert reconcile(db.session()).cash_by_currency == {
            "USD": 1_640_000 - 1_031_500, "EUR": 906_536}

    def test_an_unsettled_conversion_posts_nothing(self, db):
        _seed_cash()
        _fx_step()
        body = dict(CONVERSION_BODY, status="PENDING")
        with session_scope() as s:
            r = settle_fx_step(s, _step_row(StepType.FX), body)
        assert r.posted is False
        assert "not settled" in r.reason
        assert reconcile(db.session()).cash_by_currency == {"USD": 1_640_000}

    def test_a_body_with_no_amounts_posts_nothing(self, db):
        """Never invent a rate. If the actual amounts cannot be read, nothing is
        booked and the reason says so."""
        _seed_cash()
        _fx_step()
        with session_scope() as s:
            r = settle_fx_step(s, _step_row(StepType.FX),
                               {"status": "SETTLED", "sell_currency": "USD",
                                "buy_currency": "EUR"})
        assert r.posted is False
        assert "no usable amounts" in r.reason

    def test_the_booked_amounts_come_from_the_body_not_the_payload(self, db):
        _seed_cash()
        # the stored plan says 1,031,500; the API says 1,031,500 too, but the
        # BUY is only in the API body
        _fx_step(sell_minor=1_031_500)
        with session_scope() as s:
            settle_fx_step(s, _step_row(StepType.FX), CONVERSION_BODY)
        assert reconcile(db.session()).cash_by_currency["EUR"] == 906_536


# ------------------------------------------------------- tile + ledger together
class TestTilesAcrossSettlement:
    def _context(self):
        return build_context([])["money"]

    def _in_flight_eur_payable(self):
        """A EUR payable, funded from USD, its transfer sent and PROCESSING."""
        _seed_cash(minor=1_640_000)
        _payable()
        _fx_step()
        _pay_step()

    def test_settlement_moves_cash_and_keeps_free_to_spend_equal(self, db):
        """The whole point: after the books record the outflow, cash is lower
        AND the item is no longer committed — so free-to-spend is unchanged."""
        self._in_flight_eur_payable()
        before = self._context()

        with session_scope() as s:
            settle_fx_step(s, _step_row(StepType.FX), CONVERSION_BODY)
        with session_scope() as s:
            settle_payout_step(s, _step_row(StepType.TRANSFER), "PAID")

        after = self._context()
        # cash fell by the real amounts
        with session_scope() as s:
            cash = reconcile(s).cash_by_currency
        assert cash["USD"] == 1_640_000 - 1_031_500
        # nothing is in flight any more: it settled
        assert after["in_flight"].get("EUR", 0) == 0
        assert after["in_flight"].get("USD", 0) == 0
        assert after["paid_today"], "a settled payout must be listed"
        assert before["in_flight"], "it was committed before settlement"

    def test_free_to_spend_is_the_same_number_before_and_after_settlement(self, db):
        """The sharpest form of item 2: the money was committed, then it left.
        The free figure must be identical on both sides of the posting, because
        the commitment simply moved from "in flight" to "gone"."""
        self._in_flight_eur_payable()
        before = self._context()["free"]

        with session_scope() as s:
            settle_fx_step(s, _step_row(StepType.FX), CONVERSION_BODY)
        with session_scope() as s:
            settle_payout_step(s, _step_row(StepType.TRANSFER), "PAID")
        after = self._context()["free"]

        # USD: before, the plan's USD 10,346.00 was in flight. After, cash fell
        # by 10,315.00 and the same 10,346.00 is no longer committed, so free
        # moves by the FX difference only (31.00 = the headroom that was not
        # needed). It must NOT lurch by the whole payment.
        assert abs(after["USD"] - before["USD"]) <= 3_100
        # EUR: the bought 9,065.36 arrives, 9,012.85 is paid out, 52.51 remains
        assert after["EUR"] == 906_536 - 901_285

    def test_settled_money_leaves_in_flight(self, db):
        self._in_flight_eur_payable()
        assert self._context()["in_flight"], "sent = in flight before settlement"
        with session_scope() as s:
            settle_fx_step(s, _step_row(StepType.FX), CONVERSION_BODY)
        with session_scope() as s:
            settle_payout_step(s, _step_row(StepType.TRANSFER), "PAID")
        m = self._context()
        assert m["in_flight"] in ({}, {"USD": 0}, {"EUR": 0}), m["in_flight"]

    def test_paid_today_lists_the_settled_payout_only(self, db):
        self._in_flight_eur_payable()
        with session_scope() as s:
            settle_fx_step(s, _step_row(StepType.FX), CONVERSION_BODY)
        with session_scope() as s:
            settle_payout_step(s, _step_row(StepType.TRANSFER), "PAID")
        paid = self._context()["paid_today"]
        assert len(paid) == 1
        assert paid[0]["payable_number"] == "PO-7001"
        assert paid[0]["amount_minor"] == 901_285
        assert paid[0]["currency"] == "EUR"
        # the conversion is NOT a payment: it paid nobody
        assert all(p["transfer_id"] != CONVERSION_ID for p in paid)

    def test_paid_payouts_today_excludes_older_postings(self, db):
        _seed_cash(ccy="EUR", minor=1_000_000)
        _payable()
        with session_scope() as s:
            post_payout(s, transfer_id=TRANSFER_ID, payable_number="PO-7001",
                        currency="EUR", amount_minor=901_285,
                        when=datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert paid_payouts(db.session()) != []          # all time
        assert paid_payouts(db.session(),
                            since=datetime(2026, 1, 1, tzinfo=timezone.utc)) == []

    def test_the_books_still_balance_after_a_full_settlement(self, db):
        self._in_flight_eur_payable()
        with session_scope() as s:
            settle_fx_step(s, _step_row(StepType.FX), CONVERSION_BODY)
        with session_scope() as s:
            settle_payout_step(s, _step_row(StepType.TRANSFER), "PAID")
        rep = reconcile(db.session())
        assert rep.balanced
        assert rep.outflow_by_account, "the outflow accounts must be reported"

    def test_settled_keys_reports_what_the_ledger_holds(self, db):
        self._in_flight_eur_payable()
        assert settled_txn_keys(db.session()) == set()
        with session_scope() as s:
            settle_fx_step(s, _step_row(StepType.FX), CONVERSION_BODY)
        assert CONVERSION_ID in settled_txn_keys(db.session())
        with session_scope() as s:
            settle_payout_step(s, _step_row(StepType.TRANSFER), "PAID")
        keys = settled_txn_keys(db.session())
        assert TRANSFER_ID in keys and CONVERSION_ID in keys

    def test_an_unsent_payable_is_promised_not_in_flight(self, db):
        _seed_cash()
        _payable(number="PO-7002", ccy="USD", minor=700_000)
        m = self._context()
        assert m["promised"].get("USD", 0) >= 700_000
        assert m["in_flight"] == {}
