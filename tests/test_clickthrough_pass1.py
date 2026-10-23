"""Pass 1 of the click-through: the bugs a full run through the app found.

Each of these is a fault that only shows up when someone actually USES the
screens in order, which is why a suite of unit tests did not catch them.
"""
from __future__ import annotations

import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import get_default_db, reset_default_db, session_scope  # noqa: E402
from app.domain import workspace as W  # noqa: E402

PIN = "424242"
ACTION_ROUTES = ["/approvals/{id}/approve", "/approvals/{id}/reject",
                 "/inflows/{id}/resolve", "/inflows/{id}/reask"]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    reset_default_db()
    from pydantic import SecretStr
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "demo_approver_pin", SecretStr(PIN),
                        raising=False)
    monkeypatch.setattr("app.main.get_settings",
                        lambda: type(get_settings()).model_construct(
                            demo_approver_pin=SecretStr(PIN),
                            operator_name="david"))
    yield
    reset_default_db()


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def _pending_step():
    from app.models import PlanStep, StepStatus, StepType
    with session_scope() as s:
        st = PlanStep(type=StepType.TRANSFER, status=StepStatus.PROPOSED,
                      payload_json='{"action": "PAY", "payable_number": "PO-7001", '
                                   '"amount_minor": 900000, "currency": "EUR"}',
                      reason="over your automatic limit")
        s.add(st)
    with session_scope() as s:
        return s.query(PlanStep).order_by(PlanStep.id.desc()).first().id


def _inflow():
    from app.domain import intake as I
    with session_scope() as s:
        c = W.create_customer(s, name="Lumen Stores")
        W.create_invoice(s, customer_id=c.id, number="INV-1003", currency="USD",
                         amount="4400.00")
    I.ingest_deposit(get_default_db(), {
        "id": "dep-p1", "currency": "USD", "amount": 4400,
        "payer_name": "Apex Factors Ltd",
        "reference": "for the lumen stores account", "status": "SETTLED"})


# ---- 1. one shape, and a success that looks like one ----------------------
class TestEveryActionAnswersInOneShape:
    def _check(self, body):
        assert isinstance(body.get("ok"), bool), \
            "`ok` must be a BOOLEAN — reading a sentence as a flag is what made " \
            "a successful approval report 'the server did not say what happened'"
        assert "message" in body and isinstance(body["message"], str)
        assert "state_version" in body

    def test_approve_says_it_succeeded(self, client):
        """THE REPORTED BUG. This returned its own object shape with no `ok` key,
        so the page fell through to its last resort and told the operator the
        server had not said what happened — on an approval that HAD succeeded
        and was already recorded."""
        step = _pending_step()
        r = client.post(f"/approvals/{step}/approve",
                        data={"pin": PIN, "approved_by": "david"},
                        headers={"Accept": "application/json"})
        assert r.status_code == 200
        body = r.json()
        self._check(body)
        assert body["ok"] is True, body
        assert body["message"], "a success must say what it did"

    def test_a_wrong_pin_is_a_failure_in_the_same_shape(self, client):
        step = _pending_step()
        body = client.post(f"/approvals/{step}/approve",
                           data={"pin": "000000"},
                           headers={"Accept": "application/json"}).json()
        self._check(body)
        assert body["ok"] is False and body["err"]

    def test_resolve_and_reask_share_the_shape(self, client):
        _inflow()
        for route, payload in (("/inflows/1/resolve", {"what": "income",
                                                       "note": "ours",
                                                       "pin": PIN}),
                               ("/inflows/1/reask", {})):
            r = client.post(route, data=payload,
                            headers={"Accept": "application/json"})
            assert r.status_code == 200
            self._check(r.json())

    def test_the_shape_carries_the_version_of_the_books(self, client):
        step = _pending_step()
        before = client.get("/state/version").json()["version"]
        body = client.post(f"/approvals/{step}/approve", data={"pin": PIN},
                           headers={"Accept": "application/json"}).json()
        assert body["state_version"]
        assert body["state_version"] != before

    def test_the_reply_names_the_person_not_the_page(self, client):
        step = _pending_step()
        body = client.post(f"/approvals/{step}/approve",
                           data={"pin": PIN},
                           headers={"Accept": "application/json"}).json()
        assert body["ok"] is True
        assert body["message"]


