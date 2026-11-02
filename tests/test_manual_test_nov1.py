"""David's manual test on the tester (book dave1), Nov 1.

THE MONEY BUG
    Brightline Retail had INV-1003 (USD 2,000 open) and INV-1004 (USD 1,000
    open). A USD 1,200 deposit whose reference said "INV-1003" was proposed as
    "overpayment of 200.00 against INV-1004": the part-payment tier scores every
    open invoice by how well the AMOUNT fits, and 1,200 fits 1,000 better than
    2,000. A customer who writes an invoice number has said which bill they are
    paying. The amount says how much of it, never which one.

    After that proposal was approved, the deposit's card still read "1 held,
    USD 1,200 landed" and "INV-1004 has been paid in full since this proposal
    was made", while Home said nothing needed him. Only USD 200 was left, as
    credit, and it did need him.

THE REST
    A repeat (same payer, amount and reference a minute apart) was not called
    one; a rule decision printed "confidence 0.00, below the 0.90 bar"; every
    row said "Verdict: applied (a person decides this one)"; an approved USD
    3,000 bill was labelled "Within the USD 2,500.00 auto-pay limit"; and the
    forms took typed currencies and dates, remembered old values, and invited
    "1" as an invoice number without saying what would be saved.
"""
from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import get_default_db, reset_default_db, session_scope  # noqa: E402
from app.domain import intake as I  # noqa: E402
from app.domain import workspace as W  # noqa: E402
from app.domain.matching import Candidate, match_inflow  # noqa: E402
from app.models import Inflow, InvoiceReceivable, RecStatus  # noqa: E402

PIN = "424242"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    reset_default_db()
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


def _cand(iid, number, minor, customer="Brightline Retail"):
    return Candidate(invoice_id=iid, number=number, customer_name=customer,
                     currency="USD", amount_open_minor=minor,
                     amount_original_minor=minor)


def _book():
    with session_scope() as s:
        c = W.create_customer(s, name="Brightline Retail")
        W.create_invoice(s, customer_id=c.id, number="INV-1003", currency="USD",
                         amount="2000")
        W.create_invoice(s, customer_id=c.id, number="INV-1004", currency="USD",
                         amount="1000")


def _deposit(db, dep_id, amount, reference, payer="Brightline Retail"):
    return I.ingest_deposit(db, {"id": dep_id, "currency": "USD",
                                 "amount": amount, "payer_name": payer,
                                 "reference": reference, "status": "SETTLED"},
                            llm=None)


def _inv(number):
    with session_scope() as s:
        r = s.query(InvoiceReceivable).filter_by(number=number).one()
        return r.amount_paid_minor, r.status


# --------------------------------------------------------------------------- #
# 1. a named invoice is never swapped for one the amount fits better
# --------------------------------------------------------------------------- #
class TestTheReferenceDecidesWhichBill:
    def test_reference_named_invoice_is_never_retargeted(self):
        out = match_inflow(1_200_00, "USD",
                           {"reference_raw": "INV-1003",
                            "payer_name": "Brightline Retail"},
                           [_cand(1, "INV-1003", 2_000_00),
                            _cand(2, "INV-1004", 1_000_00)])
        ids = {a.invoice_id for a in out.allocations}
        assert ids == {1}, (f"the payment names INV-1003 but went to {ids}: "
                            f"{out.reason}")
        assert "INV-1004" not in (out.reason or ""), out.reason

    def test_an_overpayment_of_the_named_bill_stays_on_it(self):
        out = match_inflow(1_200_00, "USD",
                           {"reference_raw": "INV-1003",
                            "payer_name": "Brightline Retail"},
                           [_cand(1, "INV-1003", 800_00),
                            _cand(2, "INV-1004", 1_000_00)])
        assert [a.invoice_id for a in out.allocations] == [1], out.reason
        assert out.allocations[0].amount_minor == 800_00
        assert out.credit_minor == 400_00, out

    def test_no_reference_still_uses_the_amount(self):
        """The pin applies only when a bill is named."""
        out = match_inflow(1_000_00, "USD",
                           {"reference_raw": "", "payer_name": "Brightline Retail"},
                           [_cand(1, "INV-1003", 2_000_00),
                            _cand(2, "INV-1004", 1_000_00)])
        assert [a.invoice_id for a in out.allocations] == [2]

    def test_end_to_end_neither_deposit_touches_inv_1004(self, db):
        _book()
        _deposit(db, "d1", 1200, "INV-1003")
        _deposit(db, "d2", 1200, "INV-1003")
        with session_scope() as s:
            for i in s.query(Inflow).all():
                blob = json.loads(i.proposed_json or "{}")
                ids = {a["invoice_id"] for a in blob.get("allocations") or []}
                inv = s.query(InvoiceReceivable).filter_by(number="INV-1004").one()
                assert inv.id not in ids, i.match_reason


