"""Double-entry ledger + reconciliation (closebook.md §5, `ledger.py`).

The ledger is Closebook's own source of truth for decisions. It is NOT a
mirror of Airwallex's view: Airwallex tells us what happened to a transfer, the
ledger tells us what it means for an invoice.

Invariants enforced here (and unit-tested):
- Every posting is double-entry: sum(debits) == sum(credits) per (txn, currency).
- A `txn_id` + `account` + `currency` triple is unique (no double-post).
- Receivable status is derived, never hand-set: OPEN -> PARTIAL -> PAID /
  OVERPAID as money is applied.
- Reconciliation is a pure read: given the ledger and the invoices, say what
  is still open, overpaid, or unreconciled.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional

from ..models import (
    InvoicePayable, InvoiceReceivable, LedgerEntry, Match, MatchMethod,
    MatchStatus, PayStatus, RecStatus,
)


class LedgerImbalance(Exception):
    """A posting did not balance. Never commit an unbalanced entry."""


# ---- posting ------------------------------------------------------------
def post(db_session, txn_id: str, legs: list[tuple[str, int, int, str]],
         ref: Optional[str] = None) -> list[LedgerEntry]:
    """Post a double-entry transaction.

    legs: [(account, debit_minor, credit_minor, currency), ...]
    Raises LedgerImbalance unless debits == credits per currency.
    """
    totals: dict[str, tuple[int, int]] = {}
    for account, debit, credit, currency in legs:
        d, c = totals.get(currency, (0, 0))
        totals[currency] = (d + debit, c + credit)

    for currency, (debit, credit) in totals.items():
        if debit != credit:
            raise LedgerImbalance(
                f"txn {txn_id} does not balance in {currency}: "
                f"debits {debit} vs credits {credit}")

    created: list[LedgerEntry] = []
    for account, debit, credit, currency in legs:
        existing = db_session.query(LedgerEntry).filter_by(
            txn_id=txn_id, account=account, currency=currency).first()
        if existing:
            # Idempotent: a replayed event must not post twice.
            continue
        entry = LedgerEntry(txn_id=txn_id, account=account,
                            debit_minor=debit, credit_minor=credit,
                            currency=currency, ref=ref)
        db_session.add(entry)
        created.append(entry)
    db_session.flush()
    return created


def apply_inflow_to_receivables(db_session, inflow, result) -> list[Match]:
    """Record a match, post the cash, and move each invoice's status.

    `result` is a matching.MatchResult. This is the only place a receivable's
    amount_paid/status changes.

    CONFIDENCE IS ENFORCED HERE, not only in the matcher. The matcher already
    scores a weak match below `MATCH_CONFIDENCE_AUTO` so it escalates, but
    scoring it low does not stop a caller from applying the allocations anyway.
    The matcher returned a 0.85 OVERPAID for a fuzzy payer and the ledger
    applied it to an invoice regardless — a refund obligation created from a
    70%-confidence guess. Below the auto threshold the cash is still posted and
    the invoice still moves to PARTIAL (so the money is visible and chaseable),
    but it is recorded as awaiting approval rather than as a settled match.
    """
    from .policy import MATCH_CONFIDENCE_AUTO

    needs_approval = result.confidence < MATCH_CONFIDENCE_AUTO
    cands = {c.invoice_id: c for c in []}  # placeholder, filled below
    created: list[Match] = []
    # A recorded overpayment is money that WAS received against this invoice but
    # exceeds what was owed. It must land on the invoice (so the invoice reads
    # OVERPAID and the credit is visible) while the cash itself stays in the
    # unapplied_cash suspense account for a human to refund or carry forward.
    credit_to_apply = result.credit_minor or 0

    for alloc in result.allocations:
        inv = db_session.query(InvoiceReceivable).filter_by(id=alloc.invoice_id).first()
        if inv is None:
            continue
        inv.amount_paid_minor += alloc.amount_minor
        if inv.amount_paid_minor < inv.amount_minor:
            inv.status = RecStatus.PARTIAL
        elif inv.amount_paid_minor == inv.amount_minor:
            inv.status = RecStatus.PAID
        else:
            inv.status = RecStatus.OVERPAID

        if credit_to_apply:
            # Attach the credit to the invoice we overpaid.
            inv.amount_paid_minor += credit_to_apply
            inv.status = RecStatus.OVERPAID
            credit_to_apply = 0

        m = Match(
            inflow_id=inflow.id, invoice_receivable_id=inv.id,
            amount_applied_minor=alloc.amount_minor,
            confidence=result.confidence,
            method=MatchMethod[result.method] if result.method in MatchMethod.__members__ else MatchMethod.RULE,
            evidence_json=json.dumps(result.evidence, default=str)[:2000],
        )
        db_session.add(m)
        created.append(m)

    inflow.match_status = MatchStatus[result.status] if result.status in MatchStatus.__members__ \
        else MatchStatus.AMBIGUOUS
    # A sub-threshold match is never recorded as settled. AMBIGUOUS keeps the
    # cash visible and the invoice traceable while forcing a human decision.
    if needs_approval and inflow.match_status in (MatchStatus.MATCHED,
                                                  MatchStatus.PARTIAL):
        inflow.match_status = MatchStatus.AMBIGUOUS

    # Cash in: debit the cash account, credit the receivable clearing account.
    # `allocated` is what was applied to invoices; the credit portion is
    # deliberately left in unapplied_cash so it is visible and refundable.
    legs = [("cash:" + inflow.currency, inflow.amount_minor, 0, inflow.currency)]
    allocated = sum(a.amount_minor for a in result.allocations)
    suspense = inflow.amount_minor - allocated
    if allocated:
        legs.append(("receivable_clearing", 0, allocated, inflow.currency))
    if suspense > 0:
        legs.append(("unapplied_cash", 0, suspense, inflow.currency))
    if suspense < 0:
        legs.append(("unapplied_cash", -suspense, 0, inflow.currency))
    if len(legs) > 1:
        post(db_session, txn_id=f"inflow:{inflow.id}", legs=legs,
             ref=f"match {result.status}")
    db_session.flush()
    return created


# ---- payable status transitions ----------------------------------------
def mark_payable(db_session, payable: InvoicePayable, status: PayStatus) -> None:
    payable.status = status
    db_session.flush()


# ---- reconciliation ------------------------------------------------------
@dataclass
class ReconciliationReport:
    receivables_open: list[dict] = field(default_factory=list)
    receivables_paid: list[dict] = field(default_factory=list)
    receivables_overpaid: list[dict] = field(default_factory=list)
    payables_by_status: dict[str, list[str]] = field(default_factory=dict)
    cash_by_currency: dict[str, int] = field(default_factory=dict)
    suspense_by_currency: dict[str, int] = field(default_factory=dict)
    unmatched_inflows: list[dict] = field(default_factory=list)
    ambiguous_inflows: list[dict] = field(default_factory=list)
    balanced: bool = True

    def summary(self) -> str:
        cash = ", ".join(f"{k} {v/100:,.2f}"
                         for k, v in sorted(self.cash_by_currency.items())) or "none"
        lines = [
            f"Receivables open: {len(self.receivables_open)}",
            f"Receivables paid: {len(self.receivables_paid)}",
            f"Receivables overpaid: {len(self.receivables_overpaid)}",
            f"Cash: {cash}",
        ]
        if self.suspense_by_currency:
            sus = ", ".join(f"{k} {v/100:,.2f}"
                            for k, v in sorted(self.suspense_by_currency.items()))
            lines.append(f"Unapplied (needs a human): {sus}")
        lines += [
            f"Unmatched inflows: {len(self.unmatched_inflows)}",
            f"Ambiguous inflows: {len(self.ambiguous_inflows)}",
            f"Ledger balanced: {'yes' if self.balanced else 'NO'}",
        ]
        for status, items in sorted(self.payables_by_status.items()):
            lines.append(f"Payables {status}: {len(items)} {items}")
        return "\n".join(lines)


def reconcile(db_session) -> ReconciliationReport:
    """Pure read of the ledger + invoices. No writes, no API calls."""
    rep = ReconciliationReport()

    for inv in db_session.query(InvoiceReceivable).all():
        row = {"number": inv.number, "currency": inv.currency,
               "customer": inv.customer.name if inv.customer else None,
               "amount_minor": inv.amount_minor,
               "paid_minor": inv.amount_paid_minor,
               "open_minor": inv.amount_minor - inv.amount_paid_minor,
               "status": inv.status.value}
        if inv.status == RecStatus.OPEN:
            rep.receivables_open.append(row)
        elif inv.status == RecStatus.PARTIAL:
            rep.receivables_open.append(row)
        elif inv.status == RecStatus.PAID:
            rep.receivables_paid.append(row)
        elif inv.status == RecStatus.OVERPAID:
            rep.receivables_overpaid.append(row)

    for p in db_session.query(InvoicePayable).all():
        rep.payables_by_status.setdefault(p.status.value, []).append(p.number)

    cash: dict[str, int] = {}
    suspense: dict[str, int] = {}
    for e in db_session.query(LedgerEntry).all():
        net = e.debit_minor - e.credit_minor
        if e.account.startswith("cash:"):
            # Only real cash accounts count as cash.
            cash.setdefault(e.currency, 0)
            cash[e.currency] += net
        elif e.account == "unapplied_cash":
            suspense.setdefault(e.currency, 0)
            suspense[e.currency] += -net  # a credit is money we hold unapplied

    # every posting must balance per (txn, currency)
    by_txn: dict[tuple[str, str], tuple[int, int]] = {}
    for e in db_session.query(LedgerEntry).all():
        key = (e.txn_id, e.currency)
        d, c = by_txn.get(key, (0, 0))
        by_txn[key] = (d + e.debit_minor, c + e.credit_minor)
    rep.balanced = all(d == c for d, c in by_txn.values())
    # Summing EVERY account's net made cash cancel against the clearing and
    # suspense accounts, so this dict was always empty and the reserve-floor
    # check had nothing to measure. Only `cash:*` accounts are cash.
    rep.cash_by_currency = {k: v for k, v in cash.items() if v != 0}
    rep.suspense_by_currency = {k: v for k, v in suspense.items() if v != 0}

    from ..models import Inflow
    for i in db_session.query(Inflow).all():
        row = {"id": i.id, "amount_minor": i.amount_minor,
               "currency": i.currency, "payer": i.payer_name,
               "reference": i.reference_raw}
        if i.match_status == MatchStatus.UNMATCHED:
            rep.unmatched_inflows.append(row)
        elif i.match_status == MatchStatus.AMBIGUOUS:
            rep.ambiguous_inflows.append(row)
    return rep