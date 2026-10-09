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

SEED_DEPOSITS = [
    # (currency, major) -> both credited via POST /simulation/deposit/create
    (USD, Decimal("40000")),
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
        expect="PO-7003 auto-paid (1800 < 2500 limit). PO-7001 + PO-7002 HELD "
               "(their receivables INV-0041/INV-0042 haven't landed).",
    ),
    DemoBeat(
        "deposit_typo_match",
        inject='deposit USD 12000 reference="inv 0O41 brightline"',
        expect="EXACT match to INV-0041 despite the O/0 typo. Agent converts "
               "EUR 9000 + 12.85 SWIFT fee, releases PO-7001 -> needs approval "
               "(9012.85 > 2500). Approve in UI, then simulate SENT -> PAID.",
    ),
    DemoBeat(
        "partial_payment",
        inject='deposit GBP 4800 reference="Harrow pt1"',
        expect="PARTIAL match to INV-0042 (4800/8000 = 60%). PO-7002 stays "
               "HELD. Agent drafts a chase email for the GBP 3200 shortfall.",
    ),
    DemoBeat(
        "guardrail",
        inject='card USD 7000 limit; authorize 7000.01',
        expect="Policy verdict: the 7,000.01 authorisation is DECLINED by "
               "Closebook's inclusive per-transaction limit (7,000.00 would "
               "pass). NOTE: this is our policy verdict, not a live card "
               "decline — commercial card issuance is blocked for this sandbox "
               "account ('Invalid issuance details'; the same account returns "
               "'Admin permissions required' for /issuing/balances), so no card "
               "exists and no authorisation was simulated against Airwallex.",
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
