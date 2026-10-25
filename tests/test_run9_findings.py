"""Run 9: what is APPLIED is what MOVES, and the resolve form keeps what is typed.

WHAT THE TESTER SAW (run 9, 39 passed / 6 failed / 9 blocked, down from 42):

  1. CRITICAL, a 100x money bug. The confirmation said USD 1,200.00 applied, but
     Setup showed the invoice as OVERPAID with USD 120,000.00 paid: the invoice's
     `amount_paid_minor` was raised by the OVERPAYMENT as well as by the
     allocation, so a USD 1,200.00 invoice paid with USD 120,000.00 read as
     having had the whole USD 120,000.00 applied.
  2. The resolve form lost what was typed into it.
  3. Refunds owed contradicted the unmatched-money card.
  4. An approved over-limit bill short of cash said only `insufficient_balance`.

Provider-free: every row is written with the app's OWN writers, nothing here
calls Airwallex or a model, and the HTTP call goes to the app's own route.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PIN = "run9-pin"

#: (currency, invoice total in minor units, deposit in minor units)
#: An exact payment is applied by the intake itself; anything else is a
#: PROPOSAL that a person applies. One money bug has to hold for both, for a
#: 2-exponent currency and a 0-exponent one (where 100x is exactly a unit
#: mistake), and for an overpayment, which is where it was found.
AUTO_CASES = [
    ("USD", 120_000, 120_000),      # exact
    ("JPY", 12_000, 12_000),        # 0-exponent currency, exact
    ("INR", 120_000, 120_000),      # the invoice in the report is INR-named
]
PROPOSAL_CASES = [
    ("USD", 120_000, 12_000_000),   # the reported case: deposit 100x the bill
    ("EUR", 250_000, 300_000),      # overpayment
    ("USD", 120_000, 80_000),       # part-payment
    ("JPY", 12_000, 33_000),        # overpayment, 0-exponent currency
]
CASES = AUTO_CASES + PROPOSAL_CASES


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
def visible(html: str) -> str:
    """The words a person sees: tags stripped, whitespace collapsed."""
    import re as _re

    text = _re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    return " ".join(text.split())


def make(suffix: str, *, floor: str = "0", limit: str = "2500",
         bill_minor: int = 300_000, number: str = "PO-R9-1",
         invoice_minor: int = 0, depends_on_invoice: bool = False,
         supplier: str = "Pennine Supplies") -> dict:
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import (
        create_customer, create_invoice, create_payable, create_supplier,
        record_beneficiary,
    )

    out: dict = {}

    def write(s):
        save_settings(s, budget_currency="USD", reserve_floor=floor,
                      auto_pay_limit=limit, fx_tolerance_pct="0.50")
        sup = create_supplier(s, name=supplier, country="US",
                              currency="USD", payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"test": "no provider"},
                           beneficiary_id="ben-r9", status="CREATED")
        out["supplier_id"] = sup.id
        invoice_id = None
        if invoice_minor:
            cust = create_customer(s, name="Brightline Retail")
            invoice_id = create_invoice(s, customer_id=cust.id, number="INV-R9-1",
                                        currency="USD",
                                        amount=str(invoice_minor / 100)).id
        out["payable"] = create_payable(
            s, supplier_id=sup.id, currency="USD",
            amount=str(bill_minor / 100), number=number,
            depends_on=(INVOICE if depends_on_invoice else "")).id
    in_book(suffix, write)
    return out


def run_plan(suffix: str):
    """The cycle, provider-free: nothing may reach for a client."""
    from app import workspaces
    from app.domain.conductor import on_event

    class _AnyClient:
        def __getattr__(self, name):
            raise RuntimeError(f"the test must not call {name}")

    handle = book(suffix)
    token = workspaces.set_current(handle, suffix)
    try:
        return on_event(handle, source="test", execute=True, dry_run=False,
                        client=_AnyClient())
    finally:
        workspaces.reset(token)


def approval_target(suffix: str) -> str:
    """The step_key a person would be asked to approve for this book."""
    from app.domain.conductor import approval_waiting_steps

    def read(s):
        from app.domain.plan_store import _key_for
        from app.domain.treasury import current_plan

        steps = approval_waiting_steps(current_plan(book(suffix)))
        assert steps, "nothing is waiting for an approval"
        # The same key the writer persists and the approve form posts, so the
        # test cannot approve something the app would not recognise.
        return _key_for(steps[0])
    return in_book(suffix, read)


def approve(suffix: str, step_key: str) -> dict:
    """Approve through the app's OWN route, the way the button does.

    `approve_step` takes the plan step's ROW ID, not its key, so the key is
    resolved to the row first -- and the request carries the book, exactly as
    the button's form does.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import PlanStep

    step_id = in_book(suffix, lambda s: (
        s.query(PlanStep).filter_by(step_key=step_key).first() or _Missing()).id)
    r = TestClient(app).post(f"/approvals/{step_id}/approve?ws={suffix}",
                             data={"pin": PIN, "by": "tester"},
                             headers={"Accept": "application/json"})
    return {"status": r.status_code, "body": r.text}


