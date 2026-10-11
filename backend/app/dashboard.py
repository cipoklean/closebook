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


def _plain_reason(reason: str) -> str:
    """A one-line plain-English summary of a failure reason.

    The raw Airwallex error is kept and shown behind a Details expander, but the
    visible line must be something a person can act on — a truncated
    "field=reason c" is neither. Never truncate silently: either summarise it or
    show it whole.
    """
    r = (reason or "").strip()
    low = r.lower()
    if "validation_failed" in low or "schema validation" in low:
        return "The bank rejected the request: a required field was missing or invalid."
    if "source_currency" in low:
        return "The bank rejected the request: the currency to debit was missing."
    if "unauthorized" in low or "admin permissions" in low:
        return "Not permitted: this account is not allowed to do that."
    if "insufficient" in low:
        return "Not enough money in that currency."
    if "invalid beneficiary" in low or "beneficiary" in low and "invalid" in low:
        return "The beneficiary details were rejected by the bank."
    if "compliance" in low or "hold" in low:
        return "The payment is on a compliance hold."
    if not r:
        return "It failed. No reason was recorded."
    # First sentence only, capped, and never cut mid-word.
    first = r.split(". ")[0].rstrip(".")
    if len(first) > 120:
        first = first[:117].rsplit(" ", 1)[0] + "…"
    return first[0].upper() + first[1:] if first else r


def _latest_run_id(s) -> str | None:
    """The newest run that actually wrote a step.

    "Current plan" means the latest run, so a cancelled step from three plans
    ago is history rather than something a person must act on now.
    """
    row = (s.query(PlanStep.run_id)
           .filter(PlanStep.run_id.isnot(None))
           .order_by(PlanStep.id.desc()).first())
    return row[0] if row else None


def _is_current(st: PlanStep, latest_run: str | None = None) -> bool:
    """True when this step belongs to the newest plan run.

    Rows written before `run_id` existed have `None`, and they are the legacy
    CANCELLED rows from the early beneficiary bug. Treating a missing run_id as
    "not current" would hide every historical failure, which is exactly what the
    Problems list is for. Only a DIFFERENT, newer run makes a step historical.
    """
    if latest_run is None:
        return True
    if st.run_id is None:
        return True
    return st.run_id == latest_run


