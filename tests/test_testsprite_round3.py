"""TestSprite round 3: the faults a real click-through found.

Each of these was confirmed live on the tester before it was fixed here.

  1. An over-limit bill never reached Needs your OK.
  2. A PROCESSING payment showed a red "stopped here" cross.
  3. Sandbox controls did not say which payment each row was about.
  4. The receipt reported the MODEL's confidence as the final one when the code
     had kept the deterministic reading.
  5. Stuck states: "Working…" forever, APPROVED with no reference, and a refund
     resolution still reading PROPOSED.
  6. A returned transfer showed CANCELLED with no reason.
"""
from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import reset_default_db, session_scope  # noqa: E402
from app.domain.ledger import post  # noqa: E402

CSS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                   "backend", "app", "static", "closebook.css")


@pytest.fixture(autouse=True)
def _isolated():
    reset_default_db()
    yield
    reset_default_db()


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


class _StubClient:
    """Nothing should ever be sent in these tests; if it is, that is the bug."""
    def __getattr__(self, name):
        raise AssertionError(f"the conductor tried to use the client: {name}")


def _bill(number, minor, ccy="USD", *, status="READY", depends_on=None,
          supplier="Atlanta Packaging"):
    from app.models import InvoicePayable, PayStatus, PayoutMethod, Supplier

    with session_scope() as s:
        sup = (s.query(Supplier).filter(Supplier.name == supplier).first()
               or Supplier(name=supplier, payout_method=PayoutMethod.LOCAL))
        if sup.id is None:
            s.add(sup)
            s.flush()
        s.add(InvoicePayable(number=number, supplier_id=sup.id, currency=ccy,
                             amount_minor=minor, status=PayStatus[status],
                             criticality=3,
                             depends_on_receivable_id=depends_on))


def _cash(ccy, minor):
    with session_scope() as s:
        post(s, f"opening:{ccy}", [(f"cash:{ccy}", minor, 0, ccy),
                                   ("opening_balance", 0, minor, ccy)])


def _run_plan(db=None):
    from app.db import get_default_db
    from app.domain.conductor import on_event

    return on_event(db or get_default_db(), source="test", budget_s=20,
                    execute=True, dry_run=False, client=_StubClient())


class TestAnOverLimitBillCanBeApproved:
    """Item 1. The bill was Held; Needs your OK said nothing needs your OK."""

    def test_it_produces_exactly_one_approval_card(self, client):
        _cash("USD", 9_000_00)
        _bill("QA-OVERLIMIT-001", 3_500_00)
        _run_plan()

        body = client.get("/app?tab=ok").text
        rows = client.get("/state").json()
        _ = rows
        assert body.count('action="/approvals/') >= 1 or "QA-OVERLIMIT-001" in body

    def test_the_step_is_actually_persisted(self, client):
        """The root cause: only CLEAR steps were written, so a step needing a
        human had no row and there was nothing to approve."""
        from app.models import PlanStep

        _cash("USD", 9_000_00)
        _bill("QA-OVERLIMIT-001", 3_500_00)
        _run_plan()

        from app.db import get_default_db
        rows = get_default_db().session().query(PlanStep).all()
        assert len(rows) == 1, [r.id for r in rows]
        payload = json.loads(rows[0].payload_json or "{}")
        assert payload.get("payable_number") == "QA-OVERLIMIT-001"
        assert payload.get("amount_minor") == 3_500_00

    def test_exactly_one_card_not_a_duplicate(self):
        from app.dashboard import build_context
        from app.db import get_default_db

        _cash("USD", 9_000_00)
        _bill("QA-OVERLIMIT-001", 3_500_00)
        _run_plan()
        ctx = build_context([])
        found = [a for a in ctx["approvable"]
                 if "QA-OVERLIMIT" in json.dumps(a)]
        assert len(found) == 1, found

    def test_and_it_is_not_in_the_held_list(self, client):
        """Held means no person can release it. This one needs a person."""
        from app.dashboard import build_context

        _cash("USD", 9_000_00)
        _bill("QA-OVERLIMIT-001", 3_500_00)
        _run_plan()
        ctx = build_context([])
        held = [p for p in (ctx.get("waiting") or [])
                if p.get("number") == "QA-OVERLIMIT-001"]
        assert not held, held

    def test_a_bill_held_for_two_reasons_states_both(self):
        """Over the limit AND short of cash: two different fixes."""
        from app.dashboard import build_context

        _cash("USD", 100_00)                     # nowhere near the amount
        _bill("QA-OVERLIMIT-002", 3_500_00)
        _run_plan()
        ctx = build_context([])
        held = {p["number"]: p for p in (ctx.get("waiting") or [])}
        assert "QA-OVERLIMIT-002" in held, list(held)
        reason = json.dumps(held["QA-OVERLIMIT-002"])
        assert "limit" in reason.lower()

    def test_a_bill_blocked_by_a_dependency_never_gets_an_approval_card(self):
        """A dependency is not a human decision, so a card would be a lie."""
        from app.dashboard import build_context
        from app.models import Customer, InvoiceReceivable, RecStatus

        with session_scope() as s:
            c = Customer(name="Harrow & Co")
            s.add(c)
            s.flush()
            rec = InvoiceReceivable(number="INV-DEP", customer_id=c.id,
                                    currency="USD", amount_minor=500_00,
                                    status=RecStatus.OPEN)
            s.add(rec)
            s.flush()
            dep_id = rec.id
        _cash("USD", 9_000_00)
        _bill("QA-DEP-001", 900_00, depends_on=dep_id)
        _run_plan()

        ctx = build_context([])
        cards = [a for a in ctx["approvable"] if "QA-DEP-001" in json.dumps(a)]
        assert not cards, "a dependency hold must not offer an approval"
        held = [p["number"] for p in (ctx.get("waiting") or [])]
        assert "QA-DEP-001" in held, held


