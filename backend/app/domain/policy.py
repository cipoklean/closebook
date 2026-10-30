"""Policy — pure functions, no I/O, no model calls (closebook.md §5.2).

The core rule of the build: **the model proposes, code enforces.** Every
number that can move money is decided here, in deterministic functions that
are unit-testable and never read a prompt.

Virtual committed-cash budget
-----------------------------
The Airwallex sandbox pre-funds ~10,000,000 in every major currency, so a
policy written against the *raw* balance never binds: the reserve floor and
the auto-pay limit would never fire, and the demo would show nothing.

So Closebook runs its own **committed-cash budget**: the cash the business has
*committed* (receivables expected to land + cash already earmarked), tracked
in `settings/committed_cash.json`. The sandbox balance is treated as
*capacity* — the ceiling of what we could move — while policy is written
against the much tighter committed-cash number. This is how a real treasury
works: your bank balance is capacity, your cash position is what's committed.

All amounts are integer MINOR units plus an explicit currency.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

from ..money import CURRENCY_EXPONENT, minor_for, money_str, to_minor

# ---- defaults (overridable via .env / committed_cash.json) --------------
RESERVE_FLOOR_USD = Decimal("5000")       # never plan below this
AUTO_PAY_LIMIT_USD = Decimal("2500")      # above this, a step needs approval
MATCH_CONFIDENCE_AUTO = 0.90              # below this, payable stays HELD
MATCH_CONFIDENCE_ESCALATE = 0.75          # below this, escalate to a human
SWIFT_FLAT_FEE = {                        # flat fee per SWIFT payout
    "EUR": Decimal("12.85"),
}
BANK_FEE_TOLERANCE = Decimal("0.01")      # <= 1% for partial matching
BANK_FEE_ABS_TOLERANCE = Decimal("15")    # ...or <= 15 units, whichever larger
# Above this fraction of an invoice still outstanding, a short payment reads as
# a deliberate part-payment (apply it, chase the rest). Below it but outside
# bank-fee tolerance, the shortfall is genuinely ambiguous -> escalate.
MATERIAL_SHORT_THRESHOLD = Decimal("0.10")


class PolicyError(Exception):
    """A plan step violated policy. Carries a human-readable reason."""


# ---- THE ONE AVAILABLE-CASH RULE ----------------------------------------
def cash_waterfall(*, cash_minor: int, in_flight_minor: int = 0,
                   committed_this_run_minor: int = 0, suspense_minor: int = 0,
                   refunds_owed_minor: int = 0, floor_minor: int = 0) -> dict:
    """The SAME arithmetic as `available_cash_minor`, as ordered steps.

    One basis, two presentations. The tile prints the answer and the waterfall
    draws the working, and they cannot disagree because the working is derived
    here — beside the function it is the working OF — and the last step's
    running total IS `available_cash_minor`'s return value.

    Zero subtractions are kept as steps but flagged `empty`, so the bar can draw
    a labelled run of nothing rather than silently dropping a term the operator
    is looking for. A term that is absent from the picture is a term nobody can
    check.
    """
    terms = [
        ("cash", "cash", int(cash_minor or 0), "start"),
        ("in_flight", "in flight", int(in_flight_minor or 0), "minus"),
        ("promised", "promised", int(committed_this_run_minor or 0), "minus"),
        ("unmatched", "unmatched", int(suspense_minor or 0), "minus"),
        ("refunds_owed", "refunds owed", int(refunds_owed_minor or 0), "minus"),
        ("floor", "safety cushion", int(floor_minor or 0), "minus"),
    ]
    # `running` starts at ZERO and the first step adds the cash, so the running
    # total is literally the sum of the segments DRAWN. Starting it at the cash
    # figure double-counted it, and the closing number disagreed with the tile.
    steps, running = [], 0
    for key, label, minor, kind in terms:
        delta = -minor if kind == "minus" else minor   # cash adds, the rest take away
        running += delta
        steps.append({"key": key, "label": label, "minor": abs(minor),
                      "delta_minor": delta, "kind": kind,
                      "running_minor": running,
                      "empty": minor == 0 and kind != "start",
                      # A step that takes the total below zero is the one to
                      # paint red: the shortfall is THERE, not at the end.
                      "overdrawn": running < 0})
    return {"steps": steps, "start_minor": int(cash_minor or 0),
            "free_minor": running,
            "total_minor": sum(st["delta_minor"] for st in steps)}


def available_cash_minor(*, cash_minor: int, in_flight_minor: int = 0,
                         committed_this_run_minor: int = 0,
                         suspense_minor: int = 0,
                         refunds_owed_minor: int = 0,
                         floor_minor: int = 0) -> int:
    """Cash genuinely free to spend. THE single basis for the whole system.

        available = cash - in flight - promised this run
                    - suspense - refunds owed - floor

    Every term is REAL money. Expected inflows are deliberately ABSENT: money
    that has been invoiced but has not landed cannot pay anybody, and a system
    that lets it fund a payment is a system that bounces transfers.

    SUSPENSE IS SUBTRACTED TOO, and it is real money — that is exactly why. It
    is cash we have received but have NOT attributed to an invoice: a payment
    awaiting a human's approval, or one that matched nothing at all. It sits in
    the bank and the ledger, so counting it as spendable means a supplier can be
    paid with money that may have to be refunded tomorrow, and the refund then
    comes out of cash that is already gone. It is shown separately ("Money we
    couldn't match") so it never silently disappears from the page — the point
    is that it stops being SPENDABLE, not that it stops being visible.

    REFUNDS OWED COMES OFF TOO, and for the mirror-image reason: it is money we
    HAVE, that we have decided belongs to somebody else. It cannot fund a
    supplier payment without the refund bouncing later, out of cash that is by
    then already gone. It is the operator's stated formula:

        balance - in-flight - approved-but-unsent - floor
                - unattributed suspense - refunds owed

    Both the planner's reserve-floor check and the dashboard tiles call this, so
    they cannot drift apart again. They used to be two different answers to "can
    we afford this": the plan approved PO-7003 while the page reported the
    position USD 7,715.00 short, and a judge sees that contradiction in the first
    ten seconds.

    `cash_minor` is the LEDGER balance in the budget currency, never the
    Airwallex sandbox balance — the sandbox pre-funds 10,000,000 in every major
    currency, so a floor measured against it could never bind.
    """
    return (int(cash_minor) - int(in_flight_minor)
            - int(committed_this_run_minor) - int(suspense_minor)
            - int(refunds_owed_minor) - int(floor_minor))


@dataclass
class Forecast:
    """Money invoiced and expected to land. NEVER funds a payment.

    Kept for exactly two jobs: a display line, and naming what a deferred
    payment is waiting for ("waits for INV-0042 to land"). It is not an input to
    any affordability decision — that is `available_cash_minor`.
    """
    currency: str = "USD"
    lines: list[dict] = field(default_factory=list)   # {number, currency, usd_minor}

    def total_minor(self) -> int:
        return sum(int(x.get("usd_minor") or 0) for x in self.lines)

    def funding(self, short_minor: int, prefer: str | None = None,
                require_currency: str | None = None) -> list[str]:
        """Which expected inflows would cover `short_minor`.

        `prefer` is the invoice a payable already DEPENDS on, and it wins when it
        covers the gap on its own. Without it a payment blocked waiting on
        INV-0042 could be told it waits for INV-0041, because the largest open
        invoice comes first — true in arithmetic, useless to the reader, and it
        sends them to chase the wrong customer.

        Only meaningful when `total_minor() >= short_minor`; the caller checks
        that before quoting names, so a payment is never told it waits for an
        invoice that would not be enough either.

        `require_currency` restricts the answer to invoices paid IN the currency
        that would actually have to be spent. A GBP invoice does not unblock a
        USD payment on its own, and naming it sent the reader to chase the wrong
        customer with the wrong currency: PO-2002 needs USD and was told it
        waited for INV-1002 (GBP).
        """
        if short_minor <= 0:
            return []
        if require_currency:
            want = require_currency.upper()
            usable = [line for line in self.lines
                      if str(line.get("currency") or "").upper() == want]
            total = sum(int(line.get("usd_minor") or 0) for line in usable)
            if total < short_minor:
                return []
            lines = usable
        else:
            lines = self.lines
        if prefer:
            for line in lines:
                if str(line.get("number")) == prefer:
                    if int(line.get("usd_minor") or 0) >= short_minor:
                        return [prefer]
                    break
        out: list[str] = []
        acc = 0
        for line in sorted(lines, key=lambda r: -int(r.get("usd_minor") or 0)):
            out.append(str(line.get("number")))
            acc += int(line.get("usd_minor") or 0)
            if acc >= short_minor:
                break
        return out


# ---- loading the limits --------------------------------------------------
_SETTINGS_FILE = Path(__file__).resolve().parents[3] / "settings" / "committed_cash.json"


@dataclass
class BudgetSettings:
    """The limits policy is written against.

    This no longer carries a committed-cash budget. Policy used to run against
    "expected inflows" (USD 40,000 invoiced, not landed), which meant the reserve
    floor was measured against money the business had not received — so the plan
    approved a payment the cash position could not cover. The budget is now the
    LEDGER, through `available_cash_minor`. Expected inflows survive only as a
    `Forecast`.

    The file is still called committed_cash.json for history; the name is
    misleading and the docstring in it says so.
    """
    currency: str = "USD"
    reserve_floor_usd: Decimal = RESERVE_FLOOR_USD
    auto_pay_limit_usd: Decimal = AUTO_PAY_LIMIT_USD
    fx_tolerance_pct: Decimal = Decimal("0.50")
    approval_expiry_minutes: int = 30


def load_budget(path: str | Path | None = None) -> BudgetSettings:
    """Read settings/committed_cash.json (overridable by $CLOSEBOOK_SETTINGS).

    Falls back to the module defaults when the file is absent, so the library
    stays usable without the repo's settings directory.

    `expected_inflows_minor` and `earmarked_minor` are deliberately NOT read:
    they described a policy budget, and a policy budget is what allowed a
    payment to be planned against money that had not arrived.
    """
    p = Path(path or os.environ.get("CLOSEBOOK_SETTINGS") or _SETTINGS_FILE)
    if not p.exists():
        return BudgetSettings()
    data = json.loads(p.read_text())
    return BudgetSettings(
        currency=data.get("currency", "USD"),
        reserve_floor_usd=Decimal(str(data.get("reserve_floor_usd", RESERVE_FLOOR_USD))),
        auto_pay_limit_usd=Decimal(str(data.get("auto_pay_limit_usd", AUTO_PAY_LIMIT_USD))),
        fx_tolerance_pct=Decimal(str(data.get("fx_tolerance_pct", "0.50"))),
        approval_expiry_minutes=int(data.get("approval_expiry_minutes", 30)),
    )


# ---- limits -------------------------------------------------------------
def usd_value_minor(amount_minor: int, currency: str,
                    rates: Optional[dict[str, Decimal]] = None) -> int:
    """USD-equivalent of a (minor, currency) amount.

    RATE CONVENTION (single source of truth for the whole codebase):
        rates[CCY] = units of CCY per 1 USD.
    So EUR 0.8756 means "1 USD buys EUR 0.8756", and the USD value of a EUR
    amount is the EUR amount DIVIDED by the rate. This matches how a conversion
    actually reads (sell USD, buy CCY) and the live-probed Airwallex client_rate
    of 1.138726, which is the inverse (USD per EUR) for USD 9,012.85 ->
    EUR 7,914.85.

    Unknown currency -> 0 (conservative: never treat an unpriced currency as
    spendable)."""
    if currency == "USD":
        return amount_minor
    rate = (rates or {}).get(currency)
    if not rate:
        return 0
    major = Decimal(amount_minor) / Decimal(minor_for(currency))
    return int(major / rate * 100)


def requires_approval(amount_minor: int, currency: str,
                      rates: Optional[dict[str, Decimal]] = None,
                      auto_limit_usd: Decimal = AUTO_PAY_LIMIT_USD,
                      match_confidence: Optional[float] = None) -> tuple[bool, str]:
    """Does this step need a human? Returns (needs_approval, reason).

    Refunds/overpayment returns ALWAYS need approval (spec §5.2).
    """
    if match_confidence is not None and match_confidence < MATCH_CONFIDENCE_AUTO:
        return True, (f"receivable matched at {match_confidence:.2f} confidence, "
                      f"below the {MATCH_CONFIDENCE_AUTO} auto threshold")
    if not supported(currency):
        # Cannot be valued, therefore cannot be shown to be within any limit.
        # "Unpriced" must never read as "cheap".
        return True, f"{currency} is not a supported/priceable currency"
    usd = usd_value_minor(amount_minor, currency, rates)
    if usd == 0 and currency != "USD":
        return True, (f"no FX rate available to value {currency}; "
                      f"refusing to treat an unpriced currency as within limits")
    limit_minor = int(auto_limit_usd * 100)
    if usd > limit_minor:
        return True, (f"{money_str(usd, 'USD')} exceeds the auto-pay limit of "
                      f"{money_str(limit_minor, 'USD')}")
    return False, f"{money_str(usd, 'USD')} is within the auto-pay limit"


def swift_fee_minor(currency: str = "EUR") -> int:
    """Flat per-payout SWIFT fee (0 for LOCAL rails)."""
    fee = SWIFT_FLAT_FEE.get(currency.upper())
    return to_minor(fee, "EUR") if fee is not None else 0


def total_payout_minor(amount_minor: int, currency: str, payout_method: str) -> int:
    """Amount to actually move: the invoice plus any rail fee.

    LOCAL pays no fee. SWIFT adds the flat fee so the *total converted* is
    right — the guide is explicit that the fee must be included in the
    conversion amount, not bolted on afterwards.
    """
    if payout_method.upper() == "SWIFT":
        return amount_minor + swift_fee_minor()
    return amount_minor


def breaches_reserve_floor(available_before_floor_minor: int, currency: str = "USD",
                           floor_usd: Decimal = RESERVE_FLOOR_USD,
                           rates: Optional[dict[str, Decimal]] = None) -> bool:
    """Would this leave the position below the floor?

    Routed through `available_cash_minor`, which OWNS the rule, so this and the
    planner and the tiles are literally the same arithmetic rather than three
    transcriptions of it. `available_before_floor_minor` is the position BEFORE
    the floor is taken off.
    """
    if currency != "USD":
        available_before_floor_minor = usd_value_minor(
            available_before_floor_minor, currency, rates)
    return available_cash_minor(
        cash_minor=available_before_floor_minor,
        floor_minor=int(floor_usd * 100)) < 0


# ---- prioritisation ------------------------------------------------------
@dataclass
class PayablePriority:
    """Sort key for a payable. Higher = pay first."""
    criticality: int          # 1-5, from the invoice
    due_date: Optional[datetime]
    defer_cost_usd_minor: int # estimated cost of deferring
    amount_usd_minor: int

    def key(self) -> tuple:
        # Criticality desc, then earliest due, then costliest-to-defer desc.
        due = self.due_date or datetime.max
        return (-self.criticality, due, -self.defer_cost_usd_minor)


def priority_key(criticality: int, due_date: Optional[datetime],
                 defer_cost_usd_minor: int) -> tuple:
    """THE ordering key for payables. Single source of truth.

    Criticality desc, then earliest due date, then costliest-to-defer desc.
    `prioritise()` uses this, and the planner applies the same key to indices
    so the two can never disagree about who gets served first. Keep it here.
    """
    return (-criticality, due_date or datetime.max, -defer_cost_usd_minor)


def prioritise(items: Iterable[PayablePriority]) -> list[PayablePriority]:
    """Deterministic ordering: criticality, then due date, then defer cost.

    When cash is short this decides who gets paid and who gets deferred —
    the decision must be reproducible, so it lives in code, not in a prompt.
    """
    return sorted(items, key=lambda i: priority_key(
        i.criticality, i.due_date, i.defer_cost_usd_minor))


# ---- matching thresholds -------------------------------------------------
def within_bank_fee_tolerance(expected_minor: int, actual_minor: int) -> bool:
    """Is the shortfall explainable as a bank fee (<=1% or <=15 units)?"""
    diff = abs(actual_minor - expected_minor)
    if diff == 0:
        return True
    pct = Decimal(diff) / Decimal(max(expected_minor, 1))
    return pct <= BANK_FEE_TOLERANCE or diff <= int(
        BANK_FEE_ABS_TOLERANCE * 100)  # 15 units in minor terms


def classify_confidence(confidence: float) -> str:
    if confidence >= MATCH_CONFIDENCE_AUTO:
        return "AUTO"
    if confidence >= MATCH_CONFIDENCE_ESCALATE:
        return "APPROVAL"   # payable stays HELD, needs a human to release
    return "ESCALATE"       # below 0.75 -> escalate with evidence


def supported(currency: str) -> bool:
    return currency.upper() in CURRENCY_EXPONENT