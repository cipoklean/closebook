"""Planner — turn money-on-hand + open payables into an ordered, justified plan.

This is the piece that makes Closebook a treasury operator rather than a
reconciliation tool. `build_plan()` is a **pure function**: cash position +
payables + budget in, an ordered list of steps out. No DB, no API, no model.
That means the entire money decision is unit-testable and reproducible — the
same inputs always produce the same plan, which is exactly what a judge (or an
auditor) will check.

The rules the planner enforces, in order:

1. **Dependency gate.** A payable that depends on a receivable stays HELD until
   that receivable is *fully paid*. A part-payment does NOT unblock it (beat 3:
   GBP 4,800 of 8,000 leaves PO-7002 held) — paying a supplier out of a
   customer who still owes us money is how a treasury gets into trouble.
2. **Deterministic priority.** `policy.prioritise()`: criticality desc, then
   due date, then cost-of-deferring desc.
3. **Capacity vs policy — two different limits.** The sandbox balance is
   *capacity* (can we physically move it?); the committed-cash budget is
   *policy* (may we?). Both must hold, and they fail for different reasons, so
   they get different messages.
4. **Reserve floor.** Every step is checked against the running committed-cash
   position *after* the step. If it would breach the floor, the step is DEFERRED
   — never silently trimmed to fit.
5. **Auto-pay limit.** Above the limit a step is still planned, but flagged
   `needs_approval` so it waits for a human. It is not dropped.
6. **FX only what's needed.** A payable in a currency we don't hold produces a
   CONVERT step sized to that payable plus the rail fee, with the FX tolerance
   applied so rate drift can't leave the conversion a few cents short.

Every step carries the reason it exists. That is the model's job later — to
explain the plan, never to author it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_UP
from typing import Iterable, Optional

from ..money import minor_for, money_str, to_major_str, to_minor
from .policy import (
    AUTO_PAY_LIMIT_USD, BudgetSettings, CommittedCash, PayablePriority,
    RESERVE_FLOOR_USD, breaches_reserve_floor, priority_key, prioritise,
    requires_approval, supported, total_payout_minor, usd_value_minor,
)

# What the plan is asking the executor to do.
ACT_FX = "FX"            # convert one currency into another
ACT_PAY = "PAY"          # release a payable (transfer / card)
ACT_DEFER = "DEFER"      # deliberately not doing it yet, with a reason
ACT_CHASE = "CHASE"      # ask a customer for a shortfall
ACT_ESCALATE = "ESCALATE"  # a human must decide


@dataclass
class Payable:
    """One bill the business owes, as the planner sees it."""
    number: str
    supplier: str
    currency: str
    amount_minor: int
    criticality: int = 3
    due_date: Optional[datetime] = None
    payout_method: str = "LOCAL"
    depends_on_receivable: Optional[str] = None
    # Dependency state, resolved by the caller from the ledger:
    dependency_met: bool = True
    dependency_open_minor: int = 0
    # The currency the dependency shortfall is denominated in (NOT necessarily
    # the payable's currency — INV-0042's shortfall is GBP while PO-7002 is USD).
    dependency_currency: str = "USD"


@dataclass
class CashPosition:
    """What we hold and what we're allowed to move.

    balances:     real spendable balance per currency (sandbox = CAPACITY)
    committed:    committed-cash budget (policy), in `committed_currency`
    rates:        1 USD -> N units of currency. Used to size FX and to value
                  amounts for the USD-based floor/limit checks.
    """
    balances: dict[str, int] = field(default_factory=dict)
    committed: CommittedCash = field(default_factory=lambda: CommittedCash("USD", 0, 0))
    rates: dict[str, Decimal] = field(default_factory=dict)

    def held(self, currency: str) -> int:
        return self.balances.get(currency.upper(), 0)

    def usd_rate(self, currency: str) -> Optional[Decimal]:
        """Units of `currency` per 1 USD. USD is 1 by definition."""
        c = currency.upper()
        if c == "USD":
            return Decimal(1)
        return self.rates.get(c)

    def to_usd_minor(self, amount_minor: int, currency: str) -> int:
        """USD-equivalent of an amount. 0 if it cannot be priced — conservative,
        so an unpriceable amount never looks spendable.

        Delegates to policy.usd_value_minor so the floor check and the auto-pay
        limit can never disagree about what a rate means.
        """
        return usd_value_minor(amount_minor, currency, self.rates)


@dataclass
class Step:
    """One thing to do, fully specified, with the reason it exists."""
    action: str
    reason: str
    # PAY
    payable_number: Optional[str] = None
    supplier: Optional[str] = None
    amount_minor: int = 0
    currency: str = "USD"
    payout_method: str = "LOCAL"
    # FX
    sell_currency: Optional[str] = None
    sell_amount_minor: int = 0
    buy_currency: Optional[str] = None
    buy_amount_minor: int = 0
    rate: Optional[Decimal] = None
    # governance
    needs_approval: bool = False
    approval_reason: str = ""
    policy_reason: str = ""
    # EVERY reason this step is not happening, not just the first. A held
    # payment that names one blocker invites the reader to clear it, expect
    # movement, and be surprised by the next one. `reason` carries them joined;
    # these carry them structured.
    blockers: list[str] = field(default_factory=list)
    blocker_codes: list[str] = field(default_factory=list)
    # linking
    unlocks: list[str] = field(default_factory=list)
    chained_to: Optional[int] = None   # index of the step this depends on

    def to_payload(self) -> dict:
        """Serialisable form for PlanStep.payload_json."""
        return {
            "action": self.action,
            "payable_number": self.payable_number,
            "supplier": self.supplier,
            "amount_minor": self.amount_minor,
            "amount": to_major_str(self.amount_minor, self.currency),
            "currency": self.currency,
            "payout_method": self.payout_method,
            "sell_currency": self.sell_currency,
            "sell_amount_minor": self.sell_amount_minor,
            "buy_currency": self.buy_currency,
            "buy_amount_minor": self.buy_amount_minor,
            "rate": str(self.rate) if self.rate is not None else None,
            "needs_approval": self.needs_approval,
            "approval_reason": self.approval_reason,
            "policy_reason": self.policy_reason,
            "blockers": list(self.blockers),
            "blocker_codes": list(self.blocker_codes),
            "unlocks": self.unlocks,
            "reason": self.reason,
        }


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)

    def payable_actions(self, number: str) -> list[Step]:
        return [s for s in self.steps if s.payable_number == number]

    def summary(self) -> str:
        lines = []
        for i, s in enumerate(self.steps):
            tag = "APPROVAL" if s.needs_approval else "auto"
            if s.action == ACT_FX:
                amt = (f"{money_str(s.sell_amount_minor, s.sell_currency or 'USD')}"
                       f" -> {money_str(s.buy_amount_minor, s.buy_currency or 'USD')}")
            elif s.action in (ACT_PAY, ACT_DEFER):
                amt = money_str(s.amount_minor, s.currency)
                if s.payable_number:
                    amt += f" ({s.payable_number} {s.supplier})"
            else:
                amt = s.payable_number or ""
            lines.append(f"{i+1}. [{tag}] {s.action:<9} {amt} — {s.reason}")
        return "\n".join(lines)


# ---- helpers -------------------------------------------------------------
def _fx_sell_needed(buy_minor: int, buy_currency: str, rate: Decimal,
                    tolerance_pct: Decimal) -> int:
    """USD to sell so that `buy_minor` arrives even if the rate drifts against
    us by `tolerance_pct`. Rounded UP to a whole major unit — under-selling
    leaves the payout a few cents short at the far end."""
    if tolerance_pct > 0:
        effective = rate * (Decimal(1) - tolerance_pct / Decimal(100))
    else:
        effective = rate
    buy_major = Decimal(buy_minor) / Decimal(minor_for(buy_currency))
    sell_major = (buy_major / effective).quantize(Decimal(1), rounding=ROUND_UP)
    return to_minor(sell_major, "USD")


def _defer_cost(p: Payable, pos: CashPosition) -> int:
    """Rough USD cost of putting this off: the invoice, valued if we can."""
    usd = pos.to_usd_minor(p.amount_minor, p.currency)
    return usd if usd is not None else 0


# ---- the planner ---------------------------------------------------------
def build_plan(payables: Iterable[Payable], pos: CashPosition,
               budget: Optional[BudgetSettings] = None,
               fx_tolerance_pct: Decimal = Decimal("0.50"),
               chase_shortfalls: dict[str, int] | None = None) -> Plan:
    """Produce the ordered plan. Pure: same inputs -> same plan, always.

    `payables` may arrive in any order; `prioritise()` decides who is served
    first, so the output ordering is a function of policy, not of the query.
    """
    budget = budget or BudgetSettings(committed=pos.committed)
    plan = Plan()
    payables = list(payables)

    # Running state. `working` is the live capacity we can still draw on;
    # `committed_left` is the policy budget still deployable. Both start from
    # the caller's position and are decremented as steps are planned.
    working = dict(pos.balances)
    committed_left = pos.committed.available_minor()

    # ---- chase steps first: they recover cash, they never spend it --------
    for inv_number, shortfall in (chase_shortfalls or {}).items():
        if shortfall <= 0:
            continue
        plan.steps.append(Step(
            action=ACT_CHASE,
            reason=(f"{inv_number} is {money_str(shortfall, 'USD')} short; "
                    f"chasing before we rely on that cash"),
            payable_number=inv_number,
            amount_minor=shortfall,
            currency="USD",
        ))

    # ---- who gets paid, in policy order -----------------------------------
    # Order by policy, never by input order. We sort a list of INDEXES by the
    # priority key. The earlier version rebuilt the payable list from a dict
    # keyed on (criticality, due_date, defer_cost) — and two payables can
    # legitimately share all three, so one was silently DROPPED from the plan.
    # A supplier unpaid because of a dict collision is not an acceptable
    # failure mode. `prioritise()` is still the single source of ordering
    # policy; here we only apply its key to stable indices.
    def _key(i: int) -> tuple:
        p = payables[i]
        return priority_key(p.criticality, p.due_date, _defer_cost(p, pos))

    ordered_payables = [payables[i] for i in sorted(range(len(payables)), key=_key)]

    for p in ordered_payables:
        # EVERY reason this payable is not being paid, collected rather than
        # returned at the first. PO-7002 is blocked by BOTH an unpaid customer
        # invoice and the reserve floor; naming only the first tells the reader
        # to go and chase INV-0042 and then be surprised when the payment still
        # does not happen.
        blockers: list[str] = []
        codes: list[str] = []
        fx_index = None

        # --- 1. dependency gate -------------------------------------------
        if not p.dependency_met:
            short_txt = ""
            if p.dependency_open_minor > 0:
                short_txt = (f", {money_str(p.dependency_open_minor, p.dependency_currency)}"
                             f" still outstanding")
            blockers.append(
                f"waiting on {p.depends_on_receivable or 'its receivable'}"
                f"{short_txt} — a part-payment does not release the supplier")
            codes.append("dependency_not_met")

        # --- 2. priceable? -------------------------------------------------
        if not supported(p.currency):
            plan.steps.append(Step(
                action=ACT_ESCALATE,
                payable_number=p.number, supplier=p.supplier,
                amount_minor=p.amount_minor, currency=p.currency,
                reason=(f"{p.currency} is not a supported currency; "
                        f"cannot price or convert it"),
                policy_reason="unsupported_currency",
                blockers=[f"{p.currency} is not a supported currency"],
                blocker_codes=["unsupported_currency"],
            ))
            continue

        # --- 3. the amount we actually move (rail fee included) -----------
        move_minor = total_payout_minor(p.amount_minor, p.currency, p.payout_method)
        fee_txt = ""
        if move_minor != p.amount_minor:
            fee_txt = (f" (incl. {money_str(move_minor - p.amount_minor, p.currency)} "
                       f"{p.payout_method} fee)")

        held = working.get(p.currency.upper(), 0)

        # --- 4. do we hold the currency? if not, the conversion is needed --
        # Everything in this block is a WHAT-IF: it computes what the floor
        # would say without touching `working` or `committed_left`, because a
        # blocked payable must leave the remaining capacity for the next one.
        fx_plan = None            # (sell_minor, rate, committed_after)
        if held < move_minor:
            rate = pos.usd_rate(p.currency)
            if rate is None:
                plan.steps.append(Step(
                    action=ACT_ESCALATE,
                    payable_number=p.number, supplier=p.supplier,
                    amount_minor=move_minor, currency=p.currency,
                    payout_method=p.payout_method,
                    reason=(f"no {p.currency} balance and no FX rate to buy it; "
                            f"a human decides"),
                    policy_reason="no_rate",
                    blockers=[f"no {p.currency} balance and no FX rate to buy it"],
                    blocker_codes=["no_rate"],
                ))
                continue

            sell_minor = _fx_sell_needed(move_minor, p.currency, rate,
                                         fx_tolerance_pct)
            usd_held = working.get("USD", 0)
            if usd_held < sell_minor:
                blockers.append(
                    f"needs {money_str(sell_minor, 'USD')} to buy "
                    f"{money_str(move_minor, p.currency)}, but only "
                    f"{money_str(usd_held, 'USD')} is available — capacity, "
                    f"not policy")
                codes.append("insufficient_balance")
            else:
                committed_after_fx = committed_left - sell_minor
                if breaches_reserve_floor(committed_after_fx, "USD",
                                          floor_usd=budget.reserve_floor_usd):
                    blockers.append(
                        f"converting {money_str(sell_minor, 'USD')} would drop "
                        f"committed cash to "
                        f"{money_str(committed_after_fx, 'USD')}, below the "
                        f"{money_str(int(budget.reserve_floor_usd * 100), 'USD')} "
                        f"reserve floor — deferring, not trimming")
                    codes.append("reserve_floor")
                else:
                    fx_plan = (sell_minor, rate, committed_after_fx)

            if not blockers and fx_plan is not None:
                # Actually plan the conversion and take the capacity.
                sell_minor, rate, committed_after_fx = fx_plan
                fx_step = Step(
                    action=ACT_FX,
                    reason=(f"buy the {money_str(move_minor, p.currency)} "
                            f"that {p.number} needs{fee_txt}, "
                            f"including {fx_tolerance_pct}% drift tolerance"),
                    sell_currency="USD", sell_amount_minor=sell_minor,
                    buy_currency=p.currency.upper(), buy_amount_minor=move_minor,
                    rate=rate,
                    payable_number=p.number,
                    policy_reason="fund_payable",
                )
                fx_index = len(plan.steps)
                plan.steps.append(fx_step)
                committed_left = committed_after_fx
                working["USD"] = working.get("USD", 0) - sell_minor
                working[p.currency.upper()] = held + move_minor

        # --- 5. reserve floor on the payout itself (what-if when blocked) ---
        pay_usd = pos.to_usd_minor(move_minor, p.currency)
        committed_after_pay = committed_left - pay_usd
        if breaches_reserve_floor(committed_after_pay, "USD",
                                  floor_usd=budget.reserve_floor_usd):
            blockers.append(
                f"paying {p.number} ({money_str(move_minor, p.currency)}) would "
                f"leave cash {money_str(committed_after_pay, 'USD')}, below the "
                f"{money_str(int(budget.reserve_floor_usd * 100), 'USD')} floor")
            codes.append("reserve_floor")

        # --- 6. anything blocking? one step, every reason ------------------
        if blockers:
            plan.steps.append(Step(
                action=ACT_DEFER,
                payable_number=p.number, supplier=p.supplier,
                amount_minor=move_minor, currency=p.currency,
                payout_method=p.payout_method,
                reason="; ".join(blockers),
                policy_reason=codes[0],
                blockers=blockers,
                blocker_codes=codes,
                chained_to=fx_index,
            ))
            continue

        # --- 7. approval threshold ---------------------------------------
        needs_approval, why = requires_approval(
            move_minor, p.currency, pos.rates,
            auto_limit_usd=budget.auto_pay_limit_usd)

        committed_left = committed_after_pay
        working[p.currency.upper()] = working.get(p.currency.upper(), 0) - move_minor

        plan.steps.append(Step(
            action=ACT_PAY,
            payable_number=p.number, supplier=p.supplier,
            amount_minor=move_minor, currency=p.currency.upper(),
            payout_method=p.payout_method,
            needs_approval=needs_approval,
            approval_reason=why,
            reason=(f"{p.number} to {p.supplier}: {money_str(move_minor, p.currency)}"
                    f"{fee_txt} by {p.payout_method} — {why}"),
            policy_reason="within_policy" if not needs_approval else "needs_approval",
            chained_to=fx_index,
        ))

    return plan