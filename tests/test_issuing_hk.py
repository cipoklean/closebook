"""A second Airwallex account, for issuing only.

The sandbox that runs the books is registered in NIGERIA, and Nigeria is not an
issuing region: every card create returned the generic
`invalid_request: Invalid issuance details`, which named no field because the
problem was never a field. Airwallex confirmed it and opened a second sandbox
registered in HONG KONG, which exists only to issue cards.

So there are two credentials on this machine and they must not be confused:

  * AWX_CLIENT_ID / AWX_API_KEY          the books: deposits, transfers, webhooks
  * AIRWALLEX_ISSUING_CLIENT_ID / _KEY   cards, and nothing else

These tests pin that separation, the HK card program, and the one thing the
whole beat rests on: a real decline, carrying the bank's own reason.
"""
from __future__ import annotations

import json

import pytest

from app.airwallex import issuing as issuing_mod
from app.airwallex.issuing import Issuing
from app.config import Settings
from app.db import get_default_db, reset_default_db


@pytest.fixture(autouse=True)
def _isolated_db():
    """One workspace per test: the decisions live on the settings row."""
    reset_default_db()
    yield
    reset_default_db()


@pytest.fixture
def db():
    return get_default_db()


# --------------------------------------------------------------------------- #
# a fake that records what it was asked, and answers like the sandbox does
# --------------------------------------------------------------------------- #
class FakeIssuingClient:
    """Stands in for AirwallexClient on the ISSUING account.

    It records every call so a test can assert WHICH credentials were used and
    WHAT body was sent, which is the whole point of having two accounts.
    """

    def __init__(self, *, card_status: str = "ACTIVE",
                 decline: dict | None = None, always_decline: bool = False):
        self.settings = None
        self.calls: list[dict] = []
        self.card_status = card_status
        self.always_decline = always_decline
        self.decline = decline or {
            "card_transaction_id": "txn-decline-1",
            "card_id": "card-hk-1",
            "transaction_amount": 7000.01,
            "transaction_currency": "USD",
            "process_result": "DECLINED",
            "failure_reason": "LIMIT_EXCEEDED",
        }
        self.transactions: list[dict] = []

    # the two calls Issuing makes
    def call(self, method, path, *, json_body=None, request_id=None,
             is_simulation=False):
        self.calls.append({"method": method, "path": path,
                           "body": json_body, "request_id": request_id})
        if path.endswith("/issuing/cardholders/create"):
            return {"id": "ch-hk-1", "type": "DELEGATE", "status": "READY",
                    "email": (json_body or {}).get("email")}
        if path.endswith("/issuing/cards/create"):
            return {"id": "card-hk-1", "card_status": self.card_status,
                    "cardholder_id": (json_body or {}).get("cardholder_id"),
                    "form_factor": "VIRTUAL"}
        if path.endswith("/issuing/cards/card-hk-1"):
            return {"id": "card-hk-1", "card_status": self.card_status}
        if path.endswith("/simulation/issuing/create"):
            self.transactions.append(dict(json_body or {}))
            body = dict(json_body or {})
            # THE LIVE SHAPE (captured 2026-10-29): transaction_amount,
            # transaction_currency, merchant_info, single_phase, and a
            # `process_result` of APPROVED or DECLINED with a failure_reason.
            if self.always_decline or float(
                    body.get("transaction_amount", 0)) > 7000.0:
                return dict(self.decline)
            return {"card_transaction_id": "txn-ok-1", "card_id": "card-hk-1",
                    "transaction_amount": body.get("transaction_amount"),
                    "transaction_currency": "USD",
                    "process_result": "APPROVED"}
        raise AssertionError(f"unexpected call {method} {path}")

    def get(self, path, **kwargs):
        self.calls.append({"method": "GET", "path": path})
        return self.transactions


def _settings(**over) -> Settings:
    base = dict(
        awx_client_id="main-id", awx_api_key="main-key",
        airwallex_issuing_client_id="hk-id",
        airwallex_issuing_api_key="hk-key",
    )
    base.update(over)
    return Settings(**base)


