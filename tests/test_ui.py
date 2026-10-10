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

import os
import re

import pytest
from fastapi.testclient import TestClient

from app.db import reset_default_db, session_scope
from app.dashboard import build_context, incidents_block, steps_block
from app.domain.plan_store import new_run_id, persist_plan
from app.domain.planner import ACT_PAY, Step
from app.main import app
from app.models import StepStatus
from app.ui import e, money, render_app, render_landing


@pytest.fixture(autouse=True)
def _isolated_db():
    """A genuinely empty DB per test — including the app's own.

    `reset_default_db()` only clears the cache used by `dashboard.build_context`.
    `app.main._db` is a separate instance built at import time, so a step written
    through the app's DB is still there for the next test — and a rendering test
    would then pass or fail on rows it did not create.
    """
    reset_default_db()
    import app.main as m
    from app.db import DB
    m._db = DB()
    yield


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
        expected = f"{ctx['budget']['committed_minor'] / 100:,.2f}"
        assert expected in html
        # a number that is in neither the context nor a real record
        assert "99,999.99" not in html


# ------------------------------------------------------------ label mapping
class TestLabelMapping:
    def test_cash_you_have_is_the_ledger_label(self):
        html = render_app(_ctx(), tab="home")
        assert "Cash you have" in html
        assert "no cash yet" in html   # empty DB says so, invents nothing

    def test_cash_set_aside_is_committed_with_the_floor(self):
        html = render_app(_ctx(), tab="home")
        assert "Cash set aside" in html
        assert "Reserve floor" in html

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