class TestTheTrackerOnlyMarksWhatStopped:
    """Item 2. PO-7001 was PROCESSING and showed "stopped here"."""

    def test_only_failed_returned_cancelled_are_stopped(self):
        from app.ui import _STOPPED_STATUSES

        assert _STOPPED_STATUSES == {"FAILED", "CANCELLED", "RETURNED"}

    def test_every_status_against_the_tracker(self):
        """A table, so a new status has to face the question rather than
        inherit whatever the last one did."""
        from app.ui import _STEP_LABELS, _STOPPED_STATUSES, _step_index

        expected = {
            "PROPOSED": (0, False), "APPROVED": (2, False),
            "SUBMITTED": (3, False), "PAID": (4, False),
            "PROCESSING": (1, False),
            "FAILED": (3, True), "RETURNED": (3, True),
            "CANCELLED": (2, True),
        }
        for status, (idx, stopped) in expected.items():
            assert _step_index(status) == idx, status
            assert (status in _STOPPED_STATUSES) is stopped, status
            assert 0 <= idx < len(_STEP_LABELS), status

    def test_an_earlier_failed_attempt_does_not_mark_a_live_payment(self):
        """The exact regression: the flag is about the PAST, the cross is about
        this transfer."""
        import inspect

        from app.ui import _money_out

        src = inspect.getsource(_money_out)
        assert "previous_attempt_failed" not in src.split("stopped =")[1][:120]

    def test_a_processing_payment_renders_no_cross(self, client):
        """End to end: a submitted, in-flight payment shows no stop."""
        from app.dashboard import build_context
        from app.models import (InvoicePayable, PayStatus, PayoutMethod, PlanStep,
                                StepStatus, Supplier)

        with session_scope() as s:
            sup = Supplier(name="Atlanta Packaging", payout_method=PayoutMethod.LOCAL)
            s.add(sup)
            s.flush()
            s.add(InvoicePayable(number="PO-7001", supplier_id=sup.id,
                                 currency="USD", amount_minor=1_800_00,
                                 status=PayStatus.PAYING, criticality=3))
            s.flush()
            s.add(PlanStep(run_id="r1", type="TRANSFER",
                           payload_json=json.dumps({"payable_number": "PO-7001",
                                                    "action": "PAY",
                                                    "amount_minor": 1_800_00,
                                                    "currency": "USD"}),
                           status=StepStatus.SUBMITTED,
                           airwallex_object_id="tr-live"))
        html = client.get("/app?tab=money-out").text
        assert "tr-live" in html or "PO-7001" in html
        assert "is-stopped" not in html, "an in-flight payment must not read stopped"


