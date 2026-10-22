"""Re-plan after every event, then execute only what is unambiguously clear.

THE LOOP
--------
Cash arrives, a human approves, a transfer settles — each of those changes what
is affordable, so each re-runs the planner and acts on the NEW answer. A plan
computed once is a plan that goes stale on the first deposit, and the operator
then reads a page describing a position that no longer exists.

WHAT MAY EXECUTE AUTOMATICALLY
------------------------------
Steps arrive from `planner.build_plan`, which already carries the policy verdict:
`blockers` when a step must not happen, `needs_approval` when a human must agree.
`clear_steps()` returns a step only when EVERY one of these holds, and each
failure is a refusal with its own words rather than one silent skip:

  * it moves money (FX or PAY — DEFER/CHASE/ESCALATE move none)
  * it has no blockers
  * it does not need approval
  * a persisted `plan_step` exists for it (the idempotency spine)
  * it does not already hold an `airwallex_object_id`

Everything else is HELD with the reason attached. A held payment is a normal
outcome of a treasury, not a failure, and the page says so.

THE MODEL IS NOT IN THIS FILE. There is no path from here to a language model,
and there is no path from a language model to here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..db import DB, session_scope
from .executor import ExecutionRefused, Executor, StepResult
from .planner import ACT_DEFER, ACT_ESCALATE, ACT_FX, ACT_PAY, Plan, Step
from .plan_store import (find_step, has_retired_attempt, new_run_id,
                         persist_plan)

log = logging.getLogger("closebook.conductor")

#: The only actions that can move money. Anything else is narrated, never sent.
MONEY_ACTIONS = (ACT_FX, ACT_PAY)


@dataclass
class Verdict:
    """What happened to one step, in words an operator can act on."""

    payable_number: str
    action: str
    outcome: str            # EXECUTED | HELD | ALREADY_DONE | REFUSED | NO_ACTION
    detail: str
    object_id: Optional[str] = None

    @property
    def moved_money(self) -> bool:
        return self.outcome == "EXECUTED" and self.object_id is not None

    def as_dict(self) -> dict[str, Any]:
        return {"payable_number": self.payable_number, "action": self.action,
                "outcome": self.outcome, "detail": self.detail,
                "object_id": self.object_id}


@dataclass
class Run:
    """One pass of the loop, after one event."""

    run_id: str
    source: str
    plan: Plan
    verdicts: list[Verdict] = field(default_factory=list)

    def of(self, outcome: str) -> list[Verdict]:
        return [v for v in self.verdicts if v.outcome == outcome]

    @property
    def executed(self) -> list[Verdict]:
        return self.of("EXECUTED")

    @property
    def held(self) -> list[Verdict]:
        return self.of("HELD") + self.of("REFUSED")

    def summary(self) -> str:
        if not self.verdicts:
            return f"re-planned after {self.source}: nothing to do"
        parts = []
        for outcome, word in (("EXECUTED", "executed"),
                              ("HELD", "held"), ("REFUSED", "refused"),
                              ("ALREADY_DONE", "already done")):
            n = len(self.of(outcome))
            if n:
                parts.append(f"{n} {word}")
        return f"re-planned after {self.source}: " + ", ".join(parts)


# ---------------------------------------------------------------- selection
def clear_steps(plan: Plan) -> list[Step]:
    """The steps that may move money with nobody watching.

    Deliberately strict: anything surprising is left for a human, because the
    cost of a held payment is a delay and the cost of a wrong one is money.
    """
    out = []
    for s in plan.steps:
        if s.action not in MONEY_ACTIONS:
            continue
        if s.blockers:
            continue
        if s.needs_approval:
            continue
        out.append(s)
    return out


def held_steps(plan: Plan) -> list[tuple[Step, str]]:
    """Every payable whose money is NOT moving, with the reason why.

    Two shapes, and missing either makes the report lie by omission:

      * a PAY/FX step that exists but must wait — over the auto-pay limit (a
        human has to agree) or carrying blockers;
      * a DEFER/ESCALATE step — the planner's answer for a floor breach or a
        missing conversion. These are NOT `MONEY_ACTIONS`, so a filter on the
        action alone silently drops them and the loop then reports "nothing to
        do" while a supplier waits. It never sends them; it just has to say so.
    """
    out: list[tuple[Step, str]] = []
    for s in plan.steps:
        if s.action in MONEY_ACTIONS:
            if s.needs_approval:
                out.append((s, s.approval_reason or s.reason
                            or "needs your approval"))
            elif s.blockers:
                out.append((s, "; ".join(s.blockers)))
            continue
        if s.action in (ACT_DEFER, ACT_ESCALATE) and s.payable_number:
            out.append((s, s.reason or "not being paid this run"))
    return out


# ------------------------------------------------------------------ wiring
def _supplier_resolver(session) -> Callable[[str], str]:
    """payable number -> Airwallex beneficiary id, read fresh from the book.

    A beneficiary is created once through Setup and stored on the supplier, so
    this is a lookup rather than anything that could invent an account.
    """
    from ..models import InvoicePayable, Supplier

    def resolve(number: str) -> str:
        row = (session.query(InvoicePayable)
               .filter(InvoicePayable.number == number).first())
        if row is None:
            return "unknown"
        sup = session.get(Supplier, row.supplier_id)
        if sup is None or not sup.airwallex_beneficiary_id:
            return "unknown"
        return sup.airwallex_beneficiary_id

    return resolve


def _beneficiary_gap(session, step: Step) -> Optional[str]:
    """Why this payable cannot be paid yet, in words, or None if it can.

    The executor hands `resolve_supplier()`'s answer straight to Airwallex as
    `beneficiary_id`, so a supplier with no beneficiary would reach the API as
    the literal string "unknown" — an error from the bank about an id that never
    existed, which is a worse thing to show an operator than the plain fact.
    """
    if step.action != ACT_PAY:
        return None
    if _supplier_resolver(session)(step.payable_number or "") != "unknown":
        return None
    return (f"{step.supplier or step.payable_number} has no Airwallex beneficiary "
            f"yet, so there is nowhere to send it")


def _sendability_gap(session, step: Step) -> Optional[str]:
    """Why this clear-looking step still must not be sent, or None.

    A refusal here is a result, not an error: the payment waits, visibly, with
    the reason attached.
    """
    if step.action == ACT_PAY:
        gap = _beneficiary_gap(session, step)
        if gap:
            return gap
    if has_retired_attempt(session, step):
        return (f"an earlier payment for {step.payable_number} was returned or "
                f"failed, so this is a replacement — it needs your approval and "
                f"a new payment reference before it can be sent")
    return None


def _budget_and_position(db: DB):
    """The workspace's OWN policy, and the position measured against it.

    The budget comes from the workspace row (`settings_store`), not the vendored
    JSON — the floor and the auto-pay limit are the operator's, set on the
    Settings screen, and the executor must obey the ones on screen. Passing a
    Session to `load_budget(path=...)` silently fell back to the file defaults.
    """
    from .settings_store import workspace_budget
    from .treasury import cash_position

    s = db.session()
    budget = workspace_budget(s)
    return budget, cash_position(db, budget, session=s)


# ------------------------------------------------------------------- the run
def on_event(db: DB, *, source: str, dry_run: bool = True,
             execute: bool = True,
             client: Any = None,
             executor_factory: Optional[Callable[..., Executor]] = None) -> Run:
    """Re-plan from live state, then execute whatever is clear.

    `dry_run=True` is the default and it is the SAFE default: a caller that wants
    money moved has to say so explicitly, twice (`execute=True, dry_run=False`),
    and the whole thing is refused outside sandbox by the caller.

    Returns a `Run` describing every step it considered. Nothing here raises on a
    held payment — a refusal is a result.
    """
    from .treasury import current_plan

    run_id = new_run_id()
    # Re-plan FIRST, from live state, so the page and the executor are looking at
    # the same picture. `current_plan` re-reads cash, payables and the budget.
    plan = current_plan(db)
    run = Run(run_id=run_id, source=source, plan=plan)

    clear = clear_steps(plan)
    for step, why in held_steps(plan):
        run.verdicts.append(Verdict(
            payable_number=step.payable_number or "",
            action=step.action, outcome="HELD", detail=why))

    # A step can be clear by POLICY and still be unsendable. Two reasons, both
    # checked BEFORE anything is persisted, so a payable that cannot be sent
    # never even gets a plan_step:
    #   * nobody to pay it to;
    #   * an earlier attempt already failed. A replacement is a NEW operation,
    #     not a retry — the original request_id is spent and the terms were
    #     approved for a payment that did not happen — so it needs a human.
    with session_scope() as s:
        gaps = {_key(step): _sendability_gap(s, step) for step in clear}
    for step in list(clear):
        why = gaps.get(_key(step))
        if why:
            clear.remove(step)
            run.verdicts.append(Verdict(
                payable_number=step.payable_number or "", action=step.action,
                outcome="HELD", detail=why))

    if not execute or dry_run:
        for step in clear:
            run.verdicts.append(Verdict(
                payable_number=step.payable_number or "", action=step.action,
                outcome="HELD",
                detail=("dry run — the plan is clear to pay this, but this call "
                        "was not asked to move money")))
        return run

    if client is None:
        from ..config import get_settings
        from ..airwallex.client import AirwallexClient
        if not get_settings().sandbox_ready():
            for step in clear:
                run.verdicts.append(Verdict(
                    payable_number=step.payable_number or "", action=step.action,
                    outcome="REFUSED",
                    detail="no sandbox credentials — nothing was sent"))
            return run
        client = AirwallexClient()

    # Persist BEFORE sending: the request_id on that row is the idempotency
    # spine, and a payment with nowhere to record its identity must not be sent.
    with session_scope() as s:
        rows, skipped = persist_plan(s, run_id, clear)
        row_ids = {_key(step): row.id for step, row in zip(clear, rows)}
        skipped_keys = {_key(step) for step in skipped}

    for step in clear:
        if _key(step) in skipped_keys:
            run.verdicts.append(Verdict(
                payable_number=step.payable_number or "", action=step.action,
                outcome="ALREADY_DONE",
                detail="this payable already has a live Airwallex object; "
                       "nothing was re-sent"))
            continue
        run.verdicts.append(_run_one(db, client, step, row_ids.get(_key(step)),
                                      executor_factory))

    log.info("conductor %s", run.summary())
    return run


def _key(step: Step) -> tuple[str, str]:
    """A step's identity, matching `plan_store`: (payable_number, action)."""
    return (step.payable_number or "", step.action)