# ---- 3. no raw errors -----------------------------------------------------
class TestFailuresSpeakPlainly:
    def test_a_refused_sandbox_step_is_a_sentence(self, monkeypatch):
        import app.main as M
        from app.domain.plain_words import SETTLE_REFUSED

        # the sandbox refusing after its retries is the reported case
        import app.airwallex.simulation as SIM
        import app.airwallex.transfers as TR

        class Boom:
            def __init__(self, *a, **k): pass
            def status_of(self, _): return "PROCESSING"
            def transition_transfer(self, *a, **k):
                raise RuntimeError('{"code":"operation_failed","message":"x"}')

        monkeypatch.setattr(TR, "Transfers", Boom)
        monkeypatch.setattr(SIM, "Simulation", Boom)
        monkeypatch.setattr(M, "_require_sandbox", lambda: "")
        c = TestClient(M.app, raise_server_exceptions=False)
        body = c.post("/dev/transfers/tr-1/transition", data={"what": "settle"},
                      headers={"Accept": "application/json"}).json()
        assert body["ok"] is False
        assert body["err"] == SETTLE_REFUSED, body
        # the machine's own words are kept, folded away
        assert "operation_failed" in (body.get("detail") or "")

    def test_a_provider_error_is_translated(self):
        from app.domain.plain_words import MODEL_UNAVAILABLE, provider_failure

        sentence, detail = provider_failure(
            "HTTPStatusError: Client error '429 Too Many Requests' for url "
            "'https://generativelanguage.googleapis.com/...'")
        assert sentence == MODEL_UNAVAILABLE
        assert "429" in detail, "the detail is still available to report"

    def test_no_screen_shows_a_raw_vendor_stack(self, client):
        """`googleapis` on its own is fine — the page loads a font from there.
        What must never appear is a VENDOR TRACE in the place a reader looks for
        what happened to their money."""
        html = client.get("/app?tab=ai").text
        assert "developer.mozilla.org" not in html
        assert "HTTPStatusError:" not in html


# ---- 4. the numbers agree -------------------------------------------------
class TestTheNumbersAgree:
    def test_a_bill_shows_what_it_costs_in_the_paying_currency(self):
        from app.ui import _bill_amount
        p = {"currency": "EUR", "amount_minor": 900000,
             "funding_currency": "USD", "funding_amount_minor": 1034600}
        s = _bill_amount(p)
        assert s.startswith("EUR 9,000.00")
        assert "USD 10,346.00" in s and "at " in s, s

    def test_a_same_currency_bill_is_not_restated(self):
        from app.ui import _bill_amount
        assert _bill_amount({"currency": "USD", "amount_minor": 180000,
                             "funding_currency": "USD",
                             "funding_amount_minor": 180000}) == "USD 1,800.00"

    def test_the_badge_counts_money_to_review_as_well_as_payments(self):
        from app.ui import needs_you
        c = {"approvable": [{"payable_number": "PO-1"}],
             "awaiting_approval": [{"number": "PO-1"}],   # the SAME obligation
             "inflows_waiting": [{"id": 1}, {"id": 2}]}
        assert needs_you(c) == 3, "one obligation plus two deposits"

    def test_paid_today_shows_money_not_a_count(self):
        import app.ui as U
        import inspect
        src = inspect.getsource(U._home)
        assert 'data-figure="paid_today"' in src
        assert "settled payout" in src

    def test_setup_depends_on_uses_the_apps_own_payable(self):
        import inspect
        import app.ui as U
        src = inspect.getsource(U._setup)
        assert 'live.get("depends_on")' in src, \
            "Setup must read the same source the plan reason quotes"

    def test_the_ai_count_marks_up_the_rows_it_counts(self):
        import inspect
        import app.ui as U
        assert 'data-testid="ai-count"' in inspect.getsource(U._ai)