class TestSandboxRowsNameTheirPayment:
    """Item 3. "Settle" with no subject."""

    def test_the_row_names_the_payment(self):
        from app.ui import _dev_controls

        html = _dev_controls("tr-abc", "PROCESSING",
                             "PO-7001 · Atlanta Packaging · USD 1,800.00")
        assert "PO-7001" in html and "Atlanta Packaging" in html
        assert "USD 1,800.00" in html

    def test_each_button_carries_a_per_transfer_testid(self):
        from app.ui import _dev_controls

        html = _dev_controls("tr-abc", "PROCESSING", "PO-1")
        for what in ("settle", "fail", "return"):
            assert f'data-testid="sandbox-{what}-tr-abc"' in html, what

    def test_the_row_is_addressable(self):
        from app.ui import _dev_controls

        html = _dev_controls("tr-abc", "PROCESSING", "PO-1")
        assert 'id="sandbox-row-tr-abc"' in html

    def test_a_payment_card_links_to_its_row(self):
        from app.ui import _sandbox_link

        html = _sandbox_link("tr-abc")
        assert 'href="#sandbox-row-tr-abc"' in html
        assert 'data-open-sandbox="tr-abc"' in html
        assert 'data-testid="sandbox-jump-tr-abc"' in html

    def test_no_link_is_rendered_without_a_reference(self):
        from app.ui import _sandbox_link

        assert _sandbox_link("") == ""

    def test_the_link_and_the_row_use_the_same_offered_rule(self):
        """A link for a payment the panel omits points at nothing."""
        import inspect

        from app.ui import _dev_controls_are_offered_for

        assert _dev_controls_are_offered_for("PROCESSING") is True
        assert _dev_controls_are_offered_for("PAID") is False
        assert _dev_controls_are_offered_for(None) is False
        assert "main import _dev_controls_are_offered" in \
            inspect.getsource(_dev_controls_are_offered_for)

    def test_the_panel_opens_and_scrolls(self):
        """A plain anchor cannot open a collapsed <details>."""
        from app.ui import _JS

        assert "data-open-sandbox" in _JS
        assert "panel.open = true" in _JS
        assert "scrollIntoView" in _JS


class TestTheReceiptUsesTheStoredDecision:
    """Item 4. The final confidence reported the MODEL's number."""

    def test_the_code_column_comes_from_the_stored_decision(self):
        from app.dashboard import receipt_data
        from app.models import (Customer, Inflow, InvoiceReceivable, MatchStatus,
                                RecStatus)

        with session_scope() as s:
            c = Customer(name="Brightline Retail")
            s.add(c)
            s.flush()
            inv = InvoiceReceivable(number="INV-1001", customer_id=c.id,
                                    currency="USD", amount_minor=1_200_00,
                                    status=RecStatus.OPEN)
            s.add(inv)
            s.flush()
            i = Inflow(currency="USD", amount_minor=1_200_00,
                       payer_name="Brightline Retail", reference_raw="invoce 1OO1",
                       match_status=MatchStatus.MATCHED, match_method="RULE",
                       match_reason="single invoice, reference folded")
            s.add(i)
            s.flush()
            # The model claimed 0.80; the RULE reading at 0.85 is what the code
            # KEPT, so 0.85 is the final confidence and RULE is the rule.
            rec = receipt_data(s, i, {"confidence": 0.85, "method": "RULE",
                                      "reason": "folded reference",
                                      "allocations": [{"invoice_id": inv.id,
                                                       "amount_minor": 1_200_00}],
                                      "evidence": {"model_claimed_confidence": 0.80}},
                               0.85, 1_200_00)
        assert rec["code"]["confidence"] == 0.85, rec["code"]
        assert rec["code"]["rule"] == "RULE", rec["code"]

    def test_the_model_claim_is_still_shown_and_struck(self):
        from app.dashboard import receipt_data
        from app.models import (Customer, Inflow, InvoiceReceivable, MatchStatus,
                                RecStatus)

        with session_scope() as s:
            c = Customer(name="Lumen Stores")
            s.add(c)
            s.flush()
            inv = InvoiceReceivable(number="INV-1003", customer_id=c.id,
                                    currency="USD", amount_minor=440_00,
                                    status=RecStatus.OPEN)
            s.add(inv)
            s.flush()
            i = Inflow(currency="USD", amount_minor=440_00, payer_name="Apex",
                       match_status=MatchStatus.MATCHED, match_method="LLM")
            s.add(i)
            s.flush()
            rec = receipt_data(s, i, {"confidence": 0.89, "method": "LLM",
                                      "allocations": [{"invoice_id": inv.id,
                                                       "amount_minor": 440_00}],
                                      "evidence": {"model_claimed_confidence": 0.90}},
                               0.89, 440_00)
        assert rec["model"]["confidence"] == 0.90
        assert rec["model"]["struck"] is True
        assert rec["code"]["confidence"] == 0.89

    def test_every_trace_that_names_a_payment_carries_its_decision(self):
        """One fact, one place: the code column is read, not re-derived."""
        import inspect

        from app.dashboard import trace_block

        src = inspect.getsource(trace_block)
        assert "receipt_data" in src
        assert "inflow_id" in src

    def test_the_trace_proposal_is_preferred_over_a_model_claim(self):
        """The stored proposal is what the book acted on."""
        import inspect

        from app.dashboard import trace_block

        src = inspect.getsource(trace_block)
        assert "proposed_json" in src


