"""Everything the dashboard renders, read once and shaped for the UI.

Separated from `ui.py` so presentation stays free of queries: the page cannot
disagree with the API, because both read this context.

Design rule: this module READS. It never decides anything. A number that
appears on screen was decided by policy/matching/incidents before this ran.
"""
from __future__ import annotations

import json
import logging
import time
import re
from typing import Any, Optional

from .domain.agent_tools import ALLOWED_TOOLS
# The ONE in-flight list, shared with incident recovery. A private copy here is
# exactly how a second definition of "still moving" gets out of step — and this
# set is what decides whether a replacement payment may be created.
from .domain.incidents import in_flight as _live_in_flight
from .domain.policy import available_cash_minor
from .money import money_str
from .models import (
    AgentTrace, Approval, ApprovalStatus, Customer, Inflow, InvoicePayable,
    InvoiceReceivable, Match, MatchStatus, PayStatus, PlanStep, StepStatus,
    StepType, Supplier,
)

log = logging.getLogger("closebook.dashboard")


def _loads(text) -> dict:
    try:
        data = json.loads(text or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


# ---------------------------------------------------------------- budget
def budget_block(s=None) -> dict:
    """The limits, plus the FORECAST — shown, never spent.

    Expected inflows used to be the policy budget the reserve floor was measured
    against, which is how a payment could be approved against money the business
    had not received. They are now a forecast line and nothing else: the
    affordability rule is `policy.available_cash_minor`, and the tiles and the
    planner both call it.

    The forecast is DERIVED from the open receivables rather than read from a
    config number, so it cannot drift from the invoices it describes.

    The limits are read from the WORKSPACE ROW (the operator's own settings),
    in MINOR units as stored — this used to multiply major units by 100, which
    is only right for a 2-exponent currency and only by luck for USD.
    """
    from .db import get_default_db, session_for
    from .domain.settings_store import as_budget, read as read_settings
    from .domain.treasury import forecast_from_db, DEMO_RATES

    # THE CALLER'S SESSION, OR ONE WE CLOSE. This read
    # `session = s or get_default_db().session()` and then never closed the
    # session it had opened, so every dashboard render leaked a connection.
    with session_for(get_default_db(), s) as session:
        row = read_settings(session)
        b = as_budget(row)
        try:
            fc = forecast_from_db(session, DEMO_RATES)
            forecast_lines = fc.lines
            forecast_total = fc.total_minor()
        except Exception:
            log.exception("could not build the forecast")
            forecast_lines, forecast_total = [], 0
    return {
        "currency": b.currency,
        "reserve_floor_minor": int(row.reserve_floor_minor),
        "auto_pay_limit_minor": int(row.auto_pay_limit_minor),
        "fx_tolerance_pct": float(row.fx_tolerance_pct),
        # FORECAST ONLY. Named so no reader mistakes it for spendable cash.
        "forecast_lines": forecast_lines,
        "forecast_minor": forecast_total,
        "forecast_is_spendable": False,
    }


def money_block(s, cash: dict, budget: dict, plan=None,
                payables: Optional[list] = None,
                suspense: Optional[dict] = None,
                refunds: Optional[dict] = None) -> dict:
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
    from .domain.settlement import paid_payouts, settled_txn_keys
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

    # THE LEDGER decides what has settled — not the bank's live status and not
    # the plan. If the tile trusted a bank PAID before anything was posted, the
    # free-to-spend figure would jump at the moment the bank finished while cash
    # had not moved yet.
    posted_keys = settled_txn_keys(s)

    floor = budget["reserve_floor_minor"]
    budget_ccy = budget["currency"]
    # What we can pay FROM: balances plus the budget currency itself (we are
    # allowed to convert into it, so it is a funding source even at zero held).
    held = set(cash) | {budget_ccy}

    promised: dict[str, int] = {}
    in_flight: dict[str, int] = {}
    lines: list[dict] = []
    unfunded: list[dict] = []

    from .domain.treasury import in_flight_for

    for p in rows:
        num = p.get("number")
        if not num:
            continue
        st = (p.get("display_status") or "").upper()
        live = (p.get("live_status") or "").upper()
        payout_id = p.get("airwallex_object_id")
        bank_says_done = live in ("PAID", "COMPLETED", "SUCCESS", "SETTLED")
        in_ledger = bool(payout_id) and payout_id in posted_keys

        # IN FLIGHT comes from the SAME function the planner uses, reading the
        # executed step payloads. Deriving it here from a rebuilt plan let the
        # tile and the plan quote different numbers for the same sent payment.
        sent = None if in_ledger else in_flight_for(s, num, posted=posted_keys)

        if in_ledger:
            where = "settled"
            f_ccy, f_amt, f_src, f_note = str(p.get("currency") or "USD"), 0, "settled", ""
        elif sent is not None:
            where = "in_flight"
            f_ccy, f_amt, f_src, f_note = (
                sent[0], sent[1], "in_flight",
                "sent to the bank; the ledger has not taken it out of cash yet")
        else:
            # Unpaid and unsent. A failed payout lands here too: nothing was
            # posted, the supplier is still owed, so it is still promised.
            fx_id = p.get("fx_object_id")
            f = funding_for(plan, num, held,
                            conversion_posted=bool(fx_id) and fx_id in posted_keys)
            where = "promised"
            f_ccy, f_amt, f_src, f_note = (f.currency, f.amount_minor,
                                           f.source, f.note)

        # Counted EXACTLY ONCE, in its funding currency. Settled money is out of
        # the arithmetic because the ledger has already taken it out of cash.
        if where == "in_flight":
            in_flight[f_ccy] = in_flight.get(f_ccy, 0) + f_amt
        elif where == "promised" and f_src != "unfunded":
            # An UNFUNDED payable is NOT counted as a promise in its own
            # currency. We hold none of that currency and the plan has no
            # conversion for it, so costing it in that currency invented a
            # shortfall in a currency nobody was going to spend: a EUR 9,000
            # supplier reported as "Short by EUR 9,012.85" while USD 15,250 sat
            # in the account. It is surfaced separately, in plain words.
            promised[f_ccy] = promised.get(f_ccy, 0) + f_amt

        line = {
            "payable_number": num,
            "supplier": p.get("supplier"),
            "currency": f_ccy,
            "amount_minor": f_amt,
            "source": f_src,
            "note": f_note,
            "where": where,
            "payable_currency": p.get("currency"),
            "payable_amount_minor": p.get("amount_minor"),
            "live_status": p.get("live_status"),
            "posted": in_ledger,
            "bank_says_done": bank_says_done,
            "display_status": st,
        }
        lines.append(line)
        if f_src == "unfunded" and where == "promised":
            unfunded.append(line)

    # Money received but NOT attributed to an invoice. It is in the bank, and it
    # is not spendable — a supplier paid with it can be paid again tomorrow when
    # the payment it came from is refunded. Subtracted here AND in the planner,
    # through the same rule, and shown separately so it never disappears.
    suspense = {k: int(abs(v)) for k, v in (suspense or {}).items()}
    # Money we have decided is owed back. It is still in the bank, and it is
    # still not ours to spend — the refund will leave later, out of cash that
    # will by then be gone if we have spent it here.
    refunds = {k: int(abs(v)) for k, v in (refunds or {}).items()}

    free: dict[str, int] = {}
    shortfall: dict[str, int] = {}
    for ccy in sorted(set(cash) | set(promised) | set(in_flight)
                      | set(suspense) | set(refunds)):
        # THE ONE RULE, called — not re-implemented. The planner calls the same
        # function, which is the entire point: these two used to be separate
        # arithmetic answering the same question two ways.
        net = available_cash_minor(
            cash_minor=int(cash.get(ccy, 0)),
            in_flight_minor=int(in_flight.get(ccy, 0)),
            committed_this_run_minor=int(promised.get(ccy, 0)),
            suspense_minor=int(suspense.get(ccy, 0)),
            refunds_owed_minor=int(refunds.get(ccy, 0)),
            floor_minor=(floor if ccy == budget_ccy else 0))
        free[ccy] = net
        if net < 0:
            shortfall[ccy] = -net

    # Which payable would take the position below the floor? Walk the still-to-
    # be-paid ones in plan order and name the one that cannot be covered, using
    # the same rule so the sentence matches the tile above it.
    breaching: list[dict] = []
    remaining = available_cash_minor(
        cash_minor=int(cash.get(budget_ccy, 0)),
        in_flight_minor=int(in_flight.get(budget_ccy, 0)),
        suspense_minor=int(suspense.get(budget_ccy, 0)),
        refunds_owed_minor=int(refunds.get(budget_ccy, 0)),
        floor_minor=floor)
    for line in lines:
        if line["where"] != "promised" or line["currency"] != budget_ccy:
            continue
        if line["amount_minor"] > remaining:
            after = remaining - line["amount_minor"]
            line["cash_after_minor"] = after
            line["cash_floor_reason"] = (
                f"paying {line['payable_number']} "
                f"({money_str(line['amount_minor'], line['currency'])}) would "
                f"leave cash {money_str(after, budget_ccy)}, below the "
                f"{money_str(floor, budget_ccy)} floor")
            line["cash_floor_code"] = "cash_reserve_floor"
            breaching.append(line)
        else:
            remaining -= line["amount_minor"]

    return {
        "promised": promised,
        "in_flight": in_flight,
        "suspense": suspense,
        "refunds_owed": refunds,
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
        # Settled payouts, straight from the ledger. Only payouts — an FX
        # conversion moved money between our own currencies and paid nobody.
        "paid_today": paid_payouts(s, since=_start_of_today()),
    }


def _start_of_today():
    from datetime import datetime, time, timezone
    now = datetime.now(timezone.utc)
    return datetime.combine(now.date(), time.min, tzinfo=timezone.utc)


# ------------------------------------------------------- steps: problems
def _is_approvable(step: PlanStep, payload: dict,
                   approved_step_ids: Optional[set] = None) -> bool:
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
    # A PERSON ALREADY SAID YES. Showing the card again re-asks a question that
    # has been answered, and inviting a second approval for one payment is how
    # an operator comes to believe the first one did nothing. The step moves to
    # the held list (with whatever still blocks it) instead.
    if approved_step_ids and step.id in approved_step_ids:
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

    # WHICH STEPS A PERSON HAS ALREADY APPROVED. Read once here rather than per
    # row, and read from the STORED approval, so a step approved in an earlier
    # request is still recognised as approved in this one — which is the whole
    # point: the decision outlives the press.
    from .models import Approval, ApprovalStatus

    def _still_covers(row: PlanStep, ap) -> bool:
        """Does this approval still describe THIS payment?

        An approval is a person agreeing to an amount, a currency and a
        counterparty. Edit the amount on the bill afterwards and the approval is
        about something that no longer exists, so it must stop suppressing the
        card -- otherwise the operator is told a payment is settled business
        when the figure they agreed to has been changed underneath them.
        """
        payload = json.loads(row.payload_json or "{}")
        here = (payload.get("amount_minor") or payload.get("sell_amount_minor") or 0)
        if not (int(ap.amount_minor or 0) == int(here or 0)
                and (ap.currency or "").upper()
                == (payload.get("currency") or payload.get("sell_currency")
                    or ap.currency or "").upper()):
            return False
        # AND THE BENEFICIARY. A card is suppressed because a person already said
        # yes to THIS payment; if the payee has changed, they said yes to a
        # different one, so the question is open again. The executor refuses the
        # old approval either way; this stops the screen hiding the fact that
        # nobody has been asked.
        if ap.beneficiary_id:
            from .models import InvoicePayable, Supplier
            number = payload.get("payable_number")
            payable = (s.query(InvoicePayable)
                       .filter(InvoicePayable.number == number).first())
            supplier = (s.get(Supplier, payable.supplier_id) if payable else None)
            now = supplier.airwallex_beneficiary_id if supplier else None
            if (now or "") != (ap.beneficiary_id or ""):
                return False
        return True

    _approved_step_ids = set()
    for row in s.query(Approval).filter(
            Approval.status == ApprovalStatus.APPROVED).all():
        step_row = s.get(PlanStep, row.plan_step_id) if row.plan_step_id else None
        if step_row is not None and _still_covers(step_row, row):
            _approved_step_ids.add(row.plan_step_id)
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

        if _is_approvable(st, pl, _approved_step_ids):
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
def _proposed_invoice_numbers(session, inflow) -> list[str]:
    """Invoice numbers named by a stored proposal, for the approve button."""
    blob = _loads(inflow.proposed_json) if inflow.proposed_json else {}
    out = []
    for a in (blob.get("allocations") or []):
        try:
            inv = session.query(InvoiceReceivable).filter_by(
                id=int(a["invoice_id"])).first()
        except (KeyError, TypeError, ValueError):
            continue
        if inv is not None:
            out.append(inv.number)
    return out


# ------------------------------------------------------- money that arrived
def arrivals_block(s) -> list[dict]:
    """Every deposit we took in, with the verdict that was recorded on it.

    Read straight off `inflow` — method, confidence and reason are STORED, so
    the three tabs that show this cannot disagree, and nothing re-derives a
    decision after the fact. `awaiting_human` is the honest gate: a match below
    the auto-apply band has moved the cash and the invoice but still needs a
    person, so it belongs in "Needs your OK" as well as in Money in.
    """
    from .domain.policy import MATCH_CONFIDENCE_AUTO

    rows = s.query(Inflow).order_by(Inflow.id.desc()).limit(60).all()
    by_inflow: dict[int, list] = {}
    for m in s.query(Match).all():
        inv = s.query(InvoiceReceivable).filter_by(id=m.invoice_receivable_id).first()
        by_inflow.setdefault(m.inflow_id, []).append({
            "invoice": inv.number if inv else "?",
            "applied_minor": m.amount_applied_minor,
            "currency": inv.currency if inv else "",
        })
    from .domain.ledger import suspense_for

    out = []
    for i in rows:
        conf = i.match_confidence
        left = suspense_for(s, i)
        out.append({
            "id": i.id,
            "currency": i.currency,
            "amount_minor": i.amount_minor,
            "payer": i.payer_name or "",
            "reference": i.reference_raw or "",
            "has_reference": bool((i.reference_raw or "").strip()),
            "deposit_id": i.airwallex_deposit_id or "",
            "match_status": i.match_status.value,
            "method": i.match_method or "",
            "confidence": conf,
            "reason": i.match_reason or "",
            "applied": by_inflow.get(i.id, []),
            # "Needs your OK" is not the same question as "unmatched": a
            # part-payment at 0.85 is applied AND needs a human to chase it.
            "review_state": i.review_state or "",
            # The invoices a PENDING proposal would apply to. Read from the
            # stored proposal, so the button names the same invoice the matcher
            # proposed — not a fresh match that might read differently.
            "proposed_invoices": _proposed_invoice_numbers(s, i),
            # CAN THIS PROPOSAL STILL BE APPLIED? A proposal is a snapshot taken
            # when the money arrived. Three payments can each name the same
            # invoice at its full value, and the second one is no longer true —
            # the invoice has nothing left open. The page has to say that rather
            # than offer a button that can only fail, or worse, overpay.
            "proposal_fit": _proposal_fit(s, i),
            # WHEN MATCH IS NOT ON OFFER, SAY WHY. The stored proposal may name
            # an invoice that has since been paid, or the model may have named
            # one that is no longer a candidate at all. Either way the page must
            # not simply drop the option and leave the reader wondering what
            # happened to it.
            "no_match_why": _no_match_why(s, i, left),
            "proposal": _loads(i.proposed_json),
            # WAITING means PENDING, full stop. This used to also ask "is the
            # confidence below the band", which stayed true after approval — so
            # an already-applied match kept appearing in the review queue and
            # offered a button that could only fail.
            # WAITING means "a human still has to decide", which is not the
            # same as "PENDING". A REJECTED payment is not finished either: the
            # cash is still in suspense, so it still needs resolving — it just
            # needs a DIFFERENT decision now.
            "awaiting_human": (i.review_state != "APPROVED"
                               and left > 0 and i.review_state != "RELEASED_INCOME"
                               and i.review_state != "REFUND_OWED"),
            "settled_match": i.match_status == MatchStatus.MATCHED,
            # HOW MUCH of this payment is still unresolved, read from the
            # ledger so a part-allocated inflow shows only its remainder.
            "suspense_minor": left,
            "resolvable": bool(left > 0),
            # MODEL SAID / CODE DECIDED, read from the stored proposal and the
            # stored trace. Built here so the AI activity screen and the money-in
            # item show the SAME pair -- one component, one source.
            "receipt": receipt_data(s, i, _loads(i.proposed_json) or {},
                                    conf, left),
            "resolution_reason": i.resolution_reason or "",
            "resolution_note": i.resolution_note or "",
            "resolved_by": i.resolved_by or "",
            "resolved_at": (i.resolved_at.isoformat() if i.resolved_at else ""),
        })
    return out


def _proposal_fit(session, inflow) -> dict:
    """Whether the stored proposal still fits the invoices it names.

    Returns `{"ok": bool, "why": str, "invoices": [numbers]}`. Empty when there
    is no proposal to judge.
    """
    from .domain.ledger import _allocations_from_proposal

    try:
        allocs = _allocations_from_proposal(inflow)
    except Exception:                              # noqa: BLE001
        allocs = []
    if not allocs:
        return {}

    from .money import money_str
    already: dict[int, int] = {}
    for a in allocs:
        inv = session.query(InvoiceReceivable).filter_by(id=a.invoice_id).first()
        if inv is None:
            return {"ok": False, "why": "the invoice it names no longer exists",
                    "invoices": []}
        left = inv.amount_minor - inv.amount_paid_minor - already.get(inv.id, 0)
        if a.amount_minor > left:
            if left <= 0:
                return {"ok": False, "invoices": [inv.number], "why": (
                    f"{inv.number} has been paid in full since this proposal was "
                    f"made, so it cannot take this money. It is already somebody "
                    f"else's payment: record this one as a refund owed, or ask "
                    f"the model again.")}
            return {"ok": False, "invoices": [inv.number], "why": (
                f"{inv.number} has only {money_str(max(0, left), inv.currency)} "
                f"left open, so this cannot be applied to it in full. Record it "
                f"as a refund owed, or ask the model again.")}
        already[inv.id] = already.get(inv.id, 0) + a.amount_minor
    return {"ok": True, "why": "", "invoices": [str(x) for x in
                                                _proposed_invoice_numbers(session, inflow)]}


def _no_match_why(session, inflow, left: int) -> str:
    """Why the Match option is absent, in words, or "" when it is offered."""
    from .domain.ledger import _allocations_from_proposal

    if left <= 0:
        return ""
    fit = _proposal_fit(session, inflow)
    if fit.get("why"):
        return fit["why"]
    if fit.get("ok"):
        return ""
    named = _proposed_invoice_numbers(session, inflow)
    if named:
        return (f"{' and '.join(named)} cannot take this money as it stands.")
    reason = (inflow.match_reason or "").strip()
    if reason:
        return (f"No open invoice matches this reading ({reason.rstrip('.')}), "
                f"so there is nothing to apply it to. If the invoice this was "
                f"meant for has been paid in full, record this as a refund owed "
                f"instead.")
    return ("No open invoice matches this reading, so there is nothing to apply "
            "it to.")


def trace_block(s) -> list[dict]:
    """Model calls and code decisions, each paired with the DECISION it produced.

    THE CODE COLUMN IS NOT DERIVABLE FROM THE MODEL'S TRACE. A `propose_match`
    row records what the model CLAIMED; whether that claim was used, capped or
    thrown away is a fact about the stored decision on the payment. Reading the
    code column off the model's own trace reported the model's number as the
    final one whenever a deterministic tier had won -- a receipt that says "the
    code decided 0.80" when the code actually decided 0.85 at tier RULE.
    """
    # NO LIMIT. A cap here is a cap on the COUNT as well as on the list, so a
    # busy run silently reads as a smaller one: 26 stored traces rendered as "24
    # calls". The page can render a window of these; the number must be the
    # number.
    rows = s.query(AgentTrace).order_by(AgentTrace.id.desc()).all()
    out = []
    for t in rows:
        ins = _loads(t.inputs_json)
        outs = _loads(t.outputs_json)
        item = {
            "tool": t.tool,
            "verdict": t.verdict,
            "inputs": ins,
            "outputs": outs,
            "detail": t.detail,
            "run_id": t.run_id,
        }
        inflow_id = (ins or {}).get("inflow_id")
        if inflow_id:
            from .models import Inflow
            from .domain.ledger import suspense_for

            inf = s.get(Inflow, int(inflow_id))
            if inf is not None:
                prop = _loads(inf.proposed_json) or {}
                conf = (prop.get("confidence")
                        if prop.get("confidence") is not None
                        else _confidence_of(inf, s))
                try:
                    left = suspense_for(s, inf)
                except Exception:                  # noqa: BLE001
                    left = 0
                item["decision"] = receipt_data(s, inf, prop, conf, left)
        out.append(item)
    return out


def _confidence_of(inflow, s):
    """The confidence the book recorded on the inflow, if the proposal is gone."""
    from .models import Match

    m = (s.query(Match)
         .filter(Match.inflow_id == inflow.id)
         .order_by(Match.id.desc()).first())
    return float(m.confidence) if m is not None and m.confidence is not None else None


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

#: Last known Airwallex status per transfer id, and when we learned it. Filled
#: by `refresh_live_statuses` in a BackgroundTask, read by the render. Kept in
#: process memory on purpose: it is a cache of somebody else's data, and losing
#: it on restart costs one refresh, not a fact.
_LIVE_CACHE: dict[str, tuple[str, float]] = {}
#: How long a cached status is good for. The page shows when it last checked,
#: so a slightly old true answer beats a fresh three-second wait.
_LIVE_MAX_AGE = 60.0


def _return_reasons(incidents: list | None) -> dict:
    """transfer id -> what the BANK said went wrong, from the incident store.

    A returned transfer arrives as CANCELLED, and CANCELLED alone reads as
    "somebody stopped this". The reason is the whole point: a bad IBAN and a
    beneficiary bank returning the money need different responses. The live
    incident records are where the webhook's own failure fields survive.
    """
    out: dict = {}
    for d in incidents or []:
        tid = str(d.get("transfer_id") or "")
        if not tid:
            continue
        why = (d.get("failure_reason") or d.get("failure_type")
               or (d.get("summary") if d.get("severity") in ("ERROR", "BAD")
                   else None))
        if why:
            out.setdefault(tid, why)
    return out


def live_transfer_statuses(s) -> dict[str, str]:
    """What Airwallex last said about each referenced transfer. NO NETWORK HERE.

    A `plan_step.status` says what WE did (SUBMITTED = we sent it). It says
    nothing about whether the money moved, and calling a submitted sandbox
    transfer "paid" is a claim about someone's money that no record supports.
    The bank is the only authority.

    THAT AUTHORITY IS ASKED IN THE BACKGROUND, NOT DURING A PAGE RENDER. This
    used to call Airwallex once per referenced transfer, inline, and with four
    transfers on the book `/app` took 3.1 seconds — 3.56s of 3.64s of the whole
    render was these lookups and the token fetch behind them. A page that waits
    on someone else's API is a page that goes down when they do.

    So this reads the last known answer and says how old it is. `refresh()`
    below fills the cache, and the route schedules that after the response.
    """
    if not LIVE_STATUS_ENABLED:
        return {}
    ids = live_status_ids(s)
    if not ids:
        return {}
    now = time.monotonic()
    out: dict[str, str] = {}
    for tid in ids:
        got = _LIVE_CACHE.get(tid)
        if got is None:
            out[tid] = "UNCHECKED"
        else:
            status, at = got
            out[tid] = status if now - at < _LIVE_MAX_AGE * 4 else f"{status}"
    return out


def note_live_status(transfer_id: str, status: str) -> None:
    """Record a status we just learned FOR CERTAIN, without a lookup.

    The pill reads the cache, and the cache is filled by a background refresh so
    a page render never waits on Airwallex. That is right for a normal view. It
    is wrong immediately after an action that ALREADY ASKED: settle reads the
    real status back to report it, and then the page told the operator
    PROCESSING for a payment the same response called PAID. When we hold the
    answer, write it down instead of showing a stale one.
    """
    if not LIVE_STATUS_ENABLED or not transfer_id or not status:
        return
    _LIVE_CACHE[transfer_id] = (str(status).upper(), time.monotonic())


def live_status_ids(s) -> list[str]:
    """Every transfer id the book refers to. A local read, nothing else."""
    rows = (s.query(PlanStep)
            .filter(PlanStep.airwallex_object_id.isnot(None))
            # ONLY transfers. An FX row's object id is a CONVERSION, and asking
            # /transfers about a conversion id 404s — the failed lookup then read
            # as "we could not find out", when the truth is it was never a
            # transfer to look up.
            .filter(PlanStep.type == StepType.TRANSFER)
            .order_by(PlanStep.id.desc()).all())
    seen: dict[str, None] = {}
    for r in rows:
        seen.setdefault(r.airwallex_object_id, None)
    return list(seen)


def stale_live_status_ids(s) -> list[str]:
    """Ids whose cached answer is missing or older than the max age."""
    if not LIVE_STATUS_ENABLED:
        return []
    now = time.monotonic()
    return [tid for tid in live_status_ids(s)
            if now - _LIVE_CACHE.get(tid, ("", 0.0))[1] > _LIVE_MAX_AGE]


def refresh_live_statuses(ids: list[str]) -> None:
    """Ask Airwallex about these transfers and cache the answers.

    Called from a BackgroundTask AFTER the response, so a slow or dead sandbox
    can never delay a page. A failed lookup caches UNKNOWN, which the view
    labels honestly rather than implying the money is fine.
    """
    if not LIVE_STATUS_ENABLED or not ids:
        return
    try:
        from .airwallex.client import AirwallexClient
        from .airwallex.transfers import Transfers
        transfers = Transfers(AirwallexClient())
    except Exception as exc:  # no credentials, sandbox down
        log.warning("live status lookup unavailable: %s", exc)
        return
    for tid in ids:
        try:
            status = transfers.status_of(tid) or "UNKNOWN"
        except Exception as exc:
            log.warning("status_of failed for %s: %s", tid, exc)
            status = "UNKNOWN"
        _LIVE_CACHE[tid] = (status, time.monotonic())



def receipt_data(s, i, prop: dict, final_conf, applied) -> dict:
    """The Model said / Code decided pair for one payment.

    Read from the STORED proposal and the stored trace, never re-derived: the
    page must show the numbers the system actually acted on, not a fresh
    calculation that might read differently today.
    """
    from .domain.policy import MATCH_CONFIDENCE_AUTO

    ev = (prop or {}).get("evidence") or {}
    claimed = ev.get("model_claimed_confidence")
    att = model_attempt(s, i.id)

    model: dict = {}
    if claimed is None and att.get("claimed") is not None:
        claimed = att["claimed"]

    if claimed is not None:
        model["confidence"] = claimed
        model["struck"] = (final_conf is not None
                           and abs(float(claimed) - float(final_conf)) > 1e-9)
    if prop:
        allocs = prop.get("allocations") or []
        nums = []
        for a in allocs:
            n = a.get("invoice_number") or a.get("invoice")
            if not n and a.get("invoice_id"):
                inv = s.get(InvoiceReceivable, a["invoice_id"])
                n = inv.number if inv else None
            if n:
                nums.append(str(n))
        if nums:
            model["invoice"] = ", ".join(nums)
        if allocs:
            tot = sum(int(a.get("amount_minor") or 0) for a in allocs)
            ccy = (prop.get("evidence") or {}).get("currency") or i.currency
            model["amount"] = money_str(tot, ccy)
        model["reason"] = prop.get("reason") or i.match_reason or ""
    elif att.get("failed"):
        # TRIED AND DID NOT ANSWER. Not "not needed", not a quiet shrug: an
        # outage, and the payment went to a person because of it.
        from .domain.plain_words import MODEL_UNAVAILABLE

        model["unavailable"] = MODEL_UNAVAILABLE
    elif att.get("never_asked") or (i.match_method and i.match_method != "LLM"):
        model["reason"] = ("Not needed: a deterministic rule settled this "
                           "before the model was consulted.")
    elif i.match_method == "LLM":
        # The model ran and we have no stored claim for it. Say that, rather
        # than rendering an empty column that reads as "it claimed nothing".
        model["reason"] = ("The model answered but no claim was stored on this "
                           "payment, so the code went on the rules alone.")

    verdict = "held for you" if applied <= 0 or getattr(i, "review_state", None) in (
        "PENDING", "REJECTED") else "applied"
    if i.match_status and i.match_status.value == "ESCALATED":
        verdict = "escalated to a person"
    elif i.match_status and i.match_status.value == "UNMATCHED":
        verdict = "left unmatched"
    code = {"verdict": verdict,
            "rule": (i.match_method or "no rule"),
            "confidence": final_conf,
            "note": ("" if final_conf is None
                     or final_conf >= MATCH_CONFIDENCE_AUTO
                     else f"below the {MATCH_CONFIDENCE_AUTO:.2f} auto band, held for you")}
    return {"model": model, "code": code}


def model_attempt(s, inflow_id: int) -> dict:
    """What the MODEL did on this payment, if anything.

    Three outcomes, and the page has to tell them apart because they call for
    three different reactions:

      * it answered  -> we have a claim to show beside the code's verdict
      * it was never needed -> a deterministic tier settled it, which is the
        system working
      * it was TRIED AND DID NOT ANSWER -> an outage, and the payment went to a
        human because of it. Reporting this as "the model did not decide"
        (as the old page did) hid an outage behind a shrug.
    """
    from .models import AgentTrace

    rows = (s.query(AgentTrace)
            .filter(AgentTrace.tool == "decide_match")
            .order_by(AgentTrace.id.desc()).limit(200).all())
    for t in rows:
        ins = _loads(t.inputs_json) or {}
        if int(ins.get("inflow_id") or -1) != int(inflow_id):
            continue
        outs = _loads(t.outputs_json) or {}
        detail = (t.detail or "")
        failed = bool(outs.get("unavailable")) or bool(
            re.search(r"HTTPStatusError|Too Many Requests|Timeout|unavailable",
                      detail, re.I))
        return {"answered": not failed, "failed": failed,
                "detail": detail, "outs": outs,
                "claimed": outs.get("claimed_confidence")}
    return {"answered": False, "failed": False, "detail": "", "outs": {},
            "claimed": None, "never_asked": True}


def landing_stats(s) -> dict:
    """Real counts for the landing page's one live proof.

    Counted from the same tables the app renders, so the page cannot claim
    something the book does not show. `by_rules` is what the deterministic
    tiers settled on their own and `by_model` what the model read and the code
    then re-checked — the two numbers that are the whole pitch.
    """
    from .domain.ledger import reconcile
    from .models import Inflow, InvoicePayable, InvoiceReceivable, LedgerEntry

    inflows = s.query(Inflow).all()
    by_rules = by_model = 0
    for i in inflows:
        m = (i.match_method or "").upper()
        if m == "LLM":
            by_model += 1
        elif m:
            by_rules += 1
    try:
        balanced = bool(reconcile(s).balanced)
    except Exception:
        balanced = False
    return {
        "deposits": len(inflows),
        "by_rules": by_rules,
        "by_model": by_model,
        "invoices": s.query(InvoiceReceivable).count(),
        "bills": s.query(InvoicePayable).count(),
        "ledger_rows": s.query(LedgerEntry).count(),
        "balanced": balanced,
    }


def build_context(incidents: list | None = None) -> dict[str, Any]:
    from .db import get_default_db
    from .domain.ledger import reconcile

    s = get_default_db().session()
    try:
        # ONE RENDER, ONE CONNECTION. Passing our session down stops the
        # helpers opening their own, which both leaks connections and splits a
        # render across several inconsistent snapshots of the book.
        budget = budget_block(s)

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
        return_reasons = _return_reasons(incidents)

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

        # Which step object (if any) belongs to each payable, per action, so
        # money_block can ask the LEDGER whether a conversion is already posted.
        fx_object_by_payable: dict[str, str] = {}
        for st_row in (s.query(PlanStep)
                       .filter(PlanStep.type == StepType.FX)
                       .filter(PlanStep.airwallex_object_id.isnot(None))
                       .order_by(PlanStep.id.asc()).all()):
            try:
                pl = json.loads(st_row.payload_json or "{}")
            except (ValueError, TypeError):
                continue
            num = pl.get("payable_number")
            if num:
                fx_object_by_payable[num] = st_row.airwallex_object_id

        # One map, used for the dependency label AND by Setup, so a bill's
        # "Depends on" and the plan's "waiting on ..." cannot disagree.
        number_by_invoice_id = {inv.id: inv.number
                                for inv in s.query(InvoiceReceivable).all()}

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
                # THE BANK'S OWN WORDS about a transfer that came back, so
                # CANCELLED is never shown without its reason.
                "failure_reason": return_reasons.get(
                    str(state.get("airwallex_object_id") or ""), ""),
                "step_stored_reason": state.get("reason"),
                # The planner's own verdict on this payable: action, the exact
                # reason with its numbers, and whether it needs a human.
                "plan_action": step.action if step else None,
                "plan_reason": step.reason if step else None,
                "plan_policy_reason": step.policy_reason if step else None,
                "plan_needs_approval": bool(step.needs_approval) if step else False,
                "plan_approval_reason": step.approval_reason if step else None,
                # EVERY reason the planner gave, not just the first.
                "plan_blockers": list(step.blockers) if step else [],
                "plan_blocker_codes": list(step.blocker_codes) if step else [],
                # The conversion step's Airwallex id, if one was ever executed
                # for this payable. money_block checks it against the LEDGER.
                "fx_object_id": fx_object_by_payable.get(p.number),
                # The receivable this bill waits for, read from the same column
                # the planner gates on, so Setup and the plan reason agree.
                "depends_on": number_by_invoice_id.get(p.depends_on_receivable_id),
                # What this bill COSTS in the currency that will pay for it. An
                # EUR 9,000 bill funded from USD is USD 10,346.00 of cash, and
                # showing only the EUR figure hid the conversion that the plan
                # had already decided to make.
                "funding_currency": step.funding_currency if step else None,
                "funding_amount_minor": int(step.funding_amount_minor or 0) if step else 0,
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

        money = money_block(s, rep.cash_by_currency, budget,
                            suspense=rep.suspense_by_currency,
                            refunds=rep.refunds_owed_by_currency,
                            plan=plan, payables=payables)

        # NO cash-basis merge any more. The planner now uses the SAME rule the
        # tiles use, so its blockers already ARE the cash reasons; appending a
        # second, separately-worded cash reason would put the same fact on the
        # page twice in two different sentences.

        # The Setup screens read the workspace they are about to change, so a
        # refusal and the list it applies to are always the same generation.
        from .domain.settings_store import describe as _describe
        from .domain.settings_store import read as _read_settings
        from .domain.workspace import snapshot as _workspace_snapshot

        _ws = _workspace_snapshot(s)
        _st = _describe(_read_settings(s))

        # The global accounts a simulated deposit can land in. Cached for a
        # minute: rendering a page must not block on Airwallex, and a cold
        # failure shows an empty list with an explanation rather than an error.
        try:
            from .domain.intake import accounts_cached
            money_in_accounts = accounts_cached(get_default_db())
        except Exception:
            log.warning("could not list global accounts")
            money_in_accounts = []

        return {
            "budget": budget,
            "settings": _st,
            "workspace": _ws,
            "money_in_accounts": money_in_accounts,
            "money": money,
            "cash": rep.cash_by_currency,
            "suspense": rep.suspense_by_currency,
            "outflow": rep.outflow_by_account,
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
            "arrivals": arrivals_block(s),
            "inflows_waiting": [a for a in arrivals_block(s) if a["awaiting_human"]],
            "allowed_tools": sorted(ALLOWED_TOOLS),
        }
    finally:
        s.close()


__all__ = ["build_context", "budget_block", "incidents_block", "steps_block"]
