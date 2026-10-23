"""Hash-bound approvals (closebook.md §4).

The model may ASK to move money. It can never authorise it. What it proposes
arrives here as a set of terms — amount, currency, counterparty, invoices,
evidence — and an approver signs exactly those terms. Every term is folded
into a SHA-256 `bound_hash` together with an HMAC over the approval itself.

Why the hash and not just a PENDING/APPROVED column: an approval must DIE the
moment any bound term changes. A €9,000 approval that quietly becomes €11,400
because the FX quote moved 30% is no longer the thing a human approved. Re-
checking the hash at execution time (see `executor.assert_fresh`) is what makes
beat 4 of the demo real rather than narrated.

Deliberately NOT here: any notion of "who approved". Signing binds the TERMS.
Authorisation is the executor's job and it consults `Approval.status`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from ..config import get_settings
from ..money import to_major_str
from .policy import usd_value_minor

# How long a signed approval stays valid if untouched. Short on purpose: an
# approval for a specific amount at a specific rate should not be cashable
# three days later after the market has moved.
APPROVAL_TTL_MIN = 30


class ApprovalStale(Exception):
    """A signed approval no longer matches the terms presented for execution.

    Raised, never auto-corrected. The correct response is to VOID the approval
    and ask a human again — silently re-approving is how systems lose money.
    """


class ApprovalExpired(ApprovalStale):
    pass


@dataclass(frozen=True)
class ApprovalTerms:
    """Everything a human is agreeing to. If it is not here, it is not bound.

    Adding a field is a security change: anything not hashed cannot be trusted
    on execution. Keep the set small, and keep it complete.
    """

    step_id: int
    payable_number: str
    amount_minor: int
    currency: str
    counterparty: str
    supplier_id: str
    invoice_ids: tuple[int, ...] = ()
    fx_quote_id: Optional[str] = None
    fx_rate: Optional[Decimal] = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> str:
        """Deterministic serialisation — the exact bytes that get hashed.

        sort_keys everywhere and a fixed separator, so the same terms always
        produce the same string on any machine and any Python version.
        """
        return json.dumps(
            {
                "step_id": self.step_id,
                "payable_number": self.payable_number,
                "amount_minor": self.amount_minor,
                "currency": self.currency,
                "counterparty": self.counterparty,
                "supplier_id": self.supplier_id,
                "invoice_ids": list(self.invoice_ids),
                "fx_quote_id": self.fx_quote_id,
                # Normalise the Decimal before stringifying. str(Decimal("1.10"))
                # is '1.10' and str(Decimal("1.1")) is '1.1' — numerically
                # identical, different hashes. That would void a perfectly valid
                # approval over trailing zeros, so we canonicalise first. Rates
                # arrive from Airwallex as strings of varying precision, so this
                # case is real, not hypothetical.
                "fx_rate": (f"{self.fx_rate.normalize():f}"
                            if self.fx_rate is not None else None),
                "evidence": self.evidence,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )


def _hmac_secret() -> bytes:
    s = get_settings().closebook_hmac_secret.get_secret_value()
    if not s:
        raise RuntimeError(
            "CLOSEBOOK_HMAC_SECRET is not set: approvals cannot be signed. "
            "Add it to .env (never to a committed file)."
        )
    return s.encode("utf-8")


def bound_hash(terms: ApprovalTerms) -> str:
    """SHA-256 over the canonical terms.

    Deliberately NOT keyed: this is a content address for "which terms were
    approved", useful to show a human and to diff. The keyed half — the HMAC —
    is separate (see `sign`), so a hash leak alone cannot forge an approval.
    """
    return hashlib.sha256(terms.canonical().encode("utf-8")).hexdigest()


def sign(terms: ApprovalTerms) -> str:
    """Keyed proof that THIS Closebook instance signed these terms."""
    return hmac.new(_hmac_secret(), terms.canonical().encode("utf-8"),
                    hashlib.sha256).hexdigest()


@dataclass
class Approval:
    """A signed set of terms awaiting (or having received) a human decision."""

    terms: ApprovalTerms
    approved_by: str
    signed_at: datetime
    signature: str
    ttl_minutes: int = APPROVAL_TTL_MIN
    status: str = "APPROVED"          # APPROVED | VOID
    void_reason: Optional[str] = None

    @property
    def hash(self) -> str:
        return bound_hash(self.terms)

    def expires_at(self) -> datetime:
        return self.signed_at + timedelta(minutes=self.ttl_minutes)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now >= self.expires_at()

    def assert_fresh(self, current: ApprovalTerms, *,
                     now: Optional[datetime] = None) -> None:
        """Gate at execution time. Raises unless `current` IS what was signed.

        This is the single check that stands between a stale quote and a
        payout nobody approved. It is called on EVERY execution, including
        retries, because a retry is exactly when nobody re-reads the numbers.
        """
        if self.status == "VOID":
            raise ApprovalStale(self.void_reason or "approval was voided")
        if self.is_expired(now):
            raise ApprovalExpired(
                f"approval expired at {self.expires_at().isoformat()}")
        if bound_hash(current) != self.hash:
            raise ApprovalStale(
                "the terms presented for execution differ from the approved "
                f"terms (approved {self.hash[:12]}… vs current {bound_hash(current)[:12]}…)"
            )


def issue(terms: ApprovalTerms, *, approved_by: str,
          ttl_minutes: int = APPROVAL_TTL_MIN,
          now: Optional[datetime] = None) -> Approval:
    """Create a signed approval. The human decision already happened."""
    now = now or datetime.now(timezone.utc)
    return Approval(terms=terms, approved_by=approved_by, signed_at=now,
                    signature=sign(terms), ttl_minutes=ttl_minutes)


def void(approval: Approval, reason: str) -> Approval:
    """Kill an approval. Terminal: a void approval never revives."""
    approval.status = "VOID"
    approval.void_reason = reason
    return approval


def check_fx_drift(approved_rate: Decimal, current_rate: Decimal,
                   tolerance_pct: Decimal) -> Optional[str]:
    """Is the FX movement big enough to void the approval? Returns reason/None.

    Compared as a RATIO, not a difference: drift is relative to the rate you
    approved against, so a 0.01 move matters more at 0.88 than at 100.
    """
    if approved_rate == 0:
        return "approved rate is zero: cannot assess drift"
    drift = abs(current_rate - approved_rate) / approved_rate * Decimal(100)
    if drift > tolerance_pct:
        return (f"FX moved {drift:.4f}% from the approved rate "
                f"{approved_rate} (now {current_rate}), beyond the "
                f"{tolerance_pct}% tolerance")
    return None


def describe(terms: ApprovalTerms, rates: dict[str, Decimal]) -> str:
    """Human-readable one-liner for the approval card in the UI/log."""
    usd = usd_value_minor(terms.amount_minor, terms.currency, rates)
    amount = to_major_str(terms.amount_minor, terms.currency)
    tail = f" ≈ USD {usd / 100:,.2f}" if usd is not None else " (unpriceable)"
    inv = ", ".join(str(i) for i in terms.invoice_ids) or "none"
    return (f"{amount} {terms.currency} to {terms.counterparty} "
            f"[{terms.payable_number}]{tail} · invoices: {inv}")


__all__ = [
    "APPROVAL_TTL_MIN", "Approval", "ApprovalExpired", "ApprovalStale",
    "ApprovalTerms", "bound_hash", "check_fx_drift", "describe", "issue",
    "sign", "void",
]