def steps_block(s) -> dict:
    """Plan steps, split into approvable, current problems and past attempts.

    Also the source of truth for a payable's displayed state, so the payables
    table and the plan cannot disagree.

    Only the CURRENT plan's unresolved problems surface as problems. Stale
    CANCELLED rows from earlier plans are kept as history behind a toggle —
    without that split, a banner saying "nothing needs you" sat directly above a
    list of three dead payments, and a payable read as both SUBMITTED and failed.
    """
    steps = s.query(PlanStep).order_by(PlanStep.id.desc()).limit(200).all()
    latest_run = _latest_run_id(s)

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
                "plain_reason": _plain_reason(reason),
                "currency": pl.get("currency"),
                "amount_minor": pl.get("amount_minor"),
                # A step that already reached the bank is history, not a problem
                # needing action: it may still settle.
                "settled": bool(st.airwallex_object_id),
                "current": _is_current(st, latest_run),
            })

    # One entry per payable, but a FAILURE outranks a success: a payable whose
    # newest step succeeded is fine, while one that failed is a problem even if
    # a later row for it exists. Rows arrive newest-first, so the default pick
    # would otherwise be the newest and a failure could be silently dropped.
    _rank = {"FAILED": 0, "CANCELLED": 1}
    by_payable: dict[str, dict] = {}
    for p in problems:
        key = p["payable_number"] or "unknown"
        cur = by_payable.get(key)
        if cur is None:
            by_payable[key] = p
            continue
        if _rank.get(p["status"], 9) < _rank.get(cur["status"], 9):
            by_payable[key] = p

    current_problems: list[dict] = []
    past_attempts: list[dict] = []
    for p in by_payable.values():
        # A SUCCESS is not a past attempt at all. A settled step whose reason is
        # ordinary plan prose ("within the auto-pay limit") is a payment that
        # went through — listing it under "Past attempts" reads as a failure
        # that happened to recover, which is a lie.
        if p["settled"] and p["status"] in ("SUBMITTED", "APPROVED", "PAID"):
            continue
        if p["status"] in ("CANCELLED", "FAILED"):
            # Still-current failures are actionable; superseded ones are history.
            (current_problems if p["current"] else past_attempts).append(p)
        elif p["settled"]:
            past_attempts.append(p)
        # PROPOSED and other non-terminal, non-money states are neither.

    # A payable already shown as a current problem is not repeated in history.
    current_keys = {p["payable_number"] for p in current_problems}
    past_attempts = [p for p in past_attempts
                     if p["payable_number"] not in current_keys]

    def _dedupe(rows: list[dict]) -> list[dict]:
        seen: set[tuple] = set()
        out = []
        for p in rows:
            k = (p["payable_number"], p["status"], (p["reason"] or "")[:120])
            if k in seen:
                continue
            seen.add(k)
            out.append(p)
        return out

    return {
        "approvable": approvable,
        "problems": _dedupe(current_problems),
        "past_attempts": _dedupe(past_attempts),
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
    """Ambiguous/unmatched inflows with the model's proposal AND the reason code
    actually recorded, so 'why is this held?' is answered by the deterministic
    decision rather than recomputed by the view."""
    inflows = [i for i in s.query(Inflow).all()
               if i.match_status in (MatchStatus.AMBIGUOUS, MatchStatus.UNMATCHED)]
    out = []
    for i in inflows:
        trace = s.query(AgentTrace).filter(
            AgentTrace.inputs_json.like(f'%"inflow_id": {i.id}%')
        ).order_by(AgentTrace.id.desc()).all()
        proposed, decided = None, None
        for t in trace:
            outs = _loads(t.outputs_json)
            if t.tool == "decide_match" and decided is None:
                decided = {
                    "reason": outs.get("reason") or t.detail,
                    "band": outs.get("band"),
                    "confidence": outs.get("confidence"),
                    "status": outs.get("status"),
                    "evidence": outs.get("evidence") or {},
                }
            if t.tool == "propose_match":
                proposed = outs.get("model_proposed") or proposed
        out.append({
            "inflow_id": i.id,
            "amount_minor": i.amount_minor,
            "currency": i.currency,
            "payer_name": i.payer_name,
            "reference_raw": i.reference_raw,
            "match_status": i.match_status.value,
            "model_proposed": proposed,
            # What CODE concluded. None when the beat that produced this inflow
            # predates the trace; the UI must then say so rather than invent one.
            "decided": decided,
        })
    return out


def live_transfer_statuses(s) -> dict[str, str]:
    """Ask Airwallex what each referenced transfer actually is RIGHT NOW.

    A `plan_step.status` says what WE did (SUBMITTED = we sent it). It says
    nothing about whether the money moved, and calling a submitted sandbox
    transfer "paid" is a claim about someone's money that no record supports.
    The bank is the only authority, so ask it — and fall back to the stored
    status, clearly labelled, if the call fails.
    """
    rows = (s.query(PlanStep)
            .filter(PlanStep.airwallex_object_id.isnot(None))
            .order_by(PlanStep.id.desc()).all())
    ids = {r.airwallex_object_id: r for r in rows}
    if not ids:
        return {}

    out: dict[str, str] = {}
    try:
        from .airwallex.client import AirwallexClient
        from .airwallex.transfers import Transfers
        transfers = Transfers(AirwallexClient())
    except Exception as exc:  # no credentials, sandbox down
        log.warning("live status lookup unavailable: %s", exc)
        return {tid: "UNKNOWN" for tid in ids}

    for tid in ids:
        try:
            out[tid] = transfers.status_of(tid) or "UNKNOWN"
        except Exception as exc:
            log.warning("status_of failed for %s: %s", tid, exc)
            out[tid] = "UNKNOWN"
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
        live_status = live_transfer_statuses(s)

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
                # What the BANK says, not what we recorded. These differ, and
                # only one of them is a claim about money.
                "live_status": live_status.get(
                    state.get("airwallex_object_id") or ""),
            })

        rep = reconcile(s)

        return {
            "budget": budget,
            "cash": rep.cash_by_currency,
            "suspense": rep.suspense_by_currency,
            "ledger_balanced": rep.balanced,
            "receivables": receivables,
            "payables": payables,
            "live_transfer_statuses": live_status,
            "approvable": steps["approvable"],
            "problems": steps["problems"],
            "past_attempts": steps["past_attempts"],
            "incidents": incidents_block(incidents),
            "agent_trace": trace_block(s),
            "unmatched": unmatched_block(s),
            "allowed_tools": sorted(ALLOWED_TOOLS),
        }
    finally:
        s.close()


__all__ = ["build_context", "budget_block", "incidents_block", "steps_block"]
