"""Incident handling: what a failed payout means and what to do about it.

The rule this module exists to enforce: **a failed payment is never retried
blindly.** A webhook saying `payout.transfer.failed` tells you a payment did not
succeed; it does not tell you why, and the reason determines the only safe
recovery. Airwallex's own state machine reinforces this — a failed transfer ends
`CANCELLED`, and `CANCELLED` does not mean a human cancelled it.

Recovery classes, and why each is different:

  * `BENEFICIARY_INVALID` — the payee's details are wrong. Retrying sends the
    money nowhere. Fix the record, get a NEW approval, new request_id.
  * `INSUFFICIENT_FUNDS` — timing, not policy. Re-plan; the committed-cash
    budget must be re-checked because conditions may have changed.
  * `COMPLIANCE_HOLD` — a human must look at this. Never automated.
  * `UNKNOWN` — we do not know what happened. The correct action is to LOOK UP
    the transfer's real state before deciding anything at all.

Everything here is deterministic. The model may explain an incident to a human;
it cannot decide the recovery.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class Recovery(str, Enum):
    RESOLVE_BEFORE_RETRY = "resolve_before_retry"   # look up real state first
    IN_FLIGHT_WATCH = "in_flight_watch"             # still moving — touch nothing
    FIX_BENEFICIARY = "fix_beneficiary"
    REPLAN = "replan"                                 # timing/funds problem
    HUMAN_REVIEW = "human_review"                     # compliance/anything odd
    COMPLETE = "complete"                             # it actually succeeded
    IGNORE = "ignore"                                 # not ours / not money


# Statuses that mean the money may STILL move. A webhook claiming failure while
# the transfer is in one of these is contradicted by the ledger, and creating a
# replacement payment would risk paying twice.
_IN_FLIGHT = {"PENDING", "PROCESSING", "SUBMITTED", "SCHEDULED", "CREATED",
              "APPROVAL_PENDING", "IN_PROGRESS", "BOOKED"}


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


@dataclass
class Incident:
    """One thing that went wrong with a payment, and the required next step."""

    event_id: str
    event_name: str
    transfer_id: Optional[str]
    recovery: Recovery
    severity: Severity
    summary: str
    failure_type: Optional[str] = None
    failure_reason: Optional[str] = None
    requires_new_approval: bool = False
    requires_new_request_id: bool = False
    looked_up: bool = False
    live_status: Optional[str] = None
    created_at: Optional[datetime] = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_name": self.event_name,
            "transfer_id": self.transfer_id,
            "recovery": self.recovery.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "failure_type": self.failure_type,
            "failure_reason": self.failure_reason,
            "requires_new_approval": self.requires_new_approval,
            "requires_new_request_id": self.requires_new_request_id,
            "looked_up": self.looked_up,
            "live_status": self.live_status,
            "evidence": self.evidence,
        }

    def describe(self) -> str:
        line = f"[{self.severity.value}] {self.event_name} -> {self.recovery.value}: {self.summary}"
        if self.failure_type:
            line += f" (type={self.failure_type}"
            if self.failure_reason:
                line += f", reason={self.failure_reason}"
            line += ")"
        if self.live_status:
            line += f" [live status: {self.live_status}]"
        return line


# --- failure classification ------------------------------------------------
# Substrings observed in Airwallex `failure_type` / `failure_reason` values.
# Matched case-insensitively against both fields because the sandbox has used
# several spellings for the same condition.
_BENEFICIARY_HINTS = (
    "beneficiary", "invalid_account", "account_not_found", "invalid_account_details",
    "unsupported_currency", "invalid_payment_method", "payment_method",
)
_FUNDS_HINTS = (
    "insufficient", "exceed", "balance", "limit_exceeded",
)
_COMPLIANCE_HINTS = (
    "compliance", "sanction", "aml", "review", "hold", "blocked", "restricted",
    "decline",
)


def in_flight(status: Optional[str]) -> bool:
    """Is this transfer possibly still moving?

    Public because callers must be able to assert they are NOT creating a
    replacement for a live transfer. A private `_IN_FLIGHT` set invites
    duplicating the list, and the duplicate is where the double payment returns.
    """
    return bool(status) and status.upper() in _IN_FLIGHT


def classify_failure(failure_type: Optional[str],
                     failure_reason: Optional[str]) -> Recovery:
    """Map a failure to its recovery class. Deterministic, no model involved."""
    blob = " ".join(filter(None, [failure_type or "", failure_reason or ""])).lower()
    if not blob:
        # No reason given is NOT permission to retry. It means we do not know.
        return Recovery.RESOLVE_BEFORE_RETRY
    if any(h in blob for h in _BENEFICIARY_HINTS):
        return Recovery.FIX_BENEFICIARY
    if any(h in blob for h in _COMPLIANCE_HINTS):
        return Recovery.HUMAN_REVIEW
    if any(h in blob for h in _FUNDS_HINTS):
        return Recovery.REPLAN
    return Recovery.HUMAN_REVIEW


def _as_str(value) -> Optional[str]:
    """Coerce an extracted payload value to a string (or None).

    Webhook JSON is not schema-validated by Airwallex, so an id can arrive as an
    int/float in one event family and a string in another.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _dig(payload: dict, *names, default=None):
    """Airwallex nests the useful bits differently across event families.

    Only returns SCALARS. A nested object under a key we happen to ask for (a
    `data` dict under `id`, say) must not leak through as a dict where a string
    is expected — that turns a readable incident into a type error.
    """
    def _clean(value):
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, list) and len(value) == 1 and \
                isinstance(value[0], (str, int, float, bool)):
            return value[0]
        return None

    for name in names:
        if name in payload:
            cleaned = _clean(payload[name])
            if cleaned is not None:
                return cleaned
    data = payload.get("data")
    if isinstance(data, dict):
        for name in names:
            if name in data:
                cleaned = _clean(data[name])
                if cleaned is not None:
                    return cleaned
    return default


