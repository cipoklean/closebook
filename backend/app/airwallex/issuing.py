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
        # Eager-`or` trap: `request_id or _reserve_request_id(...)`
        # evaluates the fallback even when request_id is given, so an
        # explicit id with no plan_step crashed on None.request_id.
        if request_id is None:
            request_id = _reserve_request_id(db_session, plan_step)

        body = {"request_id": request_id, "type": cardholder_type, "email": email}
        return self._c.call("POST", "/api/v1/issuing/cardholders/create",
                            json_body=body, request_id=request_id, is_simulation=False)

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
        # Eager-`or` trap: `request_id or _reserve_request_id(...)`
        # evaluates the fallback even when request_id is given, so an
        # explicit id with no plan_step crashed on None.request_id.
        if request_id is None:
            request_id = _reserve_request_id(db_session, plan_step)

        limits = [
            {"interval": "PER_TRANSACTION",
             "amount": to_major_str(per_transaction_limit_minor, currency)},
        ]
        if all_time_limit_minor is not None:
            limits.append({"interval": "ALL_TIME",
                           "amount": to_major_str(all_time_limit_minor, currency)})

        # Shape confirmed against the Airwallex docs MCP, live probing, and
        # finally by Airwallex Developer Help.
        #
        # The 400 `invalid_request: Invalid issuance details` was OURS, not an
        # account entitlement. Airwallex Dev Help identified the cause: a
        # DELEGATE cardholder cannot hold a PERSONALIZED card. `is_personalized`
        # must be false for DELEGATE; personalized cards require an INDIVIDUAL
        # cardholder. We keep DELEGATE (it is what the spend controls attach to),
        # so the card is non-personalized.
        #
        # `program` carries ONLY `purpose`. COMMERCIAL is the value a BUSINESS
        # sandbox accepts; purpose=CONSUMER is rejected with "CONSUMER program
        # is not allowed for this account". A TOP-level `purpose` is a different
        # field (what the card is for) and is only meaningful with issue_to,
        # which is removed below.
        #
        # `issue_to` is omitted entirely: it is not required, and it was what
        # forced the top-level `purpose` field that in turn produced
        # "Purpose can only be set when card issue_to is set to ORGANISATION".
        #
        # `primary_contact_details` is documented in the MCP examples but is
        # REJECTED live: "primary_contact_details is no longer supported, please
        # use cardholder_id instead". The docs are stale here; the error wins.
        body: dict = {
            "request_id": request_id,
            "cardholder_id": cardholder_id,
            "form_factor": "VIRTUAL",
            "created_by": "Closebook",
            "is_personalized": False,
            "program": {
                "purpose": "COMMERCIAL",
            },
            "authorization_controls": {
                "allowed_transaction_count": "MULTIPLE",
                "transaction_limits": {
                    "currency": currency,
                    "limits": limits,
                },
            },
        }
        if card_number:
            body["card_number"] = card_number
        if currency_allowlist:
            body["currency_allowlist"] = currency_allowlist
        if merchant_categories:
            body["merchant_categories"] = merchant_categories
        return self._c.call("POST", "/api/v1/issuing/cards/create", json_body=body,
                            request_id=request_id, is_simulation=False)

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
