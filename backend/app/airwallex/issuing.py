"""Issuing: cardholders + virtual cards with spend-locked controls.

Rules (closebook.md §5.4/§6):
- REST field is `type`; the Developer MCP field is `cardholder_type`. We use
  REST, so `type`.
- DELEGATE cardholders come back READY in the create response (nothing to
  poll).
- Cards: PER_TRANSACTION limit is INCLUSIVE — $100.00 on a $100 limit clears,
  $100.01 fails PER_TRANSACTION_LIMIT. limits are not cumulative, so add
  ALL_TIME / MONTHLY alongside to track totals.
- Card transaction reads use DIFFERENT status vocabularies in REST vs MCP:
  after capture, REST may report APPROVED while MCP reports CLEARED, and REST
  can stay PENDING during a short demo. For demo logic, treat
  `transaction_type: CLEARING` as accepted and read `failure_reason` for a
  decline. Declined card transactions end FAILED (transfers end CANCELLED).
- Sandbox decline reasons: INSUFFICIENT_FUNDS, LIMIT_EXCEEDED,
  CURRENCY_NOT_ALLOWED, MERCHANT_CATEGORY_NOT_ALLOWED, OUT_OF_ALLOWED_TIME_RANGE,
  CARD_INACTIVE, CARD_CLOSED.
- Freeze/unfreeze/close via POST /issuing/cards/{id}/update (card_status).
"""
from __future__ import annotations

from typing import Optional

from .client import AirwallexClient, _reserve_request_id


class Issuing:
    def __init__(self, client: AirwallexClient):
        self._c = client

    def create_cardholder(self, db_session, *, email: str, cardholder_type: str = "DELEGATE",
                          request_id: Optional[str] = None, plan_step=None) -> dict:
        rid = request_id or _reserve_request_id(db_session, plan_step)
        body = {"request_id": rid, "type": cardholder_type, "email": email}
        return self._c.call("POST", "/api/v1/issuing/cardholders/create",
                            json_body=body, request_id=rid, is_simulation=False)

    def create_card(self, db_session, *, cardholder_id: str, currency: str,
                    per_transaction_limit_minor: int, all_time_limit_minor: Optional[int],
                    card_number: Optional[str] = None,  # test card, e.g. 4035501000000008
                    currency_allowlist: Optional[list[str]] = None,
                    merchant_categories: Optional[list[str]] = None,
                    request_id: Optional[str] = None, plan_step=None) -> dict:
        """Issue a virtual card whose controls ENFORCE a purchase policy.

        Policy says: enforce with card controls, not a prompt — only the
        controls can block an authorization.
        """
        from ..money import to_major_str
        rid = request_id or _reserve_request_id(db_session, plan_step)
        limits = {
            "PER_TRANSACTION": to_major_str(per_transaction_limit_minor, currency),
        }
        if all_time_limit_minor is not None:
            limits["ALL_TIME"] = to_major_str(all_time_limit_minor, currency)
        body: dict = {
            "request_id": rid,
            "cardholder_id": cardholder_id,
            "currency": currency,
            "limits": limits,
        }
        if card_number:
            body["card_number"] = card_number
        if currency_allowlist:
            body["currency_allowlist"] = currency_allowlist
        if merchant_categories:
            body["merchant_categories"] = merchant_categories
        return self._c.call("POST", "/api/v1/issuing/cards/create", json_body=body,
                            request_id=rid, is_simulation=False)

    def update_card(self, card_id: str, *, card_status: str) -> dict:
        """card_status in {ACTIVE, FROZEN, CLOSED} — freeze once the invoice is paid."""
        return self._c.call(
            "POST", f"/api/v1/issuing/cards/{card_id}/update",
            json_body={"card_status": card_status},
        )

    def transactions(self, card_id: Optional[str] = None) -> list[dict]:
        data = self._c.call("GET", "/api/v1/issuing/transactions")
        items = data.get("data", []) if isinstance(data, dict) else data
        if card_id:
            items = [t for t in items if t.get("card_id") == card_id]
        return items or []


__all__ = ["Issuing"]