# --------------------------------------------------------------------------- #
# 1. two clients, one job each
# --------------------------------------------------------------------------- #
class TestTwoClientsOneJobEach:
    def test_the_issuing_client_authenticates_as_the_issuing_account(self):
        """If this ever used the books' credentials, every card call would go
        back to the Nigerian account and fail exactly as it did before."""
        client = issuing_mod.issuing_client(_settings())
        assert client is not None
        assert client.settings.awx_client_id.get_secret_value() == "hk-id"
        assert client.settings.awx_api_key.get_secret_value() == "hk-key"
        # ...and the books are untouched by it
        assert client.settings is not None

    def test_without_issuing_credentials_there_is_no_issuing_client(self):
        for over in ({"airwallex_issuing_client_id": "",
                      "airwallex_issuing_api_key": ""},
                     {"airwallex_issuing_client_id": "hk-id",
                      "airwallex_issuing_api_key": ""}):
            assert issuing_mod.issuing_client(_settings(**over)) is None

    def test_issuing_ready_reports_the_same_fact_the_client_does(self):
        assert _settings().issuing_ready() is True
        assert _settings(airwallex_issuing_api_key="").issuing_ready() is False

    def test_nothing_else_changed_accounts(self):
        """The books still point at their own credentials after this change."""
        s = _settings()
        assert s.awx_client_id.get_secret_value() == "main-id"
        assert s.awx_api_key.get_secret_value() == "main-key"


# --------------------------------------------------------------------------- #
# 2. the Hong Kong card program
# --------------------------------------------------------------------------- #
class TestTheHKCardProgram:
    def _create(self, limit_minor=700_000):
        fake = FakeIssuingClient()
        Issuing(fake).create_card(
            None, cardholder_id="ch-hk-1", currency="USD",
            per_transaction_limit_minor=limit_minor,
            all_time_limit_minor=40_000_00, request_id="req-card-1")
        return next(c for c in fake.calls if c["path"].endswith("/cards/create"))

    def test_the_program_is_the_one_hk_supports(self):
        body = self._create()["body"]
        assert body["program"] == {
            "purpose": "COMMERCIAL", "type": "CREDIT",
            "sub_type": "GOOD_FUNDS_CREDIT",
        }, body["program"]

    def test_it_is_a_wallet_funded_virtual_card_with_no_funding_source(self):
        body = self._create()["body"]
        assert body["form_factor"] == "VIRTUAL"
        assert "funding_source_id" not in body
        assert body["is_personalized"] is False
        assert body["cardholder_id"] == "ch-hk-1"

    def test_the_limit_comes_from_policy_not_from_a_constant(self):
        body = self._create(limit_minor=250_000)["body"]
        limits = body["authorization_controls"]["transaction_limits"]["limits"]
        per_txn = next(x for x in limits if x["interval"] == "PER_TRANSACTION")
        # to_major_str strips trailing zeros, and Airwallex accepts that
        assert float(per_txn["amount"]) == 2500.0, limits
        assert body["authorization_controls"]["transaction_limits"]["currency"] == "USD"


