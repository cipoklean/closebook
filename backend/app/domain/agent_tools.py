"""A tool-calling agent that can propose but never move money.

The design rule is the thesis of the whole project, made structural: the model
is given exactly five tools and **none of them can pay anyone**.

    list_open_invoices  — read
    propose_match       — propose (always re-validated in code)
    explain_decision    — narrate a decision already made
    request_approval    — ask a human
    read_plan           — read

There is deliberately no `pay_invoice`, `convert_fx` or `create_transfer`
tool. The agent cannot express a payment, so it cannot make one. That is a
stronger guarantee than a policy check on a payment tool, because a policy
check can be mis-ordered or bypassed and a missing tool cannot.

Every call is recorded to `agent_trace` with its inputs, its outputs and the
**deterministic verdict** — what the code actually concluded, which is the only
verdict that counts.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

log = logging.getLogger("closebook.agent")

# The only tools the agent may call. Anything else is refused by name.
ALLOWED_TOOLS = frozenset({
    "list_open_invoices",
    "propose_match",
    "explain_decision",
    "request_approval",
    "read_plan",
})


class Verdict(str, Enum):
    ACCEPTED = "accepted"      # code agreed with the model
    REJECTED = "rejected"      # code refused; the proposal was wrong or unsafe
    ESCALATED = "escalated"    # a human must decide
    REFUSED = "refused"        # the tool itself is not permitted


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    tool: str
    ok: bool
    output: Any = None
    verdict: Verdict = Verdict.ACCEPTED
    detail: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"tool": self.tool, "ok": self.ok, "output": self.output,
                "verdict": self.verdict.value, "detail": self.detail,
                "inputs": self.inputs}


class Trace:
    """Collects tool calls so they can be persisted and rendered."""

    def __init__(self, run_id: Optional[str] = None):
        self.run_id = run_id
        self.rows: list[ToolResult] = []

    def record(self, result: ToolResult) -> ToolResult:
        self.rows.append(result)
        return result

    def persist(self, db_session) -> list:
        """Write every call to agent_trace.

        `inputs` are the arguments the model supplied — the thing a judge needs
        to audit, and what was previously missing: only the tool's own output
        was stored, so a proposal could not be checked against what was asked.
        `outputs` is what the tool returned; `verdict` is what CODE concluded.
        """
        from ..models import AgentTrace
        written = []
        for r in self.rows:
            row = AgentTrace(
                run_id=self.run_id,
                tool=r.tool,
                inputs_json=json.dumps(_safe(r.inputs), default=str)[:4000],
                outputs_json=json.dumps(_safe(r.output), default=str)[:4000],
                verdict=r.verdict.value,
                detail=r.detail[:2000] if r.detail else None,
            )
            db_session.add(row)
            written.append(row)
        db_session.commit()
        return written

    def summary(self) -> str:
        lines = []
        for r in self.rows:
            lines.append(f"  {r.tool} -> {r.verdict.value}"
                         + (f" ({r.detail})" if r.detail else ""))
        return "\n".join(lines) or "  (no tool calls)"


def _safe(value: Any) -> Any:
    """JSON-safe copy. Webhook/API payloads contain non-serialisable leaves."""
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class Agent:
    """A typed-tool agent. It may only CALL the five tools."""

    def __init__(self, tools: dict[str, Callable[..., Any]], *,
                 trace: Optional[Trace] = None, run_id: Optional[str] = None):
        unknown = set(tools) - ALLOWED_TOOLS
        if unknown:
            raise ValueError(
                f"refusing to register tools outside the allowed set: "
                f"{sorted(unknown)}. The agent must have no tool capable of "
                f"moving money.")
        self._tools = tools
        self.trace = trace or Trace(run_id)

    def call(self, call: ToolCall) -> ToolResult:
        """Execute one tool call, recording the deterministic verdict.

        A tool not in the allowlist is refused by NAME before it runs — this is
        the structural boundary, not a policy check.

        The arguments the model supplied are attached to the result either way,
        so a refused call still shows what it tried to do.
        """
        if call.name not in ALLOWED_TOOLS:
            result = ToolResult(
                call.name, ok=False, verdict=Verdict.REFUSED,
                inputs=dict(call.arguments),
                detail=(f"'{call.name}' is not an allowed tool. The agent may "
                        f"only call {sorted(ALLOWED_TOOLS)}; it has no tool "
                        f"capable of moving money."))
            log.warning("AGENT REFUSED %s", call.name)
            return self.trace.record(result)

        if call.name not in self._tools:
            result = ToolResult(call.name, ok=False, verdict=Verdict.REFUSED,
                                inputs=dict(call.arguments),
                                detail="tool is allowed but not wired")
            return self.trace.record(result)

        try:
            output = self._tools[call.name](**call.arguments)
        except TypeError as exc:
            # A model supplying the wrong arguments is a proposal error, not a
            # crash. Escalate rather than 500.
            return self.trace.record(ToolResult(
                call.name, ok=False, verdict=Verdict.ESCALATED,
                inputs=dict(call.arguments),
                detail=f"bad arguments: {exc}"))
        except Exception as exc:  # noqa: BLE001
            log.warning("tool %s failed", call.name)
            return self.trace.record(ToolResult(
                call.name, ok=False, verdict=Verdict.ESCALATED,
                inputs=dict(call.arguments),
                detail=f"tool error: {type(exc).__name__}"))

        if isinstance(output, ToolResult):
            if not output.inputs:
                output.inputs = dict(call.arguments)
            return self.trace.record(output)
        return self.trace.record(ToolResult(call.name, ok=True, output=output,
                                            inputs=dict(call.arguments)))

    def run(self, calls: list[ToolCall]) -> list[ToolResult]:
        return [self.call(c) for c in calls]


__all__ = ["Agent", "ToolCall", "ToolResult", "Trace", "Verdict", "ALLOWED_TOOLS"]
