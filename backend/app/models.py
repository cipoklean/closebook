"""ORM models — the full §4 schema.

Design notes:
- Money is stored as **integer minor units** (a `BigInteger` column) with its
  currency; `app.money` owns the major/minor conversion at the API boundary.
- Postgres-compatible: no SQLite-only types. `json` is used for flexible
  payloads (plan_step.payload_json, match.evidence_json).
- `plan_step.request_id` UNIQUE is the idempotency spine; `webhook_event.
  event_id` UNIQUE is the dedupe spine.
"""
from __future__ import annotations

import enum
from datetime import datetime

from .timeutils import utcnow
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String,
    Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---- enums --------------------------------------------------------------
class RecStatus(str, enum.Enum):
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    PAID = "PAID"
    OVERPAID = "OVERPAID"


class PayStatus(str, enum.Enum):
    HELD = "HELD"
    READY = "READY"
    PAYING = "PAYING"
    PAID = "PAID"
    FAILED = "FAILED"


class PayoutMethod(str, enum.Enum):
    LOCAL = "LOCAL"
    SWIFT = "SWIFT"
    CARD = "CARD"


class MatchStatus(str, enum.Enum):
    UNMATCHED = "UNMATCHED"
    MATCHED = "MATCHED"
    PARTIAL = "PARTIAL"
    AMBIGUOUS = "AMBIGUOUS"


class MatchMethod(str, enum.Enum):
    EXACT = "EXACT"
    RULE = "RULE"
    LLM = "LLM"


class StepType(str, enum.Enum):
    FX = "FX"
    TRANSFER = "TRANSFER"
    CARD = "CARD"
    CHASE = "CHASE"
    REFUND = "REFUND"
    ESCALATE = "ESCALATE"
    HOLD = "HOLD"


class StepStatus(str, enum.Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    SUBMITTED = "SUBMITTED"
    PAID = "PAID"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ApprovalStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    VOID = "VOID"


# ---- tables -------------------------------------------------------------
class Customer(Base):
    __tablename__ = "customer"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    country: Mapped[Optional[str]] = mapped_column(String(8))


class Supplier(Base):
    __tablename__ = "supplier"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    country: Mapped[Optional[str]] = mapped_column(String(8))
    currency: Mapped[Optional[str]] = mapped_column(String(3))
    payout_method: Mapped[PayoutMethod] = mapped_column(
        Enum(PayoutMethod), default=PayoutMethod.LOCAL
    )
    airwallex_beneficiary_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    mcc: Mapped[Optional[str]] = mapped_column(String(16))
    # --- beneficiary creation, from the app rather than from a script -------
    # The beneficiary is created against the REAL sandbox, so the outcome has
    # to be storable and displayable: a supplier that failed validation must be
    # visibly failed, not silently missing its id.
    beneficiary_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # The exact request we sent, so the corridor fields can be shown back and
    # re-validated without guessing what was typed.
    beneficiary_request_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Airwallex's own words, verbatim, when validation or creation is refused.
    beneficiary_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class InvoiceReceivable(Base):
    __tablename__ = "invoice_receivable"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(40), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    currency: Mapped[str] = mapped_column(String(3))
    amount_minor: Mapped[int] = mapped_column(BigInteger)  # integer minor units
    amount_paid_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[RecStatus] = mapped_column(Enum(RecStatus), default=RecStatus.OPEN)
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    customer: Mapped[Customer] = relationship()


class InvoicePayable(Base):
    __tablename__ = "invoice_payable"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(40), unique=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("supplier.id"))
    currency: Mapped[str] = mapped_column(String(3))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[PayStatus] = mapped_column(Enum(PayStatus), default=PayStatus.HELD)
    depends_on_receivable_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("invoice_receivable.id"), nullable=True
    )
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    criticality: Mapped[int] = mapped_column(Integer, default=3)  # 1-5

    supplier: Mapped[Supplier] = relationship()


class Inflow(Base):
    __tablename__ = "inflow"
    id: Mapped[int] = mapped_column(primary_key=True)
    airwallex_deposit_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True)
    currency: Mapped[str] = mapped_column(String(3))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    payer_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    reference_raw: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    match_status: Mapped[MatchStatus] = mapped_column(Enum(MatchStatus), default=MatchStatus.UNMATCHED)


