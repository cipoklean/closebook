"""Run 10: a cancelled attempt must close cleanly, and the books must agree.

WHAT THE TESTER SAW (run 10, cebf3081: 44 passed / 8 failed / 1 blocked, up
from 39):

  1. IntegrityError after sandbox Fail/Return. The page said "Re-planning failed
     (IntegrityError); nothing was sent." and the re-plan rolled back. A raw
     exception class on the operator's screen is itself a defect.
  2. A refund owed left the payment showing as unmatched and waiting on me.
  3. A duplicate bill number in Setup was accepted silently.
  4. An approved bill held on unresolved cash did not say what it was waiting on.
  5. Home did not show the money held for review.

Provider-free: the fakes from `test_approved_step_is_sent` stand in for
Airwallex, and every row is written by the app's own writers.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db import get_default_db, reset_default_db, session_scope  # noqa: E402
from test_approved_step_is_sent import (  # noqa: E402
    FakeClient, FakeFX, FakeTransfers, _bill, _cash, _factory,
)

PIN = "2468"


@pytest.fixture()
def books(tmp_path, monkeypatch):
    """A private workspace root, so `?ws=` books never touch a real one."""
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


def in_book(suffix: str, fn):
    from app import workspaces

    with workspaces.book(suffix).session_scope() as s:
        return fn(s)


def book(suffix: str):
    from app import workspaces

    return workspaces.book(suffix)


def visible(html: str) -> str:
    import re as _re

    text = _re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    return " ".join(text.split())


def make(suffix: str, *, floor: str = "0", limit: str = "2500",
         bill_minor: int = 300_000, number: str = "PO-R10-1",
         supplier: str = "Pennine Supplies") -> None:
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import (
        create_payable, create_supplier, record_beneficiary,
    )

    def write(s):
        save_settings(s, budget_currency="USD", reserve_floor=floor,
                      auto_pay_limit=limit, fx_tolerance_pct="0.50")
        sup = create_supplier(s, name=supplier, country="US", currency="USD",
                              payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"test": "no provider"},
                           beneficiary_id="ben-r10", status="CREATED")
        create_payable(s, supplier_id=sup.id, currency="USD",
                       amount=str(bill_minor / 100), number=number)
    in_book(suffix, write)


def run_plan(suffix: str):
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
    from app.domain.conductor import approval_waiting_steps
    from app.domain.plan_store import _key_for
    from app.domain.treasury import current_plan

    def read(_s):
        steps = approval_waiting_steps(current_plan(book(suffix)))
        assert steps, "nothing is waiting for an approval"
        return _key_for(steps[0])
    return in_book(suffix, read)


def approve(suffix: str, step_key: str) -> dict:
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import PlanStep

    def find(_s):
        row = _s.query(PlanStep).filter_by(step_key=step_key).first()
        return row.id if row else None
    step_id = in_book(suffix, find)
    r = TestClient(app).post(f"/approvals/{step_id}/approve?ws={suffix}",
                             data={"pin": PIN, "by": "tester"},
                             headers={"Accept": "application/json"})
    return {"status": r.status_code, "body": r.text}


def home_page(suffix: str, tab: str = "home"):
    from app import workspaces
    from app.dashboard import build_context
    from app.ui import render_app

    token = workspaces.set_current(book(suffix), suffix)
    try:
        ctx = build_context([])
        return ctx, render_app(ctx, tab=tab)
    finally:
        workspaces.reset(token)


@pytest.fixture(autouse=True)
def _isolated():
    reset_default_db()
    FakeTransfers.ALL.clear()
    yield
    reset_default_db()
    FakeTransfers.ALL.clear()


def _cycle(db, transfers, *, execute=True):
    from app.domain.conductor import on_event
    return on_event(db, source="test", budget_s=30, execute=execute,
                    dry_run=not execute, client=FakeClient(),
                    executor_factory=_factory(transfers))


def _approve(step_id, *, pin=PIN):
    from app.domain.approval_api import approve_step
    return approve_step(get_default_db(), step_id, pin=pin,
                        approved_by="tester", configured_pin=PIN)


def _rows(number="QA-OVL-1"):
    from app.models import PlanStep
    out = []
    for row in get_default_db().session().query(PlanStep).all():
        pl = json.loads(row.payload_json or "{}")
        if pl.get("payable_number") == number:
            out.append(row)
    return out


def _step_for(db, number="QA-OVL-1"):
    rows = _rows(number)
    return rows[0] if rows else None


def _sent_once():
    """A bill approved, sent, and holding a live object id. Returns the id."""
    db = get_default_db()
    _cash("USD", 9_000_00)
    _bill()
    _cycle(db, FakeTransfers())
    _approve(_step_for(db).id)
    transfers = FakeTransfers()
    _cycle(db, transfers, execute=True)
    rows = _rows()
    assert len(transfers.calls) == 1 and rows and rows[0].airwallex_object_id, (
        "the harness did not get a transfer created; the repro cannot run")
    return rows[0].airwallex_object_id


def _ledger_snapshot() -> list[tuple]:
    from app.models import LedgerEntry
    with session_scope() as s:
        return sorted(
            (e.txn_id, e.account, e.currency, e.debit_minor, e.credit_minor)
            for e in s.query(LedgerEntry).all())


# ------------------------------------------- 1. a cancelled attempt closes
class TestACancelledAttemptDoesNotWreckTheRePlan:
    """Fail/Return must close the old attempt and leave the books standing.

    The tester's Fail and Return both ended with "Re-planning failed
    (IntegrityError); nothing was sent." and a rolled-back re-plan, so the
    operator could not tell whether their money had moved.
    """

    def test_the_replan_survives_a_cancelled_transfer(self):
        from app.settle import settle_transfer

        oid = _sent_once()
        before = _ledger_snapshot()
        with session_scope() as s:
            settle_transfer(s, oid, "CANCELLED")
        # This is the call the button makes. It must not raise.
        run = _cycle(get_default_db(), FakeTransfers(), execute=True)
        assert run is not None
        # A CANCELLED transfer moves no money, so the ledger cannot change.
        assert _ledger_snapshot() == before, (
            "a cancelled attempt changed the books")

    def test_the_replan_can_also_settle_and_send_the_next_one(self):
        """The point of closing an attempt: the next cycle can still plan."""
        from app.settle import settle_transfer

        oid = _sent_once()
        with session_scope() as s:
            settle_transfer(s, oid, "CANCELLED")
        transfers = FakeTransfers()
        run = _cycle(get_default_db(), transfers, execute=True)
        verdicts = [(v.action, v.outcome) for v in run.verdicts]
        assert verdicts, "the re-plan produced no verdicts at all"

    def test_a_failure_sentence_never_names_an_exception_class(self):
        """`(IntegrityError)` is a machine word on an operator's screen."""
        from app.domain.plain_words import settle_failure

        from sqlalchemy.exc import IntegrityError

        sentence, _detail = settle_failure(
            IntegrityError("INSERT", {}, Exception("UNIQUE step_key")))
        assert "IntegrityError" not in sentence, sentence
        assert "SQL" not in sentence.upper(), sentence
        assert sentence.strip().endswith("."), sentence


