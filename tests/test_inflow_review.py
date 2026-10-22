"""Approving a PROPOSED match, through the real HTTP routes.

Below the auto band nothing is applied; the operator decides. These tests drive
the actual endpoints, because the route is where the PIN gate and the
"suspense → invoice" posting meet, and a unit test on the function would miss
both.
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
from app.models import Inflow, InvoiceReceivable, LedgerEntry, Match, RecStatus  # noqa: E402

PIN = "424242"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    reset_default_db()
    # The route reads the PIN from the app settings; set one so the gate is
    # genuinely exercised rather than short-circuited as "not configured".
    from app.config import get_settings
    from pydantic import SecretStr
    monkeypatch.setattr(get_settings(), "demo_approver_pin", SecretStr(PIN),
                        raising=False)
    yield
    reset_default_db()


@pytest.fixture
def db():
    return get_default_db()


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def _invoice(number="INV-1002", ccy="GBP", minor=800_000, customer="Harrow & Co"):
    with session_scope() as s:
        c = W.create_customer(s, name=customer)
        return W.create_invoice(s, customer_id=c.id, number=number,
                                currency=ccy, amount=str(minor / 100)).id


def _proposed_inflow(db, *, amount=480_000, ccy="GBP", payer="Harrow and Company",
                     reference="chq 1OO2 part"):
    """A deposit that lands BELOW the band, so it becomes a proposal."""
    r = I.ingest_deposit(db, {"id": "rev-1", "currency": ccy, "amount": amount / 100,
                              "payer_name": payer, "reference": reference,
                              "status": "SETTLED"})
    assert r.disposition == "PROPOSED", f"expected a proposal, got {r.disposition}"
    return r


def _balances():
    out: dict[str, int] = {}
    with session_scope() as s:
        for e in s.query(LedgerEntry).all():
            out[e.account] = out.get(e.account, 0) + e.debit_minor - e.credit_minor
    return out


class TestAProposalIsNotAMatch:
    def test_the_deposit_moves_no_invoice(self, db):
        _invoice()
        _proposed_inflow(db)
        with session_scope() as s:
            inv = s.query(InvoiceReceivable).filter_by(number="INV-1002").one()
            assert inv.amount_paid_minor == 0
            assert inv.status == RecStatus.OPEN
            assert s.query(Match).count() == 0
            inflow = s.query(Inflow).one()
            assert inflow.review_state == "PENDING"
            assert inflow.proposed_json

    def test_the_cash_is_in_suspense_not_lost(self, db):
        _invoice()
        _proposed_inflow(db)
        rows = _balances()
        assert rows["cash:GBP"] == 480_000, "we really did receive it"
        assert rows["unapplied_cash"] == -480_000
        assert "receivable_clearing" not in rows


class TestApprove:
    def test_it_needs_the_pin(self, client, db):
        _invoice()
        r = _proposed_inflow(db)
        resp = client.post(f"/inflows/{r.inflow_id}/approve",
                           data={"pin": "000000"}, follow_redirects=False)
        assert "err=" in resp.headers["location"]
        with session_scope() as s:
            inv = s.query(InvoiceReceivable).filter_by(number="INV-1002").one()
            assert inv.amount_paid_minor == 0, "a wrong PIN must move nothing"

    def test_approving_applies_it(self, client, db):
        _invoice()
        r = _proposed_inflow(db)
        resp = client.post(f"/inflows/{r.inflow_id}/approve",
                           data={"pin": PIN}, follow_redirects=False)
        assert "ok=" in resp.headers["location"]
        with session_scope() as s:
            inv = s.query(InvoiceReceivable).filter_by(number="INV-1002").one()
            assert inv.amount_paid_minor == 480_000
            assert inv.status == RecStatus.PARTIAL
            assert s.query(Match).count() == 1
            inflow = s.query(Inflow).one()
            assert inflow.review_state == "APPROVED"
        rows = _balances()
        assert rows["unapplied_cash"] == 0, "suspense cleared"
        assert rows["receivable_clearing"] == -480_000, "landed on the invoice"
        assert rows["cash:GBP"] == 480_000, "and only once"

    def test_approving_twice_changes_nothing(self, client, db):
        _invoice()
        r = _proposed_inflow(db)
        client.post(f"/inflows/{r.inflow_id}/approve", data={"pin": PIN})
        before = _balances()
        resp = client.post(f"/inflows/{r.inflow_id}/approve",
                           data={"pin": PIN}, follow_redirects=False)
        assert "err=" in resp.headers["location"], "the second one is refused"
        assert _balances() == before, "and posts nothing"
        with session_scope() as s:
            inv = s.query(InvoiceReceivable).filter_by(number="INV-1002").one()
            assert inv.amount_paid_minor == 480_000, "not 960,000"


class TestReject:
    def test_rejecting_leaves_the_cash_where_it_is(self, client, db):
        _invoice()
        r = _proposed_inflow(db)
        resp = client.post(f"/inflows/{r.inflow_id}/reject",
                           data={"pin": PIN, "reason": "not our invoice"},
                           follow_redirects=False)
        assert "ok=" in resp.headers["location"]
        with session_scope() as s:
            inv = s.query(InvoiceReceivable).filter_by(number="INV-1002").one()
            assert inv.amount_paid_minor == 0
            inflow = s.query(Inflow).one()
            assert inflow.review_state == "REJECTED"
        rows = _balances()
        assert rows["cash:GBP"] == 480_000, "we still have the money"
        assert rows["unapplied_cash"] == -480_000, "parked for a refund or a re-match"

    def test_a_rejected_proposal_cannot_then_be_approved(self, client, db):
        _invoice()
        r = _proposed_inflow(db)
        client.post(f"/inflows/{r.inflow_id}/reject", data={"pin": PIN})
        resp = client.post(f"/inflows/{r.inflow_id}/approve",
                           data={"pin": PIN}, follow_redirects=False)
        assert "err=" in resp.headers["location"]

    def test_it_also_needs_the_pin(self, client, db):
        _invoice()
        r = _proposed_inflow(db)
        resp = client.post(f"/inflows/{r.inflow_id}/reject",
                           data={"pin": "nope"}, follow_redirects=False)
        assert "err=" in resp.headers["location"]
        with session_scope() as s:
            assert s.query(Inflow).one().review_state == "PENDING"


class TestThePageOffersBothChoices:
    def test_a_pending_proposal_offers_all_three_resolutions(self, client, db):
        """Suspense can only leave three ways, so the page must offer three.

        It used to offer two: apply the proposal, or say no. Saying no left the
        cash in suspense with nothing further to do about it, which is how money
        got stuck.
        """
        _invoice()
        _proposed_inflow(db)
        html = client.get("/app?tab=ok").text
        assert "Money in that needs a look" in html
        assert "/inflows/1/resolve" in html
        assert 'value="match"' in html
        assert 'value="refund"' in html
        assert 'value="income"' in html
        assert "/reject" in html, "saying no is still allowed"
        assert "INV-1002" in html, "the match option names the invoice it applies to"
        assert 'name="pin"' in html
        assert 'name="reason"' in html, "a refund needs a reason"
        assert 'name="note"' in html, "releasing as income needs a note"

    def test_a_settled_match_is_not_offered_for_review(self, client, db):
        _invoice(number="INV-1001", ccy="USD", minor=1_200_000,
                 customer="Brightline Retail")
        r = I.ingest_deposit(db, {"id": "clean-1", "currency": "USD",
                                  "amount": 12000, "payer_name": "Brightline Retail",
                                  "reference": "payment for INV-1001",
                                  "status": "SETTLED"})
        assert r.disposition == "APPLIED"
        html = client.get("/app?tab=ok").text
        assert "Money in that needs a look" not in html
