"""The "Kano Craft Imports Ltd" demo scenario (closebook.md §9).

A Lagos importer: paid by customers in USD/GBP, paying suppliers in EUR/USD.
Starting cash: USD 40,000 + GBP 6,000 (both via simulated deposits), with a
USD 5,000 reserve floor.

This module is PURE DATA + determinism: it defines the receivables,
payables, and the 7 scripted demo beats so `run_seed.py` and `run_demo.py`
share one source of truth. No Airwallex, no DB — just the shape.

Amounts are integer MINOR units throughout (see app.money).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from ..money import to_minor

USD = "USD"
GBP = "GBP"
EUR = "EUR"


@dataclass
class SeedCustomer:
    name: str
    email: Optional[str]
    country: str


@dataclass
class SeedSupplier:
    name: str
    country: str
    currency: str
    payout_method: str = "LOCAL"  # LOCAL | SWIFT | CARD
    mcc: Optional[str] = None


@dataclass
class SeedReceivable:
    number: str
    customer: str
    currency: str
    amount_major: Decimal
    due: Optional[date] = None

    @property
    def amount_minor(self) -> int:
        return to_minor(self.amount_major, self.currency)


@dataclass
class SeedPayable:
    number: str
    supplier: str
    currency: str
    amount_major: Decimal
    payout_method: str = "LOCAL"
    criticality: int = 3
    depends_on_receivable: Optional[str] = None
    due: Optional[date] = None

    @property
    def amount_minor(self) -> int:
        return to_minor(self.amount_major, self.currency)


# ---- company + cash -----------------------------------------------------
COMPANY = "Kano Craft Imports Ltd"

# Starting cash the business ALREADY HOLDS. This is real money: the planner
# reads available cash from the LEDGER (see policy.available_cash_minor), so a
# seed that only named a number in a constant would leave the plan unable to pay
# anything. run_seed posts these as opening balances.
#
# USD 9,000 was chosen against the arithmetic, not picked: the floor is USD
# 5,000, so it leaves USD 4,000 of headroom. PO-7003 (USD 1,800) fits with USD
# 2,200 to spare and auto-pays; PO-7001's conversion (about USD 10,346) does NOT
# fit, so PO-7001 waits for INV-0041 to land — which is what beat 2 injects. The
# previous USD 40,000 was never in the ledger at all; it was the EXPECTED-INFLOW
# forecast masquerading as policy, and it let the plan approve payments against
# money the business had not received.
SEED_DEPOSITS = [
    # (currency, major) -> both credited via POST /simulation/deposit/create
    (USD, Decimal("9000")),
    (GBP, Decimal("6000")),
]

RESERVE_FLOOR_USD = Decimal("5000")
AUTO_PAY_LIMIT_USD = Decimal("2500")

# ---- receivables --------------------------------------------------------
RECEIVABLES: list[SeedReceivable] = [
    SeedReceivable("INV-0041", "Brightline Retail", USD, Decimal("12000")),
    SeedReceivable("INV-0042", "Harrow & Co", GBP, Decimal("8000")),
    SeedReceivable("INV-0043", "Lumen Stores", USD, Decimal("4500")),
]

# ---- payables -----------------------------------------------------------
PAYABLES: list[SeedPayable] = [
    # Shipment stops without it (criticality 5). SWIFT adds a flat EUR 12.85
    # fee on top of the 9,000 converted. Unblocks when INV-0041 lands.
    SeedPayable("PO-7001", "Rotterdam Freight BV", EUR, Decimal("9000"),
                payout_method="SWIFT", criticality=5,
                depends_on_receivable="INV-0041"),
    # Card-paid. Depends on INV-0042 (the partial one).
    SeedPayable("PO-7002", "Shenzhen Weave Co", USD, Decimal("7000"),
                payout_method="CARD", criticality=3,
                depends_on_receivable="INV-0042"),
    # Small, no dependency, LOCAL -> pays automatically (under the 2.5k limit).
    SeedPayable("PO-7003", "Atlanta Packaging", USD, Decimal("1800"),
                payout_method="LOCAL", criticality=2),
]

CUSTOMERS = [
    SeedCustomer("Brightline Retail", "ap@brightline.example", "US"),
    SeedCustomer("Harrow & Co", "ar@harrow.example", "GB"),
    SeedCustomer("Lumen Stores", "billing@lumen.example", "US"),
]

SUPPLIERS = [
    SeedSupplier("Rotterdam Freight BV", "NL", EUR, "SWIFT"),
    SeedSupplier("Shenzhen Weave Co", "CN", USD, "CARD", mcc="4722"),
    SeedSupplier("Atlanta Packaging", "US", USD, "LOCAL"),
]


@dataclass
class DemoBeat:
    """One scripted beat of the 5-minute demo. `inject` is the event that
    triggers the agent; `expect` is the deterministic outcome to assert."""
    name: str
    inject: str
    expect: str
    detail: str = ""


# The 7 beats, in the order `make demo` drives them.
BEATS: list[DemoBeat] = [
    DemoBeat(
        "initial_plan",
        inject="manual_run",
        expect="PO-7003 auto-paid (1800 < 2500 limit) out of REAL cash: "
               "USD 9,000 - 1,800 = 7,200, still USD 2,200 above the 5,000 "
               "floor. PO-7001 + PO-7002 HELD, each for two reasons: their "
               "receivables haven't landed AND the cash is not there - the plan "
               "says so and names the invoice it waits for.",
    ),
    DemoBeat(
        "deposit_typo_match",
        inject='deposit USD 12000 reference="inv 0O41 brightline"',
        expect="EXACT match to INV-0041 despite the O/0 typo. The USD 12,000 "
               "lands, so real cash is USD 21,000 and PO-7001 now fits: agent "
               "converts EUR 9000 + 12.85 SWIFT fee, releases PO-7001 -> needs "
               "approval (9012.85 > 2500). Approve in UI, then simulate "
               "SENT -> PAID.",
    ),
    DemoBeat(
        "partial_payment",
        inject='deposit GBP 4800 reference="Harrow pt1"',
        expect="PARTIAL match to INV-0042 (4800/8000 = 60%). PO-7002 stays "
               "HELD. Agent drafts a chase email for the GBP 3200 shortfall.",
    ),
    DemoBeat(
        "guardrail",
        inject="card with the auto-pay limit; authorise inside it, then over it",
        expect="The card itself refuses the amount over the per-transaction "
               "limit and Airwallex returns the reason (LIMIT_EXCEEDED), while "
               "the amount inside the limit is approved by the same card. Both "
               "appear in AI activity as AI suggestion / Rule checks / Result. "
               "The limit is the operator's auto-pay limit, carried on the card, "
               "so the refusal happens at the bank and the money does not move. "
               "Cards are issued from a separate Hong Kong sandbox because the "
               "books' account is registered in Nigeria, which cannot issue; see "
               "docs/AIRWALLEX_CARD_ISSUING_REQUEST.md. Until a live decline is "
               "recorded the beat reports Closebook's own verdict and says so.",
    ),
    DemoBeat(
        "failure_recovery",
        inject="simulate PO-7001 transfer FAILED (bank-return failure_type)",
        expect="Agent records what happened to the ORIGINAL, proposes a "
               "replacement under a duplicate lock with a NEW request_id. "
               "Never auto-pays twice.",
    ),
    DemoBeat(
        "ambiguous_inflow",
        inject='deposit USD 4400 payer="L. Stores"',
        expect="Below 0.75 confidence -> ESCALATE to a human with the "
               "evidence shown side by side.",
    ),
    DemoBeat(
        "close",
        inject="summarise_day",
        expect="Reconciled ledger, final balances still above the USD 5000 "
               "reserve floor, end-of-day report.",
    ),
]


def summary_lines() -> list[str]:
    """Human-readable plan-of-record for a fresh-clone README / judge walk."""
    out = [
        f"Company: {COMPANY}",
        f"Reserve floor: USD {RESERVE_FLOOR_USD}   Auto-pay limit: USD {AUTO_PAY_LIMIT_USD}",
        "Receivables:",
    ]
    for r in RECEIVABLES:
        out.append(f"  {r.number}  {r.customer:<18} {r.currency} {r.amount_major}")
    out.append("Payables:")
    for p in PAYABLES:
        dep = f"  (needs {p.depends_on_receivable})" if p.depends_on_receivable else ""
        out.append(
            f"  {p.number}  {p.supplier:<18} {p.currency} {p.amount_major} "
            f"[{p.payout_method}] crit={p.criticality}{dep}"
        )
    return out
