"""LIVE approval-gated payout: PO-7001, EUR 9,000 SWIFT.

This is the last untested link in the money path. Approvals have only ever run
against fakes; this drives `assert_fresh()` with a REAL Airwallex FX quote.

Sequence, all against the live sandbox:
  1. Plan it (EUR 9,000 SWIFT ≈ USD 10,240 > the USD 2,500 auto-pay limit),
     so the planner flags needs_approval.
  2. Try to execute WITHOUT approval            -> must be refused.
  3. Try to execute with an approval bound to a DIFFERENT amount -> refused.
  4. Issue the correct approval                -> executes.
  5. Re-run                                    -> ALREADY_DONE.

The FX step is deliberately executed too, because EUR must be bought before it
can be paid — the conversion is a money movement with the same spine.

Sandbox only.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.airwallex.client import AirwallexClient  # noqa: E402
from app.airwallex.fx import FX  # noqa: E402
from app.airwallex.transfers import Transfers  # noqa: E402
from app.db import reset_default_db, session_scope  # noqa: E402
from app.domain import approvals as ap  # noqa: E402
from app.domain.executor import ExecutionRefused, Executor  # noqa: E402
from app.domain.plan_store import is_already_paid, new_run_id, persist_plan  # noqa: E402
from app.domain.planner import (ACT_FX, ACT_PAY, CashPosition, Payable,  # noqa: E402
                                build_plan)
from app.db import get_default_db  # noqa: E402
from app.domain.policy import load_budget  # noqa: E402
from app.domain.treasury import cash_position  # noqa: E402

BEN_ROTTERDAM = "6647f065-5075-412f-ba90-199e54200fe5"   # NL/EUR SWIFT, live

PAYABLE = dict(number="PO-7001", supplier="Rotterdam Freight BV", currency="EUR",
               amount_minor=900_000, criticality=5, payout_method="SWIFT")


def main() -> int:
    reset_default_db(allow_demo_file=True)
    budget = load_budget()
    # LEDGER cash must cover the conversion (~USD 10,346) or the floor blocks
    # it. The sandbox balance is deliberately not consulted: it pre-funds 10M,
    # so a payment measured against it could never be refused.
    pos = cash_position(get_default_db(), budget)
    plan = build_plan([Payable(**PAYABLE)], pos, budget)
    print("--- plan ---")
    for s in plan.steps:
        print(f"  {s.action:<6} {s.currency} {s.amount_minor/100:>10,.2f} "
              f"needs_approval={s.needs_approval}")
        print(f"         {s.reason}")

    pay = next((s for s in plan.steps if s.action == ACT_PAY), None)
    fx = next((s for s in plan.steps if s.action == ACT_FX), None)
    if pay is None:
        print("No PAY step — the planner refused to plan it at all.")
        return 1
    if not pay.needs_approval:
        print("WARNING: planner did not flag this for approval; the test would "
              "prove nothing. Check the budget/limit wiring.")

    client = AirwallexClient()
    rates = {"USD": Decimal("1"), "EUR": Decimal("0.878174"),
             "GBP": Decimal("1.27")}

    with session_scope() as s:
        if is_already_paid(s, pay):
            print("\nAlready paid — nothing to demonstrate.")
            return 0
        rows, _ = persist_plan(s, new_run_id(), plan.steps)
        by_action = {}
        for r in rows:
            import json as _j
            by_action[_j.loads(r.payload_json).get("action")] = r
        pay_row_id = by_action.get(ACT_PAY).id
        fx_row_id = by_action.get(ACT_FX).id if fx else None
        print(f"\n  persisted plan_step: pay={pay_row_id} fx={fx_row_id}")

    def executor(session, approval=None):
        return Executor(
            db=session, client=client, transfers=Transfers(client),
            fx=FX(client), resolve_supplier=lambda n: BEN_ROTTERDAM,
            position=lambda: pos, rates=rates, dry_run=False, approval=approval)

    ok = True

    # 1. No approval -> must refuse.
    print("\n--- 1. execute with NO approval (must refuse) ---")
    try:
        with session_scope() as s:
            r = executor(s).run_step(pay)
        print(f"  FAIL: executed anyway -> {r.status} {r.airwallex_object_id}")
        ok = False
    except ExecutionRefused as exc:
        print(f"  refused as expected: {exc}")

    # 2. Approval bound to the wrong amount -> must refuse.
    print("\n--- 2. approval for a DIFFERENT amount (must refuse) ---")
    wrong = ap.ApprovalTerms(
        step_id=pay_row_id, payable_number="PO-7001",
        amount_minor=900_001,                      # one cent off
        currency="EUR", counterparty="Rotterdam Freight BV",
        supplier_id=BEN_ROTTERDAM)
    bad = ap.issue(wrong, approved_by="david")
    try:
        with session_scope() as s:
            r = executor(s, bad).run_step(pay)
        print(f"  FAIL: executed anyway -> {r.status}")
        ok = False
    except ap.ApprovalStale as exc:
        print(f"  refused as expected: {exc}")

    # 3. FX first (EUR must exist before it can be paid).
    if fx is not None:
        print("\n--- 3. buy the EUR (live conversion, fresh quote) ---")
        with session_scope() as s:
            r = executor(s).run_step(fx)
        print(f"  status={r.status} object_id={r.airwallex_object_id} {r.detail}")

    # 4. Correct approval -> executes.
    print("\n--- 4. correct approval (must execute) ---")
    right = ap.ApprovalTerms(
        step_id=pay_row_id, payable_number="PO-7001",
        amount_minor=pay.amount_minor, currency=pay.currency,
        counterparty=pay.supplier or "", supplier_id=BEN_ROTTERDAM)
    print(f"  approval card: {ap.describe(right, rates)}")
    good = ap.issue(right, approved_by="david")
    with session_scope() as s:
        r4 = executor(s, good).run_step(pay)
    print(f"  status={r4.status} object_id={r4.airwallex_object_id}")
    if r4.status != "SUBMITTED":
        print(f"  FAIL: {r4.detail}")
        ok = False

    # 5. Re-run -> no-op.
    print("\n--- 5. re-run (must be ALREADY_DONE) ---")
    with session_scope() as s:
        r5 = executor(s, good).run_step(pay)
    print(f"  status={r5.status}")
    if r5.status != "ALREADY_DONE":
        print("  FAIL: re-run was not a no-op")
        ok = False

    print("\n--- Airwallex transfers ---")
    for t in Transfers(client).list()[:6]:
        print(f"  {t.get('id')} {t.get('transfer_amount')} "
              f"{t.get('transfer_currency')} src={t.get('source_currency')} "
              f"status={t.get('status')}")
    client.close()
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())