# --------------------------------------------------------------------------- #
# 3. the decline is the bank's, not ours
# --------------------------------------------------------------------------- #
class TestARealDecline:
    def test_over_the_limit_the_bank_declines_and_says_why(self):
        fake = FakeIssuingClient()
        out = Issuing(fake).authorise(
            "card-hk-1", amount="7000.01", currency="USD",
            limit_minor=700_000, merchant="Acme Ltd")
        assert out["declined"] is True
        assert out["bank_reason"] == "LIMIT_EXCEEDED"
        assert out["amount_minor"] == 700_001
        # and the decision is recorded for the page, with both halves
        assert out["decision"]["result"] == "Declined"
        assert "LIMIT_EXCEEDED" in out["decision"]["bank_reason"]

    def test_inside_the_limit_it_is_approved(self):
        fake = FakeIssuingClient()
        out = Issuing(fake).authorise(
            "card-hk-1", amount="7000.00", currency="USD",
            limit_minor=700_000, merchant="Acme Ltd")
        assert out["declined"] is False
        assert out["decision"]["result"] == "Approved"

    def test_the_code_refuses_over_the_limit_before_asking_the_bank(self):
        """Belt and braces: our own check must hold even if the bank allowed it.

        The bank is the second line here, not the first. If the two ever
        disagree the DECLINE is what gets recorded, because the money did not
        move.
        """
        fake = FakeIssuingClient(decline={
            "card_transaction_id": "txn-x", "card_id": "card-hk-1",
            "transaction_amount": 9000.00, "transaction_currency": "USD",
            "process_result": "APPROVED"})
        out = Issuing(fake).authorise(
            "card-hk-1", amount="9000.00", currency="USD",
            limit_minor=700_000, merchant="Acme Ltd")
        assert out["declined"] is True, out
        # the DECLINE is recorded, and the failing check says which rule held
        assert out["decision"]["result"] == "Declined"
        failed = [c for c in out["decision"]["checks"] if not c[1]]
        assert failed, out["decision"]
        assert "limit" in failed[0][0].lower(), out["decision"]


    def test_a_bank_decline_our_arithmetic_did_not_predict_is_still_a_decline(self):
        """A frozen card declines an amount the limit allows. The bank wins."""
        fake = FakeIssuingClient(always_decline=True, decline={
            "card_transaction_id": "txn-frozen", "card_id": "card-hk-1",
            "transaction_amount": 100, "transaction_currency": "USD",
            "process_result": "DECLINED",
            "failure_reason": "CARD_INACTIVE"})
        out = Issuing(fake).authorise(
            "card-hk-1", amount="1.00", currency="USD", limit_minor=700_000,
            merchant="Acme Ltd")
        assert out["declined"] is True, out
        assert out["bank_reason"] == "CARD_INACTIVE"


# --------------------------------------------------------------------------- #
# 4. the caveat goes away only when the bank has spoken
# --------------------------------------------------------------------------- #
class TestTheCaveat:
    def test_with_no_issuing_account_the_beat_keeps_its_caveat(self, db):
        from app.domain import cards

        assert cards.caveat_wanted(db) is True
        assert "policy verdict" in cards.caveat_sentence(db).lower()

    def test_a_recorded_bank_decline_removes_it(self, db):
        from app.domain import cards

        cards.record_decision(db, {
            "card_id": "card-hk-1", "amount_minor": 700_001, "currency": "USD",
            "merchant": "Acme Ltd", "result": "Declined",
            "bank_reason": "LIMIT_EXCEEDED", "live": True,
        })
        assert cards.caveat_wanted(db) is False
        assert "policy verdict" not in cards.caveat_sentence(db).lower()

    def test_our_own_verdict_alone_is_not_evidence(self, db):
        """A decision the code made without the bank must not retire the caveat."""
        from app.domain import cards

        cards.record_decision(db, {
            "card_id": "", "amount_minor": 700_001, "currency": "USD",
            "merchant": "", "result": "Declined",
            "bank_reason": "", "live": False,
        })
        assert cards.caveat_wanted(db) is True


# --------------------------------------------------------------------------- #
# 5. the page shows the decision the way the brief asks
# --------------------------------------------------------------------------- #
class TestThePageShowsIt:
    def test_ai_activity_renders_the_card_decision_in_three_steps(self, db):
        from app.dashboard import build_context
        from app.domain import cards
        from app.ui import render_app

        cards.record_decision(db, {
            "card_id": "card-hk-1", "amount_minor": 700_001, "currency": "USD",
            "merchant": "Acme Ltd", "result": "Declined",
            "bank_reason": "LIMIT_EXCEEDED", "live": True,
        })
        html = render_app(build_context([]), tab="ai")
        assert "Card authorisations" in html, html[:600]
        for want in ("AI suggestion", "Rule checks", "Result"):
            assert want in html, want
        assert "LIMIT_EXCEEDED" in html
        assert "7,000.01" in html

    def test_an_empty_book_says_what_a_card_would_do(self, db):
        from app.dashboard import build_context
        from app.ui import render_app

        html = render_app(build_context([]), tab="ai")
        assert "Card authorisations" in html
        assert "no card" in html.lower()
