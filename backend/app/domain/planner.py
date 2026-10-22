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
    AUTO_PAY_LIMIT_USD, BudgetSettings, Forecast, PayablePriority,
    RESERVE_FLOOR_USD, available_cash_minor, priority_key, prioritise,
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
    """What we hold, what is already committed, and what is still coming.

    balances:  LEDGER cash per currency. Capacity — and, in the budget currency,
                                                 the cash the floor is measured against.
    in_flight: sent to the bank and not settled in the LEDGER yet. Committed
               money that has not left the books; it must not be planned twice.
    forecast:  invoiced and expected. DISPLAY AND NAMING ONLY — never funds a
               payment. See `policy.available_cash_minor`.
    rates:     1 USD -> N units of currency. Used to size FX and to value
               amounts for the USD-based floor/limit checks.

    `committed` (the old expected-inflows budget) is GONE. It let the floor be
    measured against money the business had not received, so the plan approved
    payments the cash position could not cover.
    """
    balances: dict[str, int] = field(default_factory=dict)
    in_flight: dict[str, int] = field(default_factory=dict)
    #: Received but NOT attributed to an invoice (a proposal awaiting approval,
    #: or a payment that matched nothing). Real money in the bank, and still not
    #: spendable — see `policy.available_cash_minor`.
    suspense: dict[str, int] = field(default_factory=dict)
    #: Money we have decided belongs to somebody else. Also real cash, also not
    #: spendable: a refund that bounces later comes out of cash that is gone.
    refunds_owed: dict[str, int] = field(default_factory=dict)
    forecast: Forecast = field(default_factory=Forecast)
    rates: dict[str, Decimal] = field(default_factory=dict)
    budget_currency: str = "USD"
    floor_minor: int = 500_000          # the reserve floor, in budget_currency

    def held(self, currency: str) -> int:
        return self.balances.get(currency.upper(), 0)

    def available_minor(self, committed_this_run_minor: int = 0) -> int:
        """Cash free to spend, via the ONE rule in policy.available_cash_minor."""
        bccy = self.budget_currency.upper()
        return available_cash_minor(
            cash_minor=self.balances.get(bccy, 0),
            in_flight_minor=self.in_flight.get(bccy, 0),
            committed_this_run_minor=committed_this_run_minor,
            suspense_minor=self.suspense.get(bccy, 0),
            refunds_owed_minor=self.refunds_owed.get(bccy, 0),
            floor_minor=self.floor_minor)

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
    # WHAT THIS PAYABLE COSTS, in the currency that would actually have to leave.
    # A EUR bill paid with USD costs USD. The planner works this out for the
    # steps it PLANS *and* for the ones it defers, so the dashboard can read the
    # cost from the plan instead of recomputing it — the tile and the plan used
    # to disagree because only the plan knew about the conversion.
    funding_currency: Optional[str] = None
    funding_amount_minor: int = 0
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
            "funding_currency": self.funding_currency,
            "funding_amount_minor": self.funding_amount_minor,
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