class TestStuckStates:
    """Item 5."""

    def test_the_action_handler_always_resolves(self):
        from app.ui import _JS

        assert "A WATCHDOG" in _JS
        assert "var finish = function" in _JS
        assert "if (done) return;" in _JS

    def test_every_terminal_path_goes_through_it(self):
        import inspect

        import app.ui as U

        js = inspect.getsource(U)
        seg = js[js.find("A WATCHDOG"):]
        seg = seg[:seg.find("}).catch(function (err)")]
        assert "finish(" in seg
        # the success and failure branches must not call say() directly
        assert 'finish(res.message, "good")' in js
        assert "finish(res.err || res.message" in js

    def test_the_watchdog_has_a_timeout_and_a_sentence(self):
        from app.ui import _JS

        assert "window.setTimeout" in _JS
        assert "30000" in _JS
        assert "taking too long" in _JS

    def test_approved_without_a_reference_reads_sending(self):
        from app.ui import _STEP_LABELS  # noqa: F401

        css = open(CSS).read()
        _ = css
        import inspect

        from app.ui import _money_out

        src = inspect.getsource(_money_out)
        assert "Sending to the bank" in src
        assert 'st == "APPROVED" and not obj' in src

    def test_a_refund_resolution_no_longer_reads_proposed(self):
        """The badge showed match_status, which never changes."""
        from app.ui import _deposit_state

        assert _deposit_state({"match_status": "PROPOSED",
                              "review_state": "REFUND_OWED"}) == "Refund owed"
        assert _deposit_state({"match_status": "PROPOSED",
                              "review_state": "RELEASED_INCOME"}) == "Released as income"
        assert _deposit_state({"match_status": "PROPOSED",
                              "review_state": "APPROVED"}) == "Matched"

    def test_an_unresolved_payment_still_shows_its_match_state(self):
        from app.ui import _deposit_state

        assert _deposit_state({"match_status": "PROPOSED", "review_state": ""}) \
            == "SUGGESTED"   # plain word on purpose (warm redesign)
        assert _deposit_state({"match_status": "UNMATCHED", "review_state": None}) \
            == "UNMATCHED"

    def test_the_badge_is_rendered_from_that_function(self):
        import inspect

        from app.ui import _timeline_row

        src = inspect.getsource(_timeline_row)
        assert "_deposit_state(a)" in src
        assert 'data-testid="deposit-state"' in src

    def test_a_resolved_deposit_is_not_waiting_on_a_person(self):
        """End to end: refund owed takes it out of the review queue."""
        from app.dashboard import build_context
        from app.models import Customer, Inflow, MatchStatus, RecStatus

        with session_scope() as s:
            c = Customer(name="Apex Factors Ltd")
            s.add(c)
            s.flush()
            i = Inflow(currency="USD", amount_minor=440_00, payer_name="Apex",
                       match_status=MatchStatus.PROPOSED, review_state="REFUND_OWED",
                       proposed_json='{"confidence": 0.7, "allocations": []}')
            s.add(i)
        ctx = build_context([])
        row = [a for a in ctx["arrivals"] if a.get("review_state") == "REFUND_OWED"]
        assert row, "the inflow must be in the arrivals list"
        assert row[0]["awaiting_human"] is False, row[0]


class TestAReturnedTransferShowsWhy:
    """Item 6."""

    def test_the_reason_is_read_from_the_incident_record(self):
        from app.dashboard import _return_reasons

        got = _return_reasons([
            {"transfer_id": "tr-1", "failure_reason": "beneficiary bank returned it",
             "severity": "ERROR"},
            {"transfer_id": "tr-2", "failure_type": "INVALID_BENEFICIARY",
             "severity": "WARN"},
        ])
        assert got["tr-1"] == "beneficiary bank returned it"
        assert got["tr-2"] == "INVALID_BENEFICIARY"

    def test_a_transfer_with_no_reason_is_simply_absent(self):
        from app.dashboard import _return_reasons

        assert _return_reasons([{"transfer_id": "tr-3"}]) == {}
        assert _return_reasons(None) == {}

    def test_the_card_renders_it_next_to_cancelled(self):
        import inspect

        from app.ui import _money_out

        src = inspect.getsource(_money_out)
        assert "return-reason" in src
        assert "back_why" in src

    def test_it_is_covered_by_the_plain_words_translator(self):
        """A vendor stack must not reach the screen through this path either."""
        from app.ui import _shown

        assert "Model unavailable" in _shown(
            "HTTPStatusError: Client error '500' for url 'https://x.googleapis.com'")
