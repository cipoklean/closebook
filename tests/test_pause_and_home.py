"""The kill switch, and the rebuilt Home that shows the AI's authority.

Pausing is real only if it changes what the code does: a deposit the rules
would have applied stays a proposal, and a payment the policy cleared is not
sent. Resuming needs the PIN; pausing never does.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import get_default_db, reset_default_db, session_scope  # noqa: E402
from app.domain import workspace as W  # noqa: E402

PIN = "424242"


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


def _invoice(number="INV-9001", amount="1200.00"):
    with session_scope() as s:
        c = W.create_customer(s, name="Acme Ltd")
        W.create_invoice(s, customer_id=c.id, number=number, currency="USD",
                         amount=amount)


def _deposit(dep_id="dep-1", amount=1200, reference="INV-9001"):
    from app.domain import intake as I
    return I.ingest_deposit(get_default_db(), {
        "id": dep_id, "currency": "USD", "amount": amount,
        "payer_name": "Acme Ltd", "reference": reference, "status": "SETTLED"})


def _paused() -> bool:
    from app.domain.settings_store import is_paused
    with session_scope() as s:
        return is_paused(s)


class TestTheSwitch:
    def test_a_fresh_book_is_running(self):
        assert _paused() is False

    def test_pausing_needs_no_pin(self, client):
        r = client.post("/agent/pause", headers={"Accept": "application/json"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert _paused() is True

    def test_resuming_refuses_a_wrong_pin_and_stays_paused(self, client):
        client.post("/agent/pause")
        body = client.post("/agent/resume", data={"pin": "000000"},
                           headers={"Accept": "application/json"}).json()
        assert body["ok"] is False
        assert "still paused" in body["err"]
        assert _paused() is True

    def test_resuming_with_the_pin_runs_again(self, client):
        client.post("/agent/pause")
        body = client.post("/agent/resume", data={"pin": PIN},
                           headers={"Accept": "application/json"}).json()
        assert body["ok"] is True, body
        assert _paused() is False


class TestPausedMeansNothingMovesOnItsOwn:
    def test_an_exact_match_is_applied_while_running(self):
        _invoice()
        res = _deposit()
        assert res.disposition == "APPLIED"

    def test_the_same_exact_match_is_only_proposed_while_paused(self, client):
        _invoice()
        client.post("/agent/pause")
        res = _deposit()
        assert res.disposition != "APPLIED", res
        from app.models import InvoiceReceivable
        with session_scope() as s:
            inv = s.query(InvoiceReceivable).filter_by(number="INV-9001").one()
            assert inv.amount_paid_minor == 0, "a paused AI moved an invoice"

    def test_a_cleared_payment_is_held_not_sent_while_paused(self, monkeypatch):
        """The conductor is the only thing that sends. Paused, every clear step
        comes back HELD with the reason, and the executor is never reached."""
        from app.domain import conductor as C
        from app.domain.settings_store import set_paused
        from app.domain.planner import Step

        with session_scope() as s:
            set_paused(s, True)
        step = Step(action="PAY", reason="clear", payable_number="PO-1",
                    currency="USD", amount_minor=10000)
        monkeypatch.setattr(C, "clear_steps", lambda plan: [step])
        monkeypatch.setattr(C, "held_steps", lambda plan: [])
        monkeypatch.setattr(C, "approval_waiting_steps", lambda plan: [])
        monkeypatch.setattr(C, "already_approved_steps", lambda plan: [])
        monkeypatch.setattr(C, "_sendability_gap", lambda s, st: None)
        monkeypatch.setattr(C, "persist_plan", lambda s, rid, steps: ([], []))
        sent = []
        monkeypatch.setattr(C, "_run_one", lambda *a, **k: sent.append(a))
        run = C.on_event(get_default_db(), source="test", execute=True,
                         dry_run=False, client=object())
        assert not sent, "a paused AI sent a payment"
        assert any(v.outcome == "HELD" and "paused" in v.detail
                   for v in run.verdicts)


class TestHomeShowsTheAuthority:
    def test_running_home_names_the_live_limit_and_offers_pause(self, client):
        html = client.get("/app").text
        assert 'data-testid="ai-authority"' in html
        assert 'data-testid="ai-pause"' in html
        assert "USD 2,500.00" in html          # the live auto-pay limit
        assert "Running" in html

    def test_paused_home_says_so_and_asks_for_the_pin(self, client):
        client.post("/agent/pause")
        html = client.get("/app").text
        assert 'data-testid="ai-resume"' in html
        assert 'data-testid="ai-pause"' not in html
        assert "Paused" in html

    def test_the_bar_on_screen_is_the_bar_in_the_code(self, client):
        from app.domain.policy import MATCH_CONFIDENCE_AUTO
        from app.domain.matching import _LLM_MAX_CONFIDENCE
        html = client.get("/app").text
        assert f"{round(MATCH_CONFIDENCE_AUTO * 100)}%" in html
        assert f"{round(_LLM_MAX_CONFIDENCE * 100)}%" in html
        assert "85%" not in html


class TestNeverClaimsMoreThanHappened:
    def test_an_applied_deposit_is_not_called_held(self, client):
        """THE RECEIPT BUG. The suspense figure was read as the applied amount,
        so every deposit the rules applied said 'held for you' beside a Matched
        chip."""
        _invoice()
        _deposit()
        from app.dashboard import build_context
        a = [x for x in build_context([])["arrivals"] if x["deposit_id"] == "dep-1"][0]
        assert a["receipt"]["code"]["verdict"] == "applied"

    def test_an_unapplied_deposit_is_held(self, client):
        _invoice()
        _deposit(dep_id="dep-2", amount=999, reference="no idea")
        from app.dashboard import build_context
        a = [x for x in build_context([])["arrivals"] if x["deposit_id"] == "dep-2"][0]
        assert a["receipt"]["code"]["verdict"] != "applied"

    def test_a_sent_payment_is_never_called_paid(self):
        from app.ui_home import money_out_card
        c = {"budget": {"auto_pay_limit_minor": 250000, "currency": "USD"},
             "payables": [{"number": "PO-7", "supplier": "Northwind",
                           "amount_minor": 90000, "currency": "USD",
                           "live_status": "PROCESSING"}]}
        html = money_out_card(c)
        assert "awaiting bank" in html
        assert "bank confirmed" not in html
        assert 'h-tag is-ok">Paid' not in html

    def test_deposits_waiting_never_sit_under_nothing_needs_your_ok(self, client):
        _invoice()
        _deposit(dep_id="dep-3", amount=999, reference="no idea")
        html = client.get("/app?tab=ok").text
        assert "Nothing needs your OK" not in html
        # UPDATED WITH THE COPY (deploy 5e77d8f): the header used to say "No
        # payments need your OK", which was true of payments and read as "you
        # have nothing to do" above a deposit that needed a decision. It now
        # says the same count Home does, and still names the deposit.
        assert "1 thing needs your OK" in html
        assert "1 deposit" in html

    def test_a_proposed_deposit_is_never_not_sent_yet(self, client):
        _invoice()
        _deposit(dep_id="dep-4", amount=999, reference="no idea")
        html = client.get("/app?tab=money-in").text
        assert "Not sent yet</span>" not in html


def test_a_bill_held_for_cash_is_not_counted_as_needing_your_ok():
    """Home said "Pay Orbit Supply needs your OK" while the Approve tab filed
    the same bill under "not waiting on you". A bill over the limit that cash
    cannot cover yet is held; it is not a decision until the cash is there."""
    from app.ui import needs_you
    from app.ui_home import needs_items
    c = {"approvable": [], "inflows_waiting": [],
         "awaiting_approval": [{"number": "PO-82", "amount_minor": 310_000,
                                "currency": "USD", "supplier": "Orbit"}],
         "waiting": [{"number": "PO-82"}]}
    assert needs_you(c) == 0
    assert needs_items(c) == []
    c["waiting"] = []
    assert needs_you(c) == 1
    assert len(needs_items(c)) == 1
