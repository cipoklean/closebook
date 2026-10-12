"""Create a capped virtual card on the Airwallex sandbox, for real.

Live only — never mocked. Prints the response status, card id and card_status,
and masks anything card-number shaped. No secret is printed.

    python scripts/probe_card_create.py
"""
from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.airwallex.client import AirwallexClient, ApiError  # noqa: E402
from app.airwallex.issuing import Issuing  # noqa: E402
from app.db import DB  # noqa: E402
from app.money import to_major_str  # noqa: E402

CARD_LIMIT_USD = 7_000       # per transaction
ALL_TIME_USD = 7_000

_MASK_RE = re.compile(r"\d{12,19}")


def mask(text: str) -> str:
    """Never print a card number, however it arrives."""
    return _MASK_RE.sub(lambda m: f"****{m.group(0)[-4:]}", str(text))


def show(label: str, value) -> None:
    print(f"  {label}: {mask(value)}")


def main() -> int:
    client = AirwallexClient()
    issuing = Issuing(client)
    db = DB()

    email = f"closebook-cards+{uuid.uuid4().hex[:8]}@example.com"
    print("1. cardholder")
    try:
        ch = issuing.create_cardholder(db, email=email, cardholder_type="DELEGATE")
    except ApiError as exc:
        print(f"   FAILED {exc}")
        return 1
    ch_id = ch.get("cardholder_id") or ch.get("id")
    show("cardholder_id", ch_id)
    show("type", ch.get("type"))
    show("status", ch.get("status"))
    if not ch_id:
        print(f"   no cardholder id in the response: {sorted(ch)}")
        return 1

    print("\n2. virtual card, capped")
    limits = {"per_transaction_usd": CARD_LIMIT_USD, "all_time_usd": ALL_TIME_USD}
    show("requested limits", limits)
    try:
        card = issuing.create_card(
            db,
            cardholder_id=ch_id,
            currency="USD",
            per_transaction_limit_minor=CARD_LIMIT_USD * 100,
            all_time_limit_minor=ALL_TIME_USD * 100,
        )
    except ApiError as exc:
        print(f"   FAILED: {exc}")
        detail = getattr(exc, "payload", None)
        if detail:
            show("payload", detail)
        print("\nRESULT: card creation STILL FAILS — beat 4 keeps its caveat.")
        return 2

    print("   RESPONSE OK")
    show("id", card.get("id"))
    show("card_status", card.get("card_status"))
    show("cardholder_id", card.get("cardholder_id"))
    show("form_factor", card.get("form_factor"))
    show("is_personalized", card.get("is_personalized"))
    lmt = (card.get("authorization_controls") or {}).get("transaction_limits")
    show("limits", lmt)
    # A card number may be returned; never print it.
    if card.get("card_number"):
        print("  card_number: PRESENT (masked, not printed)")
    print(f"\n  card_id: {card.get('id')}")
    print(f"  card_status: {card.get('card_status')}")
    print(f"\nCARD_ID={card.get('id')}")
    print("\nRESULT: card created LIVE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