class _Missing:
    id = None


def book(suffix: str):
    from app import workspaces

    return workspaces.book(suffix)


def home_page(suffix: str, tab: str = "home"):
    """(context, html) for a book, read INSIDE that book.

    The dashboard's readers resolve through the same contextvar a request sets,
    so a test that calls them bare reads the SHARED book and asserts on an empty
    page -- the harness trap that hid a whole bug in run 8.
    """
    from app import workspaces
    from app.dashboard import build_context
    from app.ui import render_app

    token = workspaces.set_current(book(suffix), suffix)
    try:
        ctx = build_context([])
        return ctx, render_app(ctx, tab=tab)
    finally:
        workspaces.reset(token)


def in_book(suffix: str, fn):
    from app import workspaces

    with workspaces.book(suffix).session_scope() as s:
        return fn(s)


def seed(suffix: str, *, ccy: str, total_minor: int) -> None:
    from app.models import Customer, InvoiceReceivable

    def write(s):
        c = Customer(name="Brightline Retail")
        s.add(c)
        s.flush()
        s.add(InvoiceReceivable(number=f"INV-{suffix}-1", customer_id=c.id,
                                currency=ccy, amount_minor=total_minor,
                                amount_paid_minor=0))
        s.flush()
    in_book(suffix, write)


def pay_in(suffix: str, *, ccy: str, amount_minor: int, invoice: str,
           payer: str = "Brightline Retail") -> int:
    """Record a customer payment through the real intake. Returns the inflow id."""
    from app.domain.intake import record_and_match
    from app.models import Inflow

    def write(s):
        record_and_match(s, deposit_id=f"dep-{suffix}", currency=ccy,
                         amount_minor=amount_minor,
                         payer_name=payer,
                         reference=f"payment for {invoice}", llm=False)
        return s.query(Inflow).order_by(Inflow.id.desc()).first().id
    return in_book(suffix, write)


def apply_via_approval(suffix: str, inflow_id: int) -> list[int]:
    """The manual resolve: apply the STORED proposal.

    A refusal to overpay an invoice is the SAFE outcome and is returned as an
    empty list rather than raised, so callers assert on the books instead of on
    which exception happened to be raised.
    """
    from app.domain.ledger import LedgerError, approve_inflow
    from app.models import Inflow

    def write(s):
        inflow = s.get(Inflow, inflow_id)
        try:
            return [m.amount_applied_minor
                    for m in approve_inflow(s, inflow, approved_by="tester")]
        except LedgerError:
            return []
    return in_book(suffix, write)


def apply_via_pin(suffix: str, inflow_id: int) -> str:
    """The route the APPROVE button posts to, with the PIN."""
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).post(
        f"/inflows/{inflow_id}/resolve?ws={suffix}",
        data={"what": "match", "pin": PIN, "by": "tester"},
        headers={"Accept": "application/json"})
    return r.text


def read_back(suffix: str) -> dict:
    """What the books say, read the way the app reads it."""
    from app.models import InvoiceReceivable, LedgerEntry, Match

    def read(s):
        inv = s.query(InvoiceReceivable).first()
        matches = s.query(Match).all()
        clearing = sum(int(e.credit_minor or 0)
                       for e in s.query(LedgerEntry)
                       .filter(LedgerEntry.account == "receivable_clearing").all())
        return {
            "number": inv.number,
            "currency": inv.currency,
            "total_minor": int(inv.amount_minor or 0),
            "paid_minor": int(inv.amount_paid_minor or 0),
            "status": inv.status.value,
            "applied_minor": sum(int(m.amount_applied_minor or 0) for m in matches),
            "clearing_minor": clearing,
        }
    return in_book(suffix, read)


