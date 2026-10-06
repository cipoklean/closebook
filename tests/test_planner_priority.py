"""Regression: identical priority keys must not drop a payable.

`policy.prioritise()` sorts `PayablePriority` objects; the planner then maps
that ordering back to its own `Payable` objects by the tuple key
(criticality, due_date, defer_cost). Two payables that share all three
collide in that dict — and a colliding key silently DROPS a payable from the
plan. Paying a supplier is not something to lose to a dict key.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.domain.planner import ACT_PAY, CashPosition, Payable, build_plan
from app.domain.policy import CommittedCash, load_budget


def _pos():
    return CashPosition(balances={"USD": 4_000_000},
                        committed=CommittedCash("USD", 4_000_000, 0),
                        rates={})


def test_two_identical_payables_both_appear_in_the_plan():
    """Same criticality, same due date, same amount -> same priority key."""
    due = datetime(2026, 10, 20)
    a = Payable("PO-A", "Alpha Ltd", "USD", 100_000,
                criticality=3, due_date=due, payout_method="LOCAL")
    b = Payable("PO-B", "Beta Ltd", "USD", 100_000,
                criticality=3, due_date=due, payout_method="LOCAL")
    plan = build_plan([a, b], _pos(), load_budget())
    planned = {s.payable_number for s in plan.steps if s.payable_number}
    assert planned == {"PO-A", "PO-B"}, (
        f"a payable was dropped by a priority-key collision; planned={planned}")


def test_duplicate_amount_and_criticality_different_suppliers():
    """The seed-style case: same size, same urgency, different counterparties."""
    a = Payable("PO-7003", "Atlanta Packaging", "USD", 180_000,
                criticality=2, payout_method="LOCAL")
    b = Payable("PO-7004", "Bristol Boxes", "USD", 180_000,
                criticality=2, payout_method="LOCAL")
    plan = build_plan([a, b], _pos(), load_budget())
    pays = [s.payable_number for s in plan.steps if s.action == ACT_PAY]
    assert set(pays) == {"PO-7003", "PO-7004"}


def test_three_identical_keys_all_survive():
    items = [Payable(f"PO-{i}", f"Supplier {i}", "USD", 50_000,
                     criticality=3, payout_method="LOCAL") for i in range(3)]
    plan = build_plan(items, _pos(), load_budget())
    planned = {s.payable_number for s in plan.steps if s.payable_number}
    assert planned == {"PO-0", "PO-1", "PO-2"}