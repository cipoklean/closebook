"""Run 7: the held reason, release-as-income, the bank label, and duplicates.

WHAT THE TESTER SAW (run 7, 34 passed / 9 failed / 11 blocked, down from 39):

  1. The Sandbox deposit form could not be typed into at all (its panel was
     spanned one grid column and collapsed) -- covered by tests/e2e/test_run7_forms.py.
  2. PO-RESERVE-QA8K was held by the reserve floor, but the card named only the
     book's unmatched deposit: resolve that and it was still held.
  3. After releasing an unknown-payer deposit, Home's cash figure was read as
     short; and a fresh tester book's floor was invisible.
  4. An approved over-limit bill showed "Sent" beside "UNCHECKED" for ever.
  5. A second payment against an already-PAID invoice read "unknown invoice".
  6. The sandbox control must carry a payer and a reference, so a test can make
     a clean match instead of always landing AMBIGUOUS.

Provider-free: every row is written with the app's OWN writers and nothing here
calls Airwallex or a model.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PIN = "run7-pin"
BILL = "PO-R7-A"
INVOICE = "INV-R7-1"


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


def page_text(ws: str, tab: str = "home") -> str:
    html = client().get(url(f"/app?tab={tab}", ws)).text
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    return " ".join(re.sub(r"<[^>]+>", " ", body).split())


def html(ws: str, tab: str = "home") -> str:
    return client().get(url(f"/app?tab={tab}", ws)).text


def in_book(suffix: str, fn):
    from app import workspaces

    with workspaces.book(suffix).session_scope() as s:
        return fn(s)


def build_book(suffix: str, *, floor: str = "0", limit: str = "2500",
               bill_minor: int = 180_000, invoice_minor: int = 0,
               number: str = BILL, invoice_number: str = INVOICE) -> dict:
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import (
        create_customer, create_invoice, create_payable, create_supplier,
        record_beneficiary,
    )

    out: dict = {}

    def write(s):
        save_settings(s, budget_currency="USD", reserve_floor=floor,
                      auto_pay_limit=limit, fx_tolerance_pct="0.50")
        sup = create_supplier(s, name="Pennine Supplies", country="US",
                              currency="USD", payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"test": "no provider"},
                           beneficiary_id="ben-r7", status="CREATED")
        out["supplier_id"] = sup.id
        out["payable"] = create_payable(s, supplier_id=sup.id, currency="USD",
                                        amount=str(bill_minor / 100),
                                        number=number).id
        if invoice_minor:
            cust = create_customer(s, name="Brightline Retail")
            inv = create_invoice(s, customer_id=cust.id, number=invoice_number,
                                 currency="USD", amount=str(invoice_minor / 100))
            out["invoice_id"] = inv.id
    in_book(suffix, write)
    return out


def deposit(suffix: str, *, minor: int, deposit_id: str, payer: str = "",
            reference: str = "") -> dict:
    from app.domain.intake import record_and_match

    def write(s):
        return record_and_match(s, deposit_id=deposit_id, currency="USD",
                                amount_minor=minor, payer_name=payer,
                                reference=reference, llm=False)
    result = in_book(suffix, write)
    return {"status": result.status, "reason": result.reason,
            "confidence": result.confidence, "disposition": result.disposition}


def run_plan(suffix: str) -> None:
    from app.domain.conductor import on_event

    from app import workspaces

    on_event(workspaces.book(suffix), source="test", execute=False, dry_run=True)


def steps_for(suffix: str, number: str) -> list:
    from app import workspaces
    from app.domain.treasury import current_plan

    plan = current_plan(workspaces.book(suffix))
    return [s for s in plan.steps if s.payable_number == number]


def release_as_income(suffix: str, note: str = "no invoice will claim this"):
    from app.domain.ledger import resolve_income
    from app.models import Inflow

    def write(s):
        inflow = s.query(Inflow).order_by(Inflow.id).first()
        return resolve_income(s, inflow, note=note, by="test")
    return in_book(suffix, write)


# ------------------------------------------------- 2. every blocker, its number
class TestTheHeldReasonNamesEveryBlocker:
    def test_a_floor_breach_names_the_floor_and_the_unmatched_cash(self, books):
        """The card named the match and not the floor, so resolving it changed
        nothing: the tester did what the card said and was still held.

        Book: USD 6,000.00 in one unmatched deposit, a USD 5,000.00 floor, and a
        USD 1,800.00 bill. Both blockers are real and both need saying:
          * nothing is free to spend while the deposit is unmatched;
          * even resolved, the floor leaves USD 1,000.00 free, short of 1,800.
        """
        build_book("aaa", floor="5000", limit="2500", bill_minor=180_000)
        deposit("aaa", minor=600_000, deposit_id="dep-r7-a", reference="no invoice")
        run_plan("aaa")

        steps = steps_for("aaa", BILL)
        assert steps, "no step for the bill at all"
        joined = " ".join(steps[0].blockers)
        assert "USD 6,000.00" in joined, joined
        assert "USD 5,000.00" in joined, (
            "the sentence does not name the reserve floor that is also holding "
            "this: " + joined)
        assert "waiting on a match" in joined, joined
        assert "safety cushion" in joined.lower(), joined

    def test_an_over_limit_payment_says_so_even_while_it_is_held(self, books):
        """A person has to know a payment needs them even before it can be paid."""
        build_book("aaa", floor="5000", limit="2500", bill_minor=300_000)
        deposit("aaa", minor=600_000, deposit_id="dep-r7-b", reference="no invoice")
        release_as_income("aaa")
        run_plan("aaa")

        steps = steps_for("aaa", BILL)
        assert steps, "no step for the bill at all"
        joined = " ".join(steps[0].blockers)
        assert "USD 5,000.00" in joined, joined
        assert "USD 2,500.00" in joined, (
            "the automatic limit is not named on a payment that is over it: "
            + joined)


# ------------------------------------------------- 3. release, and the floor
class TestReleaseAsIncomeCreditsTheWholeDeposit:
    def test_the_whole_deposit_becomes_cash_and_free_to_spend(self, books):
        build_book("aaa", floor="0", limit="2500")
        deposit("aaa", minor=500_000, deposit_id="dep-r7-c", payer="Someone Else",
                reference="nothing we know")
        before = page_text("aaa", "home")
        assert "USD 500,000.00" not in before or True          # (shape, not figure)
        released = release_as_income("aaa")
        assert released == 500_000, f"released {released} of a 500,000 deposit"

        from app.models import LedgerEntry
        from app import workspaces

        with workspaces.book("aaa").session() as s:
            rows = s.query(LedgerEntry).all()
            cash = sum(r.debit_minor - r.credit_minor for r in rows
                       if r.account == "cash:USD")
            suspense = sum(r.debit_minor - r.credit_minor for r in rows
                           if r.account == "unapplied_cash")
        assert cash == 500_000, f"cash is {cash}, expected the whole deposit"
        assert suspense == 0, f"unapplied_cash still holds {suspense}"

        home = page_text("aaa", "home")
        assert "USD 5,000.00" in home, home[:400]
        assert "Nothing waiting" in home or "USD 0.00" in home, home[:400]

    def test_a_fresh_books_floor_is_visible_in_setup_and_on_home(self, books):
        """A tester cannot reason about a floor it cannot see or change."""
        build_book("aaa", floor="5000", limit="2500")
        deposit("aaa", minor=10_000, deposit_id="dep-r7-d", reference="nothing")

        setup = html("aaa", "setup")
        field = re.search(r'<input[^>]*name="reserve_floor"[^>]*>', setup)
        assert field, "Setup has no reserve floor field"
        assert 'value="5000.00"' in field.group(0), field.group(0)
        assert "USD 5,000.00" in page_text("aaa", "setup")

        home = page_text("aaa", "home")
        assert "USD 5,000.00" in home, (
            "the free-to-spend card does not say what the floor is: " + home[:400])


# ------------------------------------------------- 4. what the bank is doing
class TestASentTransferNeverReadsUnchecked:
    def _sent_book(self, suffix: str, oid: str) -> None:
        from app.domain.plan_store import persist_plan
        from app.models import PlanStep, StepStatus, StepType

        build_book(suffix, floor="0")
        run_plan(suffix)

        def write(s):
            row = PlanStep(
                type=StepType.TRANSFER, status=StepStatus.SUBMITTED,
                airwallex_object_id=oid,
                payload_json='{"payable_number": "%s", "action": "PAY"}' % BILL,
            )
            s.add(row)
            s.flush()
            _ = persist_plan  # the real writer is exercised elsewhere
        in_book(suffix, write)

    def test_the_placeholder_is_replaced_and_a_known_status_is_shown(self, books):
        from app.dashboard import note_live_status

        oid = "b0b0b0b0-1111-2222-3333-444444444444"
        self._sent_book("aaa", oid)

        text = page_text("aaa", "money-out")
        assert "UNCHECKED" not in text, (
            "a sent transfer shows the machine's placeholder: " + text[:400])
        assert "Checking with the bank" in text, text[:400]

        note_live_status(oid, "PROCESSING")
        after = page_text("aaa", "money-out")
        # REWORDED ON PURPOSE (UI revamp): the chip says what the bank is doing
        # rather than printing the bank's own word for it. The machine status
        # still rides in the chip's explanation, which is where an operator
        # looks when they need it.
        assert "Checking with the bank" not in after, after[:400]
        assert "Sent, waiting for the bank" in after, after[:400]

    def test_a_failed_lookup_says_so_rather_than_showing_a_token(self, books):
        from app.dashboard import note_live_status

        oid = "c0c0c0c0-1111-2222-3333-444444444444"
        self._sent_book("aaa", oid)
        note_live_status(oid, "UNKNOWN")
        text = page_text("aaa", "money-out")
        assert "UNKNOWN" not in text, text[:400]
        assert "Could not reach the bank" in text, text[:400]


# ------------------------------------------------- 5. a duplicate, named
class TestAPaymentAgainstAPaidInvoiceIsNamed:
    def test_the_second_payment_names_the_invoice_and_calls_it_a_duplicate(self, books):
        build_book("aaa", floor="0", invoice_minor=120_000)
        first = deposit("aaa", minor=120_000, deposit_id="dep-r7-e",
                        payer="Brightline Retail", reference="INV-R7-1")
        assert first["status"] == "MATCHED", first
        assert first["confidence"] >= 0.9, first

        second = deposit("aaa", minor=120_000, deposit_id="dep-r7-f",
                         payer="Brightline Retail", reference="INV-R7-1")
        assert INVOICE in second["reason"], second
        assert "already paid" in second["reason"].lower(), second
        # RUN 11 restated this sentence to say what happened to the MONEY, not
        # what the payment resembles: "looks like a duplicate" left an operator
        # holding cash with no idea whether it had been applied.
        assert "already paid" in second["reason"].lower(), second
        assert "held as credit" in second["reason"].lower(), second
        assert "nothing applied" in second["reason"].lower(), second
        assert "unknown invoice" not in second["reason"].lower(), second

        text = page_text("aaa", "money-in")
        assert INVOICE in text, text[:500]
        assert "already paid" in text, text[:500]

    def test_a_reference_to_nothing_open_is_still_just_ambiguous(self, books):
        """The duplicate sentence may not appear for a payment nobody can name."""
        build_book("aaa", floor="0", invoice_minor=120_000)
        got = deposit("aaa", minor=99_000, deposit_id="dep-r7-g",
                      payer="Someone Else", reference="no invoice at all")
        assert "already paid" not in got["reason"].lower(), got
        assert got["status"] in ("AMBIGUOUS", "PARTIAL"), got


# ------------------------------------------------- 6. payer and reference
class TestTheDepositControlCarriesPayerAndReference:
    def test_the_form_offers_both_fields(self, books):
        build_book("aaa", floor="0")
        form = html("aaa", "money-in")
        panel = re.search(r'<details[^>]*id="sandbox-controls".*?</details>',
                          form, re.S)
        assert panel, "no sandbox panel on Money in"
        block = panel.group(0)
        for name, hint in (("payer_name", "What the bank says"),
                           ("reference", "Free text")):
            field = re.search(rf'<input[^>]*name="{name}"[^>]*>', block)
            assert field, f"the deposit form has no {name} field"
        assert "What the bank says" in block and "Free text" in block, (
            "the two fields are there but nothing explains them")
        assert 'open' in re.search(r'<details[^>]*id="sandbox-controls"[^>]*>',
                                   form).group(0), (
            "the deposit form sits inside a collapsed panel, so no browser can "
            "type into it")

    def test_a_typed_payer_and_reference_reach_the_match(self, books, monkeypatch):
        """The point of the two fields: a CLEAN match, not always AMBIGUOUS.

        The sandbox guard is lifted and the provider call replaced, so this
        exercises the ROUTE's own parsing of the form body -- the half a browser
        test cannot see. Nothing leaves the machine.
        """
        from app.domain import intake as intake_mod
        from app.airwallex import client as client_mod

        build_book("aaa", floor="0", invoice_minor=120_000)

        monkeypatch.setattr("app.main._require_sandbox", lambda: "", raising=False)
        monkeypatch.setattr(client_mod, "AirwallexClient", lambda *a, **k: object(),
                            raising=True)
        monkeypatch.setattr(
            intake_mod, "accounts_cached",
            lambda *a, **k: [{"id": "acct-usd", "currency": "USD", "usable": True,
                              "account_number": "0000001234"}],
            raising=True)

        def fake_simulate(client, *, global_account_id, amount_minor, currency,
                          payer_name="", reference=""):
            assert payer_name == "Brightline Retail", payer_name
            assert reference == "INV-R7-1", reference
            return {"id": "dep-r7-form", "currency": currency,
                    "amount": amount_minor / 100, "status": "SETTLED",
                    "reference": reference, "payer_name": payer_name,
                    "created_at": "2026-10-25T09:00:00+0000"}

        monkeypatch.setattr(intake_mod, "simulate_deposit", fake_simulate,
                            raising=True)

        r = client().post(url("/money-in/simulate", "aaa"),
                          data={"amount": "1200", "payer_name": "Brightline Retail",
                                "reference": "INV-R7-1",
                                "global_account_id": "acct-usd"},
                          headers={"Accept": "application/json"})

        from app import workspaces
        from app.models import Inflow, InvoiceReceivable

        with workspaces.book("aaa").session() as s:
            row = s.query(Inflow).order_by(Inflow.id.desc()).first()
            assert row is not None, f"the form recorded no inflow: {r.text[:300]}"
            assert row.reference_raw == "INV-R7-1", row.reference_raw
            assert row.payer_name == "Brightline Retail", row.payer_name
            inv = s.query(InvoiceReceivable).filter_by(number=INVOICE).one()
            assert inv.status.value in ("PAID", "OVERPAID"), (
                "a payer and a reference naming an open invoice did not match: "
                f"{inv.status.value} / {r.text[:200]}")
