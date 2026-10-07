"""Beneficiaries: schema-first creation + validation.

Rules (closebook.md §6):
- Call POST /beneficiaries/schema for the target country+currency FIRST.
  Fields differ per corridor; sending the wrong field name returns a bare
  code 001 that names a field you did NOT send.
- Validate with POST /beneficiaries/validate using {"beneficiary":{...},
  "transfer_methods":["LOCAL"]} — it is `transfer_methods`, NOT
  `payment_methods`.
- Corridor fields:
  * US/USD LOCAL: bank_details.account_routing_type1="aba" (flat, lowercase),
    a REAL ABA routing number with a fake account number,
    bank_account_category exactly "Checking" or "Savings" (other casing -> 016).
    Address needs street_address, city, state, postcode, country_code.
  * GB/GBP LOCAL: includes bank_name (US does not), routing type lowercase
    "sort_code" (Airwallex rejects SORT_CODE -> 011).
  * DE/EUR SWIFT: IBAN (valid checksum) + swift_code, no routing fields.
- Bad IBAN fails with code 083 at CREATION, not at pay time.
- transfer_method on transfers/create accepts only LOCAL or SWIFT.
"""
from __future__ import annotations

from typing import Optional

from .client import AirwallexClient, _reserve_request_id


class Beneficiaries:
    def __init__(self, client: AirwallexClient):
        self._c = client

    def schema(self, country: str, currency: str, transfer_method: str) -> dict:
        return self._c.call(
            "POST", "/api/v1/beneficiaries/schema",
            json_body={"country_code": country, "currency": currency,
                       "transfer_method": transfer_method},
        )

    def validate(self, beneficiary: dict, transfer_methods: list[str] | None = None) -> dict:
        body = {"beneficiary": beneficiary, "transfer_methods": transfer_methods or ["LOCAL"]}
        return self._c.call("POST", "/api/v1/beneficiaries/validate", json_body=body)

    def create(self, db_session, *, country: str, currency: str,
               transfer_method: str, beneficiary: dict,
               request_id: Optional[str] = None, plan_step=None) -> dict:
        """Create a beneficiary. `beneficiary` is the FULL inner object.

        NOTE the asymmetry, verified live 2026-10-07: `validate()` takes the
        inner beneficiary and wraps it itself, while `create()` needs the body
        to already contain a `beneficiary` key. Sending flat fields to create
        returns `field [beneficiary] is required` — so this method takes the
        whole inner object (name/address/bank_details/entity_type together)
        rather than the split-out name + bank_details my first draft assumed.

        Also: a bad IBAN fails here with code 083, at CREATION, not at pay
        time — which is the good time to find out.
        """
        if request_id is None:
            request_id = _reserve_request_id(db_session, plan_step)
        body = {
            "request_id": request_id,
            "country_code": country,
            "currency": currency,
            "transfer_method": transfer_method,
            # Verified live 2026-10-07: create wants `transfer_methods` (a list)
            # where validate uses `transfer_methods` too, but the beneficiary
            # body itself uses `payment_methods`. Deriving it here keeps the
            # two spellings from being confused at a call site.
            "transfer_methods": [transfer_method],
            "beneficiary": beneficiary,
        }
        return self._c.call("POST", "/api/v1/beneficiaries/create", json_body=body,
                            request_id=request_id, is_simulation=False)


__all__ = ["Beneficiaries"]
