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


# ---- committed-cash budget ----------------------------------------------
@dataclass
class CommittedCash:
    """The Closebook-side cash position policy is written against.

    `expected_inflows` is money already matched/expected but not yet landed;
    `earmarked` is cash reserved for a specific known obligation.
    """
    currency: str = "USD"
    expected_inflows_minor: int = 0
    earmarked_minor: int = 0

    def available_minor(self) -> int:
        return max(0, self.expected_inflows_minor - self.earmarked_minor)

    def usd_equivalent_minor(self, rates: Optional[dict[str, Decimal]] = None) -> int:
        """Convert to USD for floor/limit comparisons (see usd_value_minor for
        the rate convention: rates[CCY] = CCY per 1 USD)."""
        if self.currency == "USD":
            return self.available_minor()
        rate = (rates or {}).get(self.currency)
        if not rate:
            # Conservative: treat unknown currency as not spendable for policy.
            return 0
        major = Decimal(self.available_minor()) / Decimal(minor_for(self.currency))
        return int(major / rate * 100)


# ---- loading the committed-cash budget ----------------------------------
_SETTINGS_FILE = Path(__file__).resolve().parents[3] / "settings" / "committed_cash.json"


@dataclass
class BudgetSettings:
    """The committed-cash budget plus the limits policy is written against."""
    committed: CommittedCash
    reserve_floor_usd: Decimal = RESERVE_FLOOR_USD
    auto_pay_limit_usd: Decimal = AUTO_PAY_LIMIT_USD
    fx_tolerance_pct: Decimal = Decimal("0.50")
    approval_expiry_minutes: int = 30


def load_budget(path: str | Path | None = None) -> BudgetSettings:
    """Read settings/committed_cash.json (overridable by $CLOSEBOOK_SETTINGS).

    Falls back to the module defaults when the file is absent, so the library
    stays usable without the repo's settings directory.
    """
    p = Path(path or os.environ.get("CLOSEBOOK_SETTINGS") or _SETTINGS_FILE)
    if not p.exists():
        return BudgetSettings(committed=CommittedCash("USD", 0, 0))
    data = json.loads(p.read_text())
    committed = CommittedCash(
        currency=data.get("currency", "USD"),
        expected_inflows_minor=int(data.get("expected_inflows_minor", 0)),
        earmarked_minor=int(data.get("earmarked_minor", 0)),
    )
    return BudgetSettings(
        committed=committed,
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


def breaches_reserve_floor(available_after_minor: int, currency: str = "USD",
                           floor_usd: Decimal = RESERVE_FLOOR_USD,
                           rates: Optional[dict[str, Decimal]] = None) -> bool:
    """Would this plan push USD-equivalent available cash below the floor?"""
    if currency == "USD":
        return available_after_minor < int(floor_usd * 100)
    return usd_value_minor(available_after_minor, currency, rates) < int(floor_usd * 100)


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