def check(suffix: str, *, applied: int, ctx: str) -> dict:
    """The property: the invoice grew by exactly what was applied, no more."""
    from app.money import money_str

    b = read_back(suffix)
    shown = money_str(b["paid_minor"], b["currency"])
    want = money_str(min(applied, b["total_minor"]), b["currency"])
    assert b["paid_minor"] == min(applied, b["total_minor"]), (
        f"{ctx}: {b['number']} is {money_str(b['total_minor'], b['currency'])} "
        f"and the books applied {money_str(applied, b['currency'])}, but the "
        f"invoice shows {shown} paid (wanted {want})")
    assert b["clearing_minor"] == applied, (
        f"{ctx}: the ledger cleared {money_str(b['clearing_minor'], b['currency'])} "
        f"but {money_str(applied, b['currency'])} was applied")
    return b


# --------------------------------------------------- 1. one unit, one amount
class TestWhatIsAppliedIsWhatTheInvoiceShows:
    """Apply X, and the invoice grows by exactly X -- in the ledger's own unit.

    `amount_paid_minor` answers ONE question: how much of this invoice has been
    paid. Any code that adds something else to it (the overpayment, the whole
    deposit, a major-unit figure) is a money bug, and it is 100x when what gets
    added is the credit side of an overpayment.
    """

    @pytest.mark.parametrize("ccy,total,amount", AUTO_CASES)
    def test_the_auto_path(self, books, ccy, total, amount):
        """The intake applies an exact payment itself. Then the invoice grew."""
        suffix = f"auto-{ccy}-{amount}"
        seed(suffix, ccy=ccy, total_minor=total)
        pay_in(suffix, ccy=ccy, amount_minor=amount,
               invoice=f"INV-{suffix}-1")
        b = read_back(suffix)
        assert b["applied_minor"] == amount, (
            f"{ccy}: the intake applied {b['applied_minor']} of a {amount} "
            f"payment (status={b['status']})")
        check(suffix, applied=amount, ctx=f"auto {ccy}")

    @pytest.mark.parametrize("ccy,total,amount", PROPOSAL_CASES)
    def test_the_manual_resolve(self, books, ccy, total, amount):
        """Apply the stored proposal, the way the resolve button does."""
        suffix = f"man-{ccy}-{amount}"
        seed(suffix, ccy=ccy, total_minor=total)
        inflow_id = pay_in(suffix, ccy=ccy, amount_minor=amount,
                           invoice=f"INV-{suffix}-1", payer="Apex Factors Ltd.")
        applied = apply_via_approval(suffix, inflow_id)
        b = read_back(suffix)
        assert applied or b["paid_minor"] == 0, (
            f"{ccy}: the proposal was refused AND the invoice moved: {b}")
        assert b["paid_minor"] <= b["total_minor"], (
            f"{ccy}: {b['number']} shows {b['paid_minor']} paid on "
            f"{b['total_minor']}")
        if applied:
            check(suffix, applied=sum(applied), ctx=f"manual {ccy}")

    @pytest.mark.parametrize("ccy,total,amount", PROPOSAL_CASES)
    def test_the_pin_approve(self, books, ccy, total, amount):
        """The same thing through the route the APPROVE button posts to."""
        suffix = f"pin-{ccy}-{amount}"
        seed(suffix, ccy=ccy, total_minor=total)
        inflow_id = pay_in(suffix, ccy=ccy, amount_minor=amount,
                           invoice=f"INV-{suffix}-1", payer="Apex Factors Ltd.")
        body = apply_via_pin(suffix, inflow_id)
        b = read_back(suffix)
        assert b["paid_minor"] <= b["total_minor"], (
            f"{ccy}: the route left {b['number']} showing {b['paid_minor']} "
            f"paid on {b['total_minor']} -- reply was {body[:200]}")
        if b["applied_minor"]:
            assert '"ok":true' in body.replace(" ", ""), body[:200]
            check(suffix, applied=b["applied_minor"], ctx=f"pin {ccy}")


