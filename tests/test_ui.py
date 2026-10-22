"""Rendering tests for the new frontend: `/` landing and `/app`.

These assert on the contract the mockup sets and the data rules require:
- the landing page is static copy with the real CTAs, no live figures;
- `/app` renders ONLY from the dashboard context — nothing hardcoded;
- labels map onto the real fields (cash, committed cash, suspense, auto-pay);
- failed/stale steps are shown as problems, never as approvable;
- incidents are grouped by transfer;
- the approve form posts to the existing endpoints.

Context is built from a fresh in-memory DB each time, so a rendering test
cannot pass by relying on rows another test left behind.
"""
from __future__ import annotations

import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from app.db import get_default_db, reset_default_db, session_scope
from app.dashboard import build_context, incidents_block, steps_block
from app.domain.plan_store import new_run_id, persist_plan
from app.domain.planner import ACT_PAY, Step
from app.domain.matching import Candidate
from app.main import app
from app.models import AgentTrace, StepStatus
from app.ui import e, money, render_app, render_landing


@pytest.fixture(autouse=True)
def _isolated_db():
    """A genuinely empty DB per test — including the app's own.

    `reset_default_db()` only clears the cache used by `dashboard.build_context`.
    `app.main._db` is a separate instance built at import time, so a step written
    through the app's DB is still there for the next test — and a rendering test
    would then pass or fail on rows it did not create.

    Also disables the live Airwallex status lookup: it makes a real network call
    per referenced transfer, so leaving it on would make this suite slow, key-
    dependent, and dependent on Airwallex being up.
    """
    reset_default_db()
    import app.main as m
    import app.dashboard as dash
    from app.db import DB
    m._db = DB()
    previous = dash.LIVE_STATUS_ENABLED
    dash.LIVE_STATUS_ENABLED = False
    yield
    dash.LIVE_STATUS_ENABLED = previous


@pytest.fixture
def db():
    from app.db import get_default_db
    return get_default_db()


def _ctx(incidents=None) -> dict:
    return build_context(incidents or [])


def _client() -> TestClient:
    return TestClient(app)


def _write_step(db, *, payable, amount=180000, currency="USD",
                reason="api error: [validation_failed] field=source_currency",
                object_id=None, status=None):
    """Write a real plan step through the REAL writer.

    A hand-assembled payload agrees with every other hand-assembled payload and
    with nothing the app produces; `persist_plan` is what the app calls.
    """
    from app.db import session_scope
    from app.domain.plan_store import new_run_id, persist_plan
    from app.domain.planner import ACT_PAY, Step
    from app.models import PlanStep

    st = Step(payable_number=payable, action=ACT_PAY,
              amount_minor=amount, currency=currency, reason=reason)
    with session_scope() as s:
        persist_plan(s, new_run_id(), [st])
    if object_id is not None or status is not None:
        # session_scope commits; a bare db.session() does not, and an uncommitted
        # update is exactly the kind of thing that makes a test look flaky.
        with session_scope() as s:
            q = s.query(PlanStep)
            if object_id is not None:
                q = q.filter_by(airwallex_object_id=None)
            updates = {}
            if object_id is not None:
                updates["airwallex_object_id"] = object_id
            if status is not None:
                updates["status"] = status
            q.update(updates, synchronize_session=False)


def _cancelled_step(db, payable="PO-7003",
                    reason="api error: [validation_failed] field=source_currency",
                    amount=180000):
    """A step the executor could not send — the 0.00 'approvable' cards."""
    from app.models import StepStatus
    _write_step(db, payable=payable, reason=reason, amount=amount,
                status=StepStatus.CANCELLED)


TABS = ["home", "money-in", "money-out", "ok", "ai"]


# ------------------------------------------------------------------ landing
class TestLanding:
    def test_serves_200_and_the_hero_copy(self):
        html = _client().get("/").text
        assert "Closebook" in html
        assert "Your books close" in html
        assert "while the money moves" in html

    def test_has_every_section_the_mockup_specifies(self):
        html = _client().get("/").text
        for anchor in ('id="how"', 'id="safety"', 'id="who"'):
            assert anchor in html, f"missing section {anchor}"
        for heading in ("The problem", "How it works", "Safety",
                        "Who it's for", "Built on"):
            assert heading in html

    def test_launch_app_is_nav_and_closing_cta_only_not_the_hero(self):
        html = render_landing()
        assert html.count(">Launch app<") == 2
        assert 'href="/app"' in html

    def test_landing_carries_no_live_figures(self):
        """Illustrative numbers are fine; a live balance must never appear."""
        html = render_landing()
        assert "ledger" not in html.lower()

    def test_uses_the_mockup_palette_and_inter(self):
        """Accent #5B5BD6, ground #F6F7F9, Inter — from the stylesheet, which is
        where the mockup's tokens live once extracted."""
        from app.ui import _head
        head = _head("Closebook")
        assert "/static/closebook.css" in head
        assert "Inter" in head

        css = open(os.path.join(os.path.dirname(__file__), "..", "backend",
                                "app", "static", "closebook.css"),
                   encoding="utf-8").read().lower()
        assert "#5b5bd6" in css          # accent
        assert "#f6f7f9" in css          # ground
        assert "#ffffff" in css          # cards

    def test_footer_states_sandbox_only(self):
        assert "sandbox" in render_landing().lower()

    def test_stylesheet_is_linked_with_a_cache_buster(self):
        assert "/static/closebook.css?v=" in render_landing()

    def test_primary_button_label_is_white_on_the_accent(self):
        """The nav's `.x-l-links a` outranks `.x-btn--primary`, so a primary
        button inside the nav inherited the muted link colour and rendered at
        1.10:1 — unreadable. Assert the rule that fixes it is present."""
        css = open(os.path.join(os.path.dirname(__file__), "..", "backend",
                                "app", "static", "closebook.css"),
                   encoding="utf-8").read()
        assert ".x-l-links a.x-btn--primary" in css
        # the nested rule must actually set white, not just exist
        m = re.search(r"\.x-l-links a\.x-btn--primary[^{]*\{([^}]*)\}", css)
        assert m and "color: #fff" in m.group(1)


