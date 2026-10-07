"""Transfers (payouts).

State machine (closebook.md §5.4):
- Terminal states are PAID or CANCELLED. A FAILED transfer ends CANCELLED,
  NOT FAILED — read `failure_type` + `failure_reason` to decide the fix.
- SENT is in-flight, never final. CANCELLED does not mean a human cancelled.
- request_id: 10-50 chars; a bare UUIDv4 hex (32) is valid.
- A non-success/timeout on create is AMBIGUOUS — look up by request_id before
  retrying. Query the transfer list for the request_id to find it.
- SWIFT pays a flat fee per payout (EUR 12.85 in the demo correquest_idors);
  LOCAL is free.
"""
from __future__ import annotations

from typing import Optional

from .client import AirwallexClient, _reserve_request_id


class Transfers:
    def __init__(self, client: AirwallexClient):
        self._c = client

    def list(self) -> list[dict]:
        data = self._c.call("GET", "/api/v1/transfers")
        # Live sandbox returns a bare LIST or {"items": [...]} (not "data").
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("items") or data.get("data") or []
        return []

    def get(self, transfer_id: str) -> dict:
        return self._c.call("GET", f"/api/v1/transfers/{transfer_id}")

    def create(self, db_session, *, beneficiary_id: str, currency: str,
               amount_minor: int, transfer_method: str,
               reference: Optional[str] = None,
               reason: Optional[str] = None,
               source_currency: Optional[str] = None,
               request_id: Optional[str] = None, plan_step=None) -> dict:
        """Create a payout. The caller MUST have persisted request_id first.

        Money boundary: amount_minor (integer) -> major-unit string here.
        """
        # Eager-`or` trap: `request_id or _reserve_request_id(...)`
        # evaluates the fallback even when request_id is given, so an
        # explicit id with no plan_step crashed on None.request_id.
        if request_id is None:
            request_id = _reserve_request_id(db_session, plan_step)

        from ..money import to_major_str
        body: dict = {
            "request_id": request_id,
            "beneficiary_id": beneficiary_id,
            # Verified live 2026-10-07: transfers/create requires
            # `source_currency` (the currency being debited) in addition to
            # `transfer_currency`. Sending only the latter fails with
            # validation_failed naming `source_currency` — which is how we
            # found it.
            "source_currency": source_currency or currency,
            "transfer_currency": currency,
            "transfer_amount": to_major_str(amount_minor, currency),
            "transfer_method": transfer_method,
        }
        if reference:
            body["reference"] = reference
        # Verified live 2026-10-07: transfers/create requires `reason`
        # (a human-readable purpose shown in the Airwallex dashboard). Omitting
        # it entirely fails validation; an empty string may also be rejected,
        # so always send a real one.
        body["reason"] = reason or reference or "Supplier payout"
        return self._c.call("POST", "/api/v1/transfers/create", json_body=body,
                            request_id=request_id, is_simulation=False)

    def find_by_request_id(self, request_id: str) -> Optional[dict]:
        """The ambiguous-retry lookup: did this request_id already create a
        transfer? Returns the matching transfer object or None."""
        for t in self.list():
            if t.get("request_id") == request_id:
                return t
        return None


__all__ = ["Transfers"]