def handle_event(event_name: str, payload: dict, *,
                 event_id: Optional[str] = None,
                 live_status: Optional[str] = None,
                 now: Optional[datetime] = None) -> Incident:
    """Turn one verified webhook event into an Incident with a decided recovery.

    `live_status` is the result of looking the transfer up on Airwallex. When it
    is supplied the incident is `looked_up=True`, which is the difference
    between "we know what happened" and "we assume".
    """
    now = now or datetime.now(timezone.utc)
    raw_id = _dig(payload, "id", "event_id", default="")
    event_id = str(raw_id) if raw_id is not None else ""
    # Only an explicit transfer/transaction key may identify the transfer.
    # Falling back to the bare "id" here would pick up the EVENT id whenever the
    # payload nests its fields under `data`, and we would then look up — and act
    # on — an object that is not a transfer at all.
    transfer_id = _as_str(_dig(payload, "transfer_id", "transaction_id"))
    if transfer_id is None:
        # A `data`-nested payload's TOP-LEVEL `id` is the EVENT id, not the
        # transfer. Ask inside `data` FIRST. Falling back to the top level too
        # early looks up — and acts on — an object that is not a transfer, which
        # is the same class of bug as believing a `failed` webhook without
        # checking.
        inner = payload.get("data")
        if isinstance(inner, dict) and isinstance(inner.get("id"), str):
            transfer_id = _as_str(inner.get("id"))
    if transfer_id is None:
        transfer_id = _as_str(_dig(payload, "id", "event_id"))
    failure_type = _as_str(_dig(payload, "failure_type"))
    failure_reason = _as_str(_dig(payload, "failure_reason", "reason"))
    status = _as_str(_dig(payload, "status"))

    # Typed as Any-valued: these are splatted into Incident(**base), and a
    # narrower inferred type makes every field look like a possible mismatch.
    base: dict[str, Any] = dict(event_id=event_id, event_name=str(event_name),
                transfer_id=transfer_id, failure_type=failure_type,
                failure_reason=failure_reason, created_at=now,
                looked_up=live_status is not None,
                live_status=_as_str(live_status),
                evidence={"raw_status": status})

    # --- a "failed" webhook whose transfer is actually fine -------------
    if live_status in {"PAID", "COMPLETED", "SUCCESS"}:
        return Incident(recovery=Recovery.COMPLETE, severity=Severity.INFO,
                        summary=(f"transfer {transfer_id} reported {event_name} "
                                 f"but is {live_status} on Airwallex: no action"),
                        **base)

    # --- the transfer is still moving, so creating a replacement is unsafe ---
    if in_flight(live_status):
        return Incident(
            recovery=Recovery.IN_FLIGHT_WATCH, severity=Severity.WARN,
            summary=(f"transfer {transfer_id} reported {event_name} but is still "
                     f"{live_status} on Airwallex: watch it, do NOT create a "
                     f"replacement payment"),
            # Explicitly False: this is the case where a replacement would double
            # pay. The incident must not look actionable.
            requires_new_approval=False, requires_new_request_id=False,
            **base)

    if event_name == "payout.transfer.paid":
        return Incident(recovery=Recovery.COMPLETE, severity=Severity.INFO,
                        summary=f"transfer {transfer_id} paid", **base)

    if event_name in {"payout.transfer.failed", "payout.transfer.cancelled"}:
        recovery = classify_failure(failure_type, failure_reason)
        severity = Severity.CRITICAL if recovery in {
            Recovery.FIX_BENEFICIARY, Recovery.HUMAN_REVIEW} else Severity.WARN
        if recovery is Recovery.RESOLVE_BEFORE_RETRY:
            severity = Severity.WARN
            summary = (f"transfer {transfer_id} failed with no usable reason; "
                       f"look up its real state before doing anything")
        elif recovery is Recovery.FIX_BENEFICIARY:
            summary = (f"transfer {transfer_id} failed on the beneficiary "
                       f"({failure_type}): fix the record; do NOT retry")
        elif recovery is Recovery.REPLAN:
            summary = (f"transfer {transfer_id} failed on funds/limits, "
                       f"re-plan against the committed-cash budget")
        else:
            summary = (f"transfer {transfer_id} needs a human "
                       f"({failure_type or 'unspecified'})")
        return Incident(
            recovery=recovery, severity=severity, summary=summary,
            # A replacement payment is a DIFFERENT operation: it needs a new
            # approval bound to its own terms and a brand-new request_id. The
            # old one is spent.
            requires_new_approval=recovery in {Recovery.FIX_BENEFICIARY,
                                               Recovery.REPLAN,
                                               Recovery.HUMAN_REVIEW},
            requires_new_request_id=recovery in {Recovery.FIX_BENEFICIARY,
                                                 Recovery.REPLAN,
                                                 Recovery.HUMAN_REVIEW},
            **base)

    if event_name and event_name.startswith("issuing."):
        return Incident(recovery=Recovery.HUMAN_REVIEW, severity=Severity.WARN,
                        summary=f"issuing event {event_name} for {transfer_id}",
                        **base)

    return Incident(recovery=Recovery.IGNORE, severity=Severity.INFO,
                    summary=f"unhandled event {event_name}", **base)