# ---------------------------------------------------------------- app shell
class TestAppShell:
    def test_every_tab_renders_200(self):
        c = _client()
        for tab in TABS:
            r = c.get("/app", params={"tab": tab})
            assert r.status_code == 200, tab
            assert len(r.text) > 1500, tab

    def test_unknown_tab_falls_back_to_home(self):
        html = _client().get("/app", params={"tab": "nope"}).text
        assert "Everything money did" in html

    def test_sidebar_lists_all_five_destinations(self):
        html = render_app(_ctx(), tab="home")
        for label in ("Home", "Money in", "Money out", "Needs your OK",
                      "AI activity"):
            assert label in html

    def test_back_to_site_links_to_root(self):
        assert 'href="/"' in render_app(_ctx(), tab="home")

    def test_current_tab_is_marked_for_assistive_tech(self):
        assert "aria-current" in render_app(_ctx(), tab="money-out")

    def test_mobile_gets_a_bottom_tab_bar(self):
        # present in the DOM; shown under 768px purely by CSS
        assert 'class="x-tabs"' in render_app(_ctx(), tab="home")

    def test_no_mockup_toasts_survive(self):
        for tab in TABS:
            html = render_app(_ctx(), tab=tab)
            assert "nothing was recorded" not in html.lower()
            assert "Mockup:" not in html

    def test_no_hardcoded_currency_figures(self):
        """A number on /app must come from the context, not from ui.py.

        The budget DOES appear — it is read from settings by the context. What
        must not appear is a figure this module invented: the check is that the
        rendered value equals the context's own value.
        """
        ctx = _ctx()
        html = render_app(ctx, tab="home")
        expected = f"{ctx['budget']['expected_inflows_minor'] / 100:,.2f}"
        assert expected in html
        # a number that is in neither the context nor a real record
        assert "99,999.99" not in html


# ------------------------------------------------------------ label mapping
class TestLabelMapping:
    def test_cash_you_have_is_the_ledger_label(self):
        html = render_app(_ctx(), tab="home")
        assert "Cash you have" in html
        assert "no cash yet" in html   # empty DB says so, invents nothing

    def test_cash_set_aside_is_not_the_label_any_more(self, db):
        """The old tile called EXPECTED INFLOWS "Cash set aside" — money coming
        in, described as cash we hold and as an obligation. Both wrong."""
        html = render_app(_ctx(), tab="home")
        assert "Cash set aside" not in html
        assert "Promised to suppliers" in html

    def test_promised_to_suppliers_comes_from_open_payables(self, db):
        _payable(db, number="PO-7200", currency="USD", amount=500_000)
        ctx = build_context([])
        assert ctx["money"]["promised"].get("USD", 0) >= 500_000
        html = render_app(ctx, tab="home")
        assert "Promised to suppliers" in html
        assert "5,000.00" in html

    def test_free_to_spend_subtracts_promised_and_the_floor(self, db):
        """free = cash - promised - reserve floor, per currency."""
        _payable(db, number="PO-7201", currency="USD", amount=500_000)
        ctx = build_context([])
        m = ctx["money"]
        cash_usd = int(ctx["cash"].get("USD", 0))
        assert m["free"]["USD"] == cash_usd - 500_000 - m["reserve_floor_minor"]

    def test_the_floor_is_charged_only_to_the_budget_currency(self, db):
        _payable(db, number="PO-7202", currency="USD", amount=100_000)
        ctx = build_context([])
        m = ctx["money"]
        # GBP is not the budget currency, so no reserve is taken from it.
        assert m["reserve_applies_to"] == "USD"
        assert m["free"].get("GBP", 0) == int(ctx["cash"].get("GBP", 0))

    def test_a_negative_free_to_spend_is_reported_as_a_shortfall(self, db):
        # Owe far more than we hold.
        _payable(db, number="PO-7203", currency="USD", amount=99_000_000)
        ctx = build_context([])
        assert ctx["money"]["shortfall"], "a negative free figure must be flagged"
        html = render_app(ctx, tab="home")
        assert "Short by" in html
        # and the headline figure must not read as money we can spend
        assert "Free to spend" in html

    def test_a_shortfall_names_the_currency(self, db):
        _payable(db, number="PO-7204", currency="EUR", amount=50_000)
        ctx = build_context([])
        html = render_app(ctx, tab="home")
        assert "EUR" in html
        assert "Short by" in html or not ctx["money"]["shortfall"]

    def test_tile_labels_carry_no_question_marks(self, db):
        _payable(db, number="PO-7205", currency="USD", amount=1000)
        html = render_app(build_context([]), tab="home")
        for label in ("Cash you have", "Promised to suppliers",
                      "Free to spend", "Automatic limit"):
            assert f'>{label}<' in html, label
        assert '>?</span>' not in html

    def test_free_to_spend_is_computed_not_hardcoded(self, db):
        """Two different positions must produce two different figures."""
        _payable(db, number="PO-7207", currency="USD", amount=100_000)
        ctx_a = build_context([])
        _payable(db, number="PO-7206", currency="USD", amount=250_000)
        ctx_b = build_context([])
        assert ctx_a["money"]["free"]["USD"] != ctx_b["money"]["free"]["USD"]
        # one more promise of 2,500.00 reduces free-to-spend by exactly that
        assert (ctx_a["money"]["free"]["USD"]
                - ctx_b["money"]["free"]["USD"]) == 250_000

    def test_an_empty_book_shows_no_currency_rows(self, db):
        """No cash and no payables means nothing to list — not a wall of 0.00."""
        ctx = build_context([])
        assert ctx["money"]["free"] == {}
        assert ctx["money"]["promised"] == {}

    def test_money_we_couldnt_match_is_suspense(self):
        assert "Money we couldn" in render_app(_ctx(), tab="home")

    def test_automatic_limit_is_the_auto_pay_limit(self):
        html = render_app(_ctx(), tab="home")
        assert "Automatic limit" in html
        assert "Per payment" in html

    def test_todays_checklist_is_present(self):
        html = render_app(_ctx(), tab="home")
        assert "checklist" in html

    def test_step_tracker_uses_the_five_states(self, db):
        """The tracker needs a payable row to render; a step alone is not enough."""
        _payable(db)
        _write_step(db, payable="PO-7001", amount=900000, currency="EUR",
                    reason="ok")
        html = render_app(build_context([]), tab="money-out")
        for label in ("Planned", "Checked", "Approved", "Sent", "Confirmed"):
            assert label in html

    def test_ai_feed_labelled_as_trace_rows(self):
        html = render_app(_ctx(), tab="ai")
        assert "The model" in html
        assert "The rules" in html


def _payable(db, number="PO-7001", currency="EUR", amount=900000):
    """A supplier + payable, as the seed would create them.

    The tracker and the state-agreement test both need a payable row; a step
    alone is not enough, because the UI renders payables, not steps.
    """
    from app.db import session_scope
    from app.models import InvoicePayable, PayoutMethod, PayStatus, Supplier

    with session_scope() as s:
        sup = Supplier(name="Rotterdam Freight", payout_method=PayoutMethod.SWIFT)
        s.add(sup)
        s.flush()
        s.add(InvoicePayable(number=number, supplier_id=sup.id,
                             currency=currency, amount_minor=amount,
                             status=PayStatus.HELD, criticality=3))


