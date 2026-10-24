"""Run 6: money-out in a fresh book, and the four things that stopped it.

WHAT THE TESTER SAW (run 6, 39 passed / 4 failed / 11 blocked):

  1. A fresh book never got spendable cash. A USD 2,000 deposit made from the
     book showed as AMBIGUOUS and unapplied, and the bill there stayed Held with
     "needs USD 1,800.00 but the account holds USD 0.00".
  2. Setup refused the supplier's beneficiary with a required-field validation
     prompt, so no bill could be created at all.
  3. After a partial payment was approved, the page did not say what was left to
     collect.
  4. The AI tab's empty state said "0 calls / No model calls yet" and nothing
     about how to get a decision.

WHAT IS ACTUALLY TRUE ABOUT (1), asserted here rather than assumed: the deposit
IS recorded and credited in the book that made it — the inflow row and the
ledger's cash account are both in that book, and the other book has neither. The
webhook is not stealing it (the tester's shared book has no inflows at all).
What stops the payout is the documented suspense rule: money that no invoice
claims is real cash and is NOT spendable, so the book can pay nothing until a
human resolves it — and the sentence the page printed said the account held
USD 0.00, which is false and points the operator at the wrong thing.

Provider-free: every row is written with the app's OWN writers
(`settings_store.save`, `create_supplier`, `record_beneficiary`, `create_payable`,
`record_and_match`), and nothing here calls Airwallex or a model.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PIN = "money-out-pin"          # 13 characters: the PIN field is maxlength 12
PIN = PIN[:12]
BILL = "PO-R6-A"
DEPOSIT_MINOR = 200_000        # USD 2,000.00
BILL_MINOR = 180_000           # USD 1,800.00


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def books(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOSEBOOK_WORKSPACES", "1")
    monkeypatch.setenv("CLOSEBOOK_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("DEMO_APPROVER_PIN", PIN)
    from app import workspaces as ws
    from app.config import _settings_cached

    _settings_cached.cache_clear()
    forget = getattr(ws, "forget_cached_books", None)
    if forget:
        forget()
    yield tmp_path / "ws"
    if forget:
        forget()
    _settings_cached.cache_clear()


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    """No provider of any kind: the deterministic tiers must carry these."""
    monkeypatch.setenv("LLM_API_KEY", "")
    from app.config import _settings_cached

    _settings_cached.cache_clear()


# ------------------------------------------------------------------- helpers
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def url(path: str, ws: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}ws={ws}"


def page(ws: str, tab: str = "home", client_obj=None):
    return (client_obj or client()).get(url(f"/app?tab={tab}", ws)).text


def visible(html: str) -> str:
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    return " ".join(re.sub(r"<[^>]+>", " ", body).split())


def in_book(suffix: str, fn):
    from app import workspaces

    with workspaces.book(suffix).session_scope() as s:
        return fn(s)


def build_book(suffix: str, *, floor: str = "0", limit: str = "2500",
               bill_minor: int = BILL_MINOR, invoice_minor: int = 0) -> dict:
    """A bill (and optionally an invoice), written by the app's own writers."""
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import (
        create_customer, create_invoice, create_payable, create_supplier,
        record_beneficiary,
    )

    out: dict = {}

    def write(s):
        save_settings(s, budget_currency="USD", reserve_floor=floor,
                      auto_pay_limit=limit, fx_tolerance_pct="0.50")
        sup = create_supplier(s, name="Atlanta Packaging", country="US",
                              currency="USD", payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"test": "no provider"},
                           beneficiary_id="ben-r6", status="CREATED")
        out["supplier_id"] = sup.id
        out["payable"] = create_payable(
            s, supplier_id=sup.id, currency="USD",
            amount=str(bill_minor / 100), number=BILL).id
        if invoice_minor:
            cust = create_customer(s, name="Brightline Retail")
            inv = create_invoice(s, customer_id=cust.id, number="INV-R6",
                                 currency="USD", amount=str(invoice_minor / 100))
            out["invoice_id"] = inv.id
            out["customer_id"] = cust.id
    in_book(suffix, write)
    return out


def deposit_in(suffix: str, *, minor: int = DEPOSIT_MINOR,
               deposit_id: str = "dep-r6-1", reference: str = "dep r6",
               payer: str = "Brightline Retail"):
    """Record a deposit the way the app does, with no provider involved."""
    from app.domain.intake import record_and_match

    def write(s):
        return record_and_match(s, deposit_id=deposit_id, currency="USD",
                                amount_minor=minor, payer_name=payer,
                                reference=reference, llm=False)
    return in_book(suffix, write)


def settle_book(suffix: str) -> None:
    """Run the plan the way an event does, without sending anything.

    A stand-in for the side effects of pressing Re-plan: the plan is stored and
    the pages read it, so the assertions below are about what an operator SEES.
    """
    from app.domain.conductor import on_event

    from app import workspaces

    on_event(workspaces.book(suffix), source="test", execute=False, dry_run=True)


