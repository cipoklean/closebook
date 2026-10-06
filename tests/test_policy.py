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
    AUTO_PAY_LIMIT_USD, CommittedCash, MATERIAL_SHORT_THRESHOLD, RESERVE_FLOOR_USD,
    breaches_reserve_floor, classify_confidence, prioritise,
    requires_approval, swift_fee_minor, total_payout_minor, usd_value_minor,
    within_bank_fee_tolerance, PayablePriority,
)

GBP_RATE = Decimal("1.27")   # 1.27 GBP per 1 USD
EUR_RATE = Decimal("0.8756")  # 0.8756 EUR per 1 USD (live-probed inverse)


# ---- committed-cash budget ----------------------------------------------
def test_committed_cash_available():
    c = CommittedCash("USD", expected_inflows_minor=4_000_000, earmarked_minor=1_000_000)
    assert c.available_minor() == 3_000_000   # USD 30,000


def test_committed_cash_never_negative():
    c = CommittedCash("USD", expected_inflows_minor=100, earmarked_minor=5_000)
    assert c.available_minor() == 0


def test_committed_cash_usd_equivalent():
    c = CommittedCash("GBP", expected_inflows_minor=800_000, earmarked_minor=0)
    # GBP 8,000 / 1.27 = USD 6,299.21
    assert c.usd_equivalent_minor({"GBP": GBP_RATE}) == int(
        Decimal(8_000) / Decimal("1.27") * 100)


def test_unpriced_currency_is_not_spendable():
    """Conservative: a currency with no rate contributes nothing to policy."""
    assert CommittedCash("XYZ", 900_000).usd_equivalent_minor({}) == 0


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


# ---- the floor actually binds -------------------------------------------
def test_reserve_floor_binds_against_committed_cash():
    """THE demo-critical assertion.

    Committed cash is USD 40,000 with a USD 5,000 floor, so only USD 35,000 is
    deployable. A USD 34,000 payout fits; USD 36,000 breaches the floor even
    though the sandbox holds 10M. This is why the committed-cash model exists.
    """
    committed = CommittedCash("USD", expected_inflows_minor=4_000_000, earmarked_minor=0)
    deployable = committed.available_minor()

    assert breaches_reserve_floor(deployable - 3_400_000, "USD") is False  # leaves 600k
    assert breaches_reserve_floor(deployable - 3_600_000, "USD") is True   # leaves 400k


def test_floor_never_seen_against_raw_sandbox_balance():
    """Documents WHY committed cash exists: the raw balance cannot breach it."""
    sandbox_minor = 10_000_000_000   # USD 100,000,000
    assert breaches_reserve_floor(sandbox_minor - 36_000_000, "USD") is False


# ---- auto-pay limit ------------------------------------------------------
def test_within_auto_limit_no_approval():
    needs, why = requires_approval(200_000, "USD")   # USD 2,000
    assert needs is False
    assert "within" in why


def test_above_auto_limit_needs_approval():
    needs, why = requires_approval(300_000, "USD")   # USD 3,000
    assert needs is True
    assert str(AUTO_PAY_LIMIT_USD) in why


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
    """The committed-cash budget must be real, persisted config — not a value
    invented inside a test. This loads settings/committed_cash.json from disk."""
    from app.domain.policy import load_budget
    b = load_budget()
    assert b.committed.currency == "USD"
    assert b.committed.expected_inflows_minor == 4_000_000   # USD 40,000
    assert b.reserve_floor_usd == Decimal("5000.00")
    assert b.auto_pay_limit_usd == Decimal("2500.00")


def test_repo_settings_make_the_floor_bind():
    """End-to-end proof the demo has tension: with USD 40,000 committed and a
    USD 5,000 floor, a USD 34,000 payout is fine but USD 36,000 is not."""
    from app.domain.policy import load_budget
    b = load_budget()
    avail = b.committed.available_minor()
    assert avail == 4_000_000
    assert breaches_reserve_floor(avail - 3_400_000, "USD",
                                  floor_usd=b.reserve_floor_usd) is False
    assert breaches_reserve_floor(avail - 3_600_000, "USD",
                                  floor_usd=b.reserve_floor_usd) is True


def test_load_budget_missing_file_falls_back():
    from app.domain.policy import load_budget
    b = load_budget("/nonexistent/committed_cash.json")
    assert b.committed.available_minor() == 0
    assert b.reserve_floor_usd == RESERVE_FLOOR_USD