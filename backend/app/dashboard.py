"""Everything the dashboard renders, read once and shaped for the UI.

Separated from `ui.py` so presentation stays free of queries: the page cannot
disagree with the API, because both read this context.

Design rule: this module READS. It never decides anything. A number that
appears on screen was decided by policy/matching/incidents before this ran.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .domain.agent_tools import ALLOWED_TOOLS
from .models import (
    AgentTrace, Approval, ApprovalStatus, Customer, Inflow, InvoicePayable,
    InvoiceReceivable, MatchStatus, PlanStep, StepStatus, Supplier,
)

log = logging.getLogger("closebook.dashboard")


def _loads(text) -> dict:
    try:
        data = json.loads(text or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


# ---------------------------------------------------------------- budget
def budget_block() -> dict:
    from .domain.policy import load_budget
    b = load_budget()
    return {
        "committed_minor": b.committed.available_minor(),
        "reserve_floor_minor": int(b.reserve_floor_usd * 100),
        "auto_pay_limit_minor": int(b.auto_pay_limit_usd * 100),
    }


# ------------------------------------------------------- steps: problems
def _is_approvable(step: PlanStep, payload: dict) -> bool:
    """May a human meaningfully approve this step?

    A step is NOT approvable when it is terminal (it already moved, or it
    failed), or when it names no amount. Both cases were rendering as
    'approvable 0.00 USD' cards — three CANCELLED rows from an early bug looked
    like pending payments. They are shown as problems instead.
    """
    terminal = {
        StepStatus.CANCELLED, StepStatus.FAILED, StepStatus.PAID,
    }
    if step.status in terminal:
        return False
    if step.airwallex_object_id:
        return False
    if not (payload.get("amount_minor") or payload.get("sell_amount_minor")):
        return False
    return True


def steps_block(s) -> dict:
    """Plan steps, split into approvable vs problems.

    Also the source of truth for a payable's displayed state, so the payables
    table and the plan cannot disagree.
    """
    steps = s.query(PlanStep).order_by(PlanStep.id.desc()).limit(60).all()

    payable_state: dict[str, dict] = {}
    approvable: list[dict] = []
    problems: list[dict] = []

    for st in steps:
        pl = _loads(st.payload_json)
        payable = pl.get("payable_number")
        action = st.type.value
        reason = (st.reason or "").strip()

        if payable:
            existing = payable_state.get(payable)
            # Prefer the money-moving step over an FX step when both exist.
            if existing is None or action == "TRANSFER":
                payable_state[payable] = {
                    "status": st.status.value,
                    "reason": reason,
                    "airwallex_object_id": st.airwallex_object_id,
                }

        if _is_approvable(st, pl):
            row = s.query(Approval).filter_by(plan_step_id=st.id).first()
            approvable.append({
                "step_id": st.id,
                "action": action,
                "payable_number": payable,
                "currency": pl.get("currency"),
                "amount_minor": pl.get("amount_minor"),
                "sell_amount_minor": pl.get("sell_amount_minor"),
                "sell_currency": pl.get("sell_currency"),
                "buy_currency": pl.get("buy_currency"),
                "fx_rate": pl.get("fx_rate"),
                "supplier": pl.get("supplier"),
                "status": st.status.value,
                "reason": reason,
                "already_approved": (row.status == ApprovalStatus.APPROVED
                                     if row else False),
                "bound_hash": row.bound_hash if row else None,
                "hash_prefix": (row.bound_hash[:12] if row else None),
            })
        else:
            problems.append({
                "step_id": st.id,
                "payable_number": payable,
                "status": st.status.value,
                "reason": reason,
                "currency": pl.get("currency"),
                "amount_minor": pl.get("amount_minor"),
            })

    # One card per real problem: the three PO-7003 rows share a payable, status
    # and reason, and repeating them made one failure look like three.
    seen: set[tuple] = set()
    unique_problems = []
    for p in problems:
        key = (p["payable_number"], p["status"], (p["reason"] or "")[:120])
        if key in seen:
            continue
        seen.add(key)
        unique_problems.append(p)

    return {
        "approvable": approvable,
        "problems": unique_problems,
        "payable_state": payable_state,
    }


# ------------------------------------------------------------ incidents
def incidents_block(incidents: list | None) -> list[dict]:
    """Group incidents by transfer so one transfer is one card.

    The same transfer legitimately produces several events (failed, then
    processing, then paid). Separate cards made one incident look like three.
    """
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for inc in (incidents or []):
        d = inc.to_dict() if hasattr(inc, "to_dict") else inc
        tid = d.get("transfer_id") or "unknown"
        if tid not in grouped:
            grouped[tid] = {
                "transfer_id": tid,
                "events": [],
                # The NEWEST event wins the header: a transfer that failed and
                # then settled is settled, and showing "failed" would be a lie.
                "recovery": d.get("recovery"),
                "severity": d.get("severity"),
                "live_status": d.get("live_status"),
                "looked_up": d.get("looked_up"),
                "requires_new_request_id": d.get("requires_new_request_id"),
            }
            order.append(tid)
        else:
            grouped[tid].update({
                "recovery": d.get("recovery"),
                "severity": d.get("severity"),
                "live_status": d.get("live_status"),
                "looked_up": d.get("looked_up"),
                "requires_new_request_id": d.get("requires_new_request_id"),
            })
        grouped[tid]["events"].append({
            "event_name": d.get("event_name"),
            "recovery": d.get("recovery"),
            "severity": d.get("severity"),
            "failure_type": d.get("failure_type"),
            "failure_reason": d.get("failure_reason"),
            "summary": d.get("summary"),
            "live_status": d.get("live_status"),
        })
    return [grouped[t] for t in order]


# ------------------------------------------------------------- trace
def trace_block(s) -> list[dict]:
    rows = s.query(AgentTrace).order_by(AgentTrace.id.desc()).limit(40).all()
    return [{
        "tool": t.tool,
        "verdict": t.verdict,
        "inputs": _loads(t.inputs_json),
        "outputs": _loads(t.outputs_json),
        "detail": t.detail,
        "run_id": t.run_id,
    } for t in rows]


# ------------------------------------------------------- unmatched money
def unmatched_block(s) -> list[dict]:
    """Ambiguous/unmatched inflows with the model's proposal attached, so
    'why is this held?' is answerable from the data rather than from prose."""
    inflows = [i for i in s.query(Inflow).all()
               if i.match_status in (MatchStatus.AMBIGUOUS, MatchStatus.UNMATCHED)]
    out = []
    for i in inflows:
        trace = s.query(AgentTrace).filter(
            AgentTrace.inputs_json.like(f'%"inflow_id": {i.id}%')
        ).order_by(AgentTrace.id.desc()).first()
        proposed = _loads(trace.outputs_json).get("model_proposed") if trace else None
        out.append({
            "inflow_id": i.id,
            "amount_minor": i.amount_minor,
            "currency": i.currency,
            "payer_name": i.payer_name,
            "reference_raw": i.reference_raw,
            "match_status": i.match_status.value,
            "model_proposed": proposed,
        })
    return out


def build_context(incidents: list | None = None) -> dict[str, Any]:
    from .db import get_default_db
    from .domain.ledger import reconcile

    s = get_default_db().session()
    try:
        budget = budget_block()

        receivables = []
        for r in s.query(InvoiceReceivable).order_by(InvoiceReceivable.number).all():
            cust = s.query(Customer).filter_by(id=r.customer_id).first()
            receivables.append({
                "number": r.number,
                "customer": cust.name if cust else None,
                "currency": r.currency,
                "amount_minor": r.amount_minor,
                "paid_minor": r.amount_paid_minor,
                "open_minor": max(0, r.amount_minor - r.amount_paid_minor),
                "status": r.status.value,
                "pct": (min(100, round(r.amount_paid_minor / r.amount_minor * 100))
                        if r.amount_minor else 0),
            })

        suppliers = {sup.id: sup for sup in s.query(Supplier).all()}
        steps = steps_block(s)

        payables = []
        for p in s.query(InvoicePayable).order_by(InvoicePayable.number).all():
            sup = suppliers.get(p.supplier_id)
            state = steps["payable_state"].get(p.number, {})
            payables.append({
                "number": p.number,
                "supplier": sup.name if sup else None,
                "currency": p.currency,
                "amount_minor": p.amount_minor,
                "payout_method": sup.payout_method.value if sup else None,
                "criticality": p.criticality,
                # Source of truth is the plan step. The payable row stays HELD
                # until the executor marks it paid, which is why the two tables
                # used to disagree (HELD here, SUBMITTED in the plan).
                "display_status": state.get("status", p.status.value),
                "stored_status": p.status.value,
                "step_reason": state.get("reason"),
                "airwallex_object_id": state.get("airwallex_object_id"),
            })

        rep = reconcile(s)

        return {
            "budget": budget,
            "cash": rep.cash_by_currency,
            "suspense": rep.suspense_by_currency,
            "ledger_balanced": rep.balanced,
            "receivables": receivables,
            "payables": payables,
            "approvable": steps["approvable"],
            "problems": steps["problems"],
            "incidents": incidents_block(incidents),
            "agent_trace": trace_block(s),
            "unmatched": unmatched_block(s),
            "allowed_tools": sorted(ALLOWED_TOOLS),
        }
    finally:
        s.close()


__all__ = ["build_context", "budget_block", "incidents_block", "steps_block"]
