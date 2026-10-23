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

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from ..db import DB
from ..money import money_str
from ..models import (
    InvoicePayable, InvoiceReceivable, PayStatus, PlanStep, RecStatus, StepStatus,
    StepType,
)
from .planner import (
    ACT_CHASE, ACT_DEFER, ACT_ESCALATE, ACT_FX, ACT_PAY, CashPosition, Payable,
    Plan, build_plan,
)
from .policy import BudgetSettings, Forecast, load_budget

# CCY per 1 USD. The sandbox rate moves; these are demo inputs, not live quotes.
DEMO_RATES: dict[str, Decimal] = {
    "EUR": Decimal("0.8756"),
    "GBP": Decimal("1.27"),
    "CNY": Decimal("7.1"),
}


def _attempt_failed(session, number: str) -> bool:
    """Whether an earlier payment for this bill was returned or failed.

    Read from `plan_store`, which owns what "retired" means, so the plan and the
    conductor cannot disagree about whether a payment may go out on its own.
    """
    from .planner import ACT_PAY, Step
    from .plan_store import valid_approval, has_retired_attempt

    return has_retired_attempt(session, Step(action=ACT_PAY, reason="",
                                             payable_number=number))


def payables_from_db(session) -> list[Payable]:
    """The payables that STILL NEED PAYING, with each dependency resolved.

    A settled bill is not a payable. PO-2001 was paid, the ledger had taken the
    cash out and the invoice behind it was closed — and the planner still emitted
    a DEFER for it every run, so the page listed a paid bill under "On hold" with
    a reason about an invoice that had already landed. Anything already PAID
    leaves the plan entirely.
    """
    out: list[Payable] = []
    for p in session.query(InvoicePayable).all():
        if p.status == PayStatus.PAID:
            continue
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
        # THE STORED APPROVAL, resolved here because the planner is a PURE
        # function: it gets the world handed to it, never a session.
        from .plan_store import valid_approval
        _ap = valid_approval(
            session, payable_number=p.number, action="PAY",
            amount_minor=p.amount_minor, currency=p.currency,
            counterparty=(p.supplier.name if p.supplier else ""))
        out.append(Payable(
            has_valid_approval=_ap is not None,
            previous_attempt_failed=_attempt_failed(session, p.number),
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


def _sent_steps(session, posted: set) -> list:
    """Executed FX/transfer steps that are SENT and not yet in the ledger."""
    rows = (session.query(PlanStep)
            .filter(PlanStep.airwallex_object_id.isnot(None))
            .order_by(PlanStep.id.asc()).all())
    return [r for r in rows
            if r.airwallex_object_id not in posted
            and r.status in (StepStatus.SUBMITTED, StepStatus.APPROVED)]


def _payload_of(row) -> dict:
    import json as _json
    try:
        return _json.loads(row.payload_json or "{}")
    except (ValueError, TypeError):
        return {}


def in_flight_for(session, payable_number: str,
                  *, posted: set | None = None) -> Optional[tuple[str, int]]:
    """(currency, minor) committed in flight for ONE payable, or None.

    One payable, one commitment. A payable whose conversion has NOT been posted
    is committed in the conversion's SELL currency, because the bought currency
    does not exist yet. Once the conversion is in the ledger the bought currency
    IS cash, so the commitment becomes the transfer's own currency. Counting
    both would double the commitment.

    Reads the EXECUTED step payloads, never a freshly rebuilt plan: the payload
    is what was actually submitted. A recomputed plan can quote a different
    conversion size (rates move between runs) and the tiles would then disagree
    with the books about money that has already left.
    """
    from .settlement import settled_txn_keys
    posted = settled_txn_keys(session) if posted is None else posted

    fx = pay = None
    for row in _sent_steps(session, posted):
        pl = _payload_of(row)
        if pl.get("payable_number") != payable_number:
            continue
        if row.type == StepType.FX:
            fx = pl
        elif row.type == StepType.TRANSFER:
            pay = pl

    if fx is not None:
        ccy, amt = fx.get("sell_currency"), int(fx.get("sell_amount_minor") or 0)
        if ccy and amt > 0:
            return str(ccy).upper(), amt
    if pay is not None:
        ccy, amt = pay.get("currency"), int(pay.get("amount_minor") or 0)
        if ccy and amt > 0:
            return str(ccy).upper(), amt
    return None


def in_flight_by_currency(session, *, posted: set | None = None) -> dict[str, int]:
    """Money SENT but not settled IN THE LEDGER, per currency, summed."""
    from .settlement import settled_txn_keys
    posted = settled_txn_keys(session) if posted is None else posted

    out: dict[str, int] = {}
    for row in _sent_steps(session, posted):
        pl = _payload_of(row)
        number = pl.get("payable_number")
        if not number:
            continue
        got = in_flight_for(session, str(number), posted=posted)
        if got is None:
            continue
        ccy, amt = got
        # Count each payable ONCE, not once per step.
        if any(_payload_of(r).get("payable_number") == number
               for r in _sent_steps(session, posted) if r.id < row.id):
            continue
        out[ccy] = out.get(ccy, 0) + amt
    return out


def forecast_from_db(session, rates: dict) -> Forecast:
    """What is invoiced and expected to land. A FORECAST — it funds nothing.

    Derived from the open receivables rather than a config number, so it cannot
    drift from the invoices it claims to describe. Non-USD amounts are valued in
    the budget currency only so a shortfall can name what would cover it.
    """
    from .policy import usd_value_minor

    lines = []
    for r in session.query(InvoiceReceivable).all():
        open_minor = max(0, int(r.amount_minor) - int(r.amount_paid_minor))
        if open_minor <= 0:
            continue
        lines.append({
            "number": r.number,
            "currency": r.currency,
            "amount_minor": open_minor,
            "usd_minor": usd_value_minor(open_minor, r.currency, rates),
        })
    return Forecast(currency="USD", lines=lines)


def cash_position(db: DB, budget: BudgetSettings,
                  *, session=None) -> CashPosition:
    """The planner's position, from the LEDGER.

    This used to read `scenario.SEED_DEPOSITS` — hardcoded demo numbers that
    ignored the books entirely, so the plan could spend USD 40,000 that the
    ledger did not have. It now reads what is actually there, and subtracts what
    is already committed, through the one rule in `policy.available_cash_minor`.

    THE SESSION IS OWNED HERE. It used to be `s = session or db.session()` with
    no close, so every caller that passed no session leaked a connection.
    """
    from ..db import session_for

    with session_for(db, session) as s:
        return _position_from(s, budget)


def _position_from(s, budget: BudgetSettings) -> CashPosition:
    """The position itself, given a session whose lifetime someone else owns."""
    from .ledger import reconcile

    rep = reconcile(s)
    floor_minor = int(budget.reserve_floor_usd * 100)
    return CashPosition(
        balances={k: int(v) for k, v in rep.cash_by_currency.items()},
        in_flight=in_flight_by_currency(s),
        # Received but not attributed. Real cash, and not spendable: see
        # `policy.available_cash_minor`. Without this the plan could pay a
        # supplier with money that a human has not yet agreed belongs to any
        # invoice — and that may have to be refunded.
        suspense={k: int(abs(v)) for k, v in (rep.suspense_by_currency or {}).items()},
        # Money we have decided is owed back. Non-spendable for the mirror-image
        # reason: the refund will leave later, out of cash we can no longer see.
        refunds_owed={k: int(abs(v))
                      for k, v in (rep.refunds_owed_by_currency or {}).items()},
        forecast=forecast_from_db(s, DEMO_RATES),
        rates=DEMO_RATES,
        budget_currency=budget.currency,
        floor_minor=floor_minor,
    )


@dataclass
class Funding:
    """What a payable actually COSTS, in the currency that pays for it.

    A EUR payable funded from USD is not a EUR obligation — we hold no EUR, so
    the bill is really "buy EUR 9,012.85 with USD 10,346.00". Valuing it in EUR
    reported a EUR 9,000 shortfall against a business that was never short of
    EUR; it was simply going to convert. The funding currency is the one that
    has to be there.
    """
    currency: str
    amount_minor: int
    # "conversion"  the plan converts another currency into this payable's
    # "held"        paid straight from a currency we hold (no conversion needed)
    # "unfunded"    we do NOT hold the currency and the plan plans no conversion
    # "no_plan"     the plan says nothing about this payable at all
    source: str
    note: str = ""


def funding_for(plan, payable_number: str, held: set[str],
                *, conversion_posted: bool = False) -> Funding:
    """Cost a payable in its FUNDING currency, using the plan's own numbers.

    The planned conversion amount is used verbatim — it already carries the
    drift headroom the planner added so a 0.5% adverse move cannot leave the
    payout short. Re-deriving it here would invent a second rate, and this
    project has already been burned once by two modules disagreeing about a
    rate while every isolated test passed.

    `conversion_posted` short-circuits all of that: once the conversion is IN
    THE LEDGER, the bought currency is real cash, so the payout is funded from
    the currency we now hold. Without this the ledger would hold the conversion
    AND the tile would still charge the plan's conversion, counting the same
    money twice.
    """
    steps = plan.payable_actions(payable_number) if plan is not None else []
    fx = next((s for s in steps if s.action == ACT_FX), None)
    pay = next((s for s in steps if s.action in (ACT_PAY, ACT_DEFER, ACT_ESCALATE)),
               None)

    if conversion_posted and pay is not None:
        return Funding(
            pay.currency, pay.amount_minor, "converted",
            f"funded from {pay.currency}: the conversion that bought it is "
            f"already in the ledger, so it is cash we hold")

    if fx is not None:
        sell_ccy = fx.sell_currency or "USD"
        buy_ccy = fx.buy_currency or sell_ccy
        return Funding(
            currency=sell_ccy,
            amount_minor=fx.sell_amount_minor,
            source="conversion",
            note=(f"paid from {sell_ccy}: the plan converts "
                  f"{money_str(fx.sell_amount_minor, sell_ccy)} into "
                  f"{money_str(fx.buy_amount_minor, buy_ccy)}"
                  + (f" (incl. drift headroom)" if fx.rate else "")),
        )

    if pay is None:
        return Funding("USD", 0, "no_plan",
                       "the plan has no step for this payable")

    # THE PLAN'S OWN NUMBER, for planned and deferred payables alike. The
    # planner records `funding_currency`/`funding_amount_minor` on every step it
    # produces about a payable, so the tile reads the same arithmetic the plan
    # used rather than re-deriving it. Re-deriving is exactly how the tile came
    # to say "no conversion is planned" for a bill the plan costed at USD
    # 10,346.00.
    if pay.currency in held:
        return Funding(pay.currency, pay.amount_minor, "held", "")

    f_ccy = getattr(pay, "funding_currency", None)
    f_amt = int(getattr(pay, "funding_amount_minor", 0) or 0)
    if f_ccy and f_amt > 0:
        planned = pay.action in (ACT_FX, ACT_PAY)
        return Funding(
            f_ccy, f_amt, "conversion" if planned else "planned",
            ("" if planned else
             f"the plan converts {f_ccy} into {pay.currency} as soon as it can "
             f"be afforded — {money_str(f_amt, f_ccy)} set aside for it"))

    return Funding(
        pay.currency, pay.amount_minor, "unfunded",
        f"no conversion is planned and we hold no {pay.currency}")


def chase_shortfalls(session) -> dict[str, tuple[int, str]]:
    """Invoices with money in but not closed — chase before relying on the cash.

    Returns `{number: (shortfall_minor, currency)}`. The currency travels WITH
    the amount because the shortfall is in the INVOICE's currency, and a chase
    step that labelled a GBP 3,200 gap as "USD 3,200.00" sends the operator to
    the customer with the wrong number in the wrong unit — the same class of
    wrong-currency reason this planner was already fixed for once.
    """
    out = {}
    for r in session.query(InvoiceReceivable).all():
        short = max(0, r.amount_minor - r.amount_paid_minor)
        if short > 0 and r.amount_paid_minor > 0:
            out[r.number] = (short, r.currency)
    return out


def current_plan(db: DB, budget: Optional[BudgetSettings] = None) -> Plan:
    """Run the REAL planner over the CURRENT database state.

    This is the single place the app turns stored rows into a treasury plan, so
    a hold reason shown in the UI is the same string the planner produced — not
    a paraphrase a view invented.

    The policy comes from the WORKSPACE ROW in the database (setting_store),
    not from the vendored JSON: the floor and the limit are the operator's to
    change from the app, and the plan has to obey the ones on screen.
    """
    from .settings_store import workspace_budget

    # Same leak, same fix: this opened a session and never closed it, so the
    # plan path leaked a connection on every call as well.
    from ..db import session_for

    with session_for(db) as s:
        budget = budget or workspace_budget(s)
        return build_plan(
            payables_from_db(s),
            cash_position(db, budget, session=s),
            budget,
            chase_shortfalls=chase_shortfalls(s),
        )


__all__ = [
    "DEMO_RATES", "ACT_CHASE", "Funding", "cash_position", "chase_shortfalls",
    "current_plan", "forecast_from_db", "funding_for", "in_flight_by_currency",
    "payables_from_db",
]