def _run_one(db: DB, client: Any, step: Step, row_id: Optional[int],
             executor_factory: Optional[Callable[..., Executor]] = None) -> Verdict:
    """Send one clear step through the executor — the only thing that pays."""
    from ..airwallex.fx import FX
    from ..airwallex.transfers import Transfers

    if row_id is None:
        return Verdict(
            payable_number=step.payable_number or "", action=step.action,
            outcome="REFUSED",
            detail="no persisted plan step, so there is nowhere to record a "
                   "request_id — refusing rather than sending unprotected")

    budget, position = _budget_and_position(db)
    rates = position.rates

    try:
        with session_scope() as s:
            from ..models import PlanStep
            row = s.get(PlanStep, row_id)   # noqa: F841 - existence check
            build = executor_factory or Executor
            executor = build(
                db=s, client=client, transfers=Transfers(client), fx=FX(client),
                resolve_supplier=_supplier_resolver(s),
                position=lambda: _budget_and_position(db)[1],
                rates=rates,
                budget=budget,
                dry_run=False,
            )
            result: StepResult = executor.run_step(step)
    except ExecutionRefused as exc:
        return Verdict(payable_number=step.payable_number or "",
                       action=step.action, outcome="REFUSED",
                       detail=str(exc)[:300])
    except Exception as exc:  # noqa: BLE001
        # A dead API must not take the loop down; the step stays unresolved and
        # the next pass re-reads it.
        return Verdict(payable_number=step.payable_number or "",
                       action=step.action, outcome="REFUSED",
                       detail=f"{type(exc).__name__}: {exc}"[:300])

    if result.airwallex_object_id is None:
        return Verdict(payable_number=step.payable_number or "",
                       action=step.action, outcome="HELD",
                       detail=result.detail or result.status)
    return Verdict(payable_number=step.payable_number or "", action=step.action,
                   outcome="EXECUTED", detail=result.detail or result.status,
                   object_id=result.airwallex_object_id)


__all__ = ["Verdict", "Run", "clear_steps", "held_steps", "on_event"]
