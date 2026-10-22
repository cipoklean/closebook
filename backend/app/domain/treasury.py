"""Assemble the planner's inputs from the database, and run the real plan.

The planner itself (`domain/planner.py`) is a PURE function: cash + payables +
budget in, an ordered plan out, no DB and no network. Something has to read the
ledger and hand it those inputs — that something is this module, and it exists
so the demo and the dashboard share ONE assembly.

Two callers previously needed this: `seed/run_demo.py` (which owned the private
helpers) and the dashboard (which needed each payable's real hold reason to show
instead of "Waiting on policy"). Copying the dependency resolution into the UI
would have created a second source of truth for beat 3's rule — exactly the
class of bug this project keeps finding.

IMPORTANT: `dependency_met` is true ONLY when the receivable is fully PAID. A
part-payment does not release the supplier. That is beat 3's whole lesson, and
it lives here so both callers get it.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..db import DB
from ..models import InvoicePayable, InvoiceReceivable, RecStatus
from .planner import (
    ACT_CHASE, CashPosition, Payable, Plan, build_plan,
)
from .policy import BudgetSettings, CommittedCash, load_budget

# CCY per 1 USD. The sandbox rate moves; these are demo inputs, not live quotes.
DEMO_RATES: dict[str, Decimal] = {
    "EUR": Decimal("0.8756"),
    "GBP": Decimal("1.27"),
    "CNY": Decimal("7.1"),
}


def payables_from_db(session) -> list[Payable]:
    """Read the payables and resolve each dependency against the ledger."""
    out: list[Payable] = []
    for p in session.query(InvoicePayable).all():
        dep = p.depends_on_receivable_id
        dep_num = None
        dep_met = True
        dep_open = 0
        dep_ccy = "USD"
        if dep:
            r = session.query(InvoiceReceivable).filter_by(id=dep).first()
            if r is not None:
                dep_num = r.number
                dep_open = max(0, r.amount_minor - r.amount_paid_minor)
                dep_ccy = r.currency
                dep_met = (r.status == RecStatus.PAID)
        out.append(Payable(
            number=p.number,
            supplier=p.supplier.name if p.supplier else "",
            currency=p.currency,
            amount_minor=p.amount_minor,
            criticality=p.criticality,
            due_date=p.due_date,
            # payout_method lives on the SUPPLIER, not on the payable
            payout_method=p.supplier.payout_method.value if p.supplier else "LOCAL",
            depends_on_receivable=dep_num,
            dependency_met=dep_met,
            dependency_open_minor=dep_open,
            dependency_currency=dep_ccy,
        ))
    return out


def cash_position(db: DB, budget: BudgetSettings) -> CashPosition:
    """Balances are CAPACITY (what the sandbox holds); `committed` is POLICY
    (the virtual budget the floor and limits are measured against)."""
    from ..seed import scenario
    s = db.session()
    balances = {}
    for ccy, amt in scenario.SEED_DEPOSITS:
        balances[ccy] = int(amt * 100)
    return CashPosition(
        balances=balances,
        committed=CommittedCash(budget.committed.currency,
                                budget.committed.expected_inflows_minor,
                                budget.committed.earmarked_minor),
        rates=DEMO_RATES,
    )


def chase_shortfalls(session) -> dict[str, int]:
    """Invoices with money in but not closed — chase before relying on the cash."""
    out = {}
    for r in session.query(InvoiceReceivable).all():
        short = max(0, r.amount_minor - r.amount_paid_minor)
        if short > 0 and r.amount_paid_minor > 0:
            out[r.number] = short
    return out


def current_plan(db: DB, budget: Optional[BudgetSettings] = None) -> Plan:
    """Run the REAL planner over the CURRENT database state.

    This is the single place the app turns stored rows into a treasury plan, so
    a hold reason shown in the UI is the same string the planner produced — not
    a paraphrase a view invented.
    """
    budget = budget or load_budget()
    s = db.session()
    return build_plan(
        payables_from_db(s),
        cash_position(db, budget),
        budget,
        chase_shortfalls=chase_shortfalls(s),
    )


__all__ = [
    "DEMO_RATES", "ACT_CHASE", "cash_position", "chase_shortfalls",
    "current_plan", "payables_from_db",
]