# -------------------------------------------------- problems vs approvable
class TestProblemsNotApprovable:
    def test_cancelled_step_is_a_problem_not_an_approval(self, db):
        _cancelled_step(db)
        ctx = build_context([])
        assert ctx["approvable"] == []
        assert len(ctx["problems"]) >= 1
        assert "validation_failed" in (ctx["problems"][0]["reason"] or "")
        # plain English, not a raw API string
        assert ctx["problems"][0]["plain_reason"].startswith("The bank rejected")
        assert "Nothing needs your OK" in render_app(ctx, tab="ok")

    def test_duplicate_failures_collapse_to_one_problem(self, db):
        _cancelled_step(db)
        assert len(build_context([])["problems"]) == 1

    def test_a_step_with_no_amount_is_not_approvable(self, db):
        """A 0.00 'approvable' card is exactly how the legacy rows looked real."""
        _cancelled_step(db, amount=None, reason="stale row")
        assert build_context([])["approvable"] == []

    def test_a_step_that_already_moved_is_not_approvable(self, db):
        _write_step(db, payable="PO-7001", amount=900000,
                    currency="EUR", reason="ok",
                    object_id="e4337039-dead-beef")
        assert build_context([])["approvable"] == []

    def test_a_problem_card_renders_on_money_out(self, db):
        _cancelled_step(db)
        html = render_app(build_context([]), tab="money-out")
        assert "Problems" in html
        assert "re-plan" in html.lower()

    def test_raw_error_is_available_but_not_silently_truncated(self, db):
        """A cut-off 'field=reason c' is neither readable nor honest."""
        _cancelled_step(db, reason=("api error: [validation_failed] The request "
                                    "failed our schema validation. Refer to the "
                                    "errors object for erroneous fields, codes "
                                    "and messages. field=reason c"))
        p = build_context([])["problems"][0]
        assert "schema validation" in p["reason"]     # full, unabridged
        assert p["reason"].endswith("reason c")       # not re-cut here
        assert p["plain_reason"] != p["reason"]       # a summary is provided
        # and the summary is what the page shows as the headline
        html = render_app(build_context([]), tab="money-out")
        assert "a required field was missing" in html
        # the raw error is still present, but only behind Details
        assert "<summary>Details</summary>" in html
        i = html.find("<summary>Details</summary>")
        assert "field=reason c" in html[i:i + 900]      # unabridged, expandable
        assert "field=reason c" not in html[:i]         # not the headline


class TestProblemClassification:
    """A banner must never contradict the list under it, and a successful
    payment must never be filed as a failure."""

    def test_banner_reflects_the_problem_count(self, db):
        _cancelled_step(db)
        html = render_app(build_context([]), tab="home")
        assert "Nothing needs you right now" not in html
        assert "but 1 payment failed" in html

    def test_all_clear_banner_when_nothing_is_wrong(self, db):
        html = render_app(build_context([]), tab="home")
        assert "Nothing needs you right now" in html
        assert "payment failed" not in html

    def test_a_successful_payment_is_not_a_problem(self, db):
        """SUBMITTED + settled + plan prose is a payment that went through."""
        _write_step(db, payable="PO-7003", amount=180000, reason="ok",
                    object_id="1010946e-ok", status=StepStatus.SUBMITTED)
        ctx = build_context([])
        assert ctx["problems"] == []
        assert ctx["past_attempts"] == []

    def test_a_successful_payment_is_not_a_past_attempt(self, db):
        """Listing a settled success under 'Past attempts' reads as a recovered
        failure, which is a lie."""
        _write_step(db, payable="PO-7003", amount=180000,
                    reason="USD 1,800.00 is within the auto-pay limit",
                    object_id="1010946e-ok", status=StepStatus.SUBMITTED)
        ctx = build_context([])
        assert all(p["payable_number"] != "PO-7003"
                   for p in ctx["past_attempts"])

    def test_a_failure_is_not_hidden_behind_a_later_success(self, db):
        """Rows arrive newest-first; the failure must still win for its PO."""
        _payable(db, number="PO-7003")
        with session_scope() as s:
            persist_plan(s, new_run_id(), [Step(
                payable_number="PO-7003", action=ACT_PAY,
                amount_minor=180000, currency="USD", reason="sent ok")])
        # now cancel it, as the executor would
        _cancelled_step(db, payable="PO-7003")
        ctx = build_context([])
        assert any(p["payable_number"] == "PO-7003"
                   for p in ctx["problems"]), "failure was dropped"

    def test_a_problem_payable_is_not_also_submitted_in_the_checklist(self, db):
        """SUBMITTED in the checklist plus a Problem card was the contradiction."""
        _payable(db, number="PO-7003")
        with session_scope() as s:
            persist_plan(s, new_run_id(), [Step(
                payable_number="PO-7003", action=ACT_PAY,
                amount_minor=180000, currency="USD", reason="sent ok")])
        _cancelled_step(db, payable="PO-7003")
        html = render_app(build_context([]), tab="home")
        seg = html[html.find("checklist"):]
        assert "failed" in seg
        assert "SUBMITTED" not in seg

    def test_past_attempts_are_collapsed_not_listed_as_problems(self, db):
        from app.models import PlanStep
        # A failure from an older run is history.
        with session_scope() as s:
            persist_plan(s, "run_old", [Step(
                payable_number="PO-7009", action=ACT_PAY,
                amount_minor=1000, currency="USD", reason="api error")])
            s.query(PlanStep).filter_by().update(
                {"status": StepStatus.CANCELLED}, synchronize_session=False)
        with session_scope() as s:
            persist_plan(s, "run_new", [Step(
                payable_number="PO-7001", action=ACT_PAY,
                amount_minor=900000, currency="EUR", reason="ok")])
        ctx = build_context([])
        assert any(p["payable_number"] == "PO-7009"
                   for p in ctx["past_attempts"])
        assert not any(p["payable_number"] == "PO-7009"
                       for p in ctx["problems"])


class TestPlainReasons:
    def test_validation_error_becomes_plain_english(self):
        from app.dashboard import _plain_reason
        assert _plain_reason("api error: [validation_failed] field=source_currency"
                             ).startswith("The bank rejected")

    def test_an_empty_reason_still_says_something(self):
        from app.dashboard import _plain_reason
        assert _plain_reason("") == "It failed. No reason was recorded."
        assert _plain_reason(None) == "It failed. No reason was recorded."

    def test_unknown_reasons_are_capped_on_a_word_boundary(self):
        from app.dashboard import _plain_reason
        long = "word " * 60
        out = _plain_reason(long)
        assert len(out) < 130
        assert not out.endswith("wor")   # never cut mid-word
        assert out.endswith("…")