class TestASecondAttemptGetsAFreshKey:
    """THE CRASH: a retired attempt keeps `PAY:<bill>`, so the next one collides.

    `find_step` deliberately skips a RETIRED row (a dead attempt must not block a
    new one) -- but `_key_for` gives the new attempt the SAME `step_key`, which
    the retired row still holds under a unique index. So the first cycle after a
    Fail/Return raised `UNIQUE constraint failed: plan_step.step_key`, from a
    query-invoked autoflush inside `persist_plan`, where its own IntegrityError
    handler could not see it.
    """

    def test_the_cycle_after_a_cancelled_attempt_does_not_collide(self):
        from app.settle import settle_transfer

        oid = _sent_once()
        with session_scope() as s:
            settle_transfer(s, oid, "CANCELLED")
        # Today this raises IntegrityError out of the conductor.
        run = _cycle(get_default_db(), FakeTransfers(), execute=True)
        assert run is not None

        keys = [r.step_key for r in _rows()]
        assert len(keys) == len(set(keys)), f"duplicate step keys: {keys}"
        assert len(keys) >= 2, (
            "the new attempt was never written, so nothing can be approved: "
            f"{keys}")

    def test_the_old_attempt_is_closed_and_the_new_one_is_separate(self):
        from app.models import StepStatus
        from app.settle import settle_transfer

        oid = _sent_once()
        with session_scope() as s:
            settle_transfer(s, oid, "CANCELLED")
        _cycle(get_default_db(), FakeTransfers(), execute=True)

        rows = _rows()
        cancelled = [r for r in rows if r.status == StepStatus.CANCELLED]
        fresh = [r for r in rows if r.status != StepStatus.CANCELLED]
        assert cancelled, "the cancelled attempt was not kept as a record"
        assert fresh, "there is no open attempt to approve"
        assert cancelled[0].airwallex_object_id, (
            "the cancelled attempt lost the id of the transfer it made")
        assert fresh[0].step_key != cancelled[0].step_key

    def test_an_approval_for_the_dead_attempt_does_not_authorise_the_new_one(self):
        from app.settle import settle_transfer

        oid = _sent_once()
        with session_scope() as s:
            settle_transfer(s, oid, "CANCELLED")
        _cycle(get_default_db(), FakeTransfers(), execute=True)
        # The cancelled attempt was approved once. That approval must not be
        # read as consent for a payment that has not happened.
        from app.dashboard import build_context
        cards = build_context([])["approvable"]
        assert cards, "the replacement needs a card of its own"
        assert all(c.get("already_approved") is False for c in cards), (
            "the dead attempt's approval is being applied to the new one")


