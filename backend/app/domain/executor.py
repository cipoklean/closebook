"""Plan execution against Airwallex (closebook.md §5).

This is the ONLY place in the codebase permitted to move money. Everything it
does is gated by code, not by a prompt:

1. **request_id is persisted BEFORE the call.** `reserve()` writes the UUID to
   the `plan_step` row and commits, so a crash mid-POST still leaves the id on
   disk. Retrying reuses that id — which is exactly what Airwallex's duplicate
   window is for.
2. **A step that already holds an `airwallex_object_id` is never re-issued.**
   That is the anti-duplicate rule that matters, and it is unconditional.
3. **Ambiguity is resolved by lookup, never by re-sending.** A timeout or 5xx
   on a mutation raises `AmbiguousSubmit`; we then find the object by
   request_id. Only if the lookup proves nothing was created do we retry.
4. **Approvals are re-checked at execution**, including on retries, because a
   retry is exactly when nobody re-reads the numbers.

The executor takes the plan as data (`Step`) and a set of collaborators. It
does no planning of its own and it never talks to an LLM.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session, object_session

from ..airwallex.client import AmbiguousSubmit, ApiError, AirwallexClient, _reserve_request_id
from ..airwallex.fx import FX
from ..airwallex.transfers import Transfers
from ..models import ApprovalStatus, PlanStep, StepStatus, StepType
from . import approvals as ap
from .planner import ACT_FX, ACT_PAY, CashPosition, Plan, Step

log = logging.getLogger("closebook.executor")

# Statuses from which a step may still be submitted. SUBMITTED/PAID are not
# here on purpose: a step in those states already has (or had) a live object.
SUBMITTABLE = {StepStatus.PROPOSED, StepStatus.APPROVED}

# A step left SUBMITTED is NOT proof of anything. It means "we sent it and may
# never have found out". After a crash mid-POST that is exactly its state, and
# it must be resolved by lookup, not refused — refusing here would strand every
# payment that was in flight when the process died.
IN_FLIGHT = {StepStatus.SUBMITTED}


#: Serialises the send path in this process (see `run_step`).
#: Money-moving steps are few and sequential; the cost of the lock is
#: nothing next to a second transfer for one approval.
_SEND_LOCK = threading.Lock()


class ExecutionRefused(Exception):
    """A step was not allowed to move money. Carries the human-readable why."""

    def __init__(self, reason: str, step: Optional[Step] = None):
        self.reason = reason
        self.step = step
        super().__init__(reason)


class Ambiguous(Exception):
    """Outcome unknown: resolve by request_id lookup before deciding anything."""

    def __init__(self, reason: str, step: Optional[Step] = None):
        self.reason = reason
        self.step = step
        super().__init__(reason)


@dataclass
class StepResult:
    """What happened to one step. `attempted` distinguishes skip from do-nothing."""

    step: Step
    attempted: bool
    airwallex_object_id: Optional[str] = None
    status: str = "SKIPPED"
    detail: str = ""
    ambiguous: bool = False
    resolved_from: Optional[str] = None   # 'lookup' when a retry found it

    @property
    def moved_money(self) -> bool:
        return self.attempted and self.airwallex_object_id is not None


@dataclass
class Executor:
    """Runs a `Plan` one step at a time, safely.

    Collaborators are injected so the whole class is testable without a network:
    `transfers`, `fx`, `resolve_supplier` (payable number → beneficiary id) and
    `position` (a callable so cash is re-read between steps rather than cached
    for the life of the plan).
    """

    db: Session
    client: AirwallexClient
    transfers: Transfers
    fx: FX
    resolve_supplier: Callable[[str], str]
    position: Callable[[], CashPosition]
    budget: Any = None                      # BudgetSettings
    rates: dict[str, Decimal] = field(default_factory=dict)
    approval: Optional[ap.Approval] = None  # required iff a step needs one
    dry_run: bool = True                     # default: propose, never move money
    results: list[StepResult] = field(default_factory=list)

    # ---- idempotency spine ------------------------------------------------
    @staticmethod
    def reserve(step_row: Optional[PlanStep]) -> Optional[str]:
        """Persist a request_id for this step and return it, reusing any prior.

        Returns None only when there is no row at all. Callers on the
        money-moving paths should use `_rid`, which cannot return None.
        """
        if step_row is None:
            return None
        session = object_session(step_row)
        if session is None:
            raise ExecutionRefused("plan step is not attached to a session")
        return _reserve_request_id(session, step_row)

    @classmethod
    def _rid(cls, step_row: PlanStep) -> str:
        """Reserve for a row we know exists. Money paths use this."""
        rid = cls.reserve(step_row)
        if rid is None:  # pragma: no cover - _require_row guarantees a row
            raise ExecutionRefused("could not reserve a request_id", None)
        return rid

    @staticmethod
    def already_done(step_row: PlanStep) -> bool:
        """True when this step already has a live Airwallex object.

        This is the hard anti-duplicate gate and it is checked before anything
        is sent. No approval, no prompt, no model can talk it out of this.
        """
        return bool(step_row.airwallex_object_id)

    # ---- gates ------------------------------------------------------------
    def _require_approval(self, step: Step, terms: ap.ApprovalTerms) -> ap.Approval:
        if self.dry_run:
            raise ExecutionRefused(
                f"dry run: would need approval: {ap.describe(terms, self.rates)}",
                step)
        approval = self.approval
        if approval is None:
            raise ExecutionRefused(
                "step requires approval and none has been granted", step)
        # Re-verified on every path, including retries.
        approval.assert_fresh(terms)
        return approval

    def _require_status(self, step_row: PlanStep) -> None:
        if step_row.status in IN_FLIGHT:
            # Do not refuse: an in-flight step from a previous process may
            # already have moved money. The caller must resolve it by lookup.
            return
        if step_row.status not in SUBMITTABLE:
            raise ExecutionRefused(
                f"step is {step_row.status.value}, which is not submittable", None)

    # ---- execution ---------------------------------------------------------
    def run(self, plan: Plan, steps: list[Step]) -> list[StepResult]:
        out: list[StepResult] = []
        for step in steps:
            out.append(self.run_step(step))
        self.results = out
        return out

    def run_step(self, step: Step) -> StepResult:
        # ONE MONEY MOVEMENT AT A TIME, IN THIS PROCESS.
        #
        # The request_id is claimed with a conditional UPDATE, so two cycles can
        # never mint two keys -- but `already_done` and the in-flight check are
        # READS, and a read followed by a send is a window. Two cycles racing one
        # approved step both read "no object id yet" and both went on to create a
        # transfer: run 7's suite caught it (two request ids, one row) and it was
        # reproducible at about one run in six. The app runs a single worker by
        # design, so an in-process lock closes the window completely, and the
        # conditional UPDATE still covers a second process.
        if step.action in (ACT_PAY, ACT_FX):
            with _SEND_LOCK:
                return (self._pay(step) if step.action == ACT_PAY
                        else self._convert(step))
        # DEFER / CHASE / ESCALATE move no money; record why, touch no API.
        return StepResult(step=step, attempted=False,
                          status="NO_ACTION", detail=step.reason)

    # -- payouts -----------------------------------------------------------
    def _require_row(self, step: Step) -> PlanStep:
        """A money movement REQUIRES a durable plan_step row.

        The request_id lives on that row, and the request_id is the only thing
        standing between a crash mid-POST and a duplicate payment. If there is
        nowhere to persist it, the step cannot be executed safely — so we
        refuse rather than send. Refusing is cheap; a double payout is not.
        """
        row = self._row_for(step)
        if row is None:
            raise ExecutionRefused(
                f"no persisted plan_step for {step.payable_number or step.action}; "
                "refusing to move money without an idempotency spine", step)
        return row

    def _pay(self, step: Step) -> StepResult:
        row = self._require_row(step)
        if self.already_done(row):
            return StepResult(step, False, row.airwallex_object_id, "ALREADY_DONE",
                              "holds a live transfer; not re-issuing",
                              resolved_from="existing_object")
        self._require_status(row)

        # A step still in flight from a previous process gets RESOLVED, never
        # re-sent. This is the crash-recovery path: look up the request_id, and
        # only if the lookup proves nothing was created do we consider sending.
        if row.status in IN_FLIGHT and not self.dry_run:
            return self._resolve_ambiguous(row, step, self._rid(row),
                                           "step was already in flight "
                                           "(possible crash): resolving by lookup")

        terms = ap.ApprovalTerms(
            step_id=row.id,
            payable_number=step.payable_number or "",
            amount_minor=step.amount_minor,
            currency=step.currency,
            counterparty=step.supplier or "",
            supplier_id=self.resolve_supplier(step.payable_number or ""),
        )
        if step.needs_approval or (step.policy_reason and "auto-pay limit" in step.policy_reason):
            self._require_approval(step, terms)

        if self.dry_run:
            return StepResult(step, False, None, "DRY_RUN",
                              f"would pay {step.amount_minor} minor {step.currency} "
                              f"to {step.supplier}")

        rid = self._rid(row)
        row.status = StepStatus.SUBMITTED
        self.db.commit()          # request_id + SUBMITTED hit disk FIRST

        try:
            obj = self.transfers.create(
                self.db, beneficiary_id=terms.supplier_id,
                currency=terms.currency, amount_minor=terms.amount_minor,
                transfer_method=step.payout_method,
                reference=step.payable_number, request_id=rid, plan_step=row)
        except AmbiguousSubmit as exc:
            return self._resolve_ambiguous(row, step, rid, str(exc))
        except ApiError as exc:
            if row is not None:
                row.status = StepStatus.CANCELLED
                row.reason = f"api error: {_error_detail(exc)}"
                self.db.commit()
            return StepResult(step, True, None, "FAILED", _error_detail(exc))

        oid = _object_id(obj)
        if row is not None:
            row.airwallex_object_id = oid
            row.status = StepStatus.SUBMITTED
            self.db.commit()
        _note_what_the_bank_said(oid, obj)
        return StepResult(step, True, oid, "SUBMITTED", "transfer created")

    # -- FX ----------------------------------------------------------------
    def _convert(self, step: Step) -> StepResult:
        # A conversion moves money too, so it needs the same durable spine.
        row = self._require_row(step)
        if self.already_done(row):
            return StepResult(step, False, row.airwallex_object_id, "ALREADY_DONE",
                              "holds a live conversion; not re-issuing",
                              resolved_from="existing_object")

        # F6: a SUBMITTED conversion with no object id is a crash the writeback
        # — the POST left, the id never came back. Resolve it by request_id
        # BEFORE quoting again, exactly like _pay does for transfers. Creating
        # a second quote and conversion would book the same money twice.
        if row.status == StepStatus.SUBMITTED and row.request_id and not self.dry_run:
            found = None
            try:
                found = self.fx.find_by_request_id(row.request_id)
            except Exception:
                found = None
            if found:
                oid = _object_id(found)
                if oid:
                    row.airwallex_object_id = oid
                    row.status = StepStatus.SUBMITTED
                    self.db.commit()
                return StepResult(step, True, oid, "SUBMITTED",
                                  "found the in-flight conversion: not resent",
                                  resolved_from="lookup")
            return StepResult(step, True, None, "AMBIGUOUS",
                              "in-flight conversion not found by request_id; "
                              "a human must decide before anything is re-booked")

        if self.dry_run:
            return StepResult(step, False, None, "DRY_RUN",
                              f"would sell {step.sell_amount_minor} minor "
                              f"{step.sell_currency} for {step.buy_currency}")

        # Fresh quote every time — quotes are single-use.
        quote = self.fx.create_quote(
            sell_currency=step.sell_currency or "USD",
            buy_currency=step.buy_currency or step.currency,
            sell_amount_minor=step.sell_amount_minor)
        rid = self._rid(row)
        row.status = StepStatus.SUBMITTED
        self.db.commit()          # request_id + SUBMITTED hit disk FIRST
        try:
            obj = self.fx.create_conversion(
                self.db, sell_currency=step.sell_currency or "USD",
                buy_currency=step.buy_currency or step.currency,
                sell_amount_minor=step.sell_amount_minor,
                quote_id=quote.quote_id, request_id=rid, plan_step=row)
        except AmbiguousSubmit as exc:
            return self._resolve_ambiguous(row, step, rid, str(exc))
        except ApiError as exc:
            row.status = StepStatus.CANCELLED
            row.reason = f"api error: {exc}"
            self.db.commit()
            return StepResult(step, True, None, "FAILED", str(exc))

        oid = _object_id(obj)
        if row is not None:
            row.airwallex_object_id = oid
            row.status = StepStatus.SUBMITTED
            self.db.commit()
        return StepResult(step, True, oid, "SUBMITTED", "conversion booked")

    # -- ambiguity ---------------------------------------------------------
    def _resolve_ambiguous(self, row: Optional[PlanStep], step: Step,
                           request_id: str, why: str) -> StepResult:
        """Outcome unknown. LOOK UP by request_id; never re-send blind."""
        log.warning("ambiguous submit rid=%s step=%s: %s", request_id,
                    step.payable_number or step.action, why)
        found = self.transfers.find_by_request_id(request_id)
        if found:
            oid = _object_id(found)
            if row is not None and oid:
                row.airwallex_object_id = oid
                row.status = StepStatus.SUBMITTED
                self.db.commit()
            return StepResult(step, True, oid, "SUBMITTED",
                              "found the original submission: not resent",
                              resolved_from="lookup")
        return StepResult(step, True, None, "AMBIGUOUS",
                          "lookup found nothing; needs a human to retry",
                          ambiguous=True)

    # -- helpers -----------------------------------------------------------
    def _row_for(self, step: Step) -> Optional[PlanStep]:
        """Find the persisted plan_step for a planner step, if there is one.

        Identity is (payable_number, action) — matching on the payable ALONE is
        a bug: one payable has both an FX step and a transfer step, so the FX
        row's `airwallex_object_id` would be adopted by the transfer, the
        payout would report ALREADY_DONE, and the supplier would never get
        paid. Caught by running the real EUR payout end to end.

        Delegates to plan_store.find_step so the writer and this reader cannot
        disagree about what identifies a step.
        """
        from .plan_store import find_step

        return find_step(self.db, step)


def _error_detail(exc: ApiError) -> str:
    """The part of an Airwallex error that actually names the problem.

    `str(ApiError)` is just "[code] message" and for validation_failed that
    message is generic — the offending fields live in the payload's
    details.errors[].source. Airwallex consistently reports the field you did
    NOT send, so surfacing it turns a dead end into a one-line fix.
    """
    parts = [str(exc)]
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    details = payload.get("details") or {}
    errors = details.get("errors") if isinstance(details, dict) else None
    if errors:
        for err in errors[:5]:
            if isinstance(err, dict):
                parts.append(f"field={err.get('source')} code={err.get('code')}")
            else:
                parts.append(str(err))
    if payload.get("message"):
        parts.append(str(payload["message"]))
    return " | ".join(parts)


def _note_what_the_bank_said(oid: str, obj) -> None:
    """Record the status the CREATE call just returned, so the page can show it.

    `transfers/create` answers with the transfer's own status, and the pill on
    Money out reads a cache that used to be filled ONLY by a background refresh.
    So the first view after a send read "UNCHECKED" for a payment we had already
    been told about: run 7 saw "Sent" beside "UNCHECKED" and could not tell
    whether the money had moved. The answer is in this function's hand, held
    nowhere else, so it is written down here.

    The dashboard is imported inside the call on purpose: it reads the domain,
    never the other way round, and a module-level import would close that loop.
    A failure is logged and swallowed: the books are already written and a
    display label is not worth failing a payment over.
    """
    status = ""
    if isinstance(obj, dict):
        status = str(obj.get("status") or "").strip().upper()
    if not (oid and status):
        return
    try:
        from ..dashboard import note_live_status
        note_live_status(str(oid), status)
    except Exception:                                  # noqa: BLE001
        log.exception("could not record the live status for %s", oid)


def _object_id(obj: Any) -> Optional[str]:
    """Pull the object id out of whatever envelope Airwallex returned."""
    if not isinstance(obj, dict):
        return None
    inner = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    for k in ("id", "transfer_id", "conversion_id", "quote_id"):
        if inner.get(k):
            return str(inner[k])
    return None


__all__ = ["Ambiguous", "ExecutionRefused", "Executor", "StepResult"]