class TestReasonIsRecordedNotRecomputed:
    """The view must display what code decided.

    The two old strings ("too small to be a deliberate part-payment" / "too large
    to be a bank fee") were appended unconditionally for any matched proposal,
    while the comment claimed they were derived from the numbers. They were not.
    """

    def _unmatched(self, db, *, decided=None, reference="", proposed=None):
        """An AMBIGUOUS inflow plus its trace, shaped like the live rows."""
        from app.db import session_scope
        from app.models import Inflow, MatchStatus

        with session_scope() as s:
            inf = Inflow(currency="USD", amount_minor=440000,
                         payer_name="L. Stores", reference_raw=reference)
            s.add(inf)
            s.flush()
            inflow_id = inf.id

        with session_scope() as s:
            s.add(AgentTrace(
                run_id="r1", tool="propose_match",
                inputs_json=f'{{"inflow_id": {inflow_id}}}',
                outputs_json=json.dumps({"model_proposed": proposed or {
                    "allocations": [{"invoice_number": "INV-0043",
                                     "amount": 4400.0}],
                    "confidence": 0.6, "rationale": "L. Stores matches"}}),
                verdict="accepted", detail="proposal recorded"))

        if decided is not None:
            with session_scope() as s:
                s.add(AgentTrace(
                    run_id="r1", tool="decide_match",
                    inputs_json=f'{{"inflow_id": {inflow_id}}}',
                    outputs_json=json.dumps(decided),
                    verdict="escalated", detail=decided.get("reason")))

        with session_scope() as s:
            s.query(Inflow).filter_by(id=inflow_id).update(
                {"match_status": MatchStatus.AMBIGUOUS},
                synchronize_session=False)

    def test_displays_the_shortfall_reason_the_rule_recorded(self, db):
        self._unmatched(db, decided={
            "status": "AMBIGUOUS", "confidence": 0.70, "band": "ESCALATE",
            "reason": "amount is 100.00 short of INV-0043 (2.2%) — too large "
                      "for a bank fee, too small to be a deliberate "
                      "part-payment; needs a human",
            "evidence": {"shortfall_minor": 10000, "short_fraction": 0.0222}})
        html = render_app(build_context([]), tab="money-in")
        assert "100.00 short of INV-0043" in html
        assert "needs a human" in html
        # the escalation line names the real threshold
        assert "0.75 line" in html

    def test_overpayment_is_reported_as_an_overpayment(self, db):
        self._unmatched(db, decided={
            "status": "OVERPAID", "confidence": 0.70, "band": "ESCALATE",
            "reason": "overpayment of 50.00 against INV-0043; creates a refund "
                      "obligation that needs a human",
            "evidence": {"overpayment_minor": 5000}})
        html = render_app(build_context([]), tab="money-in")
        assert "overpayment of 50.00" in html.lower()
        assert "refund" in html.lower()
        assert "100.00 short" not in html          # no invented shortfall

    def test_within_bank_fee_tolerance_is_stated_as_such(self, db):
        self._unmatched(db, decided={
            "status": "PARTIAL", "confidence": 0.93, "band": "APPROVAL",
            "reason": "partial payment against INV-0043; 5.00 short, within "
                      "bank-fee tolerance",
            "evidence": {"shortfall_minor": 500}})
        html = render_app(build_context([]), tab="money-in")
        assert "within bank-fee tolerance" in html
        assert "too large to be a bank fee" not in html
        # the band is named as CODE's decision, not the model's score
        assert "Code: held for approval" in html

    def test_below_threshold_reasons_name_the_real_thresholds(self, db):
        self._unmatched(db, decided={
            "status": "AMBIGUOUS", "confidence": 0.70, "band": "ESCALATE",
            "reason": "no confident match; a human decides",
            "evidence": {}})
        html = render_app(build_context([]), tab="money-in")
        assert "no confident match" in html.lower()
        assert "0.75" in html
        # and never the two invented claims
        assert "too small to be a deliberate part-payment" not in html
        assert "too large to be a bank fee" not in html

    def test_no_recorded_decision_is_admitted_not_invented(self, db):
        self._unmatched(db, decided=None)
        html = render_app(build_context([]), tab="money-in")
        assert "No decision was recorded" in html

    def test_no_reference_is_stated(self, db):
        self._unmatched(db, decided={
            "status": "AMBIGUOUS", "confidence": 0.70, "band": "ESCALATE",
            "reason": "amount is 100.00 short of INV-0043", "evidence": {}},
            reference="")
        html = render_app(build_context([]), tab="money-in")
        assert "no reference" in html.lower()


class StubLLM:
    """Stands in for the OpenAI-compatible matcher."""

    def __init__(self, response):
        self.response = response

    def propose(self, inflow, candidates):
        return self.response


def _c(inv_id, number, customer, currency, open_minor, original=None):
    return Candidate(invoice_id=inv_id, number=number, customer_name=customer,
                     currency=currency, amount_open_minor=open_minor,
                     amount_original_minor=original or open_minor)


class TestLlmAllocationUnitsAndShape:
    """The LLM tier silently never worked.

    Three separate contract mismatches between `try_llm` and the real client,
    each of which made a CORRECT proposal look unusable while the beat recorded
    "the model was offline" with a working key configured.
    """

    def _cands(self):
        return [_c(1, "INV-0043", "Lumen Stores", "USD", 450_000)]

    def test_model_allocations_in_major_units_are_accepted(self):
        """The model answers 4400 for a 440000-minor inflow. Comparing them raw
        made every correct proposal read as a 100x under-allocation."""
        from app.domain.matching import match_inflow
        r = match_inflow(
            440_000, "USD", {"payer_name": "L. Stores", "reference_raw": ""},
            self._cands(),
            llm=StubLLM({"allocations": {"1": 4_400}, "confidence": 0.75,
                         "rationale": "partial payment"}))
        assert r.method == "LLM", r.reason
        assert r.status != "AMBIGUOUS" or "under-allocate" not in (r.reason or "")

    def test_a_list_of_allocations_is_accepted(self):
        """The client returns a LIST of {invoice_number, amount}; try_llm
        expected a dict keyed by invoice_id and discarded it."""
        from app.domain.matching import match_inflow
        r = match_inflow(
            440_000, "USD", {"payer_name": "L. Stores", "reference_raw": ""},
            self._cands(),
            llm=StubLLM({"allocations": [{"invoice_number": "INV-0043",
                                          "amount": 4_400}],
                         "confidence": 0.75, "rationale": "partial"}))
        assert "no allocations" not in (r.reason or ""), r.reason

    def test_the_matcher_sends_the_keys_the_client_reads(self):
        """KeyError: the payload used amount_minor/payer_name/amount_open_minor
        while the client reads amount/payer/amount_open.

        Hermetic by construction: LLMConfig is built explicitly rather than via
        from_settings(), so this passes in CI with no .env and no key. Using
        from_settings() made the test read a developer's real key — present
        locally, absent in CI, where propose() short-circuits on
        `cfg.available` and `_call` is never reached.
        """
        from app.domain.llm import LLMConfig, OpenAICompatibleMatcher
        seen = {}

        class Recorder(OpenAICompatibleMatcher):
            def _call(self, inflow, candidates):
                seen["inflow"] = inflow
                seen["cands"] = candidates
                return {"allocations": {"1": 4_400}, "confidence": 0.75}

        cfg = LLMConfig(api_key="test-key-not-a-real-secret",
                        base_url="https://example.invalid/v1",
                        model="test-model")
        assert cfg.available, "an explicit key must make the client available"
        m = Recorder(cfg)
        m.propose({"amount": "4400.00", "currency": "USD", "payer": "L. Stores",
                   "reference": ""},
                  [{"number": "INV-0043", "currency": "USD",
                    "amount_open": "4500.00", "customer": "Lumen Stores"}])
        assert seen["inflow"]["amount"] == "4400.00"
        assert "amount_minor" not in seen["inflow"]
        assert seen["cands"][0]["amount_open"] == "4500.00"


