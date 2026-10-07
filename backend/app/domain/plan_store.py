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
    # The payable is the identity. A payable has at most one transfer step and
    # at most one FX step per plan, so (payable, action) is sufficient and is
    # stable across runs.
    return (payload.get("payable_number") == step.payable_number
            and payload.get("action") == step.action)


def find_step(db: Session, step: Step) -> Optional[PlanStep]:
    """The existing row for this (payable, action), if there is one."""
    rows = (db.query(PlanStep).order_by(PlanStep.id.desc()).limit(500).all())
    for row in rows:
        if _matches(row, step):
            return row
    return None


def is_already_paid(db: Session, step: Step) -> bool:
    """True when this payable already has a live transfer on Airwallex.

    This is the cross-run duplicate guard: even if the plan is rebuilt from
    scratch, a payable that has already been paid is not planned again.
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


__all__ = ["find_step", "is_already_paid", "new_run_id", "persist_plan"]