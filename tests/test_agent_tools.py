"""The agent may CALL tools. It may never move money.

Audit item 6. The structural claim is stronger than a policy check: there is no
`pay_invoice`, `convert_fx` or `create_transfer` tool to call, so the agent
cannot express a payment even if it wants to.
"""
from __future__ import annotations

import pytest

from app.domain.agent_tools import (
    ALLOWED_TOOLS, Agent, ToolCall, ToolResult, Trace, Verdict,
)


def _tools():
    return {
        "list_open_invoices": lambda: {"invoices": []},
        "propose_match": lambda **kw: ToolResult(
            "propose_match", ok=True, verdict=Verdict.ACCEPTED,
            output={"model_proposed": kw}),
        "explain_decision": lambda **kw: {"explanation": kw.get("decision", "")},
        "request_approval": lambda **kw: {"requested": kw.get("payable_number")},
        "read_plan": lambda: {"recent_steps": []},
    }


# --- the structural boundary -----------------------------------------
def test_no_tool_can_move_money():
    """The allowlist contains no payment tool. This is the whole safety argument."""
    for forbidden in ("pay_invoice", "create_transfer", "convert_fx",
                      "transfer", "payout", "execute", "send_money"):
        assert forbidden not in ALLOWED_TOOLS, forbidden
    assert ALLOWED_TOOLS == {
        "list_open_invoices", "propose_match", "explain_decision",
        "request_approval", "read_plan"}


def test_agent_refuses_to_be_constructed_with_a_payment_tool():
    """Even a well-meaning caller cannot add one."""
    tools = _tools()
    tools["pay_invoice"] = lambda **kw: {"paid": True}
    with pytest.raises(ValueError, match="allowed set"):
        Agent(tools)


@pytest.mark.parametrize("name", ["pay_invoice", "create_transfer", "convert_fx",
                                  "execute_transfer", "anything_else"])
def test_agent_refuses_unlisted_tool_at_call_time(name):
    agent = Agent(_tools())
    result = agent.call(ToolCall(name, {"amount": 100}))
    assert result.verdict is Verdict.REFUSED
    assert result.ok is False
    assert "no tool capable of moving money" in result.detail


# --- allowed tools work ----------------------------------------------
@pytest.mark.parametrize("name", sorted(ALLOWED_TOOLS))
def test_every_allowed_tool_is_callable(name):
    agent = Agent(_tools())
    args = {} if name in {"list_open_invoices", "read_plan"} else {
        "decision": "x", "payable_number": "PO-1", "amount_minor": 100,
        "currency": "USD"}
    result = agent.call(ToolCall(name, args))
    assert result.verdict is not Verdict.REFUSED


# --- failures escalate, they do not crash ----------------------------
def test_bad_arguments_escalate_rather_than_500():
    """A model calling a tool with the wrong argument names must not 500. The
    real `propose_match` takes keyword-only params, so unexpected ones raise
    TypeError — which the agent converts to an escalation."""
    def strict_propose(*, amount_minor: int, currency: str):
        return {"ok": True}

    tools = _tools()
    tools["propose_match"] = strict_propose
    agent = Agent(tools)
    result = agent.call(ToolCall("propose_match", {"wrong_field": 1}))
    assert result.verdict is Verdict.ESCALATED
    assert "bad arguments" in result.detail
    assert result.inputs == {"wrong_field": 1}, (
        "the arguments the model supplied must be recorded for audit")


def test_raising_tool_escalates_rather_than_propagating():
    def explode(**_kw):
        raise RuntimeError("provider is down")

    tools = _tools()
    tools["propose_match"] = explode
    agent = Agent(tools)
    result = agent.call(ToolCall("propose_match", {"amount_minor": 1}))
    assert result.verdict is Verdict.ESCALATED
    assert "tool error" in result.detail


def test_allowed_but_unwired_tool_is_refused_not_crashed():
    agent = Agent({"list_open_invoices": lambda: {}})
    result = agent.call(ToolCall("read_plan"))
    assert result.verdict is Verdict.REFUSED


# --- the trace --------------------------------------------------------
def test_every_call_is_recorded():
    agent = Agent(_tools())
    agent.call(ToolCall("list_open_invoices"))
    agent.call(ToolCall("pay_invoice", {"amount": 1}))   # refused, still recorded
    assert len(agent.trace.rows) == 2
    assert agent.trace.rows[1].verdict is Verdict.REFUSED


def test_trace_records_the_verdict_not_the_models_opinion():
    """The verdict column is what CODE concluded, and is the only verdict."""
    agent = Agent(_tools())
    r = agent.call(ToolCall("propose_match", {"amount_minor": 440_000,
                                              "currency": "USD"}))
    assert r.verdict is Verdict.ACCEPTED
    assert agent.trace.rows[0].verdict is Verdict.ACCEPTED


def test_trace_summary_is_readable(tmp_path):
    agent = Agent(_tools())
    agent.call(ToolCall("list_open_invoices"))
    assert "list_open_invoices" in agent.trace.summary()


def test_trace_persists_to_agent_trace(tmp_path):
    """Audit 6: every tool call is logged with inputs, outputs and verdict."""
    from app.db import DB
    from app.models import AgentTrace

    db = DB(f"sqlite:///{tmp_path}/trace.sqlite3")
    agent = Agent(_tools(), run_id="run-1")
    agent.call(ToolCall("propose_match", {"amount_minor": 440_000,
                                          "currency": "USD"}))
    agent.call(ToolCall("list_open_invoices"))

    rows = agent.trace.persist(db.session())
    assert len(rows) == 2

    with db.session() as s:
        stored = s.query(AgentTrace).all()
    assert len(stored) == 2
    tools = {r.tool for r in stored}
    assert tools == {"propose_match", "list_open_invoices"}
    for r in stored:
        assert r.run_id == "run-1"
        assert r.verdict in {"accepted", "rejected", "escalated", "refused"}
        assert r.outputs_json


def test_trace_persists_inputs_separately_from_outputs(tmp_path):
    from app.db import DB
    from app.models import AgentTrace

    db = DB(f"sqlite:///{tmp_path}/trace2.sqlite3")
    agent = Agent(_tools())
    agent.call(ToolCall("propose_match", {"amount_minor": 440_000,
                                          "currency": "USD"}))
    agent.trace.persist(db.session())
    with db.session() as s:
        row = s.query(AgentTrace).filter_by(tool="propose_match").one()
    assert row.inputs_json and "440000" in row.inputs_json
    assert row.outputs_json and "model_proposed" in row.outputs_json


def test_refused_calls_are_persisted_too(tmp_path):
    """A refused call is exactly what a judge wants to see in the trace."""
    from app.db import DB
    from app.models import AgentTrace

    db = DB(f"sqlite:///{tmp_path}/trace3.sqlite3")
    agent = Agent(_tools())
    agent.call(ToolCall("pay_invoice", {"amount": 100}))
    agent.trace.persist(db.session())
    with db.session() as s:
        rows = s.query(AgentTrace).all()
    assert len(rows) == 1
    assert rows[0].verdict == "refused"