def _needs_llm_candidates():
    """Candidates that deliberately defeat the deterministic tiers, so a match
    can only come from the LLM tier."""
    return [_c(1, "INV-9001", "Globex", "USD", 700_000),
            _c(2, "INV-9002", "Globex", "USD", 500_000)]


class TestModelNeverGradesItself:
    """The model's self-reported confidence must never reach the AUTO band.

    MATCH_CONFIDENCE_AUTO is 0.90 and the comparison is `>=`, so before the fix
    a model claiming 0.95 (MATCHED) or 0.9 (PARTIAL) landed in AUTO and applied
    money with no human — the model marking its own homework.
    """

    def test_the_llm_ceiling_is_strictly_below_the_auto_threshold(self):
        from app.domain.matching import _LLM_MAX_CONFIDENCE
        from app.domain.policy import MATCH_CONFIDENCE_AUTO
        assert _LLM_MAX_CONFIDENCE < MATCH_CONFIDENCE_AUTO, (
            "the LLM ceiling must stay below the auto threshold, or a model "
            "can reach the AUTO band by grading itself")

    def test_a_model_claiming_099_matched_is_held_for_approval(self):
        from app.domain.matching import match_inflow
        from app.domain.policy import (MATCH_CONFIDENCE_AUTO,
                                       classify_confidence)
        r = match_inflow(
            700_000, "USD", {"payer_name": "", "reference_raw": "batch"},
            _needs_llm_candidates(),
            llm=StubLLM({"allocations": {"1": 7_000}, "confidence": 0.99,
                         "rationale": "certain about this"}))
        assert r.method == "LLM"
        assert r.status == "MATCHED"
        assert r.confidence < MATCH_CONFIDENCE_AUTO
        assert classify_confidence(r.confidence) == "APPROVAL"

    def test_a_model_claiming_09_partial_is_held_for_approval(self):
        from app.domain.matching import match_inflow
        from app.domain.policy import (MATCH_CONFIDENCE_AUTO,
                                       classify_confidence)
        r = match_inflow(
            700_000, "USD", {"payer_name": "", "reference_raw": "batch"},
            _needs_llm_candidates(),
            llm=StubLLM({"allocations": {"1": 6_000}, "confidence": 0.9,
                         "rationale": "close enough"}))
        assert r.status == "PARTIAL"
        assert r.confidence < MATCH_CONFIDENCE_AUTO
        assert classify_confidence(r.confidence) == "APPROVAL"

    def test_the_models_own_number_is_kept_as_a_separate_claim(self):
        """The number is preserved for display, labelled as the model's claim."""
        from app.domain.matching import match_inflow
        r = match_inflow(
            700_000, "USD", {"payer_name": "", "reference_raw": "batch"},
            _needs_llm_candidates(),
            llm=StubLLM({"allocations": {"1": 7_000}, "confidence": 0.99,
                         "rationale": "certain"}))
        assert r.evidence["model_claimed_confidence"] == 0.99
        assert r.confidence != 0.99

    def test_no_llm_result_of_any_confidence_reaches_auto(self):
        """Sweep the whole plausible range, not just the two reported cases."""
        from app.domain.matching import match_inflow
        from app.domain.policy import classify_confidence
        for claimed in (0.90, 0.91, 0.95, 0.99, 1.0):
            r = match_inflow(
                700_000, "USD", {"payer_name": "", "reference_raw": "batch"},
                _needs_llm_candidates(),
                llm=StubLLM({"allocations": {"1": 7_000},
                             "confidence": claimed, "rationale": "r"}))
            assert classify_confidence(r.confidence) == "APPROVAL", claimed
            assert classify_confidence(claimed) == "AUTO", \
                "the claim itself would have been AUTO — that is the bug"


class TestSandboxWording:
    """No transfer in this project may be described as live money."""

    def test_a_submitted_transfer_is_not_labelled_paid(self, db):
        """SUBMITTED is what WE did; only the bank can say PAID."""
        _payable(db, number="PO-7001")
        _write_step(db, payable="PO-7001", amount=900000, currency="EUR",
                    reason="sent", object_id="e4337039-abc",
                    status=StepStatus.SUBMITTED)
        ctx = build_context([])
        po = next(p for p in ctx["payables"] if p["number"] == "PO-7001")
        assert po["live_status"] is not None or po["live_status"] is None
        html = render_app(ctx, tab="money-out")
        assert "sandbox" in html
        # the recorded status is shown, not a "Paid" claim
        assert ">Paid<" not in html

    def test_live_status_is_read_from_the_context(self, db):
        _payable(db, number="PO-7001")
        _write_step(db, payable="PO-7001", amount=900000, currency="EUR",
                    reason="sent", object_id="e4337039-abc",
                    status=StepStatus.SUBMITTED)
        ctx = build_context([])
        ctx["payables"][0]["live_status"] = "PROCESSING"
        html = render_app(ctx, tab="money-out")
        assert "PROCESSING" in html

    def test_the_app_never_says_live_money(self):
        from app.ui import render_landing
        assert "live money" not in render_landing().lower()