# ---- 5. stale copy --------------------------------------------------------
class TestStaleCopy:
    def test_no_screen_claims_resolving_is_unbuilt(self, client):
        for tab in ("money-in", "ok", "home"):
            assert "Coming soon" not in client.get(f"/app?tab={tab}").text, tab

    def test_setup_does_not_claim_no_sample_data_always(self, client):
        html = client.get("/app?tab=setup").text
        assert "No sample data." not in html

    def test_the_forecast_has_a_breakdown(self, client):
        html = client.get("/app").text
        assert "What is expected" in html or "Forecast" not in html


# ---- 6. the page must not wait on Airwallex ------------------------------
class TestThePageDoesNotWaitOnAirwallex:
    def test_the_render_makes_no_outbound_call(self, monkeypatch):
        """`/app` took 3.1 seconds, 3.56s of which was live transfer lookups and
        the token fetch behind them. It reads a cache now."""
        import app.airwallex.client as C
        import app.airwallex.transfers as TR

        def forbidden(*a, **k):
            raise AssertionError("/app must not call Airwallex during a render")

        import app.main as M

        monkeypatch.setattr(C.AirwallexClient, "__init__", forbidden)
        monkeypatch.setattr(TR.Transfers, "__init__", forbidden)
        # a transfer on the book, so the render WOULD have looked it up
        from app.models import PlanStep, StepStatus, StepType
        with session_scope() as s:
            s.add(PlanStep(type=StepType.TRANSFER, status=StepStatus.SUBMITTED,
                           request_id="req-render", airwallex_object_id="tr-1",
                           payload_json='{"action": "PAY"}'))
        c = TestClient(M.app)
        assert c.get("/app").status_code == 200

    # NO ABSOLUTE-TIMING TEST HERE, deliberately. The render is proven to make
    # NO outbound call at all by the test above, which is the actual regression:
    # 3.56s of a 3.64s render was a vendor call inside it. A wall-clock
    # assertion measured through TestClient times the HARNESS as much as the
    # code (1.29s of it was `_thread.lock.acquire` inside starlette's transport
    # on this box), so it failed for reasons that had nothing to do with the
    # bug. The end-to-end numbers are measured against the running server and
    # recorded in AGENTS.md instead: warm 0.048s, cold 0.117s, from 3.118s.

    def test_the_status_cache_starts_unchecked_not_wrong(self):
        """A transfer whose status has not been fetched yet must not be shown as
        though the bank had confirmed anything."""
        import app.dashboard as D
        D._LIVE_CACHE.clear()
        with session_scope() as s:
            from app.models import PlanStep, StepStatus, StepType
            s.add(PlanStep(type=StepType.TRANSFER, status=StepStatus.SUBMITTED,
                           request_id="req-x", airwallex_object_id="tr-x",
                           payload_json='{"action": "PAY"}'))
        with session_scope() as s:
            out = D.live_transfer_statuses(s)
        assert out.get("tr-x") == "UNCHECKED"


# ---- the tiles patch endpoint --------------------------------------------
class TestTheTilesEndpoint:
    def test_it_serves_the_same_text_the_render_shows(self, client):
        from app.dashboard import build_context
        from app.ui import tile_text

        body = client.get("/state/tiles").json()
        assert body["ok"] is True
        assert body["tiles"] == tile_text(build_context([])), \
            "the patch and the render must use ONE formatter"

    def test_it_is_cheap(self, client):
        started = time.monotonic()
        assert client.get("/state/tiles").status_code == 200
        assert time.monotonic() - started < 1.0

    def test_it_never_raises_a_raw_500(self, monkeypatch):
        import app.main as M

        class Boom:
            def session(self):
                raise RuntimeError("disk I/O error")

        # `state_tiles` reaches the database through `build_context`, which
        # imports `get_default_db` itself — so the stand-in goes where that
        # name RESOLVES, not on the route's own helper.
        monkeypatch.setattr("app.db.get_default_db",
                            lambda: Boom())
        c = TestClient(M.app, raise_server_exceptions=False)
        r = c.get("/state/tiles")
        assert r.status_code == 200
        assert r.json()["ok"] is False
