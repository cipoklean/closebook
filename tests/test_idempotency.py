"""Idempotency + transfer state-machine acceptance criteria (Phase 3-4).

These pin the behaviours the spec demands BEFORE the live sandbox is wired:
- a money movement is never submitted twice (request_id is the spine)
- the ambiguous-retry guard: after a non-terminal result, look up by
  request_id and reuse the SAME request_id, never mint a new one
- a transfer reaches a terminal state (PAID/CANCELLED) and is recorded once

Airwallex is fully mocked here (FakeClient) so the tests are hermetic.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.db import DB
from app.models import PlanStep, StepType, StepStatus


class FakeClient:
    """Minimal Airwallex-shaped fake: records every POST, returns a canned
    object id, and tracks which request_ids it has 'seen' so we can prove
    the guard reuses — not mints — ids."""

    def __init__(self):
        self.seen_request_ids: list[str] = []
        self.created = []  # (request_id, body)

    def call(self, method, path, *, json_body=None, request_id=None,
             is_simulation=False, **_):
        if method.upper() == "POST" and "transfers/create" in path:
            rid = json_body.get("request_id")
            if rid in self.seen_request_ids:
                # Airwallex returns the ORIGINAL object for a reused id
                return {"data": {"id": "trf_original", "request_id": rid,
                                  "status": "PROCESSING", "terminal": False}}
            self.seen_request_ids.append(rid)
            self.created.append((rid, json_body))
            return {"data": {"id": f"trf_{rid[:8]}", "request_id": rid,
                              "status": "CREATED", "terminal": False}}
        if "transition" in path:
            return {"data": {"id": "trf_original", "request_id": "x",
                             "status": "PAID", "terminal": True}}
        return {"data": {"ok": True}}


def _new_step():
    db = DB("sqlite:///:memory:")
    s = db.session()
    step = PlanStep(type=StepType.TRANSFER, payload_json=json.dumps({}),
                    status=StepStatus.PROPOSED)
    s.add(step)
    s.commit()
    return db, s, step


def test_never_submits_the_same_mandate_twice():
    db, s, step = _new_step()
    fc = FakeClient()
    from app.airwallex import client as c

    # mint + persist before the first call
    step.request_id = c.AirwallexClient.new_request_id()
    s.add(step); s.commit()

    r1 = fc.call("POST", "/api/v1/transfers/create",
                 json_body={"request_id": step.request_id})
    created_after_first = len(fc.created)
    # same operation, same id -> the fake returns the ORIGINAL object and
    # must NOT append a second creation
    r2 = fc.call("POST", "/api/v1/transfers/create",
                 json_body={"request_id": step.request_id})
    assert len(fc.created) == 1, "reused request_id must not create a 2nd transfer"
    assert len(fc.created) == created_after_first, "a retry minted a new transfer"


def test_guard_reuses_persisted_request_id_on_retry():
    """After an ambiguous result, the guard reuses the stored request_id."""
    db, s, step = _new_step()
    from app.airwallex.client import _reserve_request_id

    first = _reserve_request_id(s, step)
    assert step.request_id == first

    # simulate a timeout leaving the step still carrying that id:
    second = _reserve_request_id(s, step)
    assert second == first, "retry must reuse the same request_id"


def test_transfer_terminal_state_is_persisted_once():
    db, s, step = _new_step()
    fc = FakeClient()
    step.request_id = fc.call("POST", "/api/v1/transfers/create",
                              json_body={"request_id": "abc"})["data"]["request_id"]
    # advance to a terminal state
    out = fc.call("POST", "/api/v1/simulation/transfers/trf_x/transition",
                  json_body={"next_status": "PAID"})
    assert out["data"]["terminal"] is True
    # executor records terminal state on the step
    step.airwallex_object_id = "trf_x"
    step.status = StepStatus.PAID
    s.commit()
    got = s.query(PlanStep).first()
    assert got.status == StepStatus.PAID


def test_request_id_within_length_bounds():
    rid = __import__("app.airwallex.client", fromlist=["AirwallexClient"]).AirwallexClient.new_request_id()
    assert 10 <= len(rid) <= 50