def _blocked_reason(*, needed_minor: int, available_after: int, currency_held: int,
                    currency: str, floor_minor: int, forecast, what: str,
                    conversion: bool = False,
                    prefer_inflow: Optional[str] = None,
                    fund_currency: Optional[str] = None) -> tuple[str, str]:
    """(reason, code) for a payment that does not fit, or ("", "") if it does.

    ONE basis: `available_after` already has the floor taken off, and it was
    computed by `available_cash_minor`. This function only decides WHICH
    sentence to say, so the planner and the tiles cannot describe the same
    position two different ways.

    The three outcomes are different problems with different fixes:
      * not enough money in the account at all  -> insufficient_balance
      * fits only once an invoiced payment lands -> waits_for_inflow, named
      * cannot fit and no expected inflow covers it -> reserve_floor
    """
    spends_budget = currency == "USD"
    enough_cash = currency_held >= needed_minor
    if enough_cash and (not spends_budget or available_after >= 0):
        return "", ""

    # How much MORE real cash would have to be here for this to fit. Two ways to
    # be short: the account does not hold the money at all, or it holds it but
    # spending it would breach the floor.
    gap = (needed_minor - max(currency_held, 0)) if not enough_cash else -available_after

    floor_txt = money_str(floor_minor, currency)
    left_txt = money_str(available_after + floor_minor, currency)

    # If money already INVOICED would cover the gap, this is a TIMING problem,
    # not an affordability one, and the invoice is the actionable half. Naming it
    # beats describing the arithmetic. It is still NOT paid early: the rule is
    # defer and say what it waits for.
    if forecast is not None and forecast.total_minor() >= gap:
        names = forecast.funding(gap, prefer=prefer_inflow,
                                 require_currency=fund_currency)
        # ONLY name an invoice when there IS one that could fund this. With no
        # name the sentence fell back to "waits for cash to land", which is not
        # a thing anybody can chase — so the floor arithmetic is said instead.
        if names:
            who = " and ".join(names)
            if enough_cash:
                return (f"waits for {who} to land — paying it now would leave cash "
                        f"{left_txt}, below the {floor_txt} floor"), "waits_for_inflow"
            return (f"waits for {who} to land — the account holds "
                    f"{money_str(max(currency_held, 0), currency)} and this needs "
                    f"{money_str(needed_minor, currency)}"), "waits_for_inflow"

    if not enough_cash:
        return (f"{what}: needs {money_str(needed_minor, currency)} "
                f"but the account holds "
                f"{money_str(max(currency_held, 0), currency)}"), \
               "insufficient_balance"

    return (f"{what}: paying it now would leave cash {left_txt}, below the "
            f"{floor_txt} floor"), "reserve_floor"



def _restate_deferrals(plan, deferred_ctx, *, available, holdings, floor_minor,
                       forecast) -> None:
    """Re-say every deferral against the position the WHOLE run leaves behind.

    A blocked payable is costed at its turn in the loop, before the payments
    that come after it. PO-2002 (EUR, deferred) was told "paying it now would
    leave cash USD 4,904.00" while the very same run was about to send USD 1,800
    to PO-2001 — and 4,904 is not a figure the operator would ever see, because
    both payments are in the plan. The honest number is what is left after
    everything this run will actually do: USD 3,104.00.

    Deferrals consume nothing, so `available()` gives exactly
    final_position - this_payment - floor. `holdings` is the final position,
    so the capacity-versus-floor branch is chosen the same way it would be at
    the end of the run.
    """
    for d in deferred_ctx:
        idx = d["index"]
        if idx >= len(plan.steps):
            continue
        st = plan.steps[idx]
        needed = int(d["needed_minor"] or 0)
        if needed <= 0:
            continue
        f_ccy = d["fund_ccy"] or st.currency
        bs, bc = _blocked_reason(
            needed_minor=needed, available_after=available(needed),
            currency_held=int(holdings.get(f_ccy, 0)),
            currency=f_ccy, floor_minor=floor_minor, forecast=forecast,
            what=d["what"], prefer_inflow=d["prefer"], fund_currency=f_ccy)
        if not bs:
            continue
        # Restate the cash half ONLY. A dependency blocker is not affected by
        # the arithmetic of the rest of the run, so it stays first.
        keep = [b for b in st.blockers if "waits for" not in b
                and "would leave cash" not in b]
        keep.append(bs)
        st.blockers = keep
        st.blocker_codes = [c for c in st.blocker_codes
                            if c in ("dependency_not_met", "no_rate",
                                     "unsupported_currency")] + [bc]
        st.reason = "; ".join(st.blockers)
        st.policy_reason = st.blocker_codes[0] if st.blocker_codes else st.policy_reason


