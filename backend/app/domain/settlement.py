"""Settlement — record money going OUT, exactly once.

The ledger recorded inflows only. Nothing posted when a payout left, so
`cash:USD` never fell, a payable never became PAID, and "Free to spend" treated
a sent payment as still available. This module is the outflow half.

WHERE THE NUMBERS COME FROM
    The conversion's actual amounts are read back from Airwallex
    (`GET /fx/conversions/{id}`), not from the plan. The plan carries a rate
    with 0.50% drift headroom and its numbers are a forecast; booking a forecast
    as an actual would put money in the books that never moved. If the actual
    amounts cannot be read, NOTHING is posted and the reason says so — this
    module never invents a rate.

IDEMPOTENCY
    Every posting is keyed to the Airwallex object id (`fx:<conversion_id>`,
    `pay:<transfer_id>`), and `ledger.post()` is already unique per
    (txn_id, account, currency). A repeat webhook, a repeated poll or a re-run
    of the sweep therefore posts nothing new. This is asserted by test.

ACCOUNTS (all plain strings, the ledger's existing convention)
    cash:<CCY>       real spendable balance. DEBIT = increase  (an asset)
    fx_clearing      the two sides of one conversion, held together: it carries
                     a DEBIT in the sell currency and a CREDIT in the buy
                     currency. A cross-currency clearing account cannot net to
                     zero within one currency — that is what a conversion IS —
                     so it is booked per-currency balanced and reported.
    fx_variance      actual vs planned on a conversion, so the gap is visible
                     rather than silently absorbed.
    payable:<PO-…>   what we owe a supplier (a liability). CREDIT = increase,
                     so paying it DEBITs this account.

SIGN CONVENTION, stated once because it is the thing most easily inverted:
    money IN  -> DEBIT  cash     (see ledger.apply_inflow_to_receivables)
    money OUT -> CREDIT cash
    owe more  -> CREDIT payable:<PO>
    pay it    -> DEBIT  payable:<PO>
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..models import InvoicePayable, LedgerEntry, PayStatus, PlanStep, StepType
from ..timeutils import utcnow
from .ledger import post

log = logging.getLogger("closebook.settlement")

FX_CLEARING = "fx_clearing"
FX_VARIANCE = "fx_variance"


def payable_account(number: str) -> str:
    """The liability account for one payable."""
    return f"payable:{number}"


# A transfer is DONE only when the bank says so. SUBMITTED/PROCESSING are not
# evidence of anything except that we asked.
TRANSFER_SETTLED = {"PAID", "COMPLETED", "SUCCESS", "SETTLED"}
TRANSFER_FAILED = {"CANCELLED", "CANCELED", "FAILED", "RETURNED", "REJECTED",
                   "EXPIRED", "DECLINED"}
CONVERSION_SETTLED = {"SETTLED", "COMPLETED", "SUCCESS", "PAID", "BOOKED"}
# Deliberately NOT "is this terminal" — an unknown status settles nothing, and
# guessing is how a payment gets booked twice.

TXN_FX = "fx:"
TXN_FX_VAR = "fxvar:"
TXN_PAY = "pay:"


@dataclass
class SettlementOutcome:
    """What settlement decided about one Airwallex object."""
    key: str
    kind: str                 # "conversion" | "payout" | "none"
    posted: bool
    reason: str
    txn_ids: list[str] = field(default_factory=list)
    payable_number: Optional[str] = None
    amount_minor: int = 0
    currency: str = "USD"

    def __str__(self) -> str:
        return f"{self.kind} {self.key}: {'posted' if self.posted else 'no posting'}, {self.reason}"


# ------------------------------------------------------------------ variance
def _variance_legs(ccy: str, clearing_signed: int, target_signed: int) -> list:
    """Legs that move `fx_clearing` from where it is to where the plan expected.

    Signed, debit-positive: the sell currency sits as a DEBIT and the buy
    currency as a CREDIT in the clearing account, so the two currencies move in
    opposite directions for the same plan-vs-actual gap. Expressing it as a
    single signed target is what keeps that from being got backwards.
    """
    delta = target_signed - clearing_signed
    if delta > 0:
        return [(FX_CLEARING, delta, 0, ccy), (FX_VARIANCE, 0, delta, ccy)]
    if delta < 0:
        return [(FX_VARIANCE, -delta, 0, ccy), (FX_CLEARING, 0, -delta, ccy)]
    return []


def post_conversion(db_session, *, conversion_id: str, sell_currency: str,
                    sell_minor: int, buy_currency: str, buy_minor: int,
                    planned_sell_minor: Optional[int] = None,
                    planned_buy_minor: Optional[int] = None,
                    when: Optional[datetime] = None) -> SettlementOutcome:
    """Book a completed conversion: sell one currency, buy another.

    Posted as ONE per-currency-balanced transaction against `fx_clearing`. It
    cannot be a single cross-currency balanced entry: `ledger.post()` requires
    debits == credits PER CURRENCY, and that invariant is a safety property with
    its own test (debiting USD while crediting EUR must raise LedgerImbalance).
    A conversion is therefore two balanced sides joined by a clearing account,
    which is how FX clearing is kept in real books.
    """
    if sell_minor <= 0 or buy_minor <= 0:
        return SettlementOutcome(conversion_id, "conversion", False,
                                 "sell and buy amounts must both be positive")
    if sell_currency == buy_currency:
        return SettlementOutcome(conversion_id, "conversion", False,
                                 "sell and buy currency are the same")

    txn = f"{TXN_FX}{conversion_id}"
    legs = [
        (FX_CLEARING, sell_minor, 0, sell_currency),      # what we gave up
        ("cash:" + sell_currency, 0, sell_minor, sell_currency),
        ("cash:" + buy_currency, buy_minor, 0, buy_currency),   # what we got
        (FX_CLEARING, 0, buy_minor, buy_currency),
    ]
    created = post(db_session, txn_id=txn, legs=legs,
                   ref=f"convert {sell_currency}->{buy_currency}")
    if created:
        _stamp(created, when)
    txn_ids = [txn]
    # `created` is empty on a replay. Reporting posted=True here would be a
    # claim that the second webhook moved money it did not move.
    replayed = not created

    # Variance against the plan, in each currency where a plan figure exists.
    var_legs: list = []
    notes: list[str] = []
    if planned_sell_minor is not None and planned_sell_minor != sell_minor:
        var_legs += _variance_legs(sell_currency, +sell_minor, +planned_sell_minor)
        notes.append(f"{sell_currency} planned {planned_sell_minor} "
                     f"actual {sell_minor}")
    if planned_buy_minor is not None and planned_buy_minor != buy_minor:
        var_legs += _variance_legs(buy_currency, -buy_minor, -planned_buy_minor)
        notes.append(f"{buy_currency} planned {planned_buy_minor} "
                     f"actual {buy_minor}")
    if var_legs:
        vtxn = f"{TXN_FX_VAR}{conversion_id}"
        vcreated = post(db_session, txn_id=vtxn, legs=var_legs,
                        ref="fx variance vs plan: " + "; ".join(notes)[:80])
        if vcreated:
            _stamp(vcreated, when)
        txn_ids.append(vtxn)
    else:
        vcreated = []

    if replayed and not vcreated:
        return SettlementOutcome(
            conversion_id, "conversion", False,
            "already posted: the conversion is in the ledger, so this replay "
            "posted nothing", txn_ids=txn_ids)

    reason = (f"booked {sell_currency} sell and {buy_currency} buy at the actual "
              f"amounts Airwallex returned")
    if var_legs:
        reason += f"; variance posted ({'; '.join(notes)})"
    return SettlementOutcome(conversion_id, "conversion", True, reason,
                             txn_ids=txn_ids)


def post_payout(db_session, *, transfer_id: str, payable_number: str,
                currency: str, amount_minor: int,
                when: Optional[datetime] = None) -> SettlementOutcome:
    """Book a settled payout: cash out, liability cleared.

    CREDIT cash (an asset falls) and DEBIT the payable (the liability is
    extinguished). The opposite direction posts money INTO the account when a
    supplier is paid, which is the kind of sign error that survives review
    because both legs still balance.
    """
    if amount_minor <= 0:
        return SettlementOutcome(transfer_id, "payout", False,
                                 "a payout must be a positive amount")
    txn = f"{TXN_PAY}{transfer_id}"
    legs = [
        (payable_account(payable_number), amount_minor, 0, currency),
        ("cash:" + currency, 0, amount_minor, currency),
    ]
    created = post(db_session, txn_id=txn, legs=legs,
                   ref=f"payout {payable_number} {currency}")
    if created:
        _stamp(created, when)

    payable = (db_session.query(InvoicePayable)
               .filter_by(number=payable_number).first())
    marked = False
    if payable is not None and payable.status != PayStatus.PAID:
        payable.status = PayStatus.PAID
        marked = True
    db_session.flush()
    if not created:
        return SettlementOutcome(
            transfer_id, "payout", False,
            "already posted: the payout is in the ledger, so this replay "
            "posted nothing", txn_ids=[txn], payable_number=payable_number,
            amount_minor=amount_minor, currency=currency)
    return SettlementOutcome(
        transfer_id, "payout", True,
        "cash credited and the payable marked PAID" if marked
        else "cash credited; the payable was already PAID",
        txn_ids=[txn], payable_number=payable_number,
        amount_minor=amount_minor, currency=currency)


def _stamp(entries, when: Optional[datetime]) -> None:
    """Set created_at.

    An EXPLICIT `when` always wins. Only filling empty values was wrong: the
    column default fires on INSERT, so by the time `post()` returns, every row
    already carries `utcnow()` and an explicit historical stamp was silently
    discarded — which is why "paid today" could not be tested for a payout
    posted last year.
    """
    for e in entries:
        if when is not None:
            e.created_at = when
        elif getattr(e, "created_at", None) is None:
            e.created_at = utcnow()


# ------------------------------------------------------- step-level settling
def settle_fx_step(db_session, step_row: PlanStep, conversion: dict,
                   when: Optional[datetime] = None) -> SettlementOutcome:
    """Settle an FX plan step from a retrieved conversion body."""
    oid = step_row.airwallex_object_id
    if not oid:
        return SettlementOutcome("", "conversion", False,
                                 "step holds no conversion id")
    status = str(conversion.get("status") or "").upper()
    if status not in CONVERSION_SETTLED:
        return SettlementOutcome(
            oid, "conversion", False,
            f"the conversion is {status or 'in an unknown state'}, not settled")

    try:
        sell_minor = _to_minor(conversion.get("sell_amount"),
                               str(conversion.get("sell_currency") or ""))
        buy_minor = _to_minor(conversion.get("buy_amount"),
                              str(conversion.get("buy_currency") or ""))
    except (TypeError, ValueError):
        return SettlementOutcome(oid, "conversion", False,
                                 "the conversion body has no usable amounts")

    import json
    payload = {}
    try:
        payload = json.loads(step_row.payload_json or "{}")
    except (ValueError, TypeError):
        payload = {}

    return post_conversion(
        db_session,
        conversion_id=oid,
        sell_currency=str(conversion.get("sell_currency")),
        sell_minor=sell_minor,
        buy_currency=str(conversion.get("buy_currency")),
        buy_minor=buy_minor,
        planned_sell_minor=payload.get("sell_amount_minor"),
        planned_buy_minor=payload.get("buy_amount_minor"),
        when=when,
    )


def settle_payout_step(db_session, step_row: PlanStep, status: str,
                       amount_minor: Optional[int] = None,
                       when: Optional[datetime] = None) -> SettlementOutcome:
    """Settle a transfer plan step, or explain why nothing was posted."""
    import json
    oid = step_row.airwallex_object_id
    if not oid:
        return SettlementOutcome("", "payout", False, "step holds no transfer id")

    try:
        payload = json.loads(step_row.payload_json or "{}")
    except (ValueError, TypeError):
        payload = {}
    payable = payload.get("payable_number")
    currency = payload.get("currency") or "USD"
    amount = amount_minor if amount_minor is not None else payload.get("amount_minor")

    st = str(status or "").upper()
    if st in TRANSFER_SETTLED:
        if not payable or not amount:
            return SettlementOutcome(
                oid, "payout", False,
                "settled, but the step does not name a payable and an amount")
        return post_payout(db_session, transfer_id=oid,
                           payable_number=str(payable),
                           currency=str(currency), amount_minor=int(amount),
                           when=when)

    if st in TRANSFER_FAILED:
        # Post NOTHING for the payout. If a conversion funded it, that currency
        # stays as cash in that currency — it is not converted back, and saying
        # it "returned" to USD would invent a second FX event that never
        # happened.
        return SettlementOutcome(
            oid, "payout", False,
            f"the transfer is {st}; no payout is posted and any converted "
            f"currency stays as cash in that currency")

    return SettlementOutcome(
        oid, "payout", False,
        f"the transfer is {st or 'in an unknown state'}; not settled yet")


def _to_minor(amount, currency: str) -> int:
    from ..money import to_minor
    if amount is None:
        raise ValueError("no amount")
    return to_minor(str(amount), currency or "USD")


# ------------------------------------------------------------ ledger queries
def settled_txn_keys(db_session) -> set[str]:
    """Airwallex object ids the ledger has already posted for.

    The authority for "has this settled" is the LEDGER, not the bank's live
    status. A live status can flip to PAID before anything is posted, and if the
    tiles trusted it the free-to-spend figure would jump the moment the bank
    finished while cash had not moved yet.
    """
    keys: set[str] = set()
    for e in db_session.query(LedgerEntry).all():
        t = e.txn_id or ""
        for prefix in (TXN_FX_VAR, TXN_FX, TXN_PAY):
            if t.startswith(prefix):
                keys.add(t[len(prefix):])
                break
    return keys


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalise to timezone-aware UTC.

    The column is declared `DateTime(timezone=True)` but SQLite hands back a
    NAIVE datetime, so comparing a stored value to an aware `since` raises
    "can't compare offset-naive and offset-aware datetimes". Normalising here
    keeps the comparison in one place instead of at every call site.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def paid_payouts(db_session, since: Optional[datetime] = None) -> list[dict]:
    """"Paid today" — settled payouts, read from the ledger.

    Strictly the payout transactions: an FX conversion moved money between our
    own currencies and did not pay anybody, so it does not belong on a list of
    payments made.
    """
    rows: list[dict] = []
    since = _as_utc(since)
    for e in db_session.query(LedgerEntry).all():
        if not (e.txn_id or "").startswith(TXN_PAY):
            continue
        acct = e.account or ""
        if not acct.startswith("payable:"):
            continue          # the payable leg carries the number and amount
        posted = _as_utc(e.created_at)
        if since is not None and posted is not None and posted < since:
            continue
        rows.append({
            "transfer_id": e.txn_id[len(TXN_PAY):],
            "payable_number": acct[len("payable:"):],
            "currency": e.currency,
            "amount_minor": int(e.debit_minor or 0),
            "posted_at": posted,
        })
    rows.sort(key=lambda r: (r["posted_at"] or datetime.min.replace(tzinfo=timezone.utc),
                             r["payable_number"]))
    return rows


def ledger_position(db_session) -> dict:
    """What the outflow accounts hold, for reconcile() and the dashboard."""
    out: dict[str, int] = {}
    for e in db_session.query(LedgerEntry).all():
        net = int(e.debit_minor or 0) - int(e.credit_minor or 0)
        if e.account in (FX_CLEARING, FX_VARIANCE) or (e.account or "").startswith("payable:"):
            key = f"{e.account}:{e.currency}"
            out[key] = out.get(key, 0) + net
    return out


__all__ = [
    "CONVERSION_SETTLED", "FX_CLEARING", "FX_VARIANCE", "SettlementOutcome",
    "TRANSFER_FAILED", "TRANSFER_SETTLED", "ledger_position", "paid_payouts",
    "payable_account", "post_conversion", "post_payout", "settle_fx_step",
    "settle_payout_step", "settled_txn_keys",
]
