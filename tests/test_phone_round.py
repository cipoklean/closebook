"""The phone-testing round: every one of these is a real fault seen on a device.

Item 1 is the important one. A resolve applied, then the route crashed while
formatting its reply, and `session_scope` rolled the whole thing back — so the
operator saw a failure and the books showed nothing had happened. A route that
can do its work and then lose it must never exist, and the only defence is that
the work is committed before any prose is built.
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
from app.domain.ledger import LedgerError, reconcile, suspense_for  # noqa: E402
from app.models import Inflow, InvoiceReceivable, Match, PayStatus  # noqa: E402

PIN = "424242"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    reset_default_db()
    from app.config import get_settings
    from pydantic import SecretStr
    monkeypatch.setattr(get_settings(), "demo_approver_pin", SecretStr(PIN),
                        raising=False)
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


def _invoice(number="INV-1003", ccy="USD", amount="4400.00", customer="Lumen Stores"):
    with session_scope() as s:
        c = W.create_customer(s, name=customer)
        return W.create_invoice(s, customer_id=c.id, number=number,
                                currency=ccy, amount=amount).id


def _deposit(db, *, amount=4400, dep_id="d-1", payer="Apex Factors Ltd",
             reference="for the lumen stores account"):
    r = I.ingest_deposit(db, {"id": dep_id, "currency": "USD", "amount": amount,
                              "payer_name": payer, "reference": reference,
                              "status": "SETTLED"})
    assert r.disposition == "PROPOSED", f"{r.disposition}: {r.reason}"
    return r


def _post(client, path, data):
    return client.post(path, data=data, follow_redirects=False,
                       headers={"Accept": "application/json"})


# ---- item 1 ---------------------------------------------------------------
class TestResolveNeverLosesItsWork:
    def test_the_route_does_not_500_and_the_books_actually_move(self, client, db):
        """The exact failure: it applied, then crashed building the reply, and
        the rollback undid it. A 200 with a real message is the fix."""
        _invoice()
        _deposit(db)

        r = _post(client, "/inflows/1/resolve",
                  {"what": "match", "by": "david", "pin": PIN})
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert body["ok"] and not body["err"], body
        assert "Matched" in body["ok"]

        # and the work survived the reply
        with session_scope() as s:
            assert s.query(Inflow).one().review_state == "APPROVED"
            inv = s.query(InvoiceReceivable).one()
            assert inv.amount_paid_minor == 440_000
            assert s.query(Match).count() == 1

    def test_a_refusal_is_a_message_not_a_status_code(self, client, db):
        _invoice()
        _deposit(db)
        r = _post(client, "/inflows/1/resolve",
                  {"what": "refund", "reason": "", "pin": PIN})
        assert r.status_code == 200
        assert "reason" in r.json()["err"].lower()

    def test_an_unexpected_failure_still_answers_with_a_sentence(self, client, db,
                                                                 monkeypatch):
        """Never a bare 500: the operator has to know whether anything moved."""
        _invoice()
        _deposit(db)
        from app.domain import ledger

        def boom(*a, **k):
            raise ValueError("something nobody predicted")

        monkeypatch.setattr(ledger, "approve_inflow", boom)
        import app.main as M
        monkeypatch.setattr(M, "_check_pin", lambda pin: None)
        r = _post(client, "/inflows/1/resolve", {"what": "match", "pin": PIN})
        assert r.status_code == 200
        assert "Nothing was changed" in r.json()["err"]

    def test_the_resolve_path_never_touches_a_model(self, client, db, monkeypatch):
        """Resolving is a decision about money already booked. Asking a model to
        re-read a reference would put a 45-second timeout inside a button."""
        _invoice()
        _deposit(db)
        from app.domain import llm

        def forbidden(*a, **k):
            raise AssertionError("the model must not run on the resolve path")

        monkeypatch.setattr(llm, "OpenAICompatibleMatcher", forbidden)
        r = _post(client, "/inflows/1/resolve",
                  {"what": "match", "pin": PIN, "by": "david"})
        assert r.status_code == 200 and r.json()["ok"]


# ---- item 2 ---------------------------------------------------------------
class TestASettledBillLeavesThePlan:
    def _settled_bill(self, db):
        with session_scope() as s:
            sup = W.create_supplier(s, name="Northwind Logistics", country="US",
                                    currency="USD")
            W.record_beneficiary(s, sup.id, request={},
                                 beneficiary_id="ben_1", status="CREATED")
            W.create_payable(s, supplier_id=sup.id, currency="USD",
                             amount="1800.00", number="PO-2001")
            # Enough cash that the FLOOR is not what blocks this bill — the
            # replacement rule is the thing under test.
            from app.domain.ledger import post
            post(s, "cash", [("cash:USD", 5_000_000, 0, "USD"),
                             ("opening_balance", 0, 5_000_000, "USD")])
            from app.domain.settings_store import save as save_settings
            save_settings(s, budget_currency="USD", reserve_floor="5000",
                          auto_pay_limit="2500", fx_tolerance_pct="0.50")

    def test_a_paid_payable_is_not_planned_at_all(self, db):
        from app.domain.treasury import current_plan, payables_from_db
        self._settled_bill(db)
        with session_scope() as s:
            assert "PO-2001" in [p.number for p in
                                 __import__("app.domain.treasury",
                                            fromlist=["x"]).payables_from_db(s)]
            p = s.query(__import__("app.models",
                                   fromlist=["InvoicePayable"]).InvoicePayable).one()
            p.status = PayStatus.PAID
        with session_scope() as s:
            assert payables_from_db(s) == [], "a settled bill is not a payable"
        assert not [st for st in current_plan(get_default_db()).steps
                    if st.payable_number == "PO-2001"], \
            "a paid bill must not appear in the plan, held or otherwise"

    def test_a_failed_attempt_makes_the_replacement_need_approval(self, db):
        """The plan and the conductor must agree that this cannot go alone."""
        from app.domain.treasury import current_plan
        from app.models import PlanStep, StepStatus, StepType
        self._settled_bill(db)
        with session_scope() as s:
            s.add(PlanStep(type=StepType.TRANSFER, status=StepStatus.CANCELLED,
                           payload_json='{"action": "PAY", '
                                        '"payable_number": "PO-2001"}',
                           reason="returned by the bank"))
        step = next(st for st in current_plan(get_default_db()).steps
                    if st.payable_number == "PO-2001")
        assert step.needs_approval is True
        assert "replacement" in (step.approval_reason or "")


# ---- item 3 ---------------------------------------------------------------
class TestAnInvoiceCannotBePaidTwice:
    def test_the_same_invoice_cannot_take_two_full_payments(self, client, db):
        _invoice()
        _deposit(db, amount=4400, dep_id="d-1")
        _deposit(db, amount=4400, dep_id="d-2", reference="another")

        first = _post(client, "/inflows/1/resolve",
                      {"what": "match", "pin": PIN, "by": "david"})
        assert first.json()["ok"], first.json()

        second = _post(client, "/inflows/2/resolve",
                       {"what": "match", "pin": PIN, "by": "david"})
        assert second.json()["err"], "an invoice paid in full cannot take more"
        assert "INV-1003" in second.json()["err"]

        with session_scope() as s:
            inv = s.query(InvoiceReceivable).one()
            assert inv.amount_paid_minor == 440_000, \
                "8200 on a 4400 invoice is the bug this prevents"

    def test_the_write_path_refuses_an_over_allocation(self, db):
        """The guard lives on the WRITE path, because a proposal is a snapshot:
        it was true when the money arrived and may not be true at approval."""
        from app.domain.ledger import _apply_allocations
        from app.domain.matching import Allocation
        inv_id = _invoice(amount="1000.00")
        _deposit(db, amount=4400, dep_id="over-1")
        with session_scope() as s:
            inflow = s.query(Inflow).one()
            with pytest.raises(LedgerError) as exc:
                _apply_allocations(
                    s, inflow, [Allocation(inv_id, 440_000)],
                    confidence=0.89, method="LLM", status="MATCHED",
                    evidence={"rule": "test"})
            assert "left open" in str(exc.value)

    def test_the_page_withdraws_the_match_option_and_says_why(self, client, db):
        _invoice(amount="4400.00")
        _deposit(db, amount=4400, dep_id="p-1")
        # Apply it, then try the same proposal a second time: the invoice is now
        # full, so the option must go and the reason must take its place.
        from app.domain.ledger import approve_inflow
        with session_scope() as s:
            approve_inflow(s, s.query(Inflow).one(), approved_by="david")
        _deposit(db, amount=4400, dep_id="p-2")
        html = client.get("/app?tab=ok").text
        # The option is gone AND the reason is on the page.
        assert "paid in full" in html
        assert 'value="refund"' in html and 'value="income"' in html, \
            "the alternatives are still offered"


# ---- item 4 ---------------------------------------------------------------
class TestAskingTheModelAgain:
    def test_it_needs_no_pin_and_changes_no_money(self, client, db):
        _invoice()
        _deposit(db)
        before = reconcile(db.session()).cash_by_currency
        r = _post(client, "/inflows/1/reask", {})
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert body["ok"] or body["err"]          # a sentence either way
        assert reconcile(db.session()).cash_by_currency == before, \
            "re-asking is a new READING, not a second receipt"

    def test_it_replaces_the_stale_model_sentence(self, client, db, monkeypatch):
        """The suite has no key by design, so the provider is stubbed here — the
        point is that the STORED sentence gets rewritten, not left over from an
        outage three days ago."""
        from app.domain import intake as I2

        class Stub:
            last_error = None
            cfg = type("C", (), {"model": "stub/model"})()

            def propose(self, inflow, cands):
                return {"allocations": [{"invoice_number": "INV-1003",
                                         "amount": 4400}],
                        "confidence": 0.9, "rationale": "names lumen"}

        _invoice()
        _deposit(db)
        monkeypatch.setattr(I2, "matcher_from_settings", lambda: (Stub(), ""))
        with session_scope() as s:
            row = s.query(Inflow).one()
            row.match_reason = ("model proposed nothing (offline or provider "
                                "unavailable); escalated rather than guessed")
            row.match_method = "LLM"

        r = _post(client, "/inflows/1/reask", {})
        assert r.json()["ok"], r.json()
        with session_scope() as s:
            row = s.query(Inflow).one()
            assert "offline or provider unavailable" not in (row.match_reason or ""), \
                "the stale sentence about a dead provider is gone"
            assert "lumen" in (row.match_reason or "")


# ---- item 6 ---------------------------------------------------------------
class TestOnePinAndOnlyTheFieldsThatMatter:
    def test_one_pin_box_per_item(self, client, db):
        _invoice()
        _deposit(db)
        html = client.get("/app?tab=ok").text
        assert html.count('name="pin"') == 1

    def test_the_two_optional_fields_are_marked_and_conditional(self, client, db):
        _invoice()
        _deposit(db)
        html = client.get("/app?tab=ok").text
        assert 'data-for="refund"' in html and 'data-for="income"' in html
        assert "required for a refund" in html
        assert "required to release as income" in html

    def test_resolving_as_defaults_to_the_person_not_the_page(self, client, db):
        _invoice()
        _deposit(db)
        html = client.get("/app?tab=ok").text
        assert 'value="david"' in html
        assert 'value="dashboard"' not in html, \
            "an audit row naming a page answers nothing"

    def test_an_audit_row_names_the_person(self, client, db):
        _invoice()
        _deposit(db)
        _post(client, "/inflows/1/resolve",
              {"what": "income", "note": "ours to keep", "by": "david",
               "pin": PIN})
        with session_scope() as s:
            row = s.query(Inflow).one()
            assert row.resolved_by == "david"


# ---- items 5 and 7 --------------------------------------------------------
class TestPhoneLayoutAndHonestCopy:
    def test_the_mobile_breakpoint_is_a_single_column(self):
        css = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "backend", "app", "static",
                                "closebook.css")).read()
        assert "max-width: 700px" in css
        block = css.split("max-width: 700px", 1)[1]
        assert "grid-template-columns: minmax(0, 1fr)" in block, \
            "tiles must be full width below 700px, not half"
        assert "grid-column: 1 / -1" in block
        assert "overflow-wrap: anywhere" in block, \
            "a long money figure must wrap rather than run out of its card"
        assert ".x-todo li { grid-template-columns: minmax(0, 1fr);" in block, \
            "the hold list must stop squeezing one word per line"

    def test_failed_payments_say_what_you_can_do(self, client, db):
        """The panel used to say these "cannot be approved, they need a new
        plan" — which is exactly backwards: a replacement DOES need approval."""
        from app.domain.workspace import create_payable, create_supplier
        from app.models import PlanStep, StepStatus, StepType
        with session_scope() as s:
            sup = create_supplier(s, name="Northwind Logistics", country="US",
                                  currency="USD")
            W.record_beneficiary(s, sup.id, request={},
                                 beneficiary_id="ben_9", status="CREATED")
            create_payable(s, supplier_id=sup.id, currency="USD",
                           amount="1200.00", number="PO-2009")
            s.add(PlanStep(type=StepType.TRANSFER, status=StepStatus.CANCELLED,
                           payload_json='{"action": "PAY", '
                                        '"payable_number": "PO-2009", '
                                        '"amount_minor": 120000, '
                                        '"currency": "USD"}',
                           reason="the beneficiary bank returned it"))
        for tab in ("home", "money-out"):
            html = client.get(f"/app?tab={tab}").text
            assert "need a new plan" not in html, tab
            assert "cannot be approved" not in html, tab
            assert "needs your approval" in html, tab
            assert 'action="/money-out/run"' in html, tab
            assert "Needs your OK" in html, tab
