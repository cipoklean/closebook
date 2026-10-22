"""Everything the dashboard renders, read once and shaped for the UI.

Separated from `ui.py` so presentation stays free of queries: the page cannot
disagree with the API, because both read this context.

Design rule: this module READS. It never decides anything. A number that
appears on screen was decided by policy/matching/incidents before this ran.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from .domain.agent_tools import ALLOWED_TOOLS
# The ONE in-flight list, shared with incident recovery. A private copy here is
# exactly how a second definition of "still moving" gets out of step — and this
# set is what decides whether a replacement payment may be created.
from .domain.incidents import in_flight as _live_in_flight
from .models import (
    AgentTrace, Approval, ApprovalStatus, Customer, Inflow, InvoicePayable,
    InvoiceReceivable, MatchStatus, PayStatus, PlanStep, StepStatus, StepType,
    Supplier,
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
    """The committed-cash budget, named for what it actually IS.

    `expected_inflows_minor` is money invoiced and expected to LAND — it is not
    cash on hand and it is not money set aside. Every dollar of it is USD. The
    old UI called it "Cash set aside", which read as cash we hold (we hold a
    fraction of it) and as an obligation (it is the opposite — money coming in).
    The names here match the file's own docstring in settings/committed_cash.json.
    """
    from .domain.policy import load_budget
    b = load_budget()
    return {
        "currency": b.committed.currency,
        "expected_inflows_minor": b.committed.expected_inflows_minor,
        "earmarked_minor": b.committed.earmarked_minor,
        "available_minor": b.committed.available_minor(),
        "reserve_floor_minor": int(b.reserve_floor_usd * 100),
        "auto_pay_limit_minor": int(b.auto_pay_limit_usd * 100),
    }


def money_block(s, cash: dict, budget: dict, plan=None,
                payables: Optional[list] = None) -> dict:
    """Cash, what is promised, what is already in flight, and what is free.

    Three rules this encodes, each of which was wrong before:

    1. **An in-flight payment is not still promised.** A payable whose newest
       step is SUBMITTED, or whose bank status is in flight, has been sent — the
       money is committed and on its way. It moves out of "promised" and into
       "in flight". It is counted exactly ONCE: dropping it would understate
       what is committed, and double counting it would overstate it.
    2. **Cost every payable in its FUNDING currency.** A EUR payable bought with
       USD is a USD obligation. Valuing it in EUR produced "Short by EUR
       9,000.00" against a business that was never short of EUR — it was simply
       going to convert. The planned conversion amount (which the planner sized
       WITH drift headroom) is used as-is; no new rate is invented here.
    3. **The floor is charged to the budget currency only.** A GBP balance must
       not silently absorb a USD reserve.
    """
    from .domain.treasury import current_plan, funding_for
    from .db import get_default_db

    if plan is None:
        try:
            plan = current_plan(get_default_db())
        except Exception:
            log.exception("could not build the plan for money_block")
            plan = None

    rows = payables
    if rows is None:
        rows = [{"number": p.number, "supplier": p.supplier.name if p.supplier else None,
                 "display_status": p.status.value, "live_status": None}
                for p in s.query(InvoicePayable).all()]

    floor = budget["reserve_floor_minor"]
    budget_ccy = budget["currency"]
    # What we can pay FROM: balances plus the budget currency itself (we are
    # allowed to convert into it, so it is a funding source even at zero held).
    held = set(cash) | {budget_ccy}

    promised: dict[str, int] = {}
    in_flight: dict[str, int] = {}
    lines: list[dict] = []
    unfunded: list[dict] = []

    for p in rows:
        num = p.get("number")
        if not num:
            continue
        f = funding_for(plan, num, held)
        live = (p.get("live_status") or "").upper()
        settled = live in ("PAID", "COMPLETED", "SUCCESS")
        sent = (p.get("display_status") == "SUBMITTED" or _live_in_flight(live))
        if settled:
            # The bank says it is done. The ledger will carry the debit; do not
            # also carry it as an obligation.
            where = "settled"
        elif sent:
            in_flight[f.currency] = in_flight.get(f.currency, 0) + f.amount_minor
            where = "in_flight"
        else:
            promised[f.currency] = promised.get(f.currency, 0) + f.amount_minor
            where = "promised"

        line = {
            "payable_number": num,
            "supplier": p.get("supplier"),
            "currency": f.currency,
            "amount_minor": f.amount_minor,
            "source": f.source,
            "note": f.note,
            "where": where,
            "payable_currency": p.get("currency"),
            "payable_amount_minor": p.get("amount_minor"),
            "live_status": p.get("live_status"),
        }
        lines.append(line)
        if f.source == "unfunded" and where == "promised":
            unfunded.append(line)

    free: dict[str, int] = {}
    shortfall: dict[str, int] = {}
    for ccy in sorted(set(cash) | set(promised) | set(in_flight)):
        net = (int(cash.get(ccy, 0))
               - int(promised.get(ccy, 0))
               - int(in_flight.get(ccy, 0))
               - (floor if ccy == budget_ccy else 0))
        free[ccy] = net
        if net < 0:
            shortfall[ccy] = -net

    # Which payable would take the position below the floor? Walk the still-to-
    # be-paid ones in plan order and name the one that cannot be covered.
    breaching: list[dict] = []
    remaining = (int(cash.get(budget_ccy, 0))
                 - int(in_flight.get(budget_ccy, 0)) - floor)
    for line in lines:
        if line["where"] != "promised" or line["currency"] != budget_ccy:
            continue
        if line["amount_minor"] > remaining:
            breaching.append(line)
        else:
            remaining -= line["amount_minor"]

    return {
        "promised": promised,
        "in_flight": in_flight,
        "free": free,
        "shortfall": shortfall,
        "lines": lines,
        "unfunded": unfunded,
        "breaching": breaching,
        "reserve_applies_to": budget_ccy,
        "reserve_floor_minor": floor,
        # The ONE headline figure: the budget currency's free position.
        "headline_currency": budget_ccy,
        "headline_free_minor": free.get(budget_ccy, 0),
        "headline_shortfall_minor": shortfall.get(budget_ccy, 0),
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


# `field=source_currency code=001` — the shape `executor._error_detail` writes
# into plan_step.reason, and the only place the real Airwallex error survives:
# there is no transfer-response table, so the stored reason IS the record.
_ERR_DETAIL_RE = re.compile(
    r"field=(?P<field>[A-Za-z0-9_.\[\]]+)\s+code=(?P<code>[A-Za-z0-9_.-]+)")


def parse_api_error(reason: str) -> dict:
    """Pull the Airwallex code, field and message out of a stored failure.

    Returns {} when the string carries no structured detail, so the caller can
    fall back to prose rather than inventing a field name.
    """
    r = (reason or "").strip()
    out: dict = {}
    m = _ERR_DETAIL_RE.search(r)
    if m:
        out["field"] = m.group("field")
        out["code"] = m.group("code")
    elif match := re.search(r"code=(?P<code>[A-Za-z0-9_.-]+)", r):
        out["code"] = match.group("code")
    # `[validation_failed]` / `[invalid_request]` — the top-level error code.
    tag = re.search(r"\[(?P<tag>[a-z_]+)\]", r)
    if tag:
        out["error"] = tag.group("tag")
    return out


def _plain_reason(reason: str) -> str:
    """A one-line plain-English summary of a failure reason.

    The raw Airwallex error is kept and shown behind a Details expander, but the
    visible line must be something a person can act on — a truncated
    "field=reason c" is neither. Never truncate silently: either summarise it or
    show it whole.
    """
    r = (reason or "").strip()
    low = r.lower()
    detail = parse_api_error(r)
    field = detail.get("field")

    if "validation_failed" in low or "schema validation" in low:
        if field:
            # Name the field. This is the difference between "something was
            # invalid" and a fix.
            return (f"The bank rejected the request: the `{field}` field was "
                    f"missing or invalid (code {detail.get('code', '001')}).")
        return "The bank rejected the request: a required field was missing or invalid."
    if "source_currency" in low:
        return "The bank rejected the request: the currency to debit was missing."
    if "unauthorized" in low or "admin permissions" in low:
        return "Not permitted: this account is not allowed to do that."
    if "insufficient" in low:
        return "Not enough money in that currency."
    if "beneficiary" in low and "invalid" in low:
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
            # Rows arrive NEWEST FIRST, so the first entry for a payable is its
            # current state. The one exception is a preference for the money-
            # moving step: when a payable has both an FX step and a transfer, the
            # transfer is the one that describes the payment.
            #
            # This previously read `existing is None or action == "TRANSFER"`,
            # which is true for EVERY transfer row — so each older transfer
            # overwrote the newer one and the OLDEST transfer won. That is why
            # PO-7003 displayed CANCELLED (its first, failed attempt) while its
            # newest step was SUBMITTED and in flight.
            if existing is None or (
                    action == "TRANSFER" and existing.get("action") != "TRANSFER"):
                payable_state[payable] = {
                    "status": st.status.value,
                    "action": action,
                    "step_id": st.id,
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
            api_error = parse_api_error(reason)
            if api_error.get("field"):
                # The whole point of parsing: an operator reading the log learns
                # WHICH field the bank rejected, not merely that something was.
                log.warning("plan_step %s (%s) rejected by the bank: field=%s "
                            "code=%s error=%s", st.id,
                            payable or "no payable",
                            api_error.get("field"), api_error.get("code"),
                            api_error.get("error"))
            problems.append({
                "step_id": st.id,
                "payable_number": payable,
                "status": st.status.value,
                "reason": reason,
                "plain_reason": _plain_reason(reason),
                "api_error": api_error,
                "currency": pl.get("currency"),
                "amount_minor": pl.get("amount_minor"),
                # A step that already reached the bank is history, not a problem
                # needing action: it may still settle.
                "settled": bool(st.airwallex_object_id),
                "current": _is_current(st, latest_run),
            })

    # ONE payable, ONE current state — decided by RECENCY, not severity.
    #
    # An earlier version let a FAILED/CANCELLED row outrank a success "even if a
    # later row exists". That kept a stale failure visible, but it also meant a
    # payable whose LATEST attempt succeeded was still reported as failed: three
    # pre-fix CANCELLED rows for PO-7003 outranked the newer SUBMITTED transfer
    # that actually went out, so the dashboard claimed a payment had failed when
    # it was in flight. Rows arrive newest-first (id DESC), so the FIRST entry
    # for a payable is its current state and everything after it is history.
    by_payable: dict[str, dict] = {}
    superseded: list[dict] = []
    for p in problems:
        key = p["payable_number"] or "unknown"
        if key in by_payable:
            superseded.append(p)
        else:
            by_payable[key] = p

    current_problems: list[dict] = []
    past_attempts: list[dict] = []
    for p in by_payable.values():
        if p["status"] in ("CANCELLED", "FAILED"):
            # Still-current failures are actionable; superseded ones are history.
            (current_problems if p["current"] else past_attempts).append(p)
        elif p["settled"] and p["status"] in ("SUBMITTED", "APPROVED", "PAID"):
            # A success is the current state, so it is not a problem AND not a
            # "past attempt" — listing a payment that went through under a
            # failures heading is a false claim about money that moved.
            continue
        else:
            past_attempts.append(p)

    # Superseded failures keep their own exact reason as history. They are not
    # dropped: the reason is the evidence of what went wrong, and it is what
    # makes "this was fixed" checkable rather than asserted.
    seen_hist = {p["payable_number"] for p in past_attempts}
    for p in superseded:
        if p["status"] not in ("CANCELLED", "FAILED"):
            continue
        if p["payable_number"] in seen_hist:
            continue
        past_attempts.append(p)

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


# Live status lookups make a NETWORK call per referenced transfer. That is right
# for the app and wrong for the suite: a test that reaches Airwallex is slow,
# fails without credentials, and asserts on someone else's uptime. Tests set
# this False; `make dev` leaves it True.
LIVE_STATUS_ENABLED = True


def live_transfer_statuses(s) -> dict[str, str]:
    """Ask Airwallex what each referenced transfer actually is RIGHT NOW.

    A `plan_step.status` says what WE did (SUBMITTED = we sent it). It says
    nothing about whether the money moved, and calling a submitted sandbox
    transfer "paid" is a claim about someone's money that no record supports.
    The bank is the only authority, so ask it — and fall back to the stored
    status, clearly labelled, if the call fails.
    """
    if not LIVE_STATUS_ENABLED:
        return {}
    rows = (s.query(PlanStep)
            .filter(PlanStep.airwallex_object_id.isnot(None))
            # ONLY transfers. An FX row's object id is a CONVERSION, and asking
            # /transfers about a conversion id 404s — the failed lookup then read
            # as "we could not find out", when the truth is it was never a
            # transfer to look up.
            .filter(PlanStep.type == StepType.TRANSFER)
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

        # The REAL plan, from the REAL planner. A held payment must show the
        # rule and its numbers, and the only honest source for that is the step
        # the planner produced — not a phrase the view composes.
        try:
            from .db import get_default_db
            from .domain.treasury import current_plan
            plan = current_plan(get_default_db())
            plan_steps = {st.payable_number: st for st in plan.steps
                          if st.payable_number}
        except Exception:
            log.exception("could not build the treasury plan")
            plan = None
            plan_steps = {}

        payables = []
        for p in s.query(InvoicePayable).order_by(InvoicePayable.number).all():
            sup = suppliers.get(p.supplier_id)
            state = steps["payable_state"].get(p.number, {})
            step = plan_steps.get(p.number)
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
                # The planner's own verdict on this payable: action, the exact
                # reason with its numbers, and whether it needs a human.
                "plan_action": step.action if step else None,
                "plan_reason": step.reason if step else None,
                "plan_policy_reason": step.policy_reason if step else None,
                "plan_needs_approval": bool(step.needs_approval) if step else False,
                "plan_approval_reason": step.approval_reason if step else None,
            })

        rep = reconcile(s)

        # What is waiting, and WHY — from the planner, so the header, the tab and
        # the payable cards all quote the same rule.
        waiting = [p for p in payables
                   if p["plan_action"] in ("DEFER", "ESCALATE")]

        # A payable the planner says needs approval AND which has not already
        # been sent. Once a step carries an Airwallex object id the approval was
        # given and the money is moving; asking again would invite a SECOND
        # payment for the same bill, which is the duplicate this system exists
        # to prevent.
        awaiting_approval = [p for p in payables
                             if p["plan_needs_approval"]
                             and not p["airwallex_object_id"]]

        return {
            "budget": budget,
            "money": money_block(s, rep.cash_by_currency, budget,
                                 plan=plan, payables=payables),
            "cash": rep.cash_by_currency,
            "suspense": rep.suspense_by_currency,
            "ledger_balanced": rep.balanced,
            "receivables": receivables,
            "payables": payables,
            "waiting": waiting,
            "awaiting_approval": awaiting_approval,
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