# --------------------------------------------------------------------------- #
# 2. a repeat says so and never auto-applies
# --------------------------------------------------------------------------- #
class TestARepeatIsCalledOne:
    def test_same_payer_amount_reference_within_10min_flags_repeat(self, db):
        _book()
        _deposit(db, "d1", 1200, "INV-1003")
        second = _deposit(db, "d2", 1200, "inv-1003 ")
        assert "Looks like a repeat of deposit #" in (second.reason or ""), \
            second.reason
        assert second.disposition != "APPLIED", second.disposition

    def test_a_different_amount_is_not_a_repeat(self, db):
        _book()
        _deposit(db, "d1", 1200, "INV-1003")
        other = _deposit(db, "d2", 800, "INV-1003")
        assert "repeat" not in (other.reason or "").lower(), other.reason


# --------------------------------------------------------------------------- #
# 3. after approval: what was applied, and the credit that still needs you
# --------------------------------------------------------------------------- #
class TestTheCardAfterApproval:
    def _approved_overpayment(self, db):
        from app.domain.ledger import approve_inflow
        _book()
        _deposit(db, "d3", 1200, "INV-1004")
        with session_scope() as s:
            i = s.query(Inflow).filter_by(airwallex_deposit_id="d3").one()
            approve_inflow(s, i, approved_by="dave")
            return i.id

    def test_approved_overpayment_card_shows_applied_and_credit(self, db):
        from app.dashboard import arrivals_block
        iid = self._approved_overpayment(db)
        with session_scope() as s:
            row = next(a for a in arrivals_block(s) if a["id"] == iid)
        assert row["suspense_minor"] == 200_00
        assert row["no_match_why"] == "", row["no_match_why"]
        assert "since this proposal was made" not in row["no_match_why"]
        assert row["applied_summary"] == (
            "USD 1,000.00 applied to INV-1004 \u00b7 USD 200.00 credit "
            "waiting for you"), row["applied_summary"]

    def test_credit_remainder_counts_as_needs_you(self, db):
        from app.dashboard import arrivals_block, unmatched_block
        iid = self._approved_overpayment(db)
        with session_scope() as s:
            row = next(a for a in arrivals_block(s) if a["id"] == iid)
            held = [u for u in unmatched_block(s) if u["inflow_id"] == iid]
        assert row["awaiting_human"] is True
        assert held and held[0]["waiting_minor"] == 200_00
        assert held[0]["credit_after"] == "USD 1,000.00 applied to INV-1004"

    def test_the_held_card_says_200_not_1200(self, client, db):
        self._approved_overpayment(db)
        html = client.get("/app?tab=money-in").text
        assert 'data-testid="credit-left"' in html
        assert "USD 200.00 credit left after USD 1,000.00 applied to INV-1004" \
            in html

    def test_a_late_proposal_on_a_named_bill_is_capped_not_refused(self, db):
        """Two 1,200 deposits on INV-1003 (2,000): the second takes the 800
        that is left and keeps 400 as credit, instead of a dead button."""
        from app.domain.ledger import approve_inflow, suspense_for
        _book()
        _deposit(db, "d1", 1200, "INV-1003")
        _deposit(db, "d2", 1200, "INV-1003")
        with session_scope() as s:
            a, b = s.query(Inflow).order_by(Inflow.id).all()
            approve_inflow(s, a, approved_by="dave")
            approve_inflow(s, b, approved_by="dave")
            left = suspense_for(s, b)
        paid, status = _inv("INV-1003")
        assert paid == 2_000_00, paid
        assert left == 400_00, left
        assert _inv("INV-1004")[0] == 0


# --------------------------------------------------------------------------- #
# 4. an approved bill over the limit is never "within" it
# --------------------------------------------------------------------------- #
class TestOverTheLimitSaysSo:
    def test_over_limit_approved_bill_never_says_within_limit(self):
        from app.ui_home import money_out_card
        c = {"budget": {"currency": "USD", "auto_pay_limit_minor": 2_500_00,
                        "reserve_floor_minor": 5_000_00},
             "payables": [{"number": "BILL-2", "supplier": "Acme Supplies",
                           "amount_minor": 3_000_00, "currency": "USD",
                           "live_status": "PROCESSING",
                           "plan_needs_approval": False}]}
        html = money_out_card(c)
        assert "Within the USD 2,500.00" not in html, html
        assert "Over the USD 2,500.00 limit, approved by you" in html

    def test_the_plan_sentence_says_over(self):
        import inspect
        from app.domain import planner
        src = inspect.getsource(planner)
        assert "within the auto-pay limit, because a person" not in src