# --------------------------------------------------- 1. the book's own cash
class TestTheBookOwnsItsCash:
    def test_a_deposit_lands_in_the_book_that_made_it(self, books):
        """The fact the report was unsure about: it IS credited in book X."""
        deposit_in("aaa")
        from app import workspaces

        s = workspaces.book("aaa").session()
        try:
            from app.models import Inflow, LedgerEntry
            assert s.query(Inflow).count() == 1, "the inflow is not in book aaa"
            rows = s.query(LedgerEntry).all()
            cash = sum(r.debit_minor - r.credit_minor for r in rows
                       if r.account == "cash:USD")
            assert cash == DEPOSIT_MINOR, (
                f"book aaa's own cash account holds {cash}, expected {DEPOSIT_MINOR}")
        finally:
            s.close()
        from app.db import get_default_db
        s = get_default_db().session()
        try:
            from app.models import Inflow, LedgerEntry
            assert s.query(Inflow).count() == 0, "the deposit leaked into the shared book"
            assert s.query(LedgerEntry).count() == 0
        finally:
            s.close()

    def test_the_held_sentence_names_the_books_cash_and_what_is_unattributed(
            self, books):
        """The screen may not say the account holds 0.00 when it holds 2,000.00.

        An unattributed deposit is deliberately NOT spendable, and that rule is
        right. The sentence was wrong: it reported the SPENDABLE figure as the
        account's balance, so the operator reads "the money never arrived"
        instead of "resolve the deposit and this can be paid".
        """
        build_book("aaa")
        deposit_in("aaa")
        settle_book("aaa")
        text = visible(page("aaa", "money-out"))
        assert "USD 1,800.00" in text, text[-600:]
        assert "holds USD 0.00" not in text, (
            "the page claims the account holds nothing while it holds USD 2,000.00")
        assert "USD 2,000.00" in text, (
            "the sentence does not name the money the book actually holds")
        assert "waiting on a match" in text, (
            "the sentence does not say the money is unattributed")

    def test_money_in_says_how_to_make_the_deposit_spendable(self, books):
        """The path from "in suspense" to "spendable" has to be on the page."""
        build_book("aaa")
        deposit_in("aaa")
        text = visible(page("aaa", "money-in"))
        assert "Release it as income" in text or "release it as income" in text, (
            "Money in never says how to make unmatched cash spendable")
        assert "not spendable" in text, (
            "Money in does not say the held cash cannot pay a bill yet")

    def test_a_fresh_book_polls_with_a_cutoff_that_does_not_move(
            self, books, monkeypatch):
        """An unsaved book must not filter out everything by "now".

        `settings_store.read()` returns a TRANSIENT default row whose
        `deposits_from` was `utcnow()` AT READ TIME, so a book that had never
        saved Settings had a cutoff of *this instant*: every deposit was older
        than it and the poll answered "no new deposits" for ever.
        """
        from app import workspaces
        from app.domain.settings_store import read as read_settings

        client().get(url("/app?tab=home", "aaa"))       # creates the book
        with workspaces.book("aaa").session() as s:
            first = read_settings(s).deposits_from
        with workspaces.book("aaa").session() as s:
            second = read_settings(s).deposits_from
        assert first == second, (
            "the deposit cutoff is recomputed on every read, so a poll filters "
            "out everything that arrived before the latest page view")

        from app.airwallex import simulation as sim_mod
        from app.domain import intake

        created = (first.replace(microsecond=0) + __import__("datetime").timedelta(minutes=1))
        monkeypatch.setattr(
            sim_mod.Simulation, "list_deposits",
            lambda self: [{"id": "dep-poll-1", "currency": "USD", "amount": "2000",
                           "status": "SETTLED",
                           "created_at": created.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                           "reference": "from the sandbox",
                           "statement_ref": "from the sandbox"}],
            raising=True)
        results = intake.poll_deposits(workspaces.book("aaa"))
        assert [r.deposit_id for r in results] == ["dep-poll-1"], (
            "a fresh book's poll skipped a deposit that had just been created")

    def test_releasing_as_income_makes_the_bill_payable(self, books):
        """The whole chain, with no provider: cash -> spendable -> PAY."""
        from app.domain.ledger import resolve_income
        from app.domain.treasury import current_plan
        from app.models import Inflow

        build_book("aaa", floor="0", limit="2500")
        deposit_in("aaa")

        def release(s):
            inflow = s.query(Inflow).first()
            resolve_income(s, inflow, note="no invoice will claim this", by="test")
        in_book("aaa", release)

        from app import workspaces
        plan = current_plan(workspaces.book("aaa"))
        pays = [s for s in plan.steps if s.action == "PAY" and s.payable_number == BILL]
        assert pays, f"no PAY step for {BILL}: {[(s.action, s.reason) for s in plan.steps]}"
        step = pays[0]
        assert not step.needs_approval, step.approval_reason
        assert not step.blockers, step.blockers


