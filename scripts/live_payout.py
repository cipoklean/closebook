"""FIRST LIVE MONEY MOVEMENT through the real executor.

Run: .venv/bin/python scripts/live_payout.py [PO-7003]

This is the acceptance test that matters: everything before it was fakes.

It does, in order:
  1. Build the real plan from the real committed-cash budget.
  2. Persist a real `plan_step` for the chosen payable — the idempotency spine.
  3. Execute it through the real `Executor` against the live sandbox, using
     the beneficiary created by scripts/probe_beneficiary_create.py.
  4. RE-RUN the same step to prove it is a no-op and no duplicate payment is
     created.
  5. Print the request_id and object id so both can be checked against the
     Airwallex dashboard afterwards.

Sandbox only. No real money can move.
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.airwallex.client import AirwallexClient  # noqa: E402
from app.airwallex.fx import FX  # noqa: E402
from app.airwallex.transfers import Transfers  # noqa: E402
from app.db import reset_default_db, session_scope  # noqa: E402
from app.domain.executor import ExecutionRefused, Executor  # noqa: E402
from app.domain.planner import ACT_PAY, CashPosition, build_plan  # noqa: E402
from app.domain.policy import CommittedCash, load_budget  # noqa: E402
from app.models import PlanStep, StepStatus, StepType  # noqa: E402

# Real, live-created beneficiary ids (see scripts/probe_beneficiary_create.py).
BENEFICIARIES = {
    "PO-7003": "9f187b28-9973-4535-8001-68cacc8a3cd8",   # Atlanta Packaging, US/USD LOCAL
    "PO-7001": "6647f065-5075-412f-ba90-199e54200fe5",   # Rotterdam Freight, NL/EUR SWIFT
}

PAYABLES = [
    dict(number="PO-7003", supplier="Atlanta Packaging", currency="USD",
         amount_minor=180_000, criticality=2, payout_method="LOCAL"),
]


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "PO-7003"
    reset_default_db()
    budget = load_budget()
    pos = CashPosition(
        balances={"USD": 10_120_000, "GBP": 10_018_000},
        committed=budget.committed,
        rates={"USD": Decimal("1"), "GBP": Decimal("1.27"),
               "EUR": Decimal("0.878174")},
    )

    from app.domain.planner import Payable
    plan = build_plan([Payable(**p) for p in PAYABLES], pos, budget)
    pay_steps = [s for s in plan.steps if s.action == ACT_PAY
                 and s.payable_number == target]
    if not pay_steps:
        print(f"No PAY step for {target}. Plan:")
        print(plan.summary())
        return 1
    step = pay_steps[0]
    beneficiary_id = BENEFICIARIES.get(target)
    if not beneficiary_id:
        print(f"No beneficiary id known for {target}")
        return 1

    print(f"--- plan step ---")
    print(f"  {step.payable_number} {step.amount_minor/100:.2f} {step.currency} "
          f"-> {step.supplier}")
    print(f"  reason: {step.reason}")
    print(f"  needs_approval={step.needs_approval}")

    client = AirwallexClient()
    # 2. Persist the spine BEFORE any money moves.
    with session_scope() as s:
        row = PlanStep(type=StepType.TRANSFER,
                       payload_json=json.dumps({"payable_number": target,
                                                "supplier": step.supplier}),
                       status=StepStatus.PROPOSED)
        s.add(row)
        s.commit()
        step_row_id = row.id

    def build_executor(session):
        return Executor(
            db=session, client=client, transfers=Transfers(client), fx=FX(client),
            resolve_supplier=lambda n: BENEFICIARIES.get(n, "unknown"),
            position=lambda: pos,
            rates={"USD": Decimal("1"), "EUR": Decimal("0.878174"),
                   "GBP": Decimal("1.27")},
            dry_run=False,
        )

    # 3. First execution — moves money.
    print(f"\n--- execution 1 (LIVE) ---")
    try:
        with session_scope() as s:
            ex = build_executor(s)
            r1 = ex.run_step(step)
        print(f"  status={r1.status} object_id={r1.airwallex_object_id}")
        print(f"  detail={r1.detail}")
    except ExecutionRefused as exc:
        print(f"  REFUSED: {exc}")
        return 1

    with session_scope() as s:
        row = s.get(PlanStep, step_row_id)
        print(f"  persisted: request_id={row.request_id}")
        print(f"  persisted: object_id={row.airwallex_object_id} status={row.status.value}")

    # 4. Re-run: must be a no-op.
    print(f"\n--- execution 2 (must be a NO-OP) ---")
    with session_scope() as s:
        ex = build_executor(s)
        r2 = ex.run_step(step)
    print(f"  status={r2.status} object_id={r2.airwallex_object_id}")
    print(f"  detail={r2.detail}")
    if r2.status == "ALREADY_DONE":
        print("  PASS: no duplicate payment created.")
    else:
        print("  FAIL: re-run was not a no-op!")
        return 1

    # 5. Confirm on Airwallex's side.
    print(f"\n--- Airwallex view of our transfers ---")
    for t in Transfers(client).list()[:5]:
        print(f"  {t.get('id')} rid={t.get('request_id')} "
              f"{t.get('transfer_amount')} {t.get('transfer_currency')} "
              f"status={t.get('status')}")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())