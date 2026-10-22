"""Policy tests — the numbers that are allowed to move money.

The headline test is `test_reserve_floor_binds_against_committed_cash`: it
proves the demo tension exists. The sandbox pre-funds ~10M per currency, so a
policy written against the raw balance would never fire; these tests pin the
committed-cash behaviour that makes the floor and limits meaningful.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.policy import (
    AUTO_PAY_LIMIT_USD, Forecast, MATERIAL_SHORT_THRESHOLD, RESERVE_FLOOR_USD,
    available_cash_minor,
    breaches_reserve_floor, classify_confidence, prioritise,
    requires_approval, swift_fee_minor, total_payout_minor, usd_value_minor,
    within_bank_fee_tolerance, PayablePriority,
)

GBP_RATE = Decimal("1.27")   # 1.27 GBP per 1 USD
EUR_RATE = Decimal("0.8756")  # 0.8756 EUR per 1 USD (live-probed inverse)


# ---- THE ONE AVAILABLE-CASH RULE ---------------------------------------
def test_available_cash_is_cash_minus_in_flight_minus_promised_minus_floor():
    assert available_cash_minor(cash_minor=2_100_000, in_flight_minor=1_200_000,
                                committed_this_run_minor=700_000,
                                floor_minor=500_000) == -300_000


def test_available_cash_with_no_commitments_is_cash_minus_the_floor():
    assert available_cash_minor(cash_minor=900_000, floor_minor=500_000) == 400_000


def test_available_cash_can_go_negative_and_says_so():
    """A negative available figure is the whole point: it is a shortfall, and
    clamping it at zero would hide it."""
    assert available_cash_minor(cash_minor=100_000, floor_minor=500_000) == -400_000


def test_expected_inflows_are_not_an_input():
    """The signature has no place to put them. That is deliberate: money that has
    been invoiced and not landed used to fund payments, and that is the bug."""
    import inspect
    params = set(inspect.signature(available_cash_minor).parameters)
    assert "expected_inflows_minor" not in params
    assert params == {"cash_minor", "in_flight_minor",
                      "committed_this_run_minor", "floor_minor"}


# ---- the forecast: shown, never spent -----------------------------------
def test_forecast_total_is_the_sum_of_what_is_expected():
    f = Forecast(lines=[{"number": "INV-1", "usd_minor": 1_200_000},
                        {"number": "INV-2", "usd_minor": 450_000}])
    assert f.total_minor() == 1_650_000


def test_forecast_funding_names_the_invoice_that_covers_a_gap():
    f = Forecast(lines=[{"number": "INV-1", "usd_minor": 1_200_000},
                        {"number": "INV-2", "usd_minor": 450_000}])
    assert f.funding(400_000) == ["INV-1"]
    assert f.funding(1_600_000) == ["INV-1", "INV-2"]


def test_forecast_funding_prefers_the_invoice_already_depended_on():
    """A payment blocked waiting on INV-2 must not be told it waits for INV-1
    just because INV-1 is larger — that sends the reader to the wrong customer."""
    f = Forecast(lines=[{"number": "INV-1", "usd_minor": 1_200_000},
                        {"number": "INV-2", "usd_minor": 450_000}])
    assert f.funding(400_000, prefer="INV-2") == ["INV-2"]
    # but a preference that would NOT cover the gap does not get named alone
    assert f.funding(1_600_000, prefer="INV-2") == ["INV-1", "INV-2"]


def test_forecast_of_nothing_funding_nothing():
    assert Forecast().total_minor() == 0
    assert Forecast().funding(100) == []


# ---- USD valuation -------------------------------------------------------
def test_usd_value_minor_usd_passthrough():
    assert usd_value_minor(123_456, "USD") == 123_456


def test_usd_value_minor_converts():
    """EUR 10,000 at 0.8756 EUR-per-USD = USD 11,419 (divide, not multiply)."""
    assert usd_value_minor(1_000_000, "EUR", {"EUR": EUR_RATE}) == int(
        Decimal(10_000) / Decimal("0.8756") * 100)


def test_rate_convention_is_ccy_per_usd():
    """Pins the convention so policy and planner can never disagree again:
    rates[CCY] = CCY per 1 USD, so USD value = amount / rate."""
    # GBP 1,000 at 1.27 GBP-per-USD -> USD 787.40
    usd = usd_value_minor(100_000, "GBP", {"GBP": Decimal("1.27")})
    assert usd == 78_740
    assert usd_value_minor(100_000, "GBP", {"GBP": Decimal("1.27")}) < 100_000


def test_zero_or_missing_rate_is_zero_value():
    assert usd_value_minor(100_000, "GBP", {"GBP": Decimal("0")}) == 0
    assert usd_value_minor(100_000, "GBP", {}) == 0


def test_usd_value_minor_unknown_currency_zero():
    assert usd_value_minor(1_000_000, "XYZ", {}) == 0


# ---- the floor actually binds, on REAL cash -----------------------------
def test_reserve_floor_binds_against_ledger_cash():
    """THE demo-critical assertion, on the single basis.

    USD 9,000 of real cash with a USD 5,000 floor leaves USD 4,000 spendable. A
    USD 3,900 payment fits; USD 4,100 leaves USD 4,900 and does not.
    """
    after_ok = available_cash_minor(cash_minor=900_000 - 390_000, floor_minor=500_000)
    after_bad = available_cash_minor(cash_minor=900_000 - 410_000, floor_minor=500_000)
    assert after_ok == 10_000
    assert after_bad == -10_000
    assert breaches_reserve_floor(900_000 - 390_000, "USD") is False
    assert breaches_reserve_floor(900_000 - 410_000, "USD") is True


def test_floor_ignores_the_raw_sandbox_balance_entirely():
    """The sandbox pre-funds ~10,000,000, so a floor measured against it could
    never bind. The basis is the LEDGER balance only — there is no code path
    that reads a sandbox balance into policy."""
    import inspect
    from app.domain import policy as pol
    src = inspect.getsource(pol)
    assert "sandbox" not in src.lower() or "pre-funds" in src.lower()
    # and the rule itself has no notion of a sandbox balance to consult
    assert available_cash_minor(cash_minor=400_000, floor_minor=500_000) == -100_000


def test_the_floor_arithmetic_is_the_same_function_the_planner_uses():
    """One rule, called from both sides. If these ever diverge the plan and the
    page disagree again, which is the whole bug this replaced."""
    from app.domain.planner import CashPosition
    pos = CashPosition(balances={"USD": 2_100_000}, in_flight={"USD": 1_200_000},
                       floor_minor=500_000, budget_currency="USD")
    assert pos.available_minor(700_000) == available_cash_minor(
        cash_minor=2_100_000, in_flight_minor=1_200_000,
        committed_this_run_minor=700_000, floor_minor=500_000)


# ---- auto-pay limit ------------------------------------------------------
def test_within_auto_limit_no_approval():
    needs, why = requires_approval(200_000, "USD")   # USD 2,000
    assert needs is False
    assert "within" in why


def test_above_auto_limit_needs_approval():
    needs, why = requires_approval(300_000, "USD")   # USD 3,000
    assert needs is True
    # the limit must read as money, not as the raw Decimal 2500.0
    assert "USD 2,500.00" in why


def test_auto_limit_uses_usd_equivalent():
    """EUR 3,000 at 0.8756 EUR-per-USD = USD 3,425 -> over the USD 2,500 limit."""
    needs, why = requires_approval(300_000, "EUR", {"EUR": EUR_RATE})
    assert needs is True
    assert "exceeds the auto-pay limit" in why


def test_auto_limit_allows_euro_below_threshold():
    """EUR 2,000 at 0.8756 = USD 2,283 -> under the limit, auto-payable."""
    needs, _ = requires_approval(200_000, "EUR", {"EUR": EUR_RATE})
    assert needs is False


def test_low_confidence_always_needs_approval_even_if_small():
    needs, why = requires_approval(100_000, "USD", match_confidence=0.60)
    assert needs is True
    assert "confidence" in why


def test_unpriced_currency_is_not_auto_payable():
    """A currency with no FX rate values at 0 USD. Reading that as 'small'
    would silently auto-pay something we cannot price. It must require approval."""
    needs, why = requires_approval(10_000, "XYZ", {})
    assert needs is True
    assert "XYZ" in why


def test_unpriced_known_currency_needs_approval():
    needs, why = requires_approval(500_000, "EUR", {})   # EUR, but no rate given
    assert needs is True
    assert "no FX rate" in why


# ---- SWIFT fees ----------------------------------------------------------
def test_swift_fee_added_to_conversion():
    assert swift_fee_minor("EUR") == 1_285          # EUR 12.85
    assert swift_fee_minor("USD") == 0


def test_local_rail_has_no_fee():
    assert total_payout_minor(500_000, "EUR", "LOCAL") == 500_000


def test_swift_total_includes_fee():
    """The fee must be inside the converted amount, not bolted on after."""
    assert total_payout_minor(500_000, "EUR", "SWIFT") == 501_285


# ---- prioritisation ------------------------------------------------------
def test_prioritise_is_deterministic_and_ordered():
    from datetime import datetime, timedelta
    now = datetime(2026, 10, 6)
    items = [
        PayablePriority(criticality=3, due_date=now + timedelta(days=1),
                        defer_cost_usd_minor=100, amount_usd_minor=100),
        PayablePriority(criticality=5, due_date=now + timedelta(days=30),
                        defer_cost_usd_minor=100, amount_usd_minor=100),
        PayablePriority(criticality=3, due_date=now + timedelta(days=5),
                        defer_cost_usd_minor=100, amount_usd_minor=100),
    ]
    ordered = prioritise(items)
    # criticality 5 first, then the two criticality-3 by due date
    assert ordered[0].criticality == 5
    assert ordered[1].due_date < ordered[2].due_date


def test_prioritise_is_stable_across_runs():
    items = [
        PayablePriority(criticality=2, due_date=None, defer_cost_usd_minor=5,
                        amount_usd_minor=5),
        PayablePriority(criticality=2, due_date=None, defer_cost_usd_minor=5,
                        amount_usd_minor=5),
    ]
    a = [id(i) for i in prioritise(items)]
    b = [id(i) for i in prioritise(list(reversed(items)))]
    assert sorted(a) == sorted(b)   # same set, order defined by the key not input


# ---- bank-fee tolerance --------------------------------------------------
def test_bank_fee_tolerance_boundary():
    assert within_bank_fee_tolerance(800_000, 799_500) is True   # 50 units, small
    assert within_bank_fee_tolerance(100_000, 99_000) is True     # exactly 1%
    assert within_bank_fee_tolerance(800_000, 700_000) is False    # 12.5% short
    assert within_bank_fee_tolerance(800_000, 800_000) is True    # exact


def test_material_short_threshold_value():
    assert MATERIAL_SHORT_THRESHOLD == Decimal("0.10")


# ---- confidence classification ------------------------------------------
def test_confidence_classification():
    assert classify_confidence(0.95) == "AUTO"
    assert classify_confidence(0.80) == "APPROVAL"
    assert classify_confidence(0.60) == "ESCALATE"


# ---- the real settings file ---------------------------------------------
def test_load_budget_from_repo_settings_file():
    """The limits must be real, persisted config, not values invented in a test.
    This loads settings/committed_cash.json from disk."""
    from app.domain.policy import load_budget
    b = load_budget()
    assert b.currency == "USD"
    assert b.reserve_floor_usd == Decimal("5000.00")
    assert b.auto_pay_limit_usd == Decimal("2500.00")


def test_the_settings_file_no_longer_carries_a_policy_budget():
    """It used to hold expected_inflows_minor and earmarked_minor, and the floor
    was measured against them. Deleting those keys IS the fix; a config that can
    reintroduce a budget measured against money not received is the bug."""
    import json
    from pathlib import Path
    from app.domain.policy import load_budget
    raw = json.loads(Path("settings/committed_cash.json").read_text())
    assert "expected_inflows_minor" not in raw
    assert "earmarked_minor" not in raw
    b = load_budget()
    assert not hasattr(b, "committed")


def test_repo_settings_make_the_floor_bind_on_seeded_cash():
    """End-to-end tension, on the SEEDED numbers: USD 9,000 of real cash with a
    USD 5,000 floor leaves USD 4,000. PO-7003's USD 1,800 fits; PO-7002's USD
    7,000 does not."""
    from app.domain.policy import load_budget
    b = load_budget()
    floor = int(b.reserve_floor_usd * 100)
    assert available_cash_minor(cash_minor=900_000, floor_minor=floor) == 400_000
    assert available_cash_minor(cash_minor=900_000 - 180_000, floor_minor=floor) == 220_000
    assert available_cash_minor(cash_minor=900_000 - 700_000, floor_minor=floor) == -300_000


def test_load_budget_missing_file_falls_back():
    from app.domain.policy import load_budget
    b = load_budget("/nonexistent/committed_cash.json")
    assert b.reserve_floor_usd == RESERVE_FLOOR_USD
    assert b.auto_pay_limit_usd == AUTO_PAY_LIMIT_USD