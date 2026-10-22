"""Persist a plan as PlanStep rows — the durable identity of a payment.

This module exists because of a bug found by RUNNING the demo twice. The live
script created a fresh `plan_step` on every invocation, so the executor's
`ALREADY_DONE` guard had nothing to recognise and a second real USD 1,800
transfer was created against the sandbox.

The rule: **a payable's plan step is an identity, not a log line.** Persisting
the same plan twice must converge on the SAME row rather than appending a new
one, and a payable that already holds a live transfer must not be re-planned at
all. A payment you can accidentally ask for twice is not protected by an
idempotency key — the key only helps if the same key is actually reused.
"""
from __future__ import annotations

import json
import uuid
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from ..models import PlanStep, StepStatus, StepType
from .planner import ACT_FX, ACT_PAY, Step

#: A step in one of these states is DEAD — its Airwallex object will never
#: settle. Kept here, next to `find_step`, because that is the only thing that
#: reads it, and a second copy elsewhere would drift.
RETIRED_STATUSES = frozenset({StepStatus.FAILED, StepStatus.CANCELLED})

_TYPE_FOR_ACTION = {ACT_PAY: StepType.TRANSFER, ACT_FX: StepType.FX}


def _payload_for(step: Step) -> str:
    """Serialise the identity-bearing fields of a plan step.

    Deliberately NOT the whole Step: the reason text is prose and changes
    between runs, and including it would make the same payment look like a
    different payment to the lookup.
    """
    return json.dumps({
        "payable_number": step.payable_number,
        "action": step.action,
        "amount_minor": step.amount_minor,
        "currency": step.currency,
        "supplier": step.supplier,
        "sell_currency": step.sell_currency,
        "sell_amount_minor": step.sell_amount_minor,
        "buy_currency": step.buy_currency,
    }, sort_keys=True, separators=(",", ":"))


def _matches(row: PlanStep, step: Step) -> bool:
    if not row.payload_json:
        return False
    try:
        payload = json.loads(row.payload_json)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    # IDENTITY IS (payable_number, action) — never payable_number alone.
    # A payable legitimately has BOTH an FX step and a transfer step; keying on
    # the payable alone makes them collide, so a paid FX conversion's
    # airwallex_object_id gets adopted by the transfer and the payout silently
    # never happens. That bug shipped and was caught only by running the real
    # EUR payout end to end.
    return (payload.get("payable_number") == step.payable_number
            and payload.get("action") == step.action)


def find_step(db: Session, step: Step) -> Optional[PlanStep]:
    """The CURRENT attempt at this (payable, action), if there is one.

    A RETIRED row is deliberately not returned. Its transfer will never settle,
    so treating it as the current attempt made the payable permanently
    unplannable: the payment fails once and the supplier can never be paid
    again. Retirement is how a dead attempt stops blocking a new one.
    """
    rows = (db.query(PlanStep).order_by(PlanStep.id.desc()).limit(500).all())
    for row in rows:
        if _matches(row, step) and row.status not in RETIRED_STATUSES:
            return row
    return None


def has_retired_attempt(db: Session, step: Step) -> bool:
    """Whether an earlier attempt at this payment already failed.

    A REPLACEMENT IS A NEW OPERATION, NOT A RETRY: the original request_id is
    spent and the terms were approved for a payment that did not happen. So a
    payable with a dead attempt behind it is never clear to send on its own —
    it needs a human to approve the replacement.
    """
    rows = (db.query(PlanStep).order_by(PlanStep.id.desc()).limit(500).all())
    for row in rows:
        if _matches(row, step) and row.status in RETIRED_STATUSES:
            return True
    return False


def retire_step(db: Session, row: PlanStep, *, reason: str) -> PlanStep:
    """Mark an attempt dead so a replacement can be opened.

    The `airwallex_object_id` is KEPT: it is the record of what was actually
    sent, and clearing it would erase the only link between this book and the
    transfer the bank has. The status is what decides who is the current attempt.
    """
    row.status = StepStatus.CANCELLED
    row.reason = reason
    db.flush()
    return row


def is_already_paid(db: Session, step: Step) -> bool:
    """True when THIS step already has a live Airwallex object.

    The cross-run duplicate guard: even if the plan is rebuilt from scratch, a
    payable already paid is not planned again.

    Identity is (payable_number, action) via find_step — checking only the
    payable would see a completed FX conversion for the same payable and
    conclude the PAYMENT was already made, silently skipping the supplier.
    That bug was live and is why find_step owns this decision.
    """
    row = find_step(db, step)
    return bool(row and row.airwallex_object_id)


def persist_plan(db: Session, run_id: str,
                 steps: Iterable[Step]) -> tuple[list[PlanStep], list[Step]]:
    """Write plan steps to the database, converging on existing rows.

    Returns `(rows, skipped)` where `skipped` holds the steps that were NOT
    written because they already have a live transfer. Nothing is ever written
    twice for the same (payable, action).
    """
    rows: list[PlanStep] = []
    skipped: list[Step] = []
    for step in steps:
        if step.action not in _TYPE_FOR_ACTION:
            continue    # DEFER/CHASE/ESCALATE move no money and need no row
        existing = find_step(db, step)
        if existing is not None:
            if existing.airwallex_object_id:
                skipped.append(step)
                continue
            existing.payload_json = _payload_for(step)
            existing.reason = step.reason
            if existing.status == StepStatus.PROPOSED:
                existing.status = StepStatus.APPROVED
            rows.append(existing)
            continue
        row = PlanStep(
            run_id=run_id,
            type=_TYPE_FOR_ACTION[step.action],
            payload_json=_payload_for(step),
            reason=step.reason,
            status=StepStatus.PROPOSED,
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return rows, skipped


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


__all__ = ["RETIRED_STATUSES", "find_step", "has_retired_attempt",
           "is_already_paid", "new_run_id", "persist_plan", "retire_step"]