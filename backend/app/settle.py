"""Drive settlement from Airwallex: read each object's state, post what it means.

Split deliberately from `domain/settlement.py`:

- `domain/settlement.py` DECIDES and POSTS. It is pure posting logic taking a
  transfer status or a conversion body. No network, no HTTP client, fully
  unit-testable.
- this module TALKS TO AIRWALLEX and hands the results to that logic.

Keeping them apart is what lets the tests drive settlement with a fake
conversion body and a status string, while the live path and the webhook path
share exactly the same posting code.

Idempotent by construction: the posting functions key every transaction to the
Airwallex object id, so running this sweep twice — or a webhook arriving after a
sweep — posts nothing the second time.
"""
from __future__ import annotations

import logging
from typing import Optional

from .domain.settlement import (
    SettlementOutcome, settle_fx_step, settle_payout_step, settled_txn_keys,
)
from .models import PlanStep, StepStatus, StepType

log = logging.getLogger("closebook.settle")


def _steps_with_objects(db_session) -> list[PlanStep]:
    return (db_session.query(PlanStep)
            .filter(PlanStep.airwallex_object_id.isnot(None))
            .order_by(PlanStep.id.asc()).all())


def settle_transfer(db_session, transfer_id: str, status: str,
                    amount_minor: Optional[int] = None) -> list[SettlementOutcome]:
    """Settle one transfer by id, from a status we already have.

    The webhook path uses this: the receiver has already verified the signature
    and looked the transfer up on Airwallex, so it passes the status in rather
    than trusting the payload. A `failed` webhook on a still-PROCESSING transfer
    therefore posts nothing, because PROCESSING is not a settled state.
    """
    out: list[SettlementOutcome] = []
    rows = (db_session.query(PlanStep)
            .filter(PlanStep.airwallex_object_id == transfer_id)
            .order_by(PlanStep.id.desc()).all())
    if not rows:
        log.info("settle: no plan step holds transfer %s", transfer_id)
        return out
    for row in rows:
        if row.type != StepType.TRANSFER:
            continue
        # Only the rows that actually name a payable move money.
        res = settle_payout_step(db_session, row, status,
                                 amount_minor=amount_minor)
        _apply_step_status(row, status)
        out.append(res)
    db_session.flush()
    return out


def settle_all(db, *, fx=None, transfers=None) -> list[SettlementOutcome]:
    """Sweep every planned FX and payout and settle what Airwallex says is done.

    Anything already in the ledger is skipped before a network call is made, so
    a settled book costs nothing to re-check.
    """
    from .airwallex.client import AirwallexClient

    client = None
    outcomes: list[SettlementOutcome] = []
    s = db.session()
    already = settled_txn_keys(s)

    def _fx():
        nonlocal fx
        if fx is None:
            from .airwallex.fx import FX
            fx = FX(_client())
        return fx

    def _transfers():
        nonlocal transfers
        if transfers is None:
            from .airwallex.transfers import Transfers
            transfers = Transfers(_client())
        return transfers

    def _client():
        nonlocal client
        if client is None:
            client = AirwallexClient()
        return client

    for row in _steps_with_objects(s):
        oid = row.airwallex_object_id
        if not oid:
            continue
        try:
            if row.type == StepType.FX:
                if oid in already:
                    continue
                body = _fx().conversion_of(oid)
                res = settle_fx_step(s, row, body)
                outcomes.append(res)
                if res.posted:
                    row.status = StepStatus.PAID
            elif row.type == StepType.TRANSFER:
                if oid in already:
                    continue
                status = _transfers().status_of(oid)
                res = settle_payout_step(s, row, status or "")
                _apply_step_status(row, status or "")
                outcomes.append(res)
        except Exception as exc:      # a sandbox wobble must not kill the sweep
            log.warning("settle: %s %s failed: %s", row.type, oid, exc)
            outcomes.append(SettlementOutcome(
                oid, "conversion" if row.type == StepType.FX else "payout",
                False, f"could not be checked: {type(exc).__name__}"))
    s.commit()
    return outcomes


def _apply_step_status(row: PlanStep, status: str) -> None:
    """Reflect the bank's answer on the step, but only a terminal one.

    A step left SUBMITTED means "we sent it and may never have found out" — it
    is unresolved, not done, and must not be overwritten with a guess.
    """
    from .domain.settlement import TRANSFER_FAILED, TRANSFER_SETTLED
    st = (status or "").upper()
    if st in TRANSFER_SETTLED:
        row.status = StepStatus.PAID
    elif st in TRANSFER_FAILED:
        row.status = StepStatus.CANCELLED


__all__ = ["settle_all", "settle_transfer"]
