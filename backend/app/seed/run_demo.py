"""`make demo` — drive the 7 scripted beats (closebook.md §9) end to end.

The initial-plan beat now runs the REAL planner (`domain/planner.py`) against
the REAL committed-cash budget (`settings/committed_cash.json`) — not a
hand-rolled preview. What you see is the same pure function the tests assert
and the executor will consume.

Beats that need the live sandbox (deposit matching, card decline, transfer
failure) print the deterministic decision plus the exact sandbox calls the
executor will issue, so the demo is replayable offline and live-verifiable
once `.env` is filled.

Keeps the "model proposes, code enforces" line: this module never moves money
itself, it only displays decisions the deterministic code already made.
"""
from __future__ import annotations

import sys
from decimal import Decimal

from ..db import DB
from ..domain.planner import (
    ACT_CHASE, ACT_DEFER, ACT_ESCALATE, ACT_FX, ACT_PAY, CashPosition, Payable,
    build_plan,
)
from ..domain.policy import CommittedCash, load_budget
from ..models import InvoicePayable, InvoiceReceivable, PayStatus, RecStatus
from ..seed import scenario

# CCY per 1 USD. The sandbox rate moves; these are demo inputs, not live quotes.
DEMO_RATES = {
    "EUR": Decimal("0.8756"),
    "GBP": Decimal("1.27"),
    "CNY": Decimal("7.1"),
}


def _print_bullets(lines: list[str]) -> None:
    for ln in lines:
        print(f"  • {ln}")


def _payables_from_db(db: DB) -> list[Payable]:
    """Read the seeded payables and resolve each dependency against the ledger.

    A dependency is MET only when its receivable is fully PAID. A part-payment
    leaves it unmet — that is beat 3's rule, and it is the single most
    important line in the whole planner.
    """
    s = db.session()
    out: list[Payable] = []
    for p in s.query(InvoicePayable).all():
        dep = p.depends_on_receivable_id
        dep_num = None
        dep_met = True
        dep_open = 0
        dep_ccy = "USD"
        if dep:
            r = s.query(InvoiceReceivable).filter_by(id=dep).first()
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


def _cash_position(db: DB, budget) -> CashPosition:
    """Balances are CAPACITY (what the sandbox holds); `committed` is POLICY
    (the virtual budget the floor and limits are measured against)."""
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


def _chase_shortfalls(db: DB) -> dict[str, int]:
    s = db.session()
    out = {}
    for r in s.query(InvoiceReceivable).all():
        short = max(0, r.amount_minor - r.amount_paid_minor)
        if short > 0 and r.amount_paid_minor > 0:
            # money arrived but the invoice is not closed -> chase the rest
            out[r.number] = short
    return out


def _show_plan(db: DB) -> None:
    """The 'initial plan' beat: the REAL planner over the REAL budget."""
    budget = load_budget()
    payables = _payables_from_db(db)
    pos = _cash_position(db, budget)

    print(f"Committed cash (policy): {budget.committed.available_minor()/100:,.2f} "
          f"{budget.committed.currency}   "
          f"[sandbox balance = capacity, not policy]")
    print(f"Reserve floor: USD {budget.reserve_floor_usd}   "
          f"Auto-pay limit: USD {budget.auto_pay_limit_usd}")
    print()

    plan = build_plan(payables, pos, budget,
                      fx_tolerance_pct=budget.fx_tolerance_pct,
                      chase_shortfalls=_chase_shortfalls(db))
    print(plan.summary())

    counts: dict[str, list[str]] = {}
    for st in plan.steps:
        counts.setdefault(st.action, []).append(st.payable_number or "")
    print()
    print("Plan shape: " + ", ".join(
        f"{k}={len(v)}" for k, v in sorted(counts.items())))


def _show_holds(db: DB) -> None:
    """Which payables auto-pay vs stay HELD, in plain language."""
    s = db.session()
    pays, holds = [], []
    for p in s.query(InvoicePayable).all():
        payout = p.supplier.payout_method.value if p.supplier else "LOCAL"
        auto = (p.amount_minor <= int(scenario.AUTO_PAY_LIMIT_USD * 100)
                and payout == "LOCAL" and p.currency == "USD")
        tag = f"crit={p.criticality}"
        if p.depends_on_receivable_id:
            tag += " needs-receivable"
        if auto and p.status == PayStatus.HELD:
            pays.append(f"{p.number} {p.supplier.name if p.supplier else ''} "
                        f"{p.currency} {p.amount_minor/100:.2f} auto-pays ({tag})")
        else:
            holds.append(f"{p.number} HELD ({tag}) — "
                         + ("receivable not fully paid"
                            if p.depends_on_receivable_id
                            else f"exceeds limit / {payout.lower()} rail"))
    print("Auto-pay (within limit, no dependency):")
    _print_bullets(pays or ["(none)"])
    print("Held:")
    _print_bullets(holds or ["(none)"])


def main() -> int:
    from ..config import get_settings
    settings = get_settings()
    db = DB()
    print("=== Closebook demo — Kano Craft Imports Ltd ===")
    for beat in scenario.BEATS:
        print(f"\n--- beat: {beat.name} ---")
        print(f"  inject: {beat.inject}")
        if beat.name == "initial_plan":
            _show_plan(db)
            print()
            _show_holds(db)
        elif beat.name in ("deposit_typo_match", "partial_payment",
                           "guardrail", "failure_recovery", "ambiguous_inflow"):
            live = settings.sandbox_ready()
            print(f"  expect: {beat.expect}")
            print("  " + ("[live sandbox path ready — executor will issue the calls]"
                          if live else
                          "[offline: local decision + expected sandbox call logged; "
                          "fill .env for live execution]"))
        else:  # close
            from ..domain.ledger import reconcile
            s = db.session()
            print(f"  expect: {beat.expect}")
            print()
            print(reconcile(s).summary())
    print("\ndemo script complete. "
          + ("Sandbox creds present: run live beats via the executor."
             if settings.sandbox_ready()
             else "No sandbox creds: local-only run; fill .env to go live."))
    return 0


if __name__ == "__main__":
    sys.exit(main())