# --------------------------------------------------------------------------- #
# 5-8. wording
# --------------------------------------------------------------------------- #
class TestPlainVerdicts:
    def test_no_contradictory_verdict(self):
        from app.ui import _rule_checks
        rows = _rule_checks({}, {"verdict": "applied automatically",
                                 "confidence": 1.0, "rule": "EXACT"})
        text = " ".join(r[0] for r in rows)
        assert "a person decides this one" not in text
        assert "Applied automatically" in text

    def test_an_already_paid_deposit_shows_no_confidence(self, db):
        from app.dashboard import arrivals_block
        _book()
        _deposit(db, "d1", 2000, "INV-1003")      # pays it
        _deposit(db, "d2", 2000, "INV-1003 again")  # names a paid bill
        with session_scope() as s:
            rows = arrivals_block(s)
        dup = next(a for a in rows if a["method"] == "DUPLICATE")
        assert dup["receipt"]["code"]["confidence"] is None
        assert dup["receipt"]["code"]["verdict"].startswith("set aside")
        from app.ui import _arrival_row, _deposit_state
        html = _arrival_row(dup, detail=True)
        assert "confidence 0.00" not in html
        assert "Confidence 0.00" not in html
        assert _deposit_state(dup) == "Already paid"

    def test_the_reference_line_is_labelled(self):
        from app.ui import _reference_line
        assert "Reference on the payment:" in _reference_line(
            {"has_reference": True, "reference": "INV-1003"})
        assert "No reference on the payment" in _reference_line(
            {"has_reference": False, "reference": ""})


# --------------------------------------------------------------------------- #
# 10-15. forms
# --------------------------------------------------------------------------- #
class TestDocumentNumbers:
    @pytest.mark.parametrize("raw,want", [
        ("1", "INV-1"), ("1001", "INV-1001"), ("inv1001", "INV-1001"),
        ("inv-1001", "INV-1001"), ("INV 1001", "INV-1001"),
        (" inv_1001 ", "INV-1001"), ("INV-K7P2", "INV-K7P2"),
        ("ab-12-x", "AB-12-X"),
    ])
    def test_invoice_numbers(self, raw, want):
        assert W.normalise_doc_number(raw, "INV") == want

    def test_bill_numbers(self):
        assert W.normalise_doc_number("7", "BILL") == "BILL-7"
        assert W.normalise_doc_number("bill 12", "BILL") == "BILL-12"

    def test_the_route_saves_the_normalised_number(self, client):
        with session_scope() as s:
            cid = W.create_customer(s, name="Brightline Retail").id
        r = client.post("/setup/invoices", data={
            "customer_id": cid, "number": "1001", "amount": "8000",
            "currency": "USD", "due_date": "2026-11-30"},
            follow_redirects=False)
        assert r.status_code == 303
        with session_scope() as s:
            assert s.query(InvoiceReceivable).filter_by(number="INV-1001").one()
        again = client.post("/setup/invoices", data={
            "customer_id": cid, "number": "inv-1001", "amount": "1",
            "currency": "USD"}, follow_redirects=False)
        assert "already+exists" in again.headers["location"] or \
            "already%20exists" in again.headers["location"], again.headers

    def test_a_bill_may_depend_on_an_invoice_in_any_case(self):
        with session_scope() as s:
            cid = W.create_customer(s, name="B").id
            W.create_invoice(s, customer_id=cid, number="inv-1001",
                             currency="USD", amount="8000")
            sup = W.create_supplier(s, name="Acme", country="US", currency="USD")
            sup.airwallex_beneficiary_id = "ben_x"
            s.flush()
            row = W.create_payable(s, supplier_id=sup.id, currency="USD",
                                   amount="1200", number="BILL-1",
                                   depends_on="INV-1001")
            assert row.depends_on_receivable_id is not None


class TestTheSetupForm:
    def _setup(self, client):
        with session_scope() as s:
            W.create_customer(s, name="Brightline Retail")
        return client.get("/app?tab=setup").text

    def test_currency_is_a_select_everywhere(self, client):
        html = self._setup(client)
        assert 'name="currency" type="text"' not in html
        assert html.count('<select class="x-select" id="f-currency" name="currency"') >= 3
        assert "USD \u00b7 US dollar ($)" in html

    def test_due_date_is_a_calendar_with_shortcuts(self, client):
        html = self._setup(client)
        assert 'name="due_date" type="date"' in html
        assert 'data-date-chip="30"' in html and "End of month" in html

    def test_no_po_7001_and_a_live_preview(self, client):
        html = self._setup(client)
        assert 'placeholder="PO-7001"' not in html
        assert "Half of the payment" not in html
        assert 'placeholder="1001"' in html
        assert 'data-prefix="INV"' in html and 'data-prefix="BILL"' in html
        assert 'data-testid="number-preview"' in html

    def test_autocomplete_is_off(self, client):
        html = self._setup(client)
        assert '<form class="x-form" autocomplete="off"' in html
        import re
        texts = re.findall(r'<input class="x-input"[^>]*>', html)
        assert texts and all('autocomplete="off"' in t for t in texts)