class Match(Base):
    __tablename__ = "match"
    id: Mapped[int] = mapped_column(primary_key=True)
    inflow_id: Mapped[int] = mapped_column(ForeignKey("inflow.id"))
    invoice_receivable_id: Mapped[int] = mapped_column(ForeignKey("invoice_receivable.id"))
    amount_applied_minor: Mapped[int] = mapped_column(BigInteger)
    confidence: Mapped[float] = mapped_column(Float)  # 0-1
    method: Mapped[MatchMethod] = mapped_column(Enum(MatchMethod))
    evidence_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class PlanStep(Base):
    __tablename__ = "plan_step"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    type: Mapped[StepType] = mapped_column(Enum(StepType))
    payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(
        String(64), unique=True, nullable=True
    )  # persisted BEFORE the call; UNIQUE = idempotency spine
    status: Mapped[StepStatus] = mapped_column(Enum(StepStatus), default=StepStatus.PROPOSED)
    airwallex_object_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("request_id", name="uq_plan_step_request_id"),)


class AgentTrace(Base):
    """One tool call made by the agent, and the deterministic verdict on it.

    The point of this table is that a judge can audit exactly what the model
    was allowed to do. A model may CALL a tool; it may not move money, set an
    amount, or decide a match. Every row records the inputs it supplied, the
    output the tool returned, and what the deterministic code concluded — so
    the proposal and the enforcement are visible side by side rather than
    implied.
    """
    __tablename__ = "agent_trace"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tool: Mapped[str] = mapped_column(String(64))
    inputs_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outputs_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verdict: Mapped[str] = mapped_column(String(32))
    # "accepted" | "rejected" | "escalated" — decided by CODE, never the model.
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Approval(Base):
    __tablename__ = "approval"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_step_id: Mapped[int] = mapped_column(ForeignKey("plan_step.id"))
    bound_hash: Mapped[str] = mapped_column(String(128))  # sha256 of the bound terms
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    counterparty: Mapped[str] = mapped_column(String(120))
    status: Mapped[ApprovalStatus] = mapped_column(Enum(ApprovalStatus), default=ApprovalStatus.PENDING)
    decided_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class LedgerEntry(Base):
    __tablename__ = "ledger_entry"
    __table_args__ = (UniqueConstraint("txn_id", "account", "currency", name="uq_ledger_txn_acct"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    txn_id: Mapped[str] = mapped_column(String(64))
    account: Mapped[str] = mapped_column(String(64))
    debit_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    credit_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3))
    ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # When the posting happened. Needed to answer "paid today" from the ledger
    # itself rather than from a plan step's timestamp, which is when the payment
    # was PLANNED — a different moment, and for a retry a different day.
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=True)


class WebhookEvent(Base):
    __tablename__ = "webhook_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True)  # dedupe spine
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # ordering
    payload: Mapped[str] = mapped_column(Text)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRun(Base):
    __tablename__ = "agent_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class BalanceSnapshot(Base):
    """Cache of GET /balances/current so the planner never re-reads the API
    mid-plan and can diff against the reserve floor deterministically."""
    __tablename__ = "balance_snapshot"
    id: Mapped[int] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(3))
    available_minor: Mapped[int] = mapped_column(BigInteger)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkspaceSettings(Base):
    """The workspace's OWN policy, edited in the app.

    Deliberately NOT in `.env`: these are business decisions the operator owns
    (what floor to keep, how much may go out without a human), not deployment
    configuration. `.env` holds credentials and endpoints; this holds the rules.

    Single row, enforced by `settings_store.get_or_create()` reading id == 1
    rather than by a constraint SQLite cannot express.
    """

    __tablename__ = "workspace_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    budget_currency: Mapped[str] = mapped_column(String(3), default="USD")
    reserve_floor_minor: Mapped[int] = mapped_column(BigInteger, default=500_000)
    auto_pay_limit_minor: Mapped[int] = mapped_column(BigInteger, default=250_000)
    # How much FX drift the plan leaves as headroom, in percent.
    fx_tolerance_pct: Mapped[float] = mapped_column(Float, default=0.50)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