def parse_payload(raw: str | bytes) -> dict:
    """Webhook bodies are stored as text; be forgiving about shape."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


__all__ = [
    "Incident", "Recovery", "Severity", "classify_failure", "handle_event",
    "in_flight", "parse_payload",
]


#: What the common failure types mean, in the words a person needs.
_FAILURE_WORDS = {
    "BENEFICIARY_BANK_RETURNED": "the beneficiary bank sent the money back",
    "INVALID_ACCOUNT_NAME_OR_NUMBER": "the account name or number was rejected",
    "INVALID_BENEFICIARY": "the beneficiary details were rejected",
    "INSUFFICIENT_FUNDS": "there were not enough funds to send it",
}


def _plain_failure_type(failure_type: str) -> str:
    """A failure type as a sentence, never the token itself."""
    value = str(failure_type or "").strip().upper()
    if value in _FAILURE_WORDS:
        return _FAILURE_WORDS[value]
    if value:
        return "the bank rejected this payment"
    return "the bank did not say why"


def record_bank_failure(db_session, *, transfer_id: str, kind: str,
                        failure_type: str = "",
                        failure_reason: str = "", status: str = "CANCELLED"):
    """Keep what the BANK said about a transfer that came back.

    A webhook does this on the way in; the sandbox controls are the other way a
    transfer ends badly, and they recorded only the STATUS. So the card read
    CANCELLED with no reason -- and "somebody stopped this" is a different fact
    from "the beneficiary bank returned it, the account is closed". Stored as an
    event so it reaches the same reader as a webhook's, and labelled with which
    of the two happened, because the next action differs.
    """
    import json

    from ..models import WebhookEvent
    from ..timeutils import utcnow

    label = "Returned" if str(kind).lower() == "return" else "Failed"
    # A COLON AND A SENTENCE, ALWAYS. The sandbox reports a failure_type for
    # some transitions and no reason text for others, and a card reading only
    # "Failed" tells the operator as little as "CANCELLED" did.
    why = (failure_reason or "").strip() or _plain_failure_type(failure_type)
    said = f"{label}: {why}"
    body = {
        "id": f"sim-{kind}-{transfer_id}",
        "name": "payout.transfer.failed",
        "kind": "simulated",
        "data": {"id": transfer_id, "status": "CANCELLED",
                 "failure_type": failure_type or "",
                 "failure_reason": said},
    }
    row = db_session.query(WebhookEvent).filter_by(
        event_id=body["id"]).first()
    if row is None:
        db_session.add(WebhookEvent(
            event_id=body["id"], name=body["name"], payload=json.dumps(body),
            created_at=utcnow(), processed_at=utcnow()))
    else:
        row.payload = json.dumps(body)
    db_session.flush()
    # THE SAME BUILDER A WEBHOOK USES. A page reads incidents, not events, so
    # writing the row alone left the card as silent as before.
    return said, handle_event("payout.transfer.failed", body,
                              live_status=status)