class TestHeldPaymentsAreExplained:
    """A held payment must show the rule and its numbers, and the header must
    agree with the tab. "Nothing needs your OK" above a held payment is the
    contradiction this replaced."""

    def _held(self, db, *, depends_on=None, amount=700_000, ccy="USD"):
        """A payable whose dependency is unpaid, so the planner DEFERs it."""
        from app.db import session_scope
        from app.models import (InvoicePayable, InvoiceReceivable, PayStatus,
                                PayoutMethod, RecStatus, Supplier, Customer)

        with session_scope() as s:
            dep_id = None
            if depends_on:
                cust = Customer(name="Harrow & Co")
                s.add(cust)
                s.flush()
                rec = InvoiceReceivable(
                    number=depends_on, customer_id=cust.id, currency="GBP",
                    amount_minor=800_000, amount_paid_minor=480_000,
                    status=RecStatus.PARTIAL)
                s.add(rec)
                s.flush()
                dep_id = rec.id          # WITHOUT this the payable is not held
            sup = Supplier(name="Shenzhen Weave Co",
                           payout_method=PayoutMethod.LOCAL)
            s.add(sup)
            s.flush()
            s.add(InvoicePayable(
                number="PO-7100", supplier_id=sup.id, currency=ccy,
                amount_minor=amount, status=PayStatus.HELD, criticality=3,
                depends_on_receivable_id=dep_id))

    def test_a_held_payable_shows_the_planners_own_reason(self, db):
        self._held(db, depends_on="INV-0042")
        ctx = build_context([])
        po = next(p for p in ctx["payables"] if p["number"] == "PO-7100")
        assert po["plan_action"] == "DEFER"
        assert po["plan_policy_reason"] == "dependency_not_met"
        # the reason names the invoice AND the outstanding amount
        assert "INV-0042" in po["plan_reason"]
        assert "3,200" in po["plan_reason"].replace("3200", "3,200")

    def test_the_hold_reason_reaches_the_page(self, db):
        self._held(db, depends_on="INV-0042")
        html = render_app(build_context([]), tab="home")
        assert "INV-0042" in html
        assert "part-payment does not release the supplier" in html

    def test_the_header_agrees_that_a_payment_is_held(self, db):
        self._held(db, depends_on="INV-0042")
        ctx = build_context([])
        assert ctx["waiting"], "the payable must be reported as waiting"
        html = render_app(ctx, tab="home")
        # it must NOT claim nothing is happening
        assert "on hold" in html
        assert "Nothing needs you right now" not in html

    def test_the_ok_tab_shows_held_payments_too(self, db):
        """Header and tab must agree: if the header mentions a hold, the tab
        the header links to must show it."""
        self._held(db, depends_on="INV-0042")
        ctx = build_context([])
        html = render_app(ctx, tab="ok")
        assert "Held — and not waiting on you" in html
        assert "INV-0042" in html

    def test_a_payable_over_the_limit_without_an_object_id_awaits_approval(self, db):
        """Over the auto-pay limit and not yet sent => needs a human."""
        from app.db import session_scope
        from app.models import InvoicePayable, PayStatus, PayoutMethod, Supplier

        with session_scope() as s:
            sup = Supplier(name="Big Co", payout_method=PayoutMethod.LOCAL)
            s.add(sup)
            s.flush()
            s.add(InvoicePayable(number="PO-7101", supplier_id=sup.id,
                                 currency="USD", amount_minor=900_000,
                                 status=PayStatus.HELD, criticality=3))
        ctx = build_context([])
        po = next(p for p in ctx["payables"] if p["number"] == "PO-7101")
        assert po["plan_needs_approval"] is True
        assert any(p["number"] == "PO-7101" for p in ctx["awaiting_approval"])
        html = render_app(ctx, tab="home")
        assert "waiting for your OK" in html

    def test_an_already_sent_payable_is_not_asked_for_approval_again(self, db):
        """A submitted step already has its approval. Re-asking invites a second
        payment for the same bill."""
        from app.db import session_scope
        from app.models import InvoicePayable, PayStatus, PayoutMethod, Supplier

        with session_scope() as s:
            sup = Supplier(name="Big Co", payout_method=PayoutMethod.LOCAL)
            s.add(sup)
            s.flush()
            s.add(InvoicePayable(number="PO-7102", supplier_id=sup.id,
                                 currency="USD", amount_minor=900_000,
                                 status=PayStatus.HELD, criticality=3))
        _write_step(db, payable="PO-7102", amount=900_000, currency="USD",
                    reason="sent", object_id="obj-7102",
                    status=StepStatus.SUBMITTED)
        ctx = build_context([])
        assert not any(p["number"] == "PO-7102"
                       for p in ctx["awaiting_approval"])


class TestStaleFailuresDoNotMaskSuccess:
    """Three pre-fix CANCELLED rows for PO-7003 outranked the newer SUBMITTED
    transfer that actually went out, so the page claimed a payment failed when
    it was in flight."""

    def _legacy_step(self, db, *, payable, status, reason, obj=None):
        """Insert a PlanStep row DIRECTLY.

        `persist_plan` converges on one row per (payable, action), so a payable
        can only hold several rows if they predate that convergence — which is
        exactly the real PO-7003 situation. Reproducing it means writing the rows
        by hand, not through the writer that now prevents it.
        """
        import json as _json
        from app.db import session_scope
        from app.models import PlanStep, StepType

        with session_scope() as s:
            s.add(PlanStep(
                run_id=None, type=StepType.TRANSFER,
                payload_json=_json.dumps({"payable_number": payable,
                                          "action": "PAY"}),
                reason=reason, status=status, airwallex_object_id=obj))

    def test_a_newer_success_supersedes_an_older_failure(self, db):
        from app.models import StepStatus

        self._legacy_step(db, payable="PO-7103", status=StepStatus.CANCELLED,
                          reason="api error: [validation_failed] old")
        self._legacy_step(db, payable="PO-7103", status=StepStatus.SUBMITTED,
                          reason="sent ok", obj="obj-7103")

        ctx = build_context([])
        # THE BUG: the page reported PO-7003 as failed while it was in flight.
        assert not any(p["payable_number"] == "PO-7103"
                       for p in ctx["problems"]), "a superseded failure is not current"
        # The failed attempt is still real history, and stays visible as such —
        # it is the evidence for what went wrong, not a claim about now.
        hist = [p for p in ctx["past_attempts"]
                if p["payable_number"] == "PO-7103"]
        assert len(hist) == 1
        assert hist[0]["status"] == "CANCELLED"

    def test_a_success_itself_is_never_listed_as_a_past_attempt(self, db):
        """A payment that went through must not appear under a failures heading:
        that reads as a failure that happened to recover, which is false."""
        from app.models import StepStatus

        self._legacy_step(db, payable="PO-7106", status=StepStatus.SUBMITTED,
                          reason="within the automatic limit", obj="obj-7106")
        ctx = build_context([])
        assert not any(p["payable_number"] == "PO-7106"
                       for p in ctx["problems"] + ctx["past_attempts"])

    def test_the_superseded_failure_keeps_its_exact_reason_as_history(self, db):
        from app.models import StepStatus

        self._legacy_step(
            db, payable="PO-7104", status=StepStatus.CANCELLED,
            reason=("api error: [validation_failed] bad. | "
                    "field=source_currency code=001 | bad."))
        self._legacy_step(db, payable="PO-7104", status=StepStatus.SUBMITTED,
                          reason="sent ok", obj="obj-7104")

        ctx = build_context([])
        hist = [p for p in ctx["past_attempts"]
                if p["payable_number"] == "PO-7104"]
        assert hist, "history must not be silently dropped"
        assert hist[0]["api_error"]["field"] == "source_currency"
        assert hist[0]["api_error"]["code"] == "001"

    def test_the_current_state_is_the_newest_row_not_the_worst_one(self, db):
        """Guards the original bug from the other side: an older FAILURE must not
        outrank a newer success, and a newer failure must still be reported."""
        from app.models import StepStatus

        self._legacy_step(db, payable="PO-7105", status=StepStatus.SUBMITTED,
                          reason="sent ok", obj="obj-7105")
        self._legacy_step(db, payable="PO-7105", status=StepStatus.FAILED,
                          reason="api error: [validation_failed] newest")

        ctx = build_context([])
        assert any(p["payable_number"] == "PO-7105"
                   for p in ctx["problems"]), "the NEWEST row is a failure"