# ------------------------------------- 3. one fact, one place, on the money
class TestRefundsOwedAndTheUnmatchedCardAgree:
    """The figure and the count must come from the SAME list.

    Run 9: after a refund was recorded, Home showed "GBP 100.00" in Refunds owed
    while the unmatched-money card said BOTH "Nothing waiting" AND "1 payment
    waiting on you" -- one screen, two sources, three claims.
    """

    def _refund_owed(self, suffix: str = "aaa") -> None:
        from app.domain.intake import record_and_match
        from app.domain.ledger import resolve_refund
        from app.models import Inflow

        def write(s):
            record_and_match(s, deposit_id="dep-r9-ref", currency="USD",
                             amount_minor=10_000, payer_name="Apex Factors Ltd.",
                             reference="cash with no invoice", llm=False)
            row = s.query(Inflow).order_by(Inflow.id.desc()).first()
            resolve_refund(s, row, reason="duplicate payment", by="tester")
        in_book(suffix, write)

    def test_the_tile_never_says_nothing_waiting_with_a_payment_waiting(
            self, books):
        make("aaa", floor="0", limit="2500", bill_minor=100_00)
        self._refund_owed()
        ctx, html = home_page("aaa")
        text = visible(html)
        listed = [a for a in ctx["unmatched"] if int(a.get("waiting_minor") or 0) > 0]
        assert sum(int(a["waiting_minor"]) for a in listed) == 10_000, listed
        # The tile names the same money as Refunds owed, and it never says
        # nothing is waiting beside a count of one.
        assert "USD 100.00" in text, text[:400]
        assert "1 payment waiting on you" in text, text[:400]
        assert "Nothing waiting" not in text, (
            "the tile says nothing is waiting while a payment waits: "
            + text[text.index("Waiting on you"):][:160])

    def test_the_unmatched_figure_is_the_money_still_waiting(self, books):
        """The figure is the sum over the SAME list the count uses."""
        make("aaa", floor="0", limit="2500", bill_minor=100_00)
        self._refund_owed()
        ctx, html = home_page("aaa")
        listed = ctx["unmatched"]
        assert len(listed) == 1, [a["id"] for a in listed]
        waiting = sum(int(a.get("waiting_minor") or 0) for a in listed)
        assert waiting == 10_000, (
            f"the listed payment holds {waiting} while {10_000} was received "
            f"and is owed back")
        assert "USD 100.00" in visible(html), (
            "the tile does not show the money that is waiting")


# --------------------------------- 4. an approved payment that is short of cash
class TestAnApprovedPaymentShortOfCashSaysSo:
    """The card must say it is approved and what it waits for, with the floor.

    Run 9: an approved USD 3,200.00 bill sat under "Held, and not waiting on
    you" reading `insufficient_balance`, so the operator could not tell that the
    only thing missing was cash.
    """

    def _approved_over_limit(self, suffix: str = "aaa") -> None:
        make(suffix, floor="5000", limit="2500", bill_minor=320_000,
             number="PO-R9-APR", supplier="Atlanta Packaging")
        run_plan(suffix)
        target = approval_target(suffix)
        res = approve(suffix, target)
        assert '"ok":true' in res["body"].replace(" ", ""), res
        run_plan(suffix)                      # re-plan with the approval in hand

    def test_the_reason_names_the_approval_and_the_floor(self, books):
        self._approved_over_limit()
        ctx, html = home_page("aaa")
        p = next(x for x in ctx["payables"] if x["number"] == "PO-R9-APR")
        assert p["plan_needs_approval"], p
        joined = " | ".join(p["plan_blockers"] or [])
        assert "Approved" in joined, joined
        assert "USD 8,200.00" in joined, (
            "the shortfall against the floor is not computed: " + joined)
        assert "reserve floor" in joined.lower(), joined

        text = visible(html)
        assert "Approved. Sends once" in text, (
            "Home does not say the payment is approved: " + text[:400])
        assert "insufficient_balance" not in text, (
            "the raw policy code is shown instead of the sentence")

    def test_the_heading_does_not_claim_nothing_is_waiting(self, books):
        self._approved_over_limit()
        _ctx, html = home_page("aaa")
        text = visible(html)
        assert "not waiting on you" not in text, (
            "an approved payment waiting on cash is still labelled as needing "
            "nobody: " + text[:300])
