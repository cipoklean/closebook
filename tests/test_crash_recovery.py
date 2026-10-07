"""ACCEPTANCE: kill mid-transfer, restart, no duplicate payment.

The single most expensive bug this system could have is paying a supplier
twice. This test simulates the worst realistic shape of it:

  1. The process dies AFTER the POST left the machine but BEFORE the
     airwallex_object_id was written back.
  2. A brand new Executor starts from the database — nothing in memory.
  3. It re-runs the same plan step.

The step must NOT create a second transfer. It must find the first one by
request_id and adopt it.

Note what is simulated here: the "crash" is the loss of the in-memory response,
which is exactly the window where a naive implementation re-sends. Nothing in
this test relies on network failure modes to prove the guarantee.
"""
from __future__ import annotations

import json

from app.db import reset_default_db, session_scope
from app.domain.executor import Executor
from app.domain.planner import ACT_PAY, CashPosition, Step
from app.models import PlanStep, StepStatus, StepType
from app.domain.policy import CommittedCash

from tests.test_executor import FakeClient, FakeTransfers, _pay_step


def _position():
    return CashPosition(balances={"USD": 4_000_000},
                        committed=CommittedCash("USD", 4_000_000, 0), rates={})


def _executor(session, transfers):
    from app.airwallex.fx import FX
    return Executor(
        db=session, client=FakeClient(), transfers=transfers,
        fx=FX(FakeClient()), resolve_supplier=lambda n: "bn_1",
        position=_position, rates={"USD": 1.0, "EUR": 0.878174},
        dry_run=False)


def _seed_step() -> int:
    with session_scope() as s:
        row = PlanStep(type=StepType.TRANSFER,
                       payload_json=json.dumps({"payable_number": "PO-7003"}),
                       status=StepStatus.PROPOSED)
        s.add(row)
        s.commit()
        return row.id


def test_crash_after_send_before_writeback_does_not_double_pay():
    reset_default_db()
    step_id = _seed_step()

    # ---- crash window: request_id is on disk, the POST is in flight, and the
    # process dies before airwallex_object_id is written. ----
    with session_scope() as s:
        row = s.get(PlanStep, step_id)
        crashed = FakeTransfers()
        ex = _executor(s, crashed)
        from app.airwallex.client import _reserve_request_id
        rid = _reserve_request_id(s, row)
        row.status = StepStatus.SUBMITTED
        s.commit()
        # Airwallex did receive it — this is what the lookup will find.
        crashed._existing = {"id": "tr_crashed_1", "status": "SENT",
                             "request_id": rid}
        assert row.airwallex_object_id is None, "the writeback never happened"

    # ---- restart: brand new objects, re-run the same step ----
    creates: list[dict] = []

    class CountingTransfers(FakeTransfers):
        def create(self, db_session, **kw):
            creates.append(kw)
            raise AssertionError("must not create a second transfer")

    with session_scope() as s:
        ex = _executor(s, CountingTransfers(existing={"id": "tr_crashed_1",
                                                      "status": "SENT",
                                                      "request_id": rid}))
        result = ex.run_step(_pay_step())

    assert creates == [], "a duplicate transfer was created after the crash"
    assert result.airwallex_object_id == "tr_crashed_1"
    assert result.resolved_from == "lookup"


def test_second_run_after_a_successful_writeback_is_a_no_op():
    """The happy path's own duplicate guard: run twice, pay once."""
    reset_default_db()
    _seed_step()
    t = FakeTransfers()
    with session_scope() as s:
        first = _executor(s, t).run_step(_pay_step())
    with session_scope() as s:
        second = _executor(s, t).run_step(_pay_step())

    creates = [c for c in t.calls if "lookup" not in c]
    assert len(creates) == 1
    assert first.airwallex_object_id == second.airwallex_object_id == "tr_new_1"
    assert second.status == "ALREADY_DONE"


def test_persisted_request_id_survives_a_restart_and_is_reused():
    """The id is the recovery key, so it must outlive the process."""
    reset_default_db()
    step_id = _seed_step()
    from app.airwallex.client import _reserve_request_id
    with session_scope() as s:
        row = s.get(PlanStep, step_id)
        first_id = _reserve_request_id(s, row)
        s.commit()
    with session_scope() as s:
        row = s.get(PlanStep, step_id)
        assert _reserve_request_id(s, row) == first_id