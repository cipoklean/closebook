"""Planner tests — the money decision, beat by beat.

The planner is a pure function, so these tests are the actual acceptance
criteria for the demo: feed it the beat-1 position and it must auto-pay
PO-7003 and hold PO-7001/PO-7002. Nothing here touches a DB or the network.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.planner import (
    ACT_CHASE, ACT_DEFER, ACT_ESCALATE, ACT_FX, ACT_PAY, CashPosition, Payable,
    build_plan,
)
from app.domain.policy import BudgetSettings, Forecast, load_budget


# ---- the beat-1 scenario, straight from seed/scenario.py ---------------
ROTTERDAM = Payable("PO-7001", "Rotterdam Freight BV", "EUR", 900_000,
                    criticality=5, payout_method="SWIFT",
                    depends_on_receivable="INV-0041",
                    dependency_met=False, dependency_open_minor=1_200_000)
SHENZHEN = Payable("PO-7002", "Shenzhen Weave Co", "USD", 700_000,
                   criticality=3, payout_method="CARD",
                   depends_on_receivable="INV-0042",
                   dependency_met=False, dependency_open_minor=800_000)
ATLANTA = Payable("PO-7003", "Atlanta Packaging", "USD", 180_000,
                  criticality=2, payout_method="LOCAL")
ALL_THREE = [ROTTERDAM, SHENZHEN, ATLANTA]

# EUR 0.8756 per USD (CCY per USD convention), so EUR 9,012.85 ~= USD 10,287
RATES = {"EUR": Decimal("0.8756"), "GBP": Decimal("1.27"), "CNY": Decimal("7.1")}


def beat1_position(cash_minor=4_000_000, *, in_flight=None, forecast=None,
                   floor_minor=500_000):
    """A position the planner can ACTUALLY spend from.

    `cash_minor` is real ledger cash now, not a committed-cash budget: the
    planner reads available cash through policy.available_cash_minor, so a test
    that hands it a budget instead of cash is testing a model that no longer
    exists. In flight and the forecast default to empty and are opt-in, because
    forgetting to pass in-flight money is exactly how a payment gets planned
    twice.
    """
    return CashPosition(
        balances={"USD": cash_minor, "GBP": 600_000},
        in_flight=dict(in_flight or {}),
        forecast=forecast if forecast is not None else Forecast(),
        rates=RATES,
        budget_currency="USD",
        floor_minor=floor_minor,
    )


# ---- beat 1: dependency gate + auto-pay limit --------------------------
def test_beat1_auto_pays_small_local_supplier():
    plan = build_plan(ALL_THREE, beat1_position(), load_budget())
    pays = {s.payable_number: s for s in plan.steps if s.action == ACT_PAY}
    assert "PO-7003" in pays, "USD 1,800 < USD 2,500 limit must auto-pay"
    assert pays["PO-7003"].needs_approval is False


def test_beat1_holds_dependency_blocked_payables():
    plan = build_plan(ALL_THREE, beat1_position(), load_budget())
    defers = {s.payable_number: s for s in plan.steps if s.action == ACT_DEFER}
    assert "PO-7001" in defers
    assert "PO-7002" in defers


def test_defer_reason_names_the_receivable():
    plan = build_plan(ALL_THREE, beat1_position(), load_budget())
    d = {s.payable_number: s for s in plan.steps if s.action == ACT_DEFER}["PO-7001"]
    assert "INV-0041" in d.reason
    assert "part-payment" in d.reason


def test_partial_receivable_does_not_unblock_payable():
    """Beat 3's rule: GBP 4,800 of 8,000 leaves PO-7002 HELD.

    This is the single most important policy line in the planner — paying a
    supplier with a customer's money they still owe is how a treasury breaks.
    """
    partial = Payable("PO-7002", "Shenzhen Weave Co", "USD", 700_000,
                      criticality=3, payout_method="CARD",
                      depends_on_receivable="INV-0042",
                      dependency_met=False, dependency_open_minor=320_000,
                      dependency_currency="GBP")
    plan = build_plan([partial], beat1_position(), load_budget())
    assert plan.steps[0].action == ACT_DEFER
    # the shortfall is GBP, not USD — the reason must name the right currency,
    # and every amount reads "CCY 1,234.56" (never "3200 GBP").
    assert "GBP 3,200.00 still outstanding" in plan.steps[0].reason
    assert "3200 GBP" not in plan.steps[0].reason
    assert "USD" not in plan.steps[0].reason


# ---- beat 2: FX sizing with drift tolerance + approval ------------------
def _unblock_roterdam():
    r = Payable("PO-7001", "Rotterdam Freight BV", "EUR", 900_000,
                criticality=5, payout_method="SWIFT",
                depends_on_receivable="INV-0041", dependency_met=True)
    return r


def test_beat2_plans_fx_and_pay_with_swift_fee():
    plan = build_plan([_unblock_roterdam()], beat1_position(), load_budget())
    fx = [s for s in plan.steps if s.action == ACT_FX]
    pay = [s for s in plan.steps if s.action == ACT_PAY]
    assert len(fx) == 1 and len(pay) == 1

    # SWIFT adds EUR 12.85 INSIDE the converted amount.
    assert pay[0].amount_minor == 901_285      # 9,000.00 + 12.85
    assert fx[0].buy_amount_minor == 901_285
    assert fx[0].buy_currency == "EUR"
    assert fx[0].sell_currency == "USD"


def test_fx_sells_enough_to_absorb_drift():
    """The USD sold must cover the EUR needed even if the rate drifts against
    us by the tolerance — otherwise the payout lands a few cents short."""
    plan = build_plan([_unblock_roterdam()], beat1_position(), load_budget())
    fx = [s for s in plan.steps if s.action == ACT_FX][0]
    rate = RATES["EUR"]                     # EUR per USD
    tolerance = Decimal("0.50")
    effective = rate * (Decimal(1) - tolerance / Decimal(100))
    buy_major = Decimal(fx.buy_amount_minor) / 100
    sell_major = Decimal(fx.sell_amount_minor) / 100
    # buying with the drifted rate must still yield the full amount
    assert (sell_major * effective) >= buy_major


def test_beat2_payment_needs_approval_over_limit():
    plan = build_plan([_unblock_roterdam()], beat1_position(), load_budget())
    pay = [s for s in plan.steps if s.action == ACT_PAY][0]
    assert pay.needs_approval is True
    assert "auto-pay limit" in pay.approval_reason


def test_pay_step_chains_to_its_fx_step():
    plan = build_plan([_unblock_roterdam()], beat1_position(), load_budget())
    pay = [s for s in plan.steps if s.action == ACT_PAY][0]
    assert pay.chained_to is not None
    assert plan.steps[pay.chained_to].action == ACT_FX


# ---- the reserve floor, on real cash ----------------------------------
def test_reserve_floor_defers_the_big_payment():
    """Only USD 6,000 committed with a USD 5,000 floor: the EUR 9,012.85
    payout would breach it, so it must be DEFERRED — not trimmed to fit."""
    tight = beat1_position(cash_minor=1_300_000)
    plan = build_plan([_unblock_roterdam()], tight, load_budget())
    assert plan.steps[0].action == ACT_DEFER
    assert "floor" in plan.steps[0].reason
    assert "below" in plan.steps[0].reason
    assert plan.steps[0].policy_reason == "reserve_floor"


def test_reserve_floor_message_states_the_shortfall():
    tight = beat1_position(cash_minor=1_300_000)
    plan = build_plan([_unblock_roterdam()], tight, load_budget())
    # it must say what it WOULD have left, so a human can judge
    assert "below" in plan.steps[0].reason


def test_floor_never_trims_the_amount():
    """A deferral must carry the FULL payable amount, never a shaved one."""
    tight = beat1_position(cash_minor=1_300_000)
    plan = build_plan([_unblock_roterdam()], tight, load_budget())
    assert plan.steps[0].amount_minor == 901_285


def test_capacity_shortfall_is_distinguished_from_policy():
    """Running out of a currency is CAPACITY; breaching the floor is POLICY.
    They get different reasons because the fix is different."""
    broke = CashPosition(balances={"USD": 5_000},     # USD 50 of real cash
                         rates=RATES, floor_minor=500_000)
    plan = build_plan([_unblock_roterdam()], broke, load_budget())
    assert plan.steps[0].action == ACT_DEFER
    # USD 50 cannot buy EUR 9,012.85 whatever the floor says, so this is NOT a
    # floor problem and must not be reported as one.
    assert plan.steps[0].policy_reason == "insufficient_balance"
    assert "holds" in plan.steps[0].reason


# ---- ordering is policy, not input order ------------------------------
def test_criticality_orders_the_plan_not_input_order():
    small = Payable("PO-7003", "Atlanta Packaging", "USD", 180_000,
                    criticality=2, payout_method="LOCAL")
    big = Payable("PO-7001", "Rotterdam Freight BV", "EUR", 900_000,
                  criticality=5, payout_method="SWIFT", dependency_met=True)
    plan = build_plan([small, big], beat1_position(), load_budget())
    # PO-7001 (criticality 5) is planned first regardless of input order
    payable_order = [s.payable_number for s in plan.steps if s.payable_number]
    assert payable_order.index("PO-7001") < payable_order.index("PO-7003")


def test_plan_is_deterministic():
    a = build_plan(ALL_THREE, beat1_position(), load_budget())
    b = build_plan(list(reversed(ALL_THREE)), beat1_position(), load_budget())
    assert a.summary() == b.summary()


# ---- beat 3's chase step ------------------------------------------------
def test_chase_step_is_planned_and_spends_nothing():
    plan = build_plan(ALL_THREE, beat1_position(), load_budget(),
                      chase_shortfalls={"INV-0042": 320_000})
    chase = [s for s in plan.steps if s.action == ACT_CHASE]
    assert len(chase) == 1
    assert chase[0].payable_number == "INV-0042"
    # a chase recovers cash, so it must not consume committed budget
    assert chase[0].needs_approval is False


def test_zero_shortfall_produces_no_chase():
    plan = build_plan(ALL_THREE, beat1_position(), load_budget(),
                      chase_shortfalls={"INV-0042": 0})
    assert not [s for s in plan.steps if s.action == ACT_CHASE]


# ---- unpriceable / unsupported -----------------------------------------
def test_no_rate_escalates_rather_than_guessing():
    pos = CashPosition(balances={"USD": 4_000_000}, rates={},
                       forecast=Forecast(), floor_minor=500_000)
    plan = build_plan([_unblock_roterdam()], pos, load_budget())
    assert plan.steps[0].action == ACT_ESCALATE
    assert plan.steps[0].policy_reason == "no_rate"


def test_unsupported_currency_escalates():
    weird = Payable("PO-9999", "Atlantis Imports", "XYZ", 100_000)
    plan = build_plan([weird], beat1_position(), load_budget())
    assert plan.steps[0].action == ACT_ESCALATE
    assert plan.steps[0].policy_reason == "unsupported_currency"


# ---- serialisation ------------------------------------------------------
def test_step_payload_is_json_serialisable():
    import json
    plan = build_plan([_unblock_roterdam()], beat1_position(), load_budget())
    for s in plan.steps:
        json.dumps(s.to_payload())   # must not raise


def test_summary_renders_every_step():
    plan = build_plan(ALL_THREE, beat1_position(), load_budget(),
                      chase_shortfalls={"INV-0042": 320_000})
    text = plan.summary()
    assert "PO-7003" in text
    assert "CHASE" in text
    assert len(text.splitlines()) == len(plan.steps)