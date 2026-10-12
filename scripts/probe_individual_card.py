"""Complete an INDIVIDUAL cardholder, then try a card against it.

This is the only way to test Airwallex Dev Help's claim that the cardholder TYPE
is what blocks card creation. The `individual` object requires, in the order the
API reveals them: name -> express_consent_obtained -> address.country -> ...

Live sandbox only. Omits the CONSENT flag from anything but this probe: consent
is a real-world representation about a real person, and this account has no
such person.

    python scripts/probe_individual_card.py
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.airwallex.client import AirwallexClient, ApiError  # noqa: E402

_MASK = re.compile(r"\d{12,19}")


def mask(v) -> str:
    return _MASK.sub(lambda m: f"****{m.group(0)[-4:]}", str(v))


def main() -> int:
    client = AirwallexClient()

    individual = {
        "name": {"first_name": "Sandbox", "last_name": "Tester"},
        "date_of_birth": "1990-01-01",
        "address": {"line1": "1 Broad Street", "city": "Lagos",
                    "state": "Lagos", "postcode": "100001",
                    "country": "NG", "country_code": "NG"},
        "express_consent_obtained": "yes",
    }
    body = {
        "request_id": str(uuid.uuid4()),
        "type": "INDIVIDUAL",
        "email": f"sandbox+{uuid.uuid4().hex[:8]}@example.com",
        "individual": individual,
    }
    try:
        r = client.call("POST", "/api/v1/issuing/cardholders/create",
                        json_body=body, request_id=body["request_id"],
                        is_simulation=False)
    except ApiError as exc:
        p = getattr(exc, "payload", None) or {}
        print(f"INDIVIDUAL cardholder FAILED: {p.get('code')} | "
              f"{p.get('message')} | source={p.get('source')!r}")
        return 1

    ch_id = r.get("cardholder_id") or r.get("id")
    print(f"INDIVIDUAL cardholder -> {ch_id} "
          f"type={r.get('type')} status={r.get('status')}\n")

    print("cards against the INDIVIDUAL cardholder:")
    for purpose, personalized in (("CONSUMER", True), ("COMMERCIAL", False),
                                  ("COMMERCIAL", True)):
        card = {
            "request_id": str(uuid.uuid4()),
            "cardholder_id": ch_id,
            "form_factor": "VIRTUAL",
            "created_by": "Closebook",
            "is_personalized": personalized,
            "program": {"purpose": purpose},
            "authorization_controls": {
                "allowed_transaction_count": "MULTIPLE",
                "transaction_limits": {"currency": "USD", "limits": [
                    {"interval": "PER_TRANSACTION", "amount": "7000.00"}]}},
        }
        label = f"{purpose:10} personalized={str(personalized):5}"
        try:
            cr = client.call("POST", "/api/v1/issuing/cards/create",
                             json_body=card, request_id=card["request_id"],
                             is_simulation=False)
            print(f"  OK   {label} -> id={mask(cr.get('id'))} "
                  f"card_status={cr.get('card_status')}")
            print(f"       CARD_ID={cr.get('id')}")
            print(f"       CARD_STATUS={cr.get('card_status')}")
        except ApiError as exc:
            p = getattr(exc, "payload", None) or {}
            print(f"  fail {label} -> {p.get('code')} | "
                  f"{str(p.get('message'))[:60]} | source={p.get('source')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