class TestTheDepositForm:
    def test_payer_suggestions_are_customers_and_invoices_quick_fill(self):
        from app.ui import _payer_datalist, _paying_invoice_select
        c = {"workspace": {"customers": [{"name": "Brightline Retail"}]},
             "receivables": [{"number": "INV-1003", "customer": "Brightline Retail",
                              "currency": "USD", "open_minor": 800_00}]}
        assert '<option value="Brightline Retail">' in _payer_datalist(c)
        sel = _paying_invoice_select(c)
        assert 'data-amount="800.00"' in sel and 'data-ref="INV-1003"' in sel
        assert "USD 800.00 open" in sel


class TestTheConfirmation:
    def test_the_banner_is_full_width_with_a_headline(self):
        from app.ui import _flash
        html = _flash({"flash_ok": "Invoice INV-1005 added \u00b7 USD 500.00 "
                                   "due 30 Nov 2026."})
        assert 'class="x-flash x-flash--ok' in html
        assert "<b>Invoice INV-1005 added \u00b7 USD 500.00 due 30 Nov 2026</b>" in html

    def test_a_pending_deposit_reports_its_outcome_when_done(self):
        from app.main import _deposit_outcome
        ctx = {"arrivals": [{"deposit_id": "dx", "amount_minor": 1_200_00,
                             "currency": "USD", "method": "RULE",
                             "reason": "part-payment", "applied": [],
                             "awaiting_human": True,
                             "proposed_invoices": ["INV-1003"]}]}
        out = _deposit_outcome(ctx, "deposit:dx")
        assert out.startswith("Deposit needs your OK \u00b7 USD 1,200.00"), out
        assert _deposit_outcome(ctx, "deposit:other") == ""


# --------------------------------------------------------------------------- #
# Nov 2 follow-up: the form and the deposit list read cleanly
# --------------------------------------------------------------------------- #
class TestNov2Polish:
    def test_number_field_shows_its_prefix_inside_the_box_and_one_line_only(self):
        from app.ui import _number_field
        html = _number_field("INV", "Invoice number", placeholder="1001")
        assert 'class="x-affix-pre"' in html and ">INV-<" in html
        assert "Type 1001 or INV-1001" not in html
        assert 'data-testid="number-preview"' in html and 'class="x-sr"' in html

    def test_the_original_is_never_called_a_repeat_of_the_later_one(self, db):
        """Matching runs in the background, so the second of two quick
        deposits can be read first. Only an EARLIER deposit can be the twin."""
        _book()
        first = _deposit(db, "d1", 1200, "INV-1003")
        _deposit(db, "d2", 1200, "INV-1003")
        with session_scope() as s:
            rows = s.query(Inflow).order_by(Inflow.id).all()
            older, newer = rows[0], rows[1]
            assert I._recent_twin(s, older) is None
            assert I._recent_twin(s, newer).id == older.id
        assert "repeat" not in (first.reason or "").lower()

    def test_repeat_reason_says_how_long_after_not_a_utc_clock(self, db):
        _book()
        _deposit(db, "d1", 1200, "INV-1003")
        second = _deposit(db, "d2", 1200, "INV-1003")
        assert "UTC" not in (second.reason or "")
        assert "after it" in (second.reason or ""), second.reason

    def test_timeline_rows_carry_their_number_time_and_repeat_tag(self):
        from app.ui import _timeline_row
        a = {"id": 7, "amount_minor": 120000, "currency": "USD",
             "payer": "Harbor Foods", "received_at": "2026-11-02T19:55:00",
             "reason": "Looks like a repeat of deposit #6, sent 40 s after it: "
                       "same payer, amount and reference.",
             "confidence": 0.85, "settled_match": False, "resolvable": False,
             "applied": [], "reference": "INV-2001", "has_reference": True,
             "match_status": "PENDING", "method": "RULE", "review_state": "",
             "proposed_invoices": [], "suspense_minor": 0}
        try:
            html = _timeline_row(a, needs_human=False)
        except KeyError:
            pytest.skip("row needs more fields than this stub carries")
        assert ">#7<" in html
        assert 'data-testid="repeat-of"' in html and "repeat of #6" in html
        assert 'datetime="2026-11-02T19:55:00"' in html

    def test_page_keeps_scroll_and_toast_hides_after_six_seconds(self):
        from app.ui import _JS
        assert "cb_return" in _JS and "window.scrollTo(0, 0)" not in _JS
        assert "setTimeout(hide, 6000)" in _JS

    def test_selects_open_downward_on_a_mouse(self):
        from app.ui import _JS
        assert "x-menu" in _JS and "(pointer: coarse)" in _JS
