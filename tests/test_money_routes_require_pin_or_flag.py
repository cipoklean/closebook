"""F2: money-shaped routes carry the operator PIN, and /dev/* is off by
default (only the tester instance enables it). /agent/pause stays PIN-free on
purpose: pausing is the safe direction.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.config import get_settings  # noqa: E402
from pydantic import SecretStr  # noqa: E402

PIN = "424242"


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    # The safe default: dev routes are OFF unless an instance opts in. A PIN
    # is configured so the gates are genuinely exercised.
    monkeypatch.delenv("CLOSEBOOK_DEV_ROUTES", raising=False)
    monkeypatch.setattr(get_settings(), "demo_approver_pin", SecretStr(PIN),
                        raising=False)
    yield


def _client():
    from app.main import app
    return TestClient(app)


def _post(client, path, body):
    return client.post(path, json=body,
                       headers={"Accept": "application/json"})


class TestDevRoutesOffByDefault:
    def test_dev_transition_refused_without_flag(self):
        r = _post(_client(), "/dev/transfers/x/transition",
                   {"what": "nope", "pin": PIN})
        assert r.status_code == 200
        assert "disabled" in r.json()["err"].lower(), r.json()

    def test_dev_transition_requires_pin_when_enabled(self, monkeypatch):
        monkeypatch.setenv("CLOSEBOOK_DEV_ROUTES", "1")
        r = _post(_client(), "/dev/transfers/x/transition",
                   {"what": "nope"})
        assert r.json()["ok"] is False
        assert "PIN is not right" in r.json()["err"], r.json()

    def test_dev_transition_accepts_pin_when_enabled(self, monkeypatch):
        monkeypatch.setenv("CLOSEBOOK_DEV_ROUTES", "1")
        r = _post(_client(), "/dev/transfers/x/transition",
                   {"what": "nope", "pin": PIN})
        # With the PIN accepted it proceeds to the action validation, which
        # refuses before any network call.
        assert "Pick settle, fail or return" in r.json()["err"], r.json()


class TestOperatorPinGates:
    def test_workspace_reset_requires_pin(self):
        r = _post(_client(), "/workspace/reset", {"confirm": "START FRESH"})
        assert r.json()["ok"] is False
        assert "PIN is not right" in r.json()["err"], r.json()

    def test_workspace_reset_accepts_pin(self):
        r = _post(_client(), "/workspace/reset",
                   {"confirm": "START FRESH", "pin": PIN})
        # In-memory test DB: the archive step refuses, but not on the PIN.
        assert "PIN is not right" not in r.json()["err"], r.json()

    def test_settings_requires_pin(self):
        r = _post(_client(), "/setup/settings",
                   {"budget_currency": "USD", "reserve_floor": "5000",
                    "auto_pay_limit": "2500"})
        assert r.json()["ok"] is False
        assert "PIN is not right" in r.json()["err"], r.json()

    def test_supplier_bank_details_require_pin(self):
        r = _post(_client(), "/setup/suppliers",
                   {"name": "X", "country": "US", "currency": "USD"})
        assert r.json()["ok"] is False
        assert "PIN is not right" in r.json()["err"], r.json()

    def test_pause_is_pin_free_on_purpose(self):
        r = _post(_client(), "/agent/pause", {})
        assert r.json()["ok"] is True, r.json()


class TestSimulatedDepositCap:
    def test_over_50k_usd_is_refused(self, monkeypatch):
        # Provider-free: if the cap is not enforced, the simulate call runs
        # and this stub raises, which is not the cap sentence.
        import app.domain.intake as intake_mod

        def boom(*a, **k):
            raise AssertionError("simulate_deposit was called: cap not enforced")

        monkeypatch.setattr(intake_mod, "simulate_deposit", boom, raising=True)
        monkeypatch.setattr(intake_mod, "accounts_cached",
                            lambda db, blocking=False: [
                                {"id": "acct-usd", "currency": "USD",
                                 "usable": True, "status": "ACTIVE",
                                 "account_number": "0000"}])
        r = _post(_client(), "/money-in/simulate",
                   {"currency": "USD", "amount": "60000",
                    "payer_name": "X", "reference": ""})
        assert r.json()["ok"] is False
        assert "50,000" in r.json()["err"], r.json()

    def test_under_50k_usd_still_simulates(self, monkeypatch):
        import app.domain.intake as intake_mod

        called = {}

        def fake_simulate(client, *, global_account_id, amount_minor, currency,
                          payer_name="", reference=""):
            called["amount_minor"] = amount_minor
            return {"id": "dep-cap-1", "currency": currency,
                    "amount": amount_minor / 100, "status": "SETTLED",
                    "reference": reference, "payer_name": payer_name,
                    "created_at": "2026-10-25T09:00:00+0000"}

        monkeypatch.setattr(intake_mod, "simulate_deposit", fake_simulate,
                            raising=True)
        monkeypatch.setattr(intake_mod, "accounts_cached",
                            lambda db, blocking=False: [
                                {"id": "acct-usd", "currency": "USD",
                                 "usable": True, "status": "ACTIVE",
                                 "account_number": "0000"}])
        r = _post(_client(), "/money-in/simulate",
                   {"currency": "USD", "amount": "40000",
                    "payer_name": "X", "reference": ""})
        assert called.get("amount_minor") == 4_000_000, r.json()
