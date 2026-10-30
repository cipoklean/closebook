"""TestSprite run 12 replay, two new findings.

1. TWO OPEN INVOICES OF THE SAME AMOUNT. A deposit of USD 4,200 from Lumen
   Stores, with INV-3100 and INV-3101 both open at USD 4,200, was matched to
   INV-3100 at 0.92 and paid. The payer was identified and the amount was real,
   but the amount cannot say WHICH invoice it settles -- and 0.92 clears the
   auto bar. The rule was answering a question it could not answer.

2. A CAPTION THAT OUTLIVED ITS TRUTH. After a held bill was approved and sent,
   Money out still said "Approved. Sends once USD 1,200.00 is free to spend
   (reserve floor USD 5,000.00 ...)" next to "Sent, the bank has scheduled it".
   The sentence is written when the plan defers, and nothing removed it when the
   transfer went out.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db import get_default_db, reset_default_db, session_scope
from app.domain.matching import Candidate, try_rule

PIN = "482913"          # test-only; the app's PIN never appears in a tracked file


# --------------------------------------------------------------------------- #
# 1. the matcher, asserted where it decides
# --------------------------------------------------------------------------- #
def _cand(invoice_id: int, number: str, minor: int,
          customer: str = "Lumen Stores") -> Candidate:
    return Candidate(invoice_id=invoice_id, number=number, customer_name=customer,
                     currency="USD", amount_open_minor=minor,
                     amount_original_minor=minor)


class TestTwoInvoicesOneAmount:
    def test_the_amount_alone_cannot_settle_two_identical_invoices(self):
        out = try_rule(4_200_00, "USD", "Lumen Stores",
                       [_cand(1, "INV-3100", 4_200_00),
                        _cand(2, "INV-3101", 4_200_00)])
        assert out is not None, "the rule should speak to this at all"
        assert out.status == "AMBIGUOUS", (
            f"a coin-flip between two identical invoices was reported as "
            f"{out.status} at {out.confidence}")
        assert out.allocations == [], "money was applied to a coin flip"
        assert out.confidence < 0.90, (
            f"confidence {out.confidence} clears the auto bar")

    def test_the_reason_names_both_candidates_and_asks(self):
        out = try_rule(4_200_00, "USD", "Lumen Stores",
                       [_cand(1, "INV-3100", 4_200_00),
                        _cand(2, "INV-3101", 4_200_00)])
        reason = out.reason or ""
        assert "USD 4,200.00" in reason, reason
        assert "INV-3100" in reason and "INV-3101" in reason, reason
        assert "which one?" in reason.lower(), (
            "the reason states a fact but asks for no decision: " + reason)

    def test_one_candidate_keeps_todays_behaviour(self):
        out = try_rule(4_200_00, "USD", "Lumen Stores",
                       [_cand(1, "INV-3100", 4_200_00)])
        assert out.status == "MATCHED"
        assert out.confidence >= 0.90
        assert [a.invoice_id for a in out.allocations] == [1]

    def test_two_different_amounts_still_match_the_one_that_fits(self):
        out = try_rule(4_200_00, "USD", "Lumen Stores",
                       [_cand(1, "INV-3100", 4_200_00),
                        _cand(2, "INV-3101", 9_900_00)])
        assert out.status == "MATCHED", out
        assert [a.invoice_id for a in out.allocations] == [1]

    def test_the_book_keeps_the_cash_when_it_escalates(self,):
        """Nothing applied means the invoices are untouched and the cash waits."""
        from app.domain.intake import record_and_match
        from app.domain.workspace import create_customer, create_invoice
        from app.models import InvoiceReceivable

        reset_default_db()
        db = get_default_db()
        with db.session_scope() as s:
            cust = create_customer(s, name="Lumen Stores")
            for number in ("INV-3100", "INV-3101"):
                create_invoice(s, customer_id=cust.id, number=number,
                               currency="USD", amount="4200.00")
        with db.session_scope() as s:
            out = record_and_match(s, deposit_id="dep-r13-1", currency="USD",
                                   amount_minor=4_200_00,
                                   payer_name="Lumen Stores",
                                   reference="payment for last month thanks",
                                   llm=False)
        with db.session_scope() as s:
            paid = {r.number: r.amount_paid_minor
                    for r in s.query(InvoiceReceivable).all()}
            from app.models import LedgerEntry

            # the ledger's own columns: a suspension is a CREDIT to
            # unapplied_cash, and there is no `amount_minor` on an entry
            suspense = sum(e.credit_minor for e in s.query(LedgerEntry)
                           .filter_by(account="unapplied_cash").all())
        assert all(v == 0 for v in paid.values()), paid
        assert out.status in ("AMBIGUOUS",), out
        assert suspense == 4_200_00, (
            "the deposit must still be sitting in suspense, not applied: "
            f"{suspense}")
        reset_default_db()


# --------------------------------------------------------------------------- #
# 2. the caption must not outlive the transfer
# --------------------------------------------------------------------------- #
class _FakeTransfers:
    def create(self, db_session, **kw):
        return {"id": "tr-r13-1", "status": "SENT"}

    def find_by_request_id(self, request_id):
        return None

    def status_of(self, transfer_id):
        return "SCHEDULED"


class _FakeClient:
    def __getattr__(self, name):
        raise AssertionError(f"the fake client was used for {name}")


@pytest.fixture(autouse=True)
def _isolated():
    reset_default_db()
    yield
    reset_default_db()


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def _over_limit_book(pin: str = PIN) -> None:
    from app.domain.ledger import post
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import create_payable, create_supplier, \
        record_beneficiary

    with session_scope() as s:
        save_settings(s, budget_currency="USD", reserve_floor="5000",
                      auto_pay_limit="2500", fx_tolerance_pct="0.50")
        post(s, "income:USD:r13", [("cash:USD", 900_000, 0, "USD"),
                                   ("opening_balance", 0, 900_000, "USD")])
        sup = create_supplier(s, name="Atlanta Packaging", country="US",
                              currency="USD", payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"seeded": True},
                           beneficiary_id="ben-r13", status="CREATED")
        create_payable(s, supplier_id=sup.id, currency="USD", amount="2600.00",
                       number="PO-2600")


def _short_cash_book() -> None:
    """USD 6,000 cash, USD 5,000 cushion, so an approved bill waits on cash."""
    from app.domain.ledger import post
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import (
        create_payable, create_supplier, record_beneficiary,
    )

    with session_scope() as s:
        save_settings(s, budget_currency="USD", reserve_floor="5000",
                      auto_pay_limit="2500", fx_tolerance_pct="0.50")
        post(s, "income:USD:r13", [("cash:USD", 600_000, 0, "USD"),
                                   ("opening_balance", 0, 600_000, "USD")])
        sup = create_supplier(s, name="Atlanta Packaging", country="US",
                              currency="USD", payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"seeded": True},
                           beneficiary_id="ben-r13", status="CREATED")
        create_payable(s, supplier_id=sup.id, currency="USD",
                       amount="2600.00", number="PO-2600")


class TestNoStaleCaptionAfterApproval:
    def _plan(self, monkeypatch):
        from app.domain import conductor
        from app.domain.executor import Executor

        class FakeExecutor(Executor):
            def __init__(self, *a, **kw):
                kw["client"] = _FakeClient()
                kw["transfers"] = _FakeTransfers()
                super().__init__(*a, **kw)

        monkeypatch.setattr(conductor, "Executor", FakeExecutor)
        return conductor.on_event(get_default_db(), source="test", budget_s=20,
                                  execute=True, dry_run=False)

    def _bill(self, ctx):
        return next((x for x in ctx["payables"] if x["number"] == "PO-2600"), None)

    @staticmethod
    def _said(bill: dict) -> str:
        """Everything the card can read about this bill, in one string."""
        bits = [str(bill.get("plan_approval_reason") or ""),
                str(bill.get("plan_reason") or "")]
        bits += [str(b) for b in (bill.get("plan_blockers") or [])]
        return " | ".join(bits)

    def test_the_promise_goes_once_the_step_has_a_transfer(self, monkeypatch):
        """The exact stale case: approved, waiting on cash, then sent anyway."""
        from app.dashboard import build_context
        from app.domain.approval_api import approve_step
        from app.models import PlanStep

        _short_cash_book()          # USD 6,000 against a USD 5,000 cushion
        self._plan(monkeypatch)
        with get_default_db().session_scope() as s:
            step_id = (s.query(PlanStep)
                       .filter(PlanStep.step_key.like("PAY:PO-2600%"))
                       .first().id)
        approve_step(get_default_db(), step_id, pin=PIN, approved_by="tester",
                     configured_pin=PIN)
        bill = self._bill(build_context([]))
        assert bill is not None
        assert "Sends once" in self._said(bill), (
            "this test needs the promise to exist before it can prove it goes: "
            f"{self._said(bill)!r}")

        # the money arrives and the transfer goes out, exactly as a completed
        # send records it
        with get_default_db().session_scope() as s:
            row = s.get(PlanStep, step_id)
            row.airwallex_object_id = "tr-r13-live"
            s.add(row)
            s.commit()

        gone = self._bill(build_context([]))
        assert "Sends once" not in self._said(gone), (
            "a step with a transfer still promises to send once cash arrives: "
            f"{self._said(gone)!r}")

    def test_the_waiting_on_cash_sentence_is_gone_from_the_page(self, client,
                                                               monkeypatch):
        from app.dashboard import build_context
        from app.domain.approval_api import approve_step
        from app.models import PlanStep

        _short_cash_book()
        self._plan(monkeypatch)
        with get_default_db().session_scope() as s:
            step_id = (s.query(PlanStep)
                       .filter(PlanStep.step_key.like("PAY:PO-2600%"))
                       .first().id)
        approve_step(get_default_db(), step_id, pin=PIN, approved_by="tester",
                     configured_pin=PIN)
        with get_default_db().session_scope() as s:
            row = s.get(PlanStep, step_id)
            row.airwallex_object_id = "tr-r13-live"
            s.add(row)
            s.commit()

        for tab in ("money-out", "home"):
            html = client.get(f"/app?tab={tab}").text
            assert "PO-2600" in html, f"{tab} does not show the bill at all"
            assert "Sends once" not in html, (
                f"{tab} still promises to send a payment that already went out")

    def test_no_surface_still_says_reserve_floor(self, client, monkeypatch):
        from app.dashboard import build_context
        from app.domain.approval_api import approve_step
        from app.models import PlanStep

        _short_cash_book()
        self._plan(monkeypatch)
        with get_default_db().session_scope() as s:
            step_id = (s.query(PlanStep)
                       .filter(PlanStep.step_key.like("PAY:PO-2600%"))
                       .first().id)
        approve_step(get_default_db(), step_id, pin=PIN, approved_by="tester",
                     configured_pin=PIN)
        bill = self._bill(build_context([]))
        note = self._said(bill)
        assert note, "the approved bill carries no explanation at all"
        assert "reserve floor" not in note, note
        assert "safety cushion" in note, note

    def test_the_note_builder_itself_uses_the_product_words(self):
        from app.domain.planner import _approved_waiting_note

        notes = _approved_waiting_note(True, -120_000, "USD",
                                       floor_minor=500_000)
        text = " ".join(notes)
        assert "safety cushion" in text, text
        assert "reserve floor" not in text, text


# --------------------------------------------------------------------------- #
# 3. a bill never reserves against itself, and nothing says "reserve floor"
# --------------------------------------------------------------------------- #
class TestABillDoesNotReserveAgainstItself:
    def test_an_approved_bill_already_in_flight_is_not_charged_twice(self):
        """USD 10,000, a USD 2,600 bill already reserved: nothing is short.

        The card said "paying it would leave cash USD 4,800.00, below the
        USD 5,000.00 floor" -- 2,600 of that 4,800 was the bill itself, counted
        once as committed and once as the payment.
        """
        from app.domain.planner import CashPosition, Payable, build_plan

        pos = CashPosition(balances={"USD": 1_000_000},
                           in_flight={"USD": 260_000},   # the approved bill
                           budget_currency="USD", floor_minor=500_000)
        bill = Payable(number="PO-2600", supplier="Atlanta Packaging",
                       currency="USD", amount_minor=260_000,
                       payout_method="LOCAL", has_valid_approval=True)
        plan = build_plan([bill], pos, budget=_budget())
        step = plan.steps[0]
        said = " | ".join(list(step.blockers) + [step.reason or ""])
        assert "below the" not in said, (
            "the bill is counted as committed AND as the payment: " + said)
        assert "4,800.00" not in said, said

    def test_a_bill_that_is_genuinely_short_still_says_so(self):
        """The fix must not silence a real shortfall."""
        from app.domain.planner import CashPosition, Payable, build_plan

        pos = CashPosition(balances={"USD": 300_000}, budget_currency="USD",
                           floor_minor=500_000)
        bill = Payable(number="PO-9", supplier="X", currency="USD",
                       amount_minor=260_000, payout_method="LOCAL")
        plan = build_plan([bill], pos, budget=_budget())
        said = " | ".join(list(plan.steps[0].blockers)
                          + [plan.steps[0].reason or ""])
        assert "floor" in said or "free to spend" in said, said


def _budget():
    from decimal import Decimal

    from app.domain.policy import BudgetSettings

    return BudgetSettings(currency="USD", reserve_floor_usd=Decimal("5000"),
                          auto_pay_limit_usd=Decimal("2500"))


class TestNoVisibleCopySaysReserveFloor:
    def test_no_screen_says_reserve_floor(self, client):
        for tab in ("home", "ok", "money-out", "setup", "money-in", "ai"):
            html = client.get(f"/app?tab={tab}").text.lower()
            assert "reserve floor" not in html, (
                f"{tab} still shows 'reserve floor' to a reader")

    def test_the_policy_card_says_safety_cushion(self, client):
        html = client.get("/app?tab=home").text
        assert "safety cushion" in html.lower(), (
            "the page never names the cushion at all")