def build_plan(payables: Iterable[Payable], pos: CashPosition,
               budget: Optional[BudgetSettings] = None,
               fx_tolerance_pct: Decimal = Decimal("0.50"),
               chase_shortfalls: dict[str, tuple[int, str]] | None = None) -> Plan:
    """Produce the ordered plan. Pure: same inputs -> same plan, always.

    `payables` may arrive in any order; `prioritise()` decides who is served
    first, so the output ordering is a function of policy, not of the query.
    """
    budget = budget or BudgetSettings()
    plan = Plan()
    deferred_ctx: list[dict] = []
    payables = list(payables)

    bccy = budget.currency.upper()
    floor_minor = int(budget.reserve_floor_usd * minor_for(bccy))

    # Running capacity per currency: ledger cash MINUS what is already in
    # flight MINUS what is unattributed. This is the only capacity number the
    # planner uses, and it is real money.
    #
    # SUSPENSE COMES OFF EVERY CURRENCY, not just the budget one — and it is not
    # enough to subtract it in `_available`, which only looks at the budget
    # currency. A GBP bill paid from suspicious GBP never consults the USD floor
    # (`pay_cost` is 0 when the bill is not in the budget currency), so the
    # capacity it spends from is this dict. Subtracting suspense only in the one
    # place let a GBP supplier be paid from GBP that no invoice had claimed.
    working = dict(pos.balances)
    for ccy, amt in (pos.in_flight or {}).items():
        working[ccy.upper()] = working.get(ccy.upper(), 0) - int(amt)
    for ccy, amt in (pos.suspense or {}).items():
        working[ccy.upper()] = working.get(ccy.upper(), 0) - abs(int(amt))
    # Refunds owed come off EVERY currency for the same reason suspense does:
    # a GBP bill paid from GBP never consults the budget-currency floor.
    for ccy, amt in (pos.refunds_owed or {}).items():
        working[ccy.upper()] = working.get(ccy.upper(), 0) - abs(int(amt))

    def _available(also_spend_minor: int = 0) -> int:
        """THE one available-cash rule, in the budget currency.

        `also_spend_minor` is the step being CONSIDERED, nothing else: everything
        already planned this run is inside `working` (each planned step takes its
        capacity as it is appended), so passing it again here would count the same
        money twice. That mistake made PO-7002 report a USD 11,692.00 hole when
        the real one was USD 1,346.00.

        SUSPENSE IS NOT PASSED HERE EITHER, for exactly the same reason: it is
        already subtracted out of `working` above, for every currency. Passing it
        again double-counted it — a USD 7,000 unattributed deposit flipped
        PO-2001 from PAY to DEFER even though the deposit changed nothing about
        what was spendable.
        """
        return available_cash_minor(
            cash_minor=working.get(bccy, 0),
            committed_this_run_minor=also_spend_minor,
            floor_minor=floor_minor)

    # ---- chase steps first: they recover cash, they never spend it --------
    for inv_number, entry in (chase_shortfalls or {}).items():
        # (shortfall_minor, currency) — the currency is the INVOICE's, so a GBP
        # gap is never reported as a USD one.
        shortfall, chase_ccy = int(entry[0]), str(entry[1])
        if shortfall <= 0:
            continue
        plan.steps.append(Step(
            action=ACT_CHASE,
            reason=(f"{inv_number} is {money_str(shortfall, chase_ccy)} short; "
                    f"chasing before we rely on that cash"),
            payable_number=inv_number,
            amount_minor=shortfall,
            currency=chase_ccy,
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
        # Everything in this block is a WHAT-IF: it computes what the position
        # would say without touching `working`, because a blocked payable must
        # leave the remaining capacity for the next one.
        #
        # ONLY when the payable is in a DIFFERENT currency from the budget. A
        # USD bill paid out of a USD budget needs no conversion, and modelling
        # one inflated the requirement by the FX drift headroom — a USD 1,800
        # bill reported as needing USD 1,809.00 — and said the same thing twice,
        # once with each number. Not holding enough of the budget currency is a
        # CAPACITY problem, and step 5 below states it correctly.
        # WHAT THIS COSTS, in the currency that would actually leave. Worked out
        # here for EVERY payable — planned or blocked — so the dashboard reads
        # the cost off the plan instead of recomputing it. That recomputation is
        # why the tile said "no conversion is planned" while the plan had a
        # USD 10,346 figure for the same bill.
        fund_ccy: Optional[str] = bccy
        fund_amt: int = move_minor
        if p.currency.upper() != bccy and held < move_minor:
            _r = pos.usd_rate(p.currency)
            fund_amt = (_fx_sell_needed(move_minor, p.currency, _r,
                                        fx_tolerance_pct) if _r else 0)

        fx_plan = None            # (sell_minor, rate)
        if p.currency.upper() != bccy and held < move_minor:
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
            bs, bc = _blocked_reason(
                needed_minor=sell_minor, available_after=_available(sell_minor),
                currency_held=working.get(bccy, 0), currency=bccy,
                floor_minor=floor_minor, forecast=pos.forecast,
                what=f"buy {money_str(move_minor, p.currency)} for {p.number}",
                conversion=True, prefer_inflow=p.depends_on_receivable,
                fund_currency=bccy)
            if bs:
                blockers.append(bs)
                codes.append(bc)
            else:
                fx_plan = (sell_minor, rate)

            if not blockers and fx_plan is not None:
                # Actually plan the conversion and take the capacity.
                sell_minor, rate = fx_plan
                fx_step = Step(
                    action=ACT_FX,
                    reason=(f"buy the {money_str(move_minor, p.currency)} "
                            f"that {p.number} needs{fee_txt}, "
                            f"including {fx_tolerance_pct}% drift tolerance"),
                    sell_currency=bccy, sell_amount_minor=sell_minor,
                    buy_currency=p.currency.upper(), buy_amount_minor=move_minor,
                    rate=rate,
                    payable_number=p.number,
                    policy_reason="fund_payable",
                )
                fx_index = len(plan.steps)
                plan.steps.append(fx_step)
                working[bccy] = working.get(bccy, 0) - sell_minor
                working[p.currency.upper()] = held + move_minor

        # --- 5. can we afford the payout itself? (what-if when blocked) ----
        # The cost to the budget currency is what actually leaves it: a payout in
        # a currency we already hold does not touch USD, so it does not consume
        # the USD budget.
        pay_cost = move_minor if p.currency.upper() == bccy else 0
        bs, bc = _blocked_reason(
            needed_minor=pay_cost,
            available_after=_available(pay_cost),
            currency_held=working.get(p.currency.upper(), 0),
            currency=p.currency.upper(), floor_minor=floor_minor,
            forecast=pos.forecast,
            what=f"pay {p.number} ({money_str(move_minor, p.currency)})",
            prefer_inflow=p.depends_on_receivable, fund_currency=bccy)
        if bs:
            blockers.append(bs)
            codes.append(bc)

        # --- 6. anything blocking? one step, every reason ------------------
        if blockers:
            plan.steps.append(Step(
                action=ACT_DEFER,
                payable_number=p.number, supplier=p.supplier,
                amount_minor=move_minor, currency=p.currency,
                payout_method=p.payout_method,
                # A step can collect the same sentence twice (the capacity gate
                # and the floor gate can both fire on one number). Repeating it
                # in the reason reads as two separate problems when there is one;
                # keep the order, drop the duplicates.
                reason="; ".join(dict.fromkeys(blockers)),
                policy_reason=codes[0],
                blockers=list(dict.fromkeys(blockers)),
                blocker_codes=codes,
                chained_to=fx_index,
                funding_currency=fund_ccy,
                funding_amount_minor=fund_amt,
            ))
            # Remember how to re-state this reason once the WHOLE run is
            # planned. A deferral is costed before the payments that follow it
            # in the loop, so "paying it now would leave USD 4,904.00" ignored
            # the USD 1,800 the same run was about to send.
            deferred_ctx.append({
                "index": len(plan.steps) - 1,
                "needed_minor": (fund_amt if fund_ccy == bccy else pay_cost),
                "fund_ccy": fund_ccy,
                "prefer": p.depends_on_receivable,
                "what": (f"buy {money_str(move_minor, p.currency)} for {p.number}"
                         if fund_ccy == bccy and p.currency.upper() != bccy
                         else f"pay {p.number} ({money_str(move_minor, p.currency)})"),
                "codes": list(codes),
            })
            continue

        # --- 7. approval threshold ---------------------------------------
        needs_approval, why = requires_approval(
            move_minor, p.currency, pos.rates,
            auto_limit_usd=budget.auto_pay_limit_usd)

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
            funding_currency=fund_ccy,
            funding_amount_minor=fund_amt,
        ))

    _restate_deferrals(plan, deferred_ctx, available=_available,
                       holdings=working, floor_minor=floor_minor,
                       forecast=pos.forecast)

    return plan