class TestDisplayedStateIsTheNewestRow:
    """`payable_state` read `existing is None or action == "TRANSFER"`, which is
    true for EVERY transfer row — so each older transfer overwrote the newer one
    and the OLDEST won. PO-7003 displayed CANCELLED (its first failed attempt)
    while its newest step was SUBMITTED and in flight."""

    def _step(self, db, *, payable, status, obj, reason="r", action="PAY",
              amount=180_000):
        import json as _json
        from app.db import session_scope
        from app.models import PlanStep, StepType

        with session_scope() as s:
            s.add(PlanStep(
                run_id=None, type=StepType.TRANSFER,
                payload_json=_json.dumps({"payable_number": payable,
                                          "action": action,
                                          "amount_minor": amount,
                                          "currency": "USD"}),
                reason=reason, status=status, airwallex_object_id=obj))

    def test_the_oldest_transfer_does_not_overwrite_the_newest(self, db):
        from app.models import StepStatus

        _payable(db, number="PO-7300", currency="USD", amount=180_000)
        self._step(db, payable="PO-7300", status=StepStatus.CANCELLED,
                   obj="old-7300")
        self._step(db, payable="PO-7300", status=StepStatus.SUBMITTED,
                   obj="new-7300")
        ctx = build_context([])
        po = next(p for p in ctx["payables"] if p["number"] == "PO-7300")
        assert po["display_status"] == "SUBMITTED", "the newest row is the state"
        assert po["airwallex_object_id"] == "new-7300"

    def test_a_transfer_still_outranks_an_fx_step_for_the_same_payable(self, db):
        """The original intent stands: the payment describes a payable, not the
        conversion that funds it."""
        import json as _json
        from app.db import session_scope
        from app.models import PlanStep, StepStatus, StepType

        _payable(db, number="PO-7301", currency="USD", amount=100_000)

        with session_scope() as s:
            # FX is NEWER, but the transfer is what the payable IS
            s.add(PlanStep(run_id=None, type=StepType.TRANSFER,
                           payload_json=_json.dumps({"payable_number": "PO-7301",
                                                     "action": "PAY",
                                                     "amount_minor": 100}),
                           reason="pay", status=StepStatus.SUBMITTED,
                           airwallex_object_id="tr-7301"))
            s.add(PlanStep(run_id=None, type=StepType.FX,
                           payload_json=_json.dumps({"payable_number": "PO-7301",
                                                     "action": "FX",
                                                     "amount_minor": 0}),
                           reason="fx", status=StepStatus.SUBMITTED,
                           airwallex_object_id="fx-7301"))
        ctx = build_context([])
        po = next(p for p in ctx["payables"] if p["number"] == "PO-7301")
        assert po["airwallex_object_id"] == "tr-7301"
        assert po["display_status"] == "SUBMITTED"


class TestLiveStatusOnlyAsksAboutTransfers:
    def test_an_fx_conversion_is_not_looked_up_as_a_transfer(self, db):
        """An FX row's object id is a CONVERSION. Asking /transfers about it
        404s, and the failure reads as 'we could not find out' when the truth is
        it was never a transfer."""
        import json as _json
        from app.db import session_scope
        from app.models import PlanStep, StepStatus, StepType
        import app.dashboard as dash

        with session_scope() as s:
            s.add(PlanStep(run_id=None, type=StepType.FX,
                           payload_json=_json.dumps({"payable_number": "PO-7302",
                                                     "action": "FX"}),
                           reason="fx", status=StepStatus.SUBMITTED,
                           airwallex_object_id="conversion-abc"))

        prev = dash.LIVE_STATUS_ENABLED
        dash.LIVE_STATUS_ENABLED = True
        try:
            # A real client would be constructed; with no credentials the call
            # fails, so assert on WHAT WAS ASKED, not on the answer.
            asked = []
            import app.airwallex.transfers as T

            class FakeTransfers:
                def __init__(self, *a, **k):
                    pass

                def status_of(self, tid):
                    asked.append(tid)
                    return "PROCESSING"

            import app.airwallex.client as C
            orig_c, orig_t = C.AirwallexClient, T.Transfers
            C.AirwallexClient = lambda *a, **k: object()
            T.Transfers = FakeTransfers
            try:
                out = dash.live_transfer_statuses(
                    get_default_db().session())
            finally:
                C.AirwallexClient, T.Transfers = orig_c, orig_t
        finally:
            dash.LIVE_STATUS_ENABLED = prev

        assert asked == [], "a conversion id must never be asked about as a transfer"
        assert out == {} or "conversion-abc" not in out


