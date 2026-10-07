"""Live probe: build and validate one real beneficiary per demo corridor.

Run: .venv/bin/python scripts/probe_beneficiary_create.py

This is how the corridor payloads in `seed/corridor_beneficiaries.py` were
derived — by asking Airwallex what it wants, not by guessing. Every correction
below was driven by a real error response.

Findings recorded here (all verified live 2026-10-07):
  * `entity_type` is exactly "PERSONAL" or "COMPANY" -> anything else is
    error code 011.
  * `payment_methods` sits at the TOP LEVEL of the body, sibling of
    `beneficiary` — NOT nested inside it.
  * `/beneficiaries/schema` returns a FLAT dotted map keyed
    "create_beneficiary_request.beneficiary.<field>", with the allowed values
    buried in validation_functions[].parameters.values.
  * Fields marked `rules_known: false` with
    `schema_unknown_reason: DEPENDENCY_NOT_PROVIDED` still turn out to be
    REQUIRED at create time (routing number, account number, category). The
    schema only reveals them once the dependency above them is supplied.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.airwallex.beneficiaries import Beneficiaries  # noqa: E402
from app.airwallex.client import AirwallexClient, ApiError  # noqa: E402


def us_local() -> dict:
    return {
        "payment_methods": ["LOCAL"],
        "beneficiary": {
            "entity_type": "PERSONAL",
            "name": "Marcus Bell",
            "first_name": "Marcus",
            "last_name": "Bell",
            "address": {
                "street_address": "1180 Peachtree St NE",
                "city": "Atlanta",
                "state": "GA",
                "postcode": "30309",
                "country_code": "US",
            },
            "bank_details": {
                "account_currency": "USD",
                "bank_country_code": "US",
                "bank_name": "First National Bank",
                "account_routing_type1": "aba",
                # Named account_routing_value1, NOT account_routing_number —
                # verified live: sending the latter returns error 001 naming
                # the field you did NOT send.
                "account_routing_value1": "021000021",
                "account_number": "000123456789",
                "account_name": "Marcus Bell",
                "bank_account_category": "Checking",
            },
        },
    }


VALID_NL_IBAN = "NL08INGB0000001234"  # MOD-97 checksum computed, not guessed


def nl_swift() -> dict:
    # IBAN with a valid MOD-97 checksum, fabricated for the sandbox.
    return {
        "payment_methods": ["SWIFT"],
        "beneficiary": {
            "entity_type": "COMPANY",
            "name": "Rotterdam Freight BV",
            "address": {
                "street_address": "Wilhelminakade 173",
                "city": "Rotterdam",
                "postcode": "3072 AP",
                "country_code": "NL",
            },
            "bank_details": {
                "account_currency": "EUR",
                "bank_country_code": "NL",
                "bank_name": "ING Bank",
                "swift_code": "INGBNL2A",
                "account_number": VALID_NL_IBAN,
                # EUR corridors want an explicit `iban`, not account_number.
                # Verified live: omitting it returns error 001 naming `iban`.
                "iban": VALID_NL_IBAN,
                "account_name": "Rotterdam Freight BV",
            },
        },
    }


CASES = [("US", "USD", "LOCAL", us_local),
         ("NL", "EUR", "SWIFT", nl_swift)]


def main() -> int:
    c = AirwallexClient()
    ben = Beneficiaries(c)
    for country, ccy, method, builder in CASES:
        print(f"\n{'='*70}\n{country}/{ccy} via {method}\n{'='*70}")
        payload = builder()
        try:
            # NOTE: `validate()` wraps its argument in {"beneficiary": ...}
            # itself, so pass the INNER beneficiary — not the whole body.
            # Passing the full body double-nests it and returns error 001 on
            # fields that are in fact present.
            out = ben.validate(payload["beneficiary"], [method])
            print("  VALIDATE OK ->", json.dumps(out)[:300])
        except ApiError as exc:
            print(f"  VALIDATE FAILED {exc.status} {exc.code}")
            print("  ", json.dumps(exc.payload)[:600])
            continue
        try:
            created = ben.create(None, country=country, currency=ccy,
                                 transfer_method=method,
                                 beneficiary=payload["beneficiary"])
            bid = created.get("id") if isinstance(created, dict) else None
            print(f"  CREATE OK -> id={bid}")
            print("  ", json.dumps(created)[:400])
        except ApiError as exc:
            print(f"  CREATE FAILED {exc.status} {exc.code}")
            print("  ", json.dumps(exc.payload)[:600])
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())