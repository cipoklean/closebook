"""Run 11: a payment naming a settled invoice is credit, and captions survive.

WHAT THE TESTER SAW (run 11: 49 passed / 2 failed / 2 blocked of 53):

  1. A second deposit quoting INV-K7P2, already fully paid, was shown as
     "matched to INV-K7P2". The balance stayed settled, but nothing told the
     operator the invoice was already paid: the money simply vanished into a
     match that could not have applied anything.
  2. "Failed: ..." / "Returned: ..." captions lived in an in-memory list, so a
     restart of the tester erased them from every card.

Provider-free: the deposits go through the app's own intake, the invoices are
written by its own writers, and nothing here reaches a provider.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PIN = "r11-pin-1"
INVOICE = "INV-R11-1001"


def visible(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    return " ".join(text.split())


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


def in_book(suffix: str, fn):
    from app import workspaces

    with workspaces.book(suffix).session_scope() as s:
        return fn(s)


def book(suffix: str):
    from app import workspaces

    return workspaces.book(suffix)


def home_page(suffix: str, tab: str):
    from app import workspaces
    from app.dashboard import build_context
    from app.ui import render_app

    token = workspaces.set_current(book(suffix), suffix)
    try:
        ctx = build_context([])
        return ctx, render_app(ctx, tab=tab)
    finally:
        workspaces.reset(token)


def seed_invoice(suffix: str, *, minor: int = 120_000, ccy: str = "USD") -> None:
    from app.models import Customer, InvoiceReceivable

    def write(s):
        c = Customer(name="Brightline Retail")
        s.add(c)
        s.flush()
        s.add(InvoiceReceivable(number=INVOICE, customer_id=c.id, currency=ccy,
                                amount_minor=minor, amount_paid_minor=0))
        s.flush()
    in_book(suffix, write)


def deposit(suffix: str, *, minor: int, reference: str = INVOICE,
            ccy: str = "USD", payer: str = "Brightline Retail") -> int:
    from app.domain.intake import record_and_match
    from app.models import Inflow

    def write(s):
        record_and_match(s, deposit_id=f"dep-{suffix}-{minor}", currency=ccy,
                         amount_minor=minor, payer_name=payer,
                         reference=reference, llm=False)
        return s.query(Inflow).order_by(Inflow.id.desc()).first().id
    return in_book(suffix, write)


def invoice_state(suffix: str) -> tuple[int, str]:
    from app.models import InvoiceReceivable

    def read(s):
        inv = s.query(InvoiceReceivable).first()
        return int(inv.amount_paid_minor or 0), inv.status.value
    return in_book(suffix, read)


def inflow_state(suffix: str, inflow_id: int) -> dict:
    from app.models import Inflow

    def read(s):
        row = s.get(Inflow, inflow_id)
        return {"status": row.match_status.value, "method": row.match_method,
                "confidence": row.match_confidence,
                "reason": row.match_reason or ""}
    return in_book(suffix, read)


# ------------------------- 1. a payment naming a settled invoice is not a match
class TestAPaymentNamingAPaidInvoice:
    def test_it_lands_as_credit_and_says_so(self, books):
        seed_invoice("aaa", minor=120_000)
        first = deposit("aaa", minor=120_000)
        paid_after_first, status = invoice_state("aaa")
        assert (paid_after_first, status) == (120_000, "PAID"), (paid_after_first, status)

        # THE TESTER'S SECOND DEPOSIT: same reference, invoice already settled.
        second = deposit("aaa", minor=50_000)
        seen = inflow_state("aaa", second)
        assert seen["status"] != "MATCHED", (
            f"a deposit naming a paid invoice was recorded as {seen['status']}")

        paid_after_second, status2 = invoice_state("aaa")
        assert (paid_after_second, status2) == (120_000, "PAID"), (
            "the settled invoice moved: "
            f"paid {paid_after_second}, status {status2}")

        # AND THE SENTENCE, on the deposit row, in the page.
        _ctx, html = home_page("aaa", tab="money-in")
        text = visible(html)
        assert (f"{INVOICE} is already paid; USD 500.00 held as credit, "
                f"nothing applied.") in text, (
            "the deposit row does not say the invoice is already paid: "
            + text[:800])

    def test_the_sentence_is_on_ai_activity_too(self, books):
        seed_invoice("aaa", minor=120_000)
        deposit("aaa", minor=120_000)
        deposit("aaa", minor=50_000)
        _ctx, html = home_page("aaa", tab="ai")
        text = visible(html)
        assert "is already paid" in text, (
            "AI activity does not carry the duplicate sentence: " + text[:800])
        assert "USD 500.00 held as credit" in text, text[:800]


# ------------------------------- 2. captions come from the stored event rows
class TestTheCaptionSurvivesARestart:
    """`main.INCIDENTS` is a list in memory: a restart erased every caption."""

    def _cancelled_with_event(self, suffix: str = "aaa") -> str:
        from app.domain.incidents import record_bank_failure
        from app.models import PlanStep, StepStatus, StepType

        def write(s):
            from app.models import InvoicePayable, PayStatus, Supplier

            sup = s.query(Supplier).first()
            if sup is None:
                sup = Supplier(name="Pennine Supplies", currency="USD")
                s.add(sup)
                s.flush()
            if s.query(InvoicePayable).filter_by(number="PO-R11-RET").first() is None:
                s.add(InvoicePayable(number="PO-R11-RET", supplier_id=sup.id,
                                     currency="USD", amount_minor=10_000,
                                     status=PayStatus.HELD))
                s.flush()
            s.add(PlanStep(
                type=StepType.TRANSFER, status=StepStatus.CANCELLED,
                step_key="PAY:PO-R11-RET",
                airwallex_object_id="aaaa1111-2222-3333-4444-555566667777",
                payload_json=('{"payable_number": "PO-R11-RET", "action": "PAY",'
                              ' "amount_minor": 10000, "currency": "USD",'
                              ' "supplier": "Pennine Supplies"}')))
            s.flush()
            record_bank_failure(
                s, transfer_id="aaaa1111-2222-3333-4444-555566667777",
                kind="return", failure_type="BENEFICIARY_BANK_RETURNED",
                failure_reason="account closed")
            return "aaaa1111-2222-3333-4444-555566667777"
        return in_book(suffix, write)

    def test_the_reason_renders_with_no_in_memory_list(self, books):
        from app.dashboard import build_context
        from app.ui import render_app

        oid = self._cancelled_with_event()
        # A RESTART: nothing in memory, only what the database kept.
        import app.main as main

        main.INCIDENTS.clear()
        # build_context is given NO incidents, exactly as after a restart, and
        # is read INSIDE the book (the dashboard resolves through the same
        # contextvar a request sets).
        from app import workspaces

        token = workspaces.set_current(book("aaa"), "aaa")
        try:
            text = visible(render_app(build_context([]), tab="money-out"))
        finally:
            workspaces.reset(token)
        assert "Returned:" in text, (
            "the caption is gone after a restart: " + text[:800])
        assert "account closed" in text, text[:800]
        assert oid  # the id the event names