class TestTheBankReasonSurvivesTheControl:
    def test_a_returned_transfer_keeps_its_reason(self):
        """'CANCELLED' alone reads as 'somebody stopped this'."""
        from app.domain.incidents import record_bank_failure
        from app.models import PlanStep
        from app.settle import settle_transfer

        oid = _sent_once()
        with session_scope() as s:
            settle_transfer(s, oid, "CANCELLED")
            said, incident = record_bank_failure(
                s, transfer_id=oid, kind="return",
                failure_type="BENEFICIARY_BANK_RETURNED",
                failure_reason="account closed")
        assert said.startswith("Returned:"), said
        from app.dashboard import build_context
        # The card reads the payable's `failure_reason`, so that is where the
        # reason has to arrive -- not in a private helper's dict.
        ctx = build_context([incident])
        seen = " | ".join(str(p.get("failure_reason") or "")
                          for p in ctx["payables"])
        assert "Returned" in seen, seen
        assert "account closed" in seen, seen


# ------------------------------------------------- 2. a refund is a resolution
class TestARefundLeavesTheWaitingList:
    """Run 10: resolving as refund owed left the payment waiting on me.

    A refund owed IS resolved: a person decided, and what remains is a liability
    to pay, which is a different list. Leaving it in "waiting on you" (and in the
    unmatched list) asked for the same decision twice.
    """

    def _refunded(self):
        from app.domain.intake import record_and_match
        from app.domain.ledger import resolve_refund
        from app.models import Inflow

        with session_scope() as s:
            record_and_match(s, deposit_id="dep-r10-ref", currency="USD",
                             amount_minor=10_000,
                             payer_name="Apex Factors Ltd.",
                             reference="nothing matches this", llm=False)
            row = s.query(Inflow).order_by(Inflow.id.desc()).first()
            resolve_refund(s, row, reason="duplicate payment", by="tester")
            return row.id

    def test_it_is_not_unmatched_any_more(self):
        from app.dashboard import build_context

        iid = self._refunded()
        ctx = build_context([])
        assert [a for a in ctx["unmatched"] if a.get("inflow_id") == iid] == [], (
            "a resolved payment is still listed as unmatched")

    def test_the_waiting_sentence_does_not_count_it(self):
        from app.dashboard import build_context
        from app.ui import render_app

        self._refunded()
        ctx = build_context([])
        html = render_app(ctx, tab="home")
        text = " ".join(__import__("re").sub("<[^>]+>", " ", html).split())
        assert "1 payment waiting on you" not in text, text[:400]
        assert "0 payments waiting on you" in text or "Nothing waiting" in text, (
            text[:400])

    def test_it_is_shown_under_resolved_money_as_refund_owed(self):
        from app.dashboard import build_context
        from app.ui import render_app

        self._refunded()
        html = render_app(build_context([]), tab="ok")
        text = " ".join(__import__("re").sub("<[^>]+>", " ", html).split())
        assert "Resolved money" in text, text[:400]
        assert "Refund owed" in text, text[:400]


