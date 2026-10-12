"""Find which field makes POST /issuing/cards/create reject us.

Airwallex returns `invalid_request: Invalid issuance details` with an EMPTY
`source`, so the error names nothing. The only way to locate the field is to
vary the body and watch which change moves the error.

Live sandbox only. Never prints a card number or a secret.

    python scripts/probe_card_fields.py
"""
from __future__ import annotations

import copy
import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.airwallex.client import AirwallexClient, ApiError  # noqa: E402
from app.airwallex.issuing import Issuing  # noqa: E402
from app.db import DB  # noqa: E402

_MASK = re.compile(r"\d{12,19}")


def mask(v) -> str:
    return _MASK.sub(lambda m: f"****{m.group(0)[-4:]}", str(v))


def attempt(client, body: dict, label: str) -> str:
    """Return 'OK' or the error code+message+source."""
    try:
        r = client.call("POST", "/api/v1/issuing/cards/create",
                        json_body=body, request_id=body["request_id"],
                        is_simulation=False)
        return f"OK id={mask(r.get('id'))} status={r.get('card_status')}"
    except ApiError as exc:
        p = getattr(exc, "payload", None) or {}
        if isinstance(p, dict):
            return (f"{p.get('code')} | {p.get('message')} | "
                    f"source={p.get('source')!r}")
        return str(exc)[:160]


def main() -> int:
    client = AirwallexClient()
    issuing = Issuing(client)
    db = DB()
    ch = issuing.create_cardholder(
        db, email=f"probe+{uuid.uuid4().hex[:8]}@example.com",
        cardholder_type="DELEGATE")
    ch_id = ch.get("cardholder_id") or ch.get("id")
    print(f"cardholder: {ch_id}  type={ch.get('type')} status={ch.get('status')}\n")

    def rid() -> str:
        return str(uuid.uuid4())

    base = {
        "cardholder_id": ch_id,
        "form_factor": "VIRTUAL",
        "created_by": "Closebook",
        "is_personalized": False,
        "program": {"purpose": "COMMERCIAL"},
        "authorization_controls": {
            "allowed_transaction_count": "MULTIPLE",
            "transaction_limits": {"currency": "USD",
                                   "limits": [{"interval": "PER_TRANSACTION",
                                               "amount": "7000.00"}]},
        },
    }

    variants: list[tuple[str, dict]] = []

    def add(label: str, body: dict) -> None:
        b = copy.deepcopy(body)
        b["request_id"] = rid()
        variants.append((label, b))

    add("baseline (Dev Help shape)", base)

    b = copy.deepcopy(base); b.pop("created_by"); add("minus created_by", b)
    b = copy.deepcopy(base); b.pop("program"); add("minus program", b)
    b = copy.deepcopy(base); b.pop("is_personalized"); add("minus is_personalized", b)
    b = copy.deepcopy(base); b.pop("authorization_controls"); add("minus authorization_controls", b)
    b = copy.deepcopy(base); b["form_factor"] = "PHYSICAL"; add("form_factor PHYSICAL", b)
    b = copy.deepcopy(base); b["is_personalized"] = True; add("is_personalized True", b)
    b = copy.deepcopy(base); b["program"] = {"purpose": "CONSUMER"}; add("program CONSUMER", b)
    b = copy.deepcopy(base)
    b["authorization_controls"]["transaction_limits"]["limits"] = [
        {"interval": "PER_TRANSACTION", "amount": 7000}]
    add("limits amount as NUMBER", b)
    b = copy.deepcopy(base)
    b["authorization_controls"] = {"allowed_transaction_count": "MULTIPLE"}
    add("controls without limits", b)
    b = copy.deepcopy(base); b["currency"] = "USD"; add("plus top-level currency", b)

    for label, body in variants:
        print(f"  {label:34} -> {attempt(client, body, label)}")

    print("\n(If EVERY variant returns the same error, the field is not in this "
          "set and the rejection is upstream of the body.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