# --------------------------------------------------------- 2. the beneficiary
class TestTheBeneficiaryFormSaysWhatIsMissing:
    def test_a_us_local_supplier_with_no_address_names_the_missing_fields(
            self, books):
        """Airwallex's own words are a schema dump, not a message.

        Verified live: a US LOCAL beneficiary with no address is refused with
        "The request failed our schema validation. Refer to the errors object
        ...", naming beneficiary.address.postcode / street_address / city /
        state. A tester read that as a required-field prompt and was stuck.
        """
        from app.domain.beneficiary_form import validate_and_create
        from app.domain.workspace import WorkspaceRefused

        form = {"name": "Atlanta Packaging", "country": "US", "currency": "USD",
                "payout_method": "LOCAL", "entity_type": "COMPANY",
                "account_name": "Atlanta Packaging", "account_number": "1234567890",
                "routing_value": "021000021", "bank_account_category": "Checking"}
        outcome = validate_and_create(None, form, beneficiaries=None)
        assert outcome.ok is False
        msg = outcome.error or ""
        assert "postcode" in msg.lower() and "street" in msg.lower(), msg
        assert "City" in msg, msg
        assert "State" in msg, msg
        # And it must say what to DO, not just what is missing.
        assert "1 Peachtree St" in msg or "Atlanta" in msg, (
            "the refusal carries no example of an input that works")

    def test_the_address_is_required_only_as_a_whole(self, books):
        """A complete address passes the local check with no provider call."""
        from app.domain.beneficiary_form import missing_address_fields

        complete = {"country": "US", "city": "Atlanta",
                    "street_address": "1 Peachtree St", "postcode": "30303",
                    "state": "GA"}
        assert missing_address_fields(complete) == []
        partial = {"country": "US", "city": "Atlanta"}
        assert set(missing_address_fields(partial)) == {
            "street_address", "postcode", "state"}

    def test_the_setup_form_shows_one_known_good_input(self, books):
        html = page("aaa", "setup")
        assert "1 Peachtree St" in html, (
            "the Setup form does not document a beneficiary input that works")
        assert "30303" in html


# ------------------------------------------------------- 3. what is left to pay
class TestWhatIsStillToCollect:
    def test_a_partial_invoice_says_what_is_left_on_money_in(self, books):
        """After a partial payment the page must name the shortfall.

        A deposit of USD 800 onto a USD 1,200 invoice leaves USD 400 to chase,
        and "USD 800.00 of 1,200.00 · 66%" does not say that in words.
        """
        from app.domain.ledger import approve_inflow
        from app.models import Inflow

        build_book("aaa", invoice_minor=120_000)
        deposit_in("aaa", minor=80_000, deposit_id="dep-partial",
                   payer="Brightline Retail", reference="inv r6")

        def apply_it(s):
            inflow = s.query(Inflow).first()
            approve_inflow(s, inflow, approved_by="test")
        in_book("aaa", apply_it)
        text = visible(page("aaa", "money-in"))
        assert "400.00" in text, text[:600]
        assert "still to collect" in text, text[:600]

    def test_the_approval_message_names_the_amount_still_to_collect(self, books):
        """The post-approval message is where the operator looks first."""
        c = client()
        build_book("aaa", invoice_minor=120_000)
        deposit_in("aaa", minor=80_000, deposit_id="dep-partial",
                   payer="Brightline Retail", reference="inv r6")
        r = c.post(url("/inflows/1/approve", "aaa"),
                   data={"pin": PIN}, headers={"Accept": "application/json"},
                   follow_redirects=False)
        assert r.status_code == 200, r.status_code
        body = r.json()
        assert body.get("ok") is True, body
        message = body.get("message") or ""
        assert "400.00" in message, message
        assert "still to collect" in message, message


# ------------------------------------------------------------- 4. AI activity
class TestAiActivitySaysHowToGetADecision:
    def test_the_empty_state_says_how_to_get_a_decision(self, books):
        html = page("aaa", "ai")
        assert "No model calls yet" in visible(html), "the empty state changed shape"
        # THE EMPTY STATE'S OWN ELEMENT, not the page. An assertion that "Money
        # in" appears somewhere is satisfied by the navigation bar, which is on
        # every screen: a positive substring match that passes for a reason
        # unrelated to the thing under test.
        m = re.search(r'data-testid="ai-empty"[^>]*>(.*?)</p>', html, re.S)
        assert m, "the empty state has no element of its own to assert on"
        empty = " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())
        assert "Money in" in empty, empty
        assert "inflow" in empty.lower() or "deposit" in empty.lower(), empty

    def test_the_first_inflow_in_a_fresh_book_writes_a_trace_the_page_shows(
            self, books):
        build_book("aaa")
        deposit_in("aaa")
        text = visible(page("aaa", "ai"))
        assert "No model calls yet" not in text, (
            "the first inflow in a fresh book left the AI tab empty")
        assert "Brightline Retail" in text, text[:400]
        from app import workspaces
        from app.models import AgentTrace

        s = workspaces.book("aaa").session()
        try:
            assert s.query(AgentTrace).count() >= 1, "no trace was recorded"
        finally:
            s.close()