class TestApiErrorIsExact:
    """Item 3: pull the real code and field out of the stored failure."""

    def test_the_field_and_code_are_parsed(self):
        from app.dashboard import parse_api_error
        r = ("api error: [validation_failed] The request failed our schema "
             "validation. | field=source_currency code=001 | The request failed.")
        d = parse_api_error(r)
        assert d["field"] == "source_currency"
        assert d["code"] == "001"
        assert d["error"] == "validation_failed"

    def test_the_plain_reason_names_the_field(self):
        from app.dashboard import _plain_reason
        r = "api error: [validation_failed] bad. | field=reason code=001 | bad."
        out = _plain_reason(r)
        assert "`reason`" in out
        assert "code 001" in out
        # and it never invents a field when none is recorded
        assert "required field" in _plain_reason(
            "api error: [validation_failed] The request failed our schema "
            "validation. Refer to the errors object.")

    def test_a_reason_with_no_structure_returns_no_detail(self):
        from app.dashboard import parse_api_error
        assert parse_api_error("something went wrong") == {}
        assert parse_api_error("") == {}

    def test_the_raw_error_stays_available_in_full(self, db):
        _cancelled_step(db, reason=("api error: [validation_failed] The request "
                                    "failed our schema validation. | "
                                    "field=source_currency code=001 | tail."))
        ctx = build_context([])
        row = (ctx["problems"] or ctx["past_attempts"])[0]
        assert "schema validation" in row["reason"]
        assert row["reason"].endswith("tail.")    # unabridged


# ------------------------------------------------------------- incidents
class _Inc(dict):
    def to_dict(self):
        return self


def _inc(tid, event, status, recovery="in_flight_watch"):
    return _Inc(transfer_id=tid, event_name=event, recovery=recovery,
                severity="WARN", live_status=status, looked_up=True,
                requires_new_request_id=False, failure_type=None,
                failure_reason=None, summary=None)


class TestIncidentsGrouped:
    def test_three_events_on_one_transfer_make_one_card(self):
        grouped = incidents_block([
            _inc("e4337039", "payout.transfer.failed", "PROCESSING"),
            _inc("e4337039", "payout.transfer.processing", "PROCESSING"),
            _inc("e4337039", "payout.transfer.paid", "PAID", "complete"),
        ])
        assert len(grouped) == 1, "one transfer must be one card"
        assert len(grouped[0]["events"]) == 3
        assert grouped[0]["live_status"] == "PAID"

    def test_different_transfers_stay_separate(self):
        assert len(incidents_block([_inc("a", "x", "P"),
                                    _inc("b", "y", "P")])) == 2

    def test_grouped_card_shows_the_live_status(self):
        ctx = build_context([_inc("e4337039", "failed", "PROCESSING")])
        html = render_app(ctx, tab="home")
        assert "Payments in flight" in html
        assert "PROCESSING" in html


# --------------------------------------------------------- state agreement
class TestStateAgreement:
    def test_payable_status_comes_from_the_plan_step(self, db):
        """HELD on the payable while the plan said SUBMITTED was bug 4."""
        from app.models import StepStatus
        _payable(db)
        _write_step(db, payable="PO-7001", amount=900000,
                    currency="EUR", reason="ok", status=StepStatus.SUBMITTED)

        ctx = build_context([])
        po = next(p for p in ctx["payables"] if p["number"] == "PO-7001")
        assert po["display_status"] == "SUBMITTED"
        assert po["stored_status"] == "HELD"
        assert "SUBMITTED" in render_app(ctx, tab="money-out")


# ------------------------------------------------------------ approve form
def _pending_step(db, payable="PO-7001"):
    """A real approvable step: PROPOSED, no object id, names an amount."""
    _write_step(db, payable=payable, amount=900000,
                currency="EUR", reason="over your automatic limit")


def _pin(monkeypatch, value="1234"):
    """Point the app at a known PIN.

    `demo_approver_pin` is a SecretStr, so the fake must be one too — a plain
    string passes the presence check and then explodes on `.get_secret_value()`,
    which is a failure mode the real config cannot produce.
    """
    from pydantic import SecretStr
    from app.config import Settings
    monkeypatch.setattr("app.main._settings",
                        Settings.model_construct(
                            demo_approver_pin=SecretStr(value)))


class TestApproveWiring:
    def test_pin_input_is_masked_and_the_form_hits_the_endpoint(self, db, monkeypatch):
        _pending_step(db)
        _pin(monkeypatch)
        html = render_app(build_context([]), tab="ok")
        assert 'type="password"' in html
        assert 'autocomplete="one-time-code"' in html
        assert "/approvals/" in html and "/approve" in html

    def test_no_new_money_moving_endpoint_is_invented(self):
        html = (render_app(_ctx(), tab="ok")
                + render_app(_ctx(), tab="money-out")
                + render_app(_ctx(), tab="money-in"))
        for forbidden in ("/pay", "/transfer", "/payout", "/send-money",
                          "/apply-inflow", "/match-invoice"):
            assert forbidden not in html

    def test_unimplemented_action_is_coming_soon(self):
        assert "Coming soon" in render_app(_ctx(), tab="money-in")

    def test_approve_accepts_a_form_post(self, db, monkeypatch):
        _pending_step(db)
        _pin(monkeypatch)
        r = _client().post("/approvals/1/approve",
                           data={"pin": "1234", "approved_by": "test"})
        assert r.status_code == 200
        assert "error" not in r.json()

    def test_wrong_pin_is_refused_with_a_readable_reason(self, db, monkeypatch):
        _pending_step(db)
        _pin(monkeypatch)
        body = _client().post("/approvals/1/approve",
                              data={"pin": "0000"}).json()
        assert body.get("detail")

    def test_reject_accepts_a_form_post(self, db, monkeypatch):
        _pending_step(db)
        _pin(monkeypatch)
        r = _client().post("/approvals/1/reject",
                           data={"pin": "1234", "reason": "not now"})
        assert r.status_code == 200

    def test_closed_without_a_configured_pin(self, db, monkeypatch):
        from app.config import Settings
        _pending_step(db)
        monkeypatch.setattr("app.main._settings",
                            Settings.model_construct(demo_approver_pin=None))
        body = _client().post("/approvals/1/approve",
                              data={"pin": "1234"}).json()
        assert "not configured" in (body.get("detail") or "")


# ------------------------------------------------------------- formatting
class TestFormatting:
    def test_money_is_comma_grouped_two_dp(self):
        assert money(123456789) == "1,234,567.89"
        assert money(0) == "0.00"
        assert money(None) == "0.00"

    def test_escape_prevents_markup_injection(self):
        assert e("<script>") == "&lt;script&gt;"
        assert e('a"b') == "a&quot;b"
        assert e(None) == ""

    def test_no_script_tag_can_be_rendered_from_data(self):
        assert "<script>alert" not in render_app(_ctx(), tab="ai")


# ---------------------------------------------------------------- grouping
class TestStepsBlock:
    def test_empty_db_yields_no_approvable_and_no_problems(self, db):
        blk = steps_block(db.session())
        assert blk["approvable"] == []
        assert blk["problems"] == []
