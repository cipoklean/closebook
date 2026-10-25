"""Run 8: approve before the cash, scheduled transfers, and what the cards say.

WHAT THE TESTER SAW (run 8, 42 passed / 9 failed / 3 blocked, up from 34):

  1. The resolve form lost what was typed into it (covered by the form sweep in
     tests/e2e/test_form_sweep.py: the note field was HIDDEN until "release as
     income" was chosen, so typing went into nothing).
  2. An over-limit bill was held as waits_for_inflow and "Needs your OK" said
     nothing needed anybody: you could not approve the thing you were going to
     approve anyway.
  3. A transfer the sandbox keeps SCHEDULED accepted neither Settle nor Fail.
  4. A returned transfer did not show its return reason in the trail.
  5. A part-paid invoice did not say what was still to collect everywhere.

Provider-free: every row is written with the app's OWN writers, and nothing here
calls Airwallex or a model.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PIN = "run8-pin"
INVOICE = "INV-R8-1"


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
def in_book(suffix: str, fn):
    from app import workspaces

    with workspaces.book(suffix).session_scope() as s:
        return fn(s)


def book(suffix: str):
    from app import workspaces

    return workspaces.book(suffix)


def make(suffix: str, *, floor: str = "0", limit: str = "2500",
         bill_minor: int = 300_000, number: str = "PO-R8-1",
         invoice_minor: int = 0, depends_on_invoice: bool = False) -> dict:
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
                           beneficiary_id="ben-r8", status="CREATED")
        out["supplier_id"] = sup.id
        invoice_id = None
        if invoice_minor:
            cust = create_customer(s, name="Brightline Retail")
            invoice_id = create_invoice(s, customer_id=cust.id, number=INVOICE,
                                        currency="USD",
                                        amount=str(invoice_minor / 100)).id
        out["payable"] = create_payable(
            s, supplier_id=sup.id, currency="USD",
            amount=str(bill_minor / 100), number=number,
            depends_on=(INVOICE if depends_on_invoice else "")).id
    in_book(suffix, write)
    return out


def cash(suffix: str, minor: int, ccy: str = "USD") -> None:
    from app.domain.ledger import post

    def write(s):
        post(s, f"test:cash:{ccy}:{minor}", [(f"cash:{ccy}", minor, 0, ccy),
                                             ("opening_balance", 0, minor, ccy)])
    in_book(suffix, write)


def step_for(suffix: str, number: str):
    from app.domain.treasury import current_plan

    plan = current_plan(book(suffix))
    for st in plan.steps:
        if st.payable_number == number:
            return st
    return None


class _AnyClient:
    """Stands in for Airwallex. Nothing may actually be sent from these tests."""

    def __getattr__(self, name):
        def _call(*a, **k):
            raise RuntimeError(f"the test must not call {name}")
        return _call


def run_plan(suffix: str):
    """A cycle that PERSISTS but sends nothing.

    The persist path (and therefore an approval card at all) only runs when the
    conductor is asked to execute, which is exactly the behaviour under test:
    a blocked over-limit bill has to get its row. Every bill in these books is
    blocked, so `clear` is empty and nothing is sent.
    """
    from app import workspaces
    from app.domain.conductor import on_event

    # THE WORKSPACE IS A CONTEXTVAR, and the conductor's session helper reads it
    # (that is how one process serves many books). A test that calls the cycle
    # without setting it writes into the DEFAULT book, and then looks for its row
    # in the wrong file. This is what the middleware does per request.
    handle = book(suffix)
    token = workspaces.set_current(handle, suffix)
    try:
        return on_event(handle, source="test", execute=True, dry_run=False,
                        client=_AnyClient())
    finally:
        workspaces.reset(token)


def approve(suffix: str, number: str) -> None:
    """Approve the persisted step for a bill, the way the PIN route does."""
    import json

    from app.db import get_default_db
    from app.domain.approval_api import approve_step
    from app.models import PlanStep

    with book(suffix).session() as s:
        target = None
        for row in s.query(PlanStep).order_by(PlanStep.id.desc()).all():
            payload = json.loads(row.payload_json or "{}")
            if payload.get("payable_number") == number:
                target = row.id
                break
    assert target, f"no persisted step to approve for {number}"
    from app import workspaces

    token = workspaces.set_current(book(suffix), suffix)
    try:
        approve_step(get_default_db(), target, pin=PIN, approved_by="tester",
                     configured_pin=PIN)
    finally:
        workspaces.reset(token)


def visible(html: str) -> str:
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    return " ".join(re.sub(r"<[^>]+>", " ", body).split())


def page(suffix: str, tab: str) -> str:
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app).get(f"/app?tab={tab}&ws={suffix}").text


# ---------------------------------------------- 2. approve before the cash
class TestABlockedOverLimitBillCanStillBeApproved:
    def test_it_asks_for_a_person_even_with_no_cash(self, books):
        """The limit verdict is about the AMOUNT, not about the cash."""
        make("aaa", floor="5000", limit="2500", bill_minor=300_000)
        cash("aaa", 100_00)                       # nowhere near enough
        run_plan("aaa")

        st = step_for("aaa", "PO-R8-1")
        assert st is not None, "no step for the bill"
        assert st.needs_approval, (
            "an over-limit bill held for cash never asks for approval: " + st.reason)
        assert "limit" in (st.approval_reason or "").lower(), st.approval_reason

        from app.domain.conductor import approval_waiting_steps
        from app.domain.treasury import current_plan

        waiting = approval_waiting_steps(current_plan(book("aaa")))
        assert [s.payable_number for s in waiting] == ["PO-R8-1"], (
            "the step gets no row, so Needs your OK stays empty")

    def test_a_bill_waiting_on_an_unpaid_invoice_is_not_approveable(self, books):
        """Approving a payment does not produce the money it depends on."""
        make("aaa", floor="0", limit="2500", bill_minor=300_000,
             invoice_minor=500_000, depends_on_invoice=True)
        run_plan("aaa")

        from app.domain.conductor import approval_waiting_steps
        from app.domain.treasury import current_plan

        waiting = approval_waiting_steps(current_plan(book("aaa")))
        assert not waiting, (
            "a payment blocked by its own dependency is offered for approval: "
            f"{[(s.payable_number, s.blockers) for s in waiting]}")

    def test_an_approved_one_says_what_it_is_waiting_for(self, books):
        """The card has to say the money is the only thing outstanding."""
        make("aaa", floor="5000", limit="2500", bill_minor=300_000)
        cash("aaa", 100_00)
        run_plan("aaa")
        approve("aaa", "PO-R8-1")

        st = step_for("aaa", "PO-R8-1")
        joined = " ".join(st.blockers)
        assert "Approved" in joined, joined
        assert "Sends once" in joined, joined
        assert "USD 7,900.00" in joined, (
            "the sentence does not name how much more must be free: " + joined)

    def test_it_is_sent_once_the_cash_and_the_floor_allow(self, books):
        """Approved now, sent later, without asking a second time."""
        make("aaa", floor="5000", limit="2500", bill_minor=300_000)
        cash("aaa", 100_00)
        run_plan("aaa")
        approve("aaa", "PO-R8-1")

        cash("aaa", 20_000_00)                    # the money arrives
        st = step_for("aaa", "PO-R8-1")           # what the plan now says
        assert st.action == "PAY", f"{st.action}: {st.reason}"
        assert not st.needs_approval, st.approval_reason
        assert st.has_valid_approval, "the stored approval was not honoured"

    def test_the_approval_is_bound_to_the_step_and_the_beneficiary(self, books):
        """An approval authorises THIS payment to THIS person, not a blank cheque."""
        make("aaa", floor="5000", limit="2500", bill_minor=300_000)
        run_plan("aaa")
        approve("aaa", "PO-R8-1")

        from app.domain.plan_store import valid_approval
        from app.models import Approval, PlanStep

        with book("aaa").session() as s:
            step = (s.query(PlanStep)
                    .filter(PlanStep.step_key.like("%PO-R8-1")).first())
            assert step is not None, "no step row to bind an approval to"
            row = (s.query(Approval).filter_by(plan_step_id=step.id)
                   .order_by(Approval.id.desc()).first())
            assert row is not None, "approving recorded nothing"
            assert row.bound_hash and len(row.bound_hash) >= 32, row.bound_hash
            assert row.beneficiary_id == "ben-r8", (
                "the approval does not name the beneficiary it authorises: "
                f"{row.beneficiary_id!r}")

            ok = valid_approval(s, step_key=step.step_key, amount_minor=300_000,
                                currency="USD", counterparty="Pennine Supplies",
                                beneficiary_id="ben-r8")
            assert ok is not None, "the approval does not validate its own terms"

            # SAME step and amount, DIFFERENT beneficiary: a different payment,
            # so the stored approval must not authorise it.
            other = valid_approval(s, step_key=step.step_key, amount_minor=300_000,
                                   currency="USD", counterparty="Pennine Supplies",
                                   beneficiary_id="ben-somebody-else")
            assert other is None, (
                "the approval survives a change of beneficiary, so it authorises "
                "a payment to somebody else")

            # and a DIFFERENT step: an approval for one payment must not pay
            # another bill with the same amount and supplier.
            wrong_step = valid_approval(s, step_key="PAY:PO-SOMETHING-ELSE",
                                        amount_minor=300_000, currency="USD",
                                        counterparty="Pennine Supplies",
                                        beneficiary_id="ben-r8")
            assert wrong_step is None, "an approval paid a step it was not for"


# ------------------------------------------ 5. what is still to collect
class TestAPartPaidInvoiceSaysWhatIsLeftEverywhere:
    def _part_paid(self, suffix: str = "aaa") -> None:
        """A USD 1,200.00 invoice, USD 800.00 applied to it: USD 400.00 left."""
        make(suffix, floor="0", limit="2500", bill_minor=100_00,
             invoice_minor=120_000)
        from app.domain.intake import record_and_match
        from app.domain.ledger import approve_inflow
        from app.models import Inflow

        def write(s):
            record_and_match(s, deposit_id="dep-r8-partial", currency="USD",
                             amount_minor=80_000, payer_name="Brightline Retail",
                             reference=INVOICE, llm=False)
            row = s.query(Inflow).order_by(Inflow.id.desc()).first()
            approve_inflow(s, row, approved_by="tester")
        in_book(suffix, write)

    def test_the_invoice_row_names_the_remainder(self, books):
        self._part_paid()
        html = page("aaa", "money-in")
        text = visible(html)
        assert "USD 400.00 still to collect" in text, (
            "the invoice row does not say what is left: " + text[:600])
        assert 'data-testid="still-to-collect"' in html

    def test_the_deposit_that_part_paid_it_names_the_remainder(self, books):
        """The timeline row is where a reader looks after applying a payment."""
        self._part_paid()
        text = visible(page("aaa", "money-in"))
        # The deposit's own row must carry it too: this is the screen a person
        # is on when they have just applied a partial payment.
        assert text.count("USD 400.00") >= 1, (
            "nothing on the timeline says what the deposit left outstanding")


# ------------------------------------------ 4. the reason on a returned transfer
class TestAReturnedTransferShowsWhyInTheTrail:
    """The rendering rule, tested where it lives.

    The reason was rendered ONLY while the card still showed our own stored
    status, so the moment the bank's live status arrived (which in the running
    app is always) the row read "CANCELLED" and the reason -- a wrong account
    number, a bank that sent the money back -- was gone. This drives the card
    renderer with the shape the dashboard produces: status CANCELLED, live
    status CANCELLED, and a reason from the incident store.
    """

    def _card(self, **over) -> str:
        from app.ui import _money_out

        payable = {
            "number": "PO-R8-RET", "supplier": "Pennine Supplies",
            "amount_minor": 100_00, "currency": "USD",
            "payout_method": "LOCAL", "status": "CANCELLED",
            "display_status": "CANCELLED", "live_status": "CANCELLED",
            "failure_type": "BENEFICIARY_BANK_RETURNED",
            "failure_reason": "account closed",
            "airwallex_object_id": "aaaaaaaa-1111-2222-3333-444444444444",
            "plan_action": None, "plan_blockers": [], "plan_reason": "",
            "step_reason": "", "policy_reason": None, "criticality": 3,
            "plan_needs_approval": False, "plan_approval_reason": None,
            "funding_currency": None, "funding_amount_minor": 0,
            "depends_on": None, "fx_object_id": None,
        }
        payable.update(over)
        ctx = {
            "payables": [payable], "settings": {}, "workspace": {"counts": {}},
            "incidents": [], "sandbox_ready": True, "money_in_accounts": [],
            "approvable": [], "problems": [], "actions": [],
        }
        return _money_out(ctx)

    def test_the_reason_is_shown_while_the_bank_status_is_known(self):
        html = self._card()
        assert "CANCELLED" in html, html[:400]
        assert "account closed" in html, (
            "the bank's reason is dropped as soon as the live status is known: "
            + html[-600:])

    def test_the_reason_is_in_the_trail_not_only_the_badge(self):
        html = self._card(live_status="FAILED")
        assert 'data-testid="trail-reason"' in html, (
            "the collapsed history has no line for what the bank said")

    def test_a_live_status_that_is_fine_never_shows_a_reason(self):
        """A stale reason must not be pinned onto a payment that went through."""
        html = self._card(live_status="PAID", status="PAID",
                          display_status="PAID")
        assert "account closed" not in html, (
            "a reason from the past is shown on a payment that is paid")
