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


class LedgerError(Exception):
    """A review action that cannot be taken, with the reason in plain words."""


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


@dataclass
class _Alloc:
    """Just enough of matching.Allocation for the apply path. Local to avoid
    importing the matcher into the ledger."""
    invoice_id: int
    amount_minor: int


def _allocations_from_proposal(inflow) -> list[_Alloc]:
    """The stored proposal, as it was shown to the human.

    Returns objects with `.invoice_id`/`.amount_minor`, NOT dicts: the apply
    path is shared with the automatic one and reads attributes, so handing it
    dicts raised `'dict' object has no attribute 'invoice_id'` the first time a
    human approved anything.
    """
    if not inflow.proposed_json:
        return []
    try:
        blob = json.loads(inflow.proposed_json)
    except ValueError:
        return []
    out: list[_Alloc] = []
    for a in (blob.get("allocations") or []):
        try:
            out.append(_Alloc(int(a["invoice_id"]), int(a["amount_minor"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _proposal_blob(result) -> str:
    return json.dumps({
        "status": result.status,
        "confidence": float(result.confidence),
        "method": result.method,
        "reason": result.reason,
        "shortfall_minor": int(result.shortfall_minor or 0),
        "credit_minor": int(result.credit_minor or 0),
        "evidence": result.evidence,
        "allocations": [{"invoice_id": a.invoice_id,
                         "amount_minor": a.amount_minor}
                        for a in result.allocations],
    }, default=str)[:4000]


def apply_inflow_to_receivables(db_session, inflow, result) -> list[Match]:
    """Record a match, post the cash, and move each invoice's status.

    `result` is a matching.MatchResult. This is the only place a receivable's
    amount_paid/status changes.

    BELOW THE AUTO BAND NOTHING IS APPLIED. A score under
    `MATCH_CONFIDENCE_AUTO` is a PROPOSAL, not a decision: the invoice does not
    move, no Match row is written, and the whole amount is parked in
    `unapplied_cash`. The proposal is stored verbatim on the inflow so that
    approving it later applies THE NUMBERS THAT WERE SHOWN.

    The previous behaviour applied the allocation anyway and merely *labelled*
    it unconfirmed — which meant a 0.70 guess moved an invoice's paid amount and
    could create a refund obligation. Labelling is not a control.
    """
    from .policy import MATCH_CONFIDENCE_AUTO

    applied_statuses = ("MATCHED", "PARTIAL", "OVERPAID")
    confident = (result.confidence >= MATCH_CONFIDENCE_AUTO
                 and result.status in applied_statuses
                 and bool(result.allocations))
    if not confident:
        return _propose(db_session, inflow, result)
    # PAUSED MEANS NOTHING IS APPLIED ON ITS OWN. A confident match becomes a
    # proposal a person approves, with the same numbers, so pausing loses
    # nothing and resuming needs no replay.
    from .settings_store import is_paused
    if is_paused(db_session):
        return _propose(db_session, inflow, result)

    created = _apply_allocations(db_session, inflow, result.allocations,
                                 confidence=result.confidence,
                                 method=result.method,
                                 status=result.status,
                                 evidence=result.evidence,
                                 credit_minor=int(result.credit_minor or 0))

    # Cash in: debit the cash account, credit the clearing account for what was
    # applied. Anything left over — an unmatchable remainder, or the credit side
    # of an overpayment — goes to suspense so it stays visible and refundable.
    # (The proposal path above posts its own legs; the approval path posts from
    # suspense in `approve_inflow`.)
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


def _propose(db_session, inflow, result) -> list[Match]:
    """Park the cash and store the proposal. Move no invoice."""
    inflow.match_status = (MatchStatus.PROPOSED if result.allocations
                           else MatchStatus.AMBIGUOUS)
    inflow.proposed_json = _proposal_blob(result)
    inflow.review_state = "PENDING"
    legs = [("cash:" + inflow.currency, inflow.amount_minor, 0, inflow.currency),
            ("unapplied_cash", 0, inflow.amount_minor, inflow.currency)]
    post(db_session, txn_id=f"inflow:{inflow.id}", legs=legs,
         ref=f"proposal {result.status} ({result.confidence:.2f})")
    db_session.flush()
    return []


def _apply_allocations(db_session, inflow, allocations, *, confidence, method,
                       status, evidence, credit_minor: int = 0) -> list[Match]:
    """Move the cash onto the invoices. Shared by the auto path and approval,
    so an approved proposal lands exactly where an automatic one would.

    `credit_minor` is money received AGAINST an invoice beyond what it owed. It
    is attached to the invoice so the invoice reads OVERPAID and the credit is
    visible, while the cash itself stays in `unapplied_cash` for a refund or a
    carry-forward.
    """
    created: list[Match] = []
    # Amounts already applied to the SAME invoice inside THIS proposal, so two
    # allocations to one invoice are checked against what is left after the
    # first — not against the invoice's original balance twice.
    already: dict[int, int] = {}
    for alloc in allocations:
        inv = db_session.query(InvoiceReceivable).filter_by(
            id=alloc.invoice_id).first()
        if inv is None:
            continue
        # AN INVOICE CANNOT BE PAID TWICE. Three proposals can each name the same
        # invoice at its full value; applying the second one anyway pushed it to
        # 8,800 paid on a 4,400 bill, invented an OVERPAID credit, and left two
        # invoices' worth of cash parked against one invoice. The open balance is
        # re-read HERE, at the write path, because a proposal is a snapshot: it
        # was true when the money arrived and may not be true when a human gets
        # round to approving it.
        open_minor = inv.amount_minor - inv.amount_paid_minor
        open_minor -= already.get(inv.id, 0)
        if alloc.amount_minor > open_minor:
            from ..money import money_str
            raise LedgerError(
                f"{inv.number} has only {money_str(max(0, open_minor), inv.currency)} "
                f"left open. Applying {money_str(alloc.amount_minor, inv.currency)} "
                f"would overpay it. Record this one as a refund owed, or match it "
                f"to a different invoice.")
        already[inv.id] = already.get(inv.id, 0) + alloc.amount_minor
        inv.amount_paid_minor += alloc.amount_minor
        if inv.amount_paid_minor < inv.amount_minor:
            inv.status = RecStatus.PARTIAL
        elif inv.amount_paid_minor == inv.amount_minor:
            inv.status = RecStatus.PAID
        else:
            inv.status = RecStatus.OVERPAID
        # A CREDIT IS NOT A PAYMENT OF THIS INVOICE. This added the overpayment
        # to the invoice's paid amount, so a USD 1,200.00 invoice paid by a
        # USD 120,000.00 deposit read as having had the WHOLE 120,000.00 applied
        # to it: run 9's confirmation said "USD 1,200.00 has been applied" and
        # Setup showed 120,000.00 paid on the same invoice, a 100x money bug, on
        # every currency and on the automatic, manual and PIN paths alike.
        #
        # amount_paid_minor answers ONE question -- how much of this invoice has
        # been paid -- and the ledger's clearing leg answers the same question in
        # the same unit. The credit stays where it already is: the cash sits in
        # `unapplied_cash`, the inflow reports it as still unresolved, and the
        # Match carries it, so the invoice is not the place it gets stored.
        credit_to_note = 0
        if credit_minor:
            inv.status = RecStatus.OVERPAID
            credit_to_note = credit_minor
            credit_minor = 0

        m = Match(
            inflow_id=inflow.id, invoice_receivable_id=inv.id,
            amount_applied_minor=alloc.amount_minor,
            confidence=confidence,
            method=(MatchMethod[method] if method in MatchMethod.__members__
                    else MatchMethod.RULE),
            evidence_json=json.dumps(
                {**(evidence or {}), "credit_minor": credit_to_note},
                default=str)[:2000],
        )
        db_session.add(m)
        created.append(m)

    inflow.match_status = (MatchStatus[status] if status in MatchStatus.__members__
                           else MatchStatus.AMBIGUOUS)
    db_session.flush()
    return created


def approve_inflow(db_session, inflow, *, approved_by: str = "dashboard") -> list[Match]:
    """Apply a stored PROPOSAL. This is where a below-band match becomes real.

    Posts from suspense to the invoice, so the net effect on the books is
    identical to an automatic apply: cash debited once, receivable_clearing
    credited for what was allocated.
    """
    if inflow.review_state == "APPROVED":
        raise LedgerError(f"inflow {inflow.id} was already approved")
    if inflow.review_state == "REJECTED":
        raise LedgerError(f"inflow {inflow.id} was rejected; re-match it "
                          f"rather than approving an old proposal")
    allocs = _allocations_from_proposal(inflow)
    if not allocs:
        raise LedgerError(f"inflow {inflow.id} has no proposal to approve")

    try:
        blob = json.loads(inflow.proposed_json or "{}")
    except ValueError:
        blob = {}
    created = _apply_allocations(
        db_session, inflow, allocs,
        confidence=float(blob.get("confidence") or 0.0),
        method=str(blob.get("method") or "RULE"),
        status=str(blob.get("status") or "MATCHED"),
        evidence=dict(blob.get("evidence") or {},
                      approved_by=approved_by, approved_from_proposal=True),
        credit_minor=int(blob.get("credit_minor") or 0))

    allocated = sum(a.amount_minor for a in allocs)
    if allocated:
        from ..timeutils import utcnow
        post(db_session, txn_id=f"inflow:{inflow.id}:approved",
             legs=[("unapplied_cash", allocated, 0, inflow.currency),
                   ("receivable_clearing", 0, allocated, inflow.currency)],
             ref=f"approved by {approved_by}")
        inflow.reviewed_at = utcnow()
    inflow.review_state = "APPROVED"
    inflow.reviewed_by = approved_by
    db_session.flush()
    return created


def reject_inflow(db_session, inflow, *, rejected_by: str = "dashboard",
                  reason: str = "") -> None:
    """Say no. The cash STAYS in suspense — it is real money we received."""
    from ..timeutils import utcnow

    inflow.review_state = "REJECTED"
    inflow.reviewed_by = rejected_by
    inflow.reviewed_at = utcnow()
    inflow.match_reason = (reason or "rejected; the cash stays in suspense "
                                    "until it is matched or refunded")
    db_session.flush()


# ---- resolving suspense: the only three ways it can leave ----------------
#: Money we received and do NOT owe to our own invoices. It is in the bank and
#: it is not spendable: a refund is a liability, not cash.
REFUNDS_OWED = "refunds_owed"
#: Money that turned out to be ours to keep. Releasing to here is the ONLY way
#: suspense becomes spendable, and it needs a note because "unexplained money
#: became income" is the entry an auditor asks about.
OTHER_INCOME = "other_income"

#: A resolution already taken. Suffix states — the inflow itself is unchanged.
RESOLVED_STATES = ("REFUND_OWED", "RELEASED_INCOME")


def suspense_for(db_session, inflow) -> int:
    """How much of THIS inflow is still sitting in suspense.

    Read off the LEDGER, not off the inflow's amount: an inflow that was part
    allocated leaves only its remainder in suspense, and resolving the whole
    amount would invent money we already applied to an invoice.
    """
    from ..models import LedgerEntry

    prefix = f"inflow:{inflow.id}"
    total = 0
    for e in db_session.query(LedgerEntry).all():
        if e.account != "unapplied_cash":
            continue
        t = e.txn_id or ""
        if t == prefix or t.startswith(prefix + ":"):
            # A CREDIT to unapplied_cash is money we are holding unapplied.
            total += e.credit_minor - e.debit_minor
    return total


def refunded_for(db_session, inflow) -> int:
    """How much of THIS inflow has become a refund obligation.

    Read off the LEDGER for the same reason `suspense_for` is: after a refund is
    recorded the cash has moved from `unapplied_cash` to `refunds_owed`, so an
    inflow can hold GBP 100.00 that a person is still waiting on while sitting in
    zero suspense. Reading only the suspense made the unmatched-money tile say
    "Nothing waiting" directly beside "1 payment waiting on you" (run 9).
    """
    from ..models import LedgerEntry

    prefix = f"inflow:{inflow.id}"
    total = 0
    for e in db_session.query(LedgerEntry).all():
        if e.account != REFUNDS_OWED:
            continue
        t = e.txn_id or ""
        if t == prefix or t.startswith(prefix + ":"):
            total += e.credit_minor - e.debit_minor
    return total


def waiting_minor_for(db_session, inflow) -> int:
    """EVERY PENNY of this payment a person is still waiting on.

    One formula, because two screens ask this question and they disagreed when
    each did its own arithmetic: suspense is money nobody has decided about, and
    refunds_owed is money we have decided to give back but have not paid. Both
    are waiting on a person, so both count here.
    """
    return suspense_for(db_session, inflow) + refunded_for(db_session, inflow)


def _resolvable_amount(db_session, inflow) -> int:
    """The amount a resolution may act on, or a refusal saying why not."""
    if inflow.review_state in RESOLVED_STATES:
        raise LedgerError(
            f"inflow {inflow.id} was already resolved as {inflow.review_state}. "
            f"Resolving it twice would move the same money twice.")
    amount = suspense_for(db_session, inflow)
    if amount <= 0:
        raise LedgerError(
            f"inflow {inflow.id} has nothing left in suspense, so there is "
            f"nothing to resolve.")
    return amount


def resolve_refund(db_session, inflow, *, reason: str, by: str = "dashboard") -> int:
    """Record a refund obligation. NO TRANSFER IS SENT.

    The cash does not move — it is still in the bank and still in `cash:CCY` —
    so the ledger swaps which LIABILITY it sits against: out of `unapplied_cash`
    and into `refunds_owed`. That is what stops it being spendable without
    pretending the money left, and it is why the refund transfer itself is out
    of scope: this records what we owe, it does not pay it.

    Returns the amount moved, in minor units.
    """
    from ..timeutils import utcnow

    text = (reason or "").strip()
    if not text:
        raise LedgerError(
            "A refund needs a reason, for example 'duplicate payment'. The "
            "reason is the only thing that explains this liability later.")
    amount = _resolvable_amount(db_session, inflow)

    post(db_session, txn_id=f"inflow:{inflow.id}:refund",
         legs=[("unapplied_cash", amount, 0, inflow.currency),
               (REFUNDS_OWED, 0, amount, inflow.currency)],
         ref=f"refund owed, {text} (by {by})")
    inflow.review_state = "REFUND_OWED"
    inflow.resolution_reason = text
    inflow.resolved_by = by
    inflow.resolved_at = utcnow()
    inflow.match_reason = f"refund owed: {text}"
    db_session.flush()
    return amount


def resolve_income(db_session, inflow, *, note: str, by: str = "dashboard") -> int:
    """Release suspense as unallocated income — the only way it becomes spendable.

    Requires a NOTE as well as the acting user, and both are stored on the
    inflow: releasing someone else's money as profit is the decision most likely
    to be questioned, so the row has to say who decided and why.

    Returns the amount released, in minor units.
    """
    from ..timeutils import utcnow

    text = (note or "").strip()
    if not text:
        raise LedgerError(
            "Releasing money as income needs a note. It is the only thing that "
            "explains, later, why cash that arrived against nobody's invoice "
            "became ours to keep.")
    amount = _resolvable_amount(db_session, inflow)

    post(db_session, txn_id=f"inflow:{inflow.id}:income",
         legs=[("unapplied_cash", amount, 0, inflow.currency),
               (OTHER_INCOME, 0, amount, inflow.currency)],
         ref=f"released as unallocated income, {text} (by {by})")
    inflow.review_state = "RELEASED_INCOME"
    inflow.resolution_reason = text
    inflow.resolution_note = text
    inflow.resolved_by = by
    inflow.resolved_at = utcnow()
    inflow.match_reason = f"released as unallocated income: {text}"
    db_session.flush()
    return amount


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
    #: Money received, not attributed to an invoice, and recorded as owed back.
    #: A LIABILITY, not cash — kept apart from suspense because the two need
    #: different actions: suspense needs a decision, a refund needs paying.
    refunds_owed_by_currency: dict[str, int] = field(default_factory=dict)
    #: Money released as ours to keep. Spendable, and worth showing, because it
    #: is profit that arrived as an unexplained bank credit.
    other_income_by_currency: dict[str, int] = field(default_factory=dict)
    # The outflow side: FX clearing, FX variance and each supplier payable.
    # Reported so the outflow half of the books is visible, not implied.
    outflow_by_account: dict[str, int] = field(default_factory=dict)
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
    refunds: dict[str, int] = {}
    income: dict[str, int] = {}
    outflow: dict[str, int] = {}
    for e in db_session.query(LedgerEntry).all():
        net = e.debit_minor - e.credit_minor
        if e.account.startswith("cash:"):
            # Only real cash accounts count as cash.
            cash.setdefault(e.currency, 0)
            cash[e.currency] += net
        elif e.account == "unapplied_cash":
            suspense.setdefault(e.currency, 0)
            suspense[e.currency] += -net  # a credit is money we hold unapplied
        elif e.account == REFUNDS_OWED:
            # A CREDIT is a liability we have taken on. It is money still in the
            # bank that we must not spend.
            refunds.setdefault(e.currency, 0)
            refunds[e.currency] += -net
        elif e.account == OTHER_INCOME:
            # A CREDIT is money released as ours. Spendable, and shown, because
            # it is profit that arrived as an unexplained bank credit.
            income.setdefault(e.currency, 0)
            income[e.currency] += -net
        elif (e.account in ("fx_clearing", "fx_variance")
              or e.account.startswith("payable:")):
            key = f"{e.account} {e.currency}"
            outflow[key] = outflow.get(key, 0) + net

    # every posting must balance per (txn, currency) — this now covers the
    # outflows too, because they are posted through the same `post()`.
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
    rep.refunds_owed_by_currency = {k: v for k, v in refunds.items() if v != 0}
    rep.other_income_by_currency = {k: v for k, v in income.items() if v != 0}
    rep.outflow_by_account = {k: v for k, v in outflow.items() if v != 0}

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