# --------------------------------------------- 3. a duplicate bill is refused
class TestADuplicateBillIsRefusedOutLoud:
    def test_the_second_one_is_refused_with_a_sentence(self, books):
        """Run 10: submitting PO-2001 twice looked like it worked."""
        from fastapi.testclient import TestClient

        from app.main import app

        suffix = "dup"
        make(suffix, floor="0", limit="2500", bill_minor=100_00,
             number="PO-R10-FIRST", supplier="Pennine Supplies")
        from app.models import Supplier

        sup = in_book(suffix, lambda s: s.query(Supplier).first().id)
        client = TestClient(app)
        body = {"supplier_id": str(sup), "currency": "USD", "amount": "100",
                "number": "PO-R10-DUP", "criticality": "3"}
        first = client.post(f"/setup/payables?ws={suffix}", data=body,
                            headers={"Accept": "application/json"})
        second = client.post(f"/setup/payables?ws={suffix}", data=body,
                             headers={"Accept": "application/json"})
        assert '"ok":true' in first.text.replace(" ", ""), first.text
        assert '"ok":false' in second.text.replace(" ", ""), second.text
        assert "already exists" in second.text.lower(), second.text
        assert "nothing added" in second.text.lower(), second.text
        assert "PO-R10-DUP" in second.text, second.text
        # AND ON THE SCREEN IT WAS TYPED ON. A refusal delivered to another
        # tab reads as no refusal at all, which is what run 10 saw.
        assert '"tab":"setup"' in second.text.replace(" ", ""), second.text
        from app.models import InvoicePayable
        count = in_book(suffix, lambda s: s.query(InvoicePayable)
                        .filter_by(number="PO-R10-DUP").count())
        assert count == 1, f"{count} rows for one bill number"


# ------------------------------- 4. an approved payment held on unresolved cash
class TestAnApprovedPaymentHeldOnUnresolvedCash:
    """Run 10: it said `insufficient_balance` and stopped there.

    The cash was THERE, in suspense: a USD 12,000.00 deposit nobody had decided
    about. "Approved. Waiting on cash" with the deposit named, and where to
    resolve it, is the sentence that tells the operator what to do next.
    """

    def test_it_names_the_deposit_and_where_to_go(self, books):
        from app.domain.intake import record_and_match

        make("aaa", floor="0", limit="2500", bill_minor=300_000,
             number="PO-R10-APR", supplier="Atlanta Packaging")
        in_book("aaa", lambda s: record_and_match(
            s, deposit_id="dep-r10-susp", currency="USD",
            amount_minor=1_200_000, payer_name="Apex Factors Ltd.",
            reference="nothing matches this", llm=False))
        run_plan("aaa")
        approve("aaa", approval_target("aaa"))
        run_plan("aaa")

        ctx, html = home_page("aaa")
        p = next(x for x in ctx["payables"] if x["number"] == "PO-R10-APR")
        joined = " | ".join(p["plan_blockers"] or [])
        assert "Approved" in joined, joined
        assert "USD 12,000.00" in joined, joined
        assert "unresolved" in joined.lower(), joined
        assert "money in" in joined.lower(), joined
        text = visible(html)
        assert "Approved. Waiting on cash" in text, text[:400]


