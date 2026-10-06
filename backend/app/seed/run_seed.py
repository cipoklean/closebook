"""`make seed` — persist the demo company into the ledger + (if creds present)
fund it in the sandbox and print balances.

Runs standalone, so a fresh clone can run it before `.env` is filled:
- without creds: seeds the local DB only, prints the plan-of-record.
- with creds: also creates/reuses global accounts, deposits the seed cash,
  snapshots balances, and prints the final state.
"""
from __future__ import annotations

import sys
from decimal import Decimal

from ..config import get_settings
from ..db import DB
from ..money import to_major, to_minor
from ..models import (
    Customer, InvoicePayable, InvoiceReceivable, PayStatus, RecStatus,
    Supplier,
)
from . import scenario


def _seed_local(db: DB) -> None:
    """Write the scenario into the local ledger (idempotent by natural keys)."""
    s = db.session()
    for c in scenario.CUSTOMERS:
        if not s.query(Customer).filter(Customer.name == c.name).first():
            s.add(Customer(name=c.name, email=c.email, country=c.country))
    suppliers = {}
    for p in scenario.SUPPLIERS:
        row = s.query(Supplier).filter(Supplier.name == p.name).first()
        if not row:
            row = Supplier(name=p.name, country=p.country, currency=p.currency,
                           payout_method=p.payout_method, mcc=p.mcc)
            s.add(row)
        suppliers[p.name] = row

    for r in scenario.RECEIVABLES:
        cust = s.query(Customer).filter(Customer.name == r.customer).first()
        if not s.query(InvoiceReceivable).filter(
            InvoiceReceivable.number == r.number).first():
            s.add(InvoiceReceivable(
                number=r.number, customer_id=cust.id, currency=r.currency,
                amount_minor=r.amount_minor,
                amount_paid_minor=0, status=RecStatus.OPEN,
                due_date=r.due,
            ))

    for p in scenario.PAYABLES:
        if not s.query(InvoicePayable).filter(
            InvoicePayable.number == p.number).first():
            dep = None
            if p.depends_on_receivable:
                dep = s.query(InvoiceReceivable).filter(
                    InvoiceReceivable.number == p.depends_on_receivable
                ).first().id
            s.add(InvoicePayable(
                number=p.number, supplier_id=suppliers[p.supplier].id,
                currency=p.currency, amount_minor=p.amount_minor,
                status=PayStatus.HELD, depends_on_receivable_id=dep,
                criticality=p.criticality, due_date=p.due,
            ))
    s.commit()


def _fund_sandbox(db: DB, settings) -> None:
    """Create/reuse global accounts, deposit seed cash, snapshot balances."""
    from ..airwallex.client import AirwallexClient
    from ..airwallex.global_accounts import GlobalAccounts
    from ..airwallex.balances import Balances
    from ..airwallex.simulation import Simulation

    client = AirwallexClient(settings)
    ga = GlobalAccounts(client)
    sim = Simulation(client)

    # Reuse existing global accounts first (created on a prior seed run).
    existing = {
        (b.get("currency") or (b.get("data") or {}).get("currency")):
        b.get("id") or b.get("global_account_id") or b.get("data", {}).get("id")
        for b in ga.list()
    }
    for currency, _major in scenario.SEED_DEPOSITS:
        ga_id: str | None = existing.get(currency)
        if not ga_id:
            country = _country_for_currency(currency)
            created = ga.create(db.session(), country_code=country,
                                currency=currency, transfer_method="LOCAL")
            # Verified live 2026-10-06: global_accounts/create returns a BARE
            # object ({"id": ..., "status": "PROCESSING", ...}), not wrapped in
            # "data". Accept both shapes.
            node = created
            if isinstance(created, dict) and isinstance(created.get("data"), dict):
                node = created["data"]
            if isinstance(node, dict):
                ga_id = node.get("id") or node.get("global_account_id")
            if not ga_id:
                raise RuntimeError(f"could not resolve global account for {currency}: {created}")
        existing[currency] = ga_id

    for currency, major in scenario.SEED_DEPOSITS:
        # A deposit lands in the account's own currency — reuse the account we
        # just provisioned; don't send a currency field (it is discarded).
        sim.deposit(global_account_id=existing[currency],
                    amount_minor=to_minor(major, currency),
                    currency=currency,
                    payer_name="Kano Craft seed funding")
    # Deposits report PENDING but credit immediately — re-read balances.
    balances = Balances(client)
    balances.snapshot_to_db(db.session())
    print(f"Global accounts: {existing}")
    print("Balances (major units):")
    for ccy, minor in _latest_balances(db).items():
        print(f"  {ccy}  {to_major(minor, ccy)}")


def _country_for_currency(currency: str) -> str:
    # EUR uses NL (closebook.md §6: BE is rejected); US, GB for the rest.
    return {"USD": "US", "GBP": "GB", "EUR": "NL"}.get(currency, "US")


def _latest_balances(db: DB) -> dict[str, int]:
    """The most-recent balance snapshot per currency (minor units)."""
    from ..models import BalanceSnapshot
    with db.session() as s:
        rows = s.query(BalanceSnapshot).all()
        latest: dict[str, tuple[object, int]] = {}
        for r in rows:
            prev = latest.get(r.currency)
            if prev is None or (r.fetched_at and r.fetched_at >= prev[0]):
                latest[r.currency] = (r.fetched_at, r.available_minor)
        return {ccy: val[1] for ccy, val in latest.items()}


def main() -> int:
    settings = get_settings()
    db = DB()
    _seed_local(db)
    print("\n".join(scenario.summary_lines()))
    print()

    if not settings.sandbox_ready():
        print("NOTE: .env has no AWX_CLIENT_ID/AWX_API_KEY — seeded local ledger only.")
        print("      Fill .env, then re-run `make seed` to fund the sandbox accounts.")
        return 0

    _fund_sandbox(db, settings)
    print("\nseed complete: ledger + sandbox funded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
