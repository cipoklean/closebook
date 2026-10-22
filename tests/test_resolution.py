"""Suspense can only leave in three ways, and each one leaves different books.

  match  -> someone's invoice payment; the invoice and the ledger both move
  refund -> owed back; stops being spendable, nothing is sent
  income -> ours; becomes spendable, and it needs a note

These tests pin the two properties that make the feature safe: a resolution
moves the money EXACTLY once, and the three numbers on Home (suspense, refunds
owed, free to spend) reconcile to the cash they come from.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import get_default_db, reset_default_db, session_scope  # noqa: E402
from app.domain import intake as I  # noqa: E402
from app.domain import workspace as W  # noqa: E402
from app.domain.ledger import (  # noqa: E402
    OTHER_INCOME, REFUNDS_OWED, LedgerError, reconcile, resolve_income,
    resolve_refund, suspense_for,
)
from app.models import Inflow, LedgerEntry  # noqa: E402

PIN = "424242"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    reset_default_db()
    from app.config import get_settings
    from pydantic import SecretStr
    monkeypatch.setattr(get_settings(), "demo_approver_pin", SecretStr(PIN),
                        raising=False)
    # `build_context` reads live transfer statuses from Airwallex when it thinks
    # it can. A rendering test must not depend on a provider being reachable —
    # that is how a suite passes locally and fails everywhere else.
    from app import dashboard as dash
    previous = dash.LIVE_STATUS_ENABLED
    dash.LIVE_STATUS_ENABLED = False
    yield
    dash.LIVE_STATUS_ENABLED = previous
    reset_default_db()


@pytest.fixture
def db():
    return get_default_db()


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def _unmatched(db, *, amount=7000, ccy="USD", payer="Unknown Party",
               reference="ZXQ-99881", dep_id="res-1"):
    """Money that matches nothing: the pure suspense case."""
    r = I.ingest_deposit(db, {"id": dep_id, "currency": ccy, "amount": amount,
                              "payer_name": payer, "reference": reference,
                              "status": "SETTLED"})
    assert r.disposition in ("ESCALATED", "PROPOSED", "HELD"), r.disposition
    return r


def _flash(resp, which="ok"):
    """The redirect's query string, decoded.

    Flask-style flashes ride on the Location header, so every sentence is
    URL-encoded: 'Refund owed' arrives as 'Refund+owed'. Asserting on the raw
    header tests the encoder, not the message.
    """
    from urllib.parse import parse_qs, unquote_plus, urlparse
    q = parse_qs(urlparse(resp.headers["location"]).query)
    return unquote_plus((q.get(which) or [""])[0])


def _legs(txn_prefix):
    with session_scope() as s:
        return [(e.account, e.debit_minor, e.credit_minor, e.currency)
                for e in s.query(LedgerEntry).all()
                if (e.txn_id or "").startswith(txn_prefix)]


class TestRefundOwed:
    def test_it_moves_the_money_out_of_suspense_and_into_a_liability(self, db):
        _unmatched(db)
        with session_scope() as s:
            inflow = s.query(Inflow).one()
            before = suspense_for(s, inflow)
            assert before == 700_000
            resolve_refund(s, inflow, reason="duplicate payment", by="david")
            assert suspense_for(s, inflow) == 0

        legs = _legs("inflow:1:refund")
        assert ("unapplied_cash", 700_000, 0, "USD") in legs
        assert (REFUNDS_OWED, 0, 700_000, "USD") in legs

        rep = reconcile(db.session())
        assert rep.suspense_by_currency == {}
        assert rep.refunds_owed_by_currency == {"USD": 700_000}

    def test_the_cash_does_not_move_and_nothing_is_sent(self, db):
        """A refund is a RECORDED LIABILITY, not a payment."""
        _unmatched(db)
        before = reconcile(db.session()).cash_by_currency
        with session_scope() as s:
            resolve_refund(s, s.query(Inflow).one(), reason="duplicate payment")
        after = reconcile(db.session()).cash_by_currency
        assert before == after == {"USD": 700_000}, \
            "the money is still in the bank; only which liability it sits against changed"
        assert _legs("pay:") == [], "no transfer is sent"

    def test_a_refund_needs_a_reason(self, db):
        _unmatched(db)
        with session_scope() as s:
            with pytest.raises(LedgerError) as exc:
                resolve_refund(s, s.query(Inflow).one(), reason="   ")
        assert "reason" in str(exc.value)

    def test_it_carries_the_reason_and_the_decider(self, db):
        _unmatched(db)
        with session_scope() as s:
            resolve_refund(s, s.query(Inflow).one(),
                           reason="duplicate payment", by="david")
        with session_scope() as s:
            row = s.query(Inflow).one()
            assert row.review_state == "REFUND_OWED"
            assert row.resolution_reason == "duplicate payment"
            assert row.resolved_by == "david"
            assert row.resolved_at is not None


class TestReleasedAsIncome:
    def test_it_becomes_spendable(self, db):
        _unmatched(db)
        from app.domain.policy import load_budget
        from app.domain.treasury import cash_position
        pos = cash_position(db, load_budget())
        assert pos.suspense.get("USD") == 700_000

        with session_scope() as s:
            resolve_income(s, s.query(Inflow).one(),
                           note="no invoice will ever claim this", by="david")

        pos = cash_position(db, load_budget())
        assert pos.suspense.get("USD", 0) == 0, "it is no longer held back"
        rep = reconcile(db.session())
        assert rep.other_income_by_currency == {"USD": 700_000}

    def test_the_ledger_credits_other_income(self, db):
        _unmatched(db)
        with session_scope() as s:
            resolve_income(s, s.query(Inflow).one(), note="ours to keep")
        legs = _legs("inflow:1:income")
        assert ("unapplied_cash", 700_000, 0, "USD") in legs
        assert (OTHER_INCOME, 0, 700_000, "USD") in legs

    def test_releasing_needs_a_note(self, db):
        """'Unexplained money became profit' is the entry an auditor asks about."""
        _unmatched(db)
        with session_scope() as s:
            with pytest.raises(LedgerError) as exc:
                resolve_income(s, s.query(Inflow).one(), note="")
        assert "note" in str(exc.value)

    def test_the_audit_row_names_the_approver(self, db):
        _unmatched(db)
        with session_scope() as s:
            resolve_income(s, s.query(Inflow).one(),
                           note="no invoice will ever claim this", by="david")
        with session_scope() as s:
            row = s.query(Inflow).one()
            assert row.resolved_by == "david"
            assert row.resolution_note == "no invoice will ever claim this"
            assert row.resolved_at is not None


class TestAResolutionHappensExactlyOnce:
    def test_a_second_refund_is_refused_and_writes_nothing(self, db):
        _unmatched(db)
        with session_scope() as s:
            resolve_refund(s, s.query(Inflow).one(), reason="duplicate payment")
        n_before = len(_legs("inflow:1:refund"))

        with session_scope() as s:
            with pytest.raises(LedgerError) as exc:
                resolve_refund(s, s.query(Inflow).one(), reason="again")
        assert "already resolved" in str(exc.value)
        assert len(_legs("inflow:1:refund")) == n_before, "no second posting"

    def test_refund_then_income_cannot_double_release(self, db):
        _unmatched(db)
        with session_scope() as s:
            resolve_refund(s, s.query(Inflow).one(), reason="duplicate payment")
        with session_scope() as s:
            with pytest.raises(LedgerError):
                resolve_income(s, s.query(Inflow).one(), note="and mine too")
        rep = reconcile(db.session())
        assert rep.other_income_by_currency == {}, "it was never released"
        assert rep.refunds_owed_by_currency == {"USD": 700_000}

    def test_a_part_allocated_inflow_only_resolves_its_remainder(self, db):
        """The amount comes off the LEDGER, not off the inflow's face value.

        Resolving the whole amount would invent money already applied to an
        invoice, and the difference would have to come out of somewhere.
        """
        with session_scope() as s:
            c = W.create_customer(s, name="Lumen Stores")
            W.create_invoice(s, customer_id=c.id, number="INV-1003",
                             currency="USD", amount="4400.00")
        r = I.ingest_deposit(db, {"id": "part-1", "currency": "USD",
                                  "amount": 4370, "payer_name": "Apex Factors Ltd",
                                  "reference": "on behalf of lumen stores",
                                  "status": "SETTLED"})
        assert r.disposition == "PROPOSED"
        with session_scope() as s:
            from app.domain.ledger import approve_inflow
            approve_inflow(s, s.query(Inflow).one(), approved_by="david")
        with session_scope() as s:
            inflow = s.query(Inflow).one()
            left = suspense_for(s, inflow)
            assert left == 0, "the whole thing was allocated, so there is nothing left"
            with pytest.raises(LedgerError) as exc:
                resolve_refund(s, inflow, reason="duplicate")
            assert "nothing left in suspense" in str(exc.value)


class TestRejectedMoneyIsStillResolvable:
    def test_a_rejected_proposal_can_still_be_resolved(self, db):
        """Saying no is not a decision about where the money GOES."""
        with session_scope() as s:
            c = W.create_customer(s, name="Harrow & Co")
            W.create_invoice(s, customer_id=c.id, number="INV-1002",
                             currency="GBP", amount="8000.00")
        I.ingest_deposit(db, {"id": "rej-1", "currency": "GBP", "amount": 4800,
                              "payer_name": "Harrow and Company",
                              "reference": "chq 1OO2 part", "status": "SETTLED"})
        with session_scope() as s:
            from app.domain.ledger import reject_inflow
            reject_inflow(s, s.query(Inflow).one(), rejected_by="david",
                          reason="not that invoice")
        with session_scope() as s:
            row = s.query(Inflow).one()
            assert row.review_state == "REJECTED"
            assert suspense_for(s, row) == 480_000, "the cash is still there"
            resolve_refund(s, row, reason="customer says it was a duplicate")
        rep = reconcile(db.session())
        assert rep.refunds_owed_by_currency == {"GBP": 480_000}


class TestTheRouteIsGatedAndIdempotent:
    def test_a_wrong_pin_resolves_nothing(self, client, db):
        _unmatched(db)
        r = client.post("/inflows/1/resolve",
                        data={"what": "income", "note": "mine", "pin": "000000"},
                        follow_redirects=False)
        assert _flash(r, "err"), "a wrong PIN must be refused"
        with session_scope() as s:
            assert s.query(Inflow).one().review_state != "RELEASED_INCOME"

    def test_no_pin_configured_closes_the_endpoint(self, client, db, monkeypatch):
        from app.config import get_settings
        from pydantic import SecretStr
        monkeypatch.setattr(get_settings(), "demo_approver_pin", SecretStr(""),
                            raising=False)
        _unmatched(db)
        r = client.post("/inflows/1/resolve",
                        data={"what": "income", "note": "mine", "pin": ""},
                        follow_redirects=False)
        assert "closed rather than open" in _flash(r, "err")

    def test_the_route_refuses_an_unknown_choice(self, client, db):
        _unmatched(db)
        r = client.post("/inflows/1/resolve",
                        data={"what": "keep it", "pin": PIN},
                        follow_redirects=False)
        assert _flash(r, "err")

    def test_the_route_resolves_once_and_then_refuses(self, client, db):
        _unmatched(db)
        first = client.post("/inflows/1/resolve",
                            data={"what": "refund", "reason": "duplicate payment",
                                  "by": "david", "pin": PIN},
                            follow_redirects=False)
        assert not _flash(first, "err"), _flash(first, "err")
        assert "Refund owed" in _flash(first)

        second = client.post("/inflows/1/resolve",
                             data={"what": "refund", "reason": "again",
                                   "pin": PIN},
                             follow_redirects=False)
        assert _flash(second, "err"), "a second resolution must be refused"
        rep = reconcile(db.session())
        assert rep.refunds_owed_by_currency == {"USD": 700_000}


class TestTheNumbersAddUpOnScreen:
    def test_suspense_refunds_and_free_reconcile_to_cash(self, db, client):
        """The three Home figures must be checkable against each other.

        cash - suspense - refunds owed - floor == free to spend, in the budget
        currency. A page where the numbers do not add up is a page nobody
        trusts, and this one is read by an operator deciding whether to pay.
        """
        from app.dashboard import build_context

        from app.domain.settings_store import save as save_settings
        with session_scope() as s:
            save_settings(s, budget_currency="USD", reserve_floor="5000",
                          auto_pay_limit="2500", fx_tolerance_pct="0.50")
        _unmatched(db, amount=7000, dep_id="recon-1")
        _unmatched(db, amount=3000, payer="Another Stranger",
                   reference="nothing at all", dep_id="recon-2")
        with session_scope() as s:
            rows = s.query(Inflow).order_by(Inflow.id).all()
            resolve_refund(s, rows[0], reason="duplicate payment")

        c = build_context([])
        m = c["money"]
        cash = c["cash"]["USD"]
        floor = c["budget"]["reserve_floor_minor"]
        assert cash == 1_000_000
        assert m["suspense"] == {"USD": 300_000}
        assert m["refunds_owed"] == {"USD": 700_000}
        # 10,000 cash - 3,000 unattributed - 7,000 owed back - 5,000 floor
        assert m["free"]["USD"] == -500_000
        assert m["free"]["USD"] == cash - 300_000 - 700_000 - floor

        html = client.get("/app?tab=home").text
        assert "Suspense" in html and "not spendable" in html
        assert "Refunds owed" in html
        assert "Free to spend" in html
        # the working, so the figure can be checked by hand
        assert "unattributed" in html and "owed back" in html