# --------------------------------------------- 5. Home shows held for review
class TestHomeShowsTheMoneyHeldForReview:
    def test_the_figure_is_there_and_matches_the_helper(self, books):
        from app.domain.intake import record_and_match
        from app.domain.ledger import waiting_minor_for
        from app.models import Inflow

        make("aaa", floor="0", limit="2500", bill_minor=100_00)
        in_book("aaa", lambda s: record_and_match(
            s, deposit_id="dep-r10-held", currency="USD", amount_minor=700_000,
            payer_name="Apex Factors Ltd.", reference="nothing matches this",
            llm=False))
        held = in_book("aaa", lambda s: sum(
            waiting_minor_for(s, i) for i in s.query(Inflow).all()))
        assert held == 700_000, held

        _ctx, html = home_page("aaa")
        text = visible(html)
        assert "Unmatched money" in text, text[:500]
        assert "USD 7,000.00" in text, (
            "the held figure is missing or wrong: " + text[:500])


class TestTheCardShowsWhyTheAttemptCameBack:
    """Run 10's one gap, closed in the RENDERED page.

    After a Fail, `payable_state` held the NEWEST transfer row -- the
    replacement, which has no transfer at all -- so the reason was looked up
    under an empty transfer id and the caption never rendered, even though the
    event row was there and named the CANCELLED attempt's transfer.
    """

    def _cancelled_then_replanned(self, kind: str = "fail"):
        from app.domain.incidents import record_bank_failure
        from app.settle import settle_transfer

        oid = _sent_once()
        with session_scope() as s:
            settle_transfer(s, oid, "CANCELLED")
            _said, incident = record_bank_failure(
                s, transfer_id=oid, kind=kind,
                failure_type=("BENEFICIARY_BANK_RETURNED" if kind == "return"
                              else "INVALID_ACCOUNT_NAME_OR_NUMBER"),
                failure_reason=("account closed" if kind == "return" else ""))
        _cycle(get_default_db(), FakeTransfers(), execute=True)   # the #2 row
        return oid, incident

    def test_the_rendered_page_carries_the_failed_caption(self):
        from app.dashboard import build_context
        from app.ui import render_app

        oid, incident = self._cancelled_then_replanned("fail")
        html = render_app(build_context([incident]), tab="money-out")
        text = " ".join(__import__("re").sub("<[^>]+>", " ", html).split())
        assert "Failed:" in text, (
            "the money-out page does not say why the attempt came back: "
            + text[:600])
        assert "rejected" in text.lower() or "account" in text.lower(), text[:600]

    def test_the_rendered_page_carries_the_returned_caption(self):
        from app.dashboard import build_context
        from app.ui import render_app

        oid, incident = self._cancelled_then_replanned("return")
        html = render_app(build_context([incident]), tab="money-out")
        text = " ".join(__import__("re").sub("<[^>]+>", " ", html).split())
        assert "Returned:" in text, (
            "the money-out page does not say the money came back: "
            + text[:600])
        assert "account closed" in text, text[:600]

    def test_the_reason_follows_the_attempt_that_went_to_the_bank(self):
        """The card's own figure: it names the attempt the bank refused."""
        from app.dashboard import build_context

        oid, incident = self._cancelled_then_replanned("return")
        ctx = build_context([incident])
        p = next(x for x in ctx["payables"] if x["number"] == "QA-OVL-1")
        assert p["failure_reason"].startswith("Returned:"), p["failure_reason"]
        assert p["bank_attempt_id"] == oid, (
            "the card is not pointed at the attempt that has the transfer: "
            f"{p.get('bank_attempt_id')!r}")
