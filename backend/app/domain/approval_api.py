"""Human approval over HTTP.

The rule this enforces: **a machine cannot approve its own payment.** The
approval endpoints are gated by a shared demo PIN, and the approval they issue
is the same HMAC-bound object the executor already re-verifies on every path —
so approving in the UI changes nothing about how execution is checked.

The endpoint deliberately does NOT move money. It records a human decision. The
executor still re-checks the bound hash, the amount, the counterparty and the FX
rate at execution time, which is the only place that check counts.
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from ..domain.approvals import (
    APPROVAL_TTL_MIN, ApprovalTerms, bound_hash, issue,
)
from ..models import Approval, ApprovalStatus, PlanStep, StepStatus
from ..timeutils import utcnow

log = logging.getLogger("closebook.approvals")


class ApprovalRefused(Exception):
    """A human-facing refusal. Carries a reason safe to show in the UI."""


def check_pin(pin: str, configured: str) -> bool:
    """Constant-time compare against the demo PIN.

    An open approval endpoint is the whole security model removed: anyone who
    could reach the server could authorise their own payout. If no PIN is
    configured the endpoint is CLOSED, not open.
    """
    if not configured:
        return False
    return hmac.compare_digest(pin or "", configured)


def terms_from_step(step: PlanStep) -> ApprovalTerms:
    """Rebuild the exact terms a step represents.

    Read from the step's own persisted payload so the approval binds to what
    will actually be executed — not to whatever the UI happened to display.
    """
    import json

    try:
        pl = json.loads(step.payload_json or "{}")
    except ValueError:
        pl = {}
    if not isinstance(pl, dict):
        pl = {}
    rate = pl.get("fx_rate")
    return ApprovalTerms(
        step_id=step.id,
        payable_number=pl.get("payable_number") or "",
        amount_minor=int(pl.get("amount_minor") or 0),
        currency=pl.get("currency") or "USD",
        counterparty=pl.get("supplier") or "",
        supplier_id=str(pl.get("supplier_id") or pl.get("supplier") or ""),
        invoice_ids=tuple(pl.get("invoice_ids") or ()),
        fx_quote_id=pl.get("fx_quote_id"),
        fx_rate=Decimal(str(rate)) if rate is not None else None,
        evidence={"source": "dashboard approval", "step_status": step.status.value},
    )


def describe_for_ui(approval: Approval, step: Optional[PlanStep]) -> dict:
    """The terms shown next to the Approve/Reject buttons.

    A human should see exactly what they are agreeing to — including the hash
    prefix, so the UI and the executor are provably talking about the same
    object.
    """
    terms = terms_from_step(step) if step is not None else None
    return {
        "approval_id": approval.id,
        "step_id": approval.plan_step_id,
        "amount_minor": approval.amount_minor,
        "currency": approval.currency,
        "counterparty": approval.counterparty,
        "bound_hash": approval.bound_hash,
        "hash_prefix": approval.bound_hash[:12],
        "status": approval.status.value,
        "expires_at": (approval.decided_at + timedelta(minutes=APPROVAL_TTL_MIN)
                       ).isoformat() if approval.decided_at else None,
        "payable_number": terms.payable_number if terms else None,
        "fx_quote_id": terms.fx_quote_id if terms else None,
        "fx_rate": str(terms.fx_rate) if terms and terms.fx_rate else None,
        "note": ("Approving records a human decision. Execution still re-verifies "
                 "the bound hash, amount, counterparty and FX rate."),
    }


def approve_step(db, step_id: int, *, pin: str, approved_by: str,
                 configured_pin: str) -> dict:
    """Issue an HMAC-bound approval for a plan step."""
    if not check_pin(pin, configured_pin):
        raise ApprovalRefused("wrong PIN: approval not recorded")

    s = db.session()
    step = s.query(PlanStep).filter_by(id=step_id).first()
    if step is None:
        raise ApprovalRefused(f"no plan step {step_id}")
    if step.airwallex_object_id:
        raise ApprovalRefused(
            "this step already has a live Airwallex object: approving it now "
            "would authorise nothing and could confuse the audit trail")

    existing = s.query(Approval).filter_by(plan_step_id=step_id).first()
    if existing and existing.status == ApprovalStatus.APPROVED:
        raise ApprovalRefused("this step is already approved")
    if existing and existing.status == ApprovalStatus.REJECTED:
        # A human said no. Re-approving silently would override a deliberate
        # decision, so the step must be re-planned under a new identity instead.
        raise ApprovalRefused(
            "this step was rejected by a human: re-plan it rather than "
            "re-approving the same terms")

    terms = terms_from_step(step)
    approval = issue(terms, approved_by=approved_by)

    if existing is None:
        row = Approval(
            plan_step_id=step_id,
            bound_hash=bound_hash(terms),
            amount_minor=terms.amount_minor,
            currency=terms.currency,
            counterparty=terms.counterparty,
            status=ApprovalStatus.APPROVED,
            decided_by=approved_by,
            decided_at=utcnow(),
        )
        s.add(row)
    else:
        existing.bound_hash = bound_hash(terms)
        existing.amount_minor = terms.amount_minor
        existing.currency = terms.currency
        existing.counterparty = terms.counterparty
        existing.status = ApprovalStatus.APPROVED
        existing.decided_by = approved_by
        existing.decided_at = utcnow()
        row = existing
    s.commit()

    log.warning("APPROVAL ISSUED step=%s hash=%s by=%s",
                step_id, row.bound_hash[:12], approved_by)
    return describe_for_ui(row, step)


def reject_step(db, step_id: int, *, pin: str, rejected_by: str,
                configured_pin: str, reason: str = "") -> dict:
    """Record a human rejection. Terminal for this step."""
    if not check_pin(pin, configured_pin):
        raise ApprovalRefused("wrong PIN: rejection not recorded")

    s = db.session()
    step = s.query(PlanStep).filter_by(id=step_id).first()
    if step is None:
        raise ApprovalRefused(f"no plan step {step_id}")

    existing = s.query(Approval).filter_by(plan_step_id=step_id).first()
    if existing is None:
        existing = Approval(plan_step_id=step_id, bound_hash="",
                            amount_minor=0, currency="USD", counterparty="")
        s.add(existing)
    existing.status = ApprovalStatus.REJECTED
    existing.decided_by = rejected_by
    existing.decided_at = utcnow()
    s.commit()

    log.warning("APPROVAL REJECTED step=%s by=%s reason=%s",
                step_id, rejected_by, reason or "(none given)")
    out = describe_for_ui(existing, step)
    out["rejected_reason"] = reason
    return out


__all__ = ["ApprovalRefused", "approve_step", "check_pin", "describe_for_ui",
           "reject_step", "terms_from_step"]
