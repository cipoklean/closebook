"""Issue a real virtual card on the HONG KONG sandbox and make it refuse.

WHY A SECOND ACCOUNT. The sandbox that runs the books is registered in NIGERIA,
and Nigeria is not an issuing region: every card create returned the generic
`invalid_request: Invalid issuance details`, which names no field because the
problem was never a field. Airwallex confirmed the cause was the business
country and opened a second sandbox in HONG KONG, used only for cards.

This script does the whole beat against the REAL API and prints what came back:

  1. create a DELEGATE cardholder on the issuing account
  2. issue a VIRTUAL card with the HK program and the operator's own limit
  3. authorise an amount INSIDE the limit   (expect: approved)
  4. authorise an amount OVER the limit     (expect: declined, bank's reason)
  5. record both decisions so AI activity can show them

Nothing else uses these credentials, and no credential is printed.

STOP RULE: three failed live attempts, then report the exact responses instead
of trying a fourth. The script itself makes one attempt per invocation.

Usage:
    .venv/bin/python scripts/probe_issuing_hk.py [book]
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:      # so `import app` works from the repo root
    sys.path.insert(0, str(BACKEND))
BOOK = sys.argv[1] if len(sys.argv) > 1 else "issuing-hk"
SHOTS = Path("/tmp/proof/issuing-hk")


def _db():
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("CLOSEBOOK_WORKSPACE_ROOT", str(REPO / "data" / "workspaces"))
    from app.db import DB

    path = REPO / "data" / "workspaces" / f"{BOOK}.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return DB(f"sqlite:///{path}")


def _clean(obj):
    """Print-able copy: never a token, never a full PAN."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(s in k.lower() for s in ("token", "secret", "api_key",
                                            "client_id", "password")):
                out[k] = "<redacted>"
            elif k in ("card_number", "cvc", "pan") and isinstance(v, str):
                out[k] = f"{v[:6]}...{v[-4:]}" if len(v) > 10 else "<redacted>"
            else:
                out[k] = _clean(v)
        return out
    if isinstance(obj, list):
        return [_clean(x) for x in obj]
    return obj


def main() -> int:
    from app.airwallex.issuing import Issuing, issuing_client
    from app.domain import cards
    from app.money import money_str

    client = issuing_client()
    if client is None:
        print("NO ISSUING CREDENTIALS (airwallex_issuing_client_id/api_key empty)")
        print("Nothing was attempted; beat 4 keeps its policy-verdict caveat.")
        return 2
    print("issuing account: credentials present (values not printed)")
    print("books account:   unchanged, still the default credentials")

    db = _db()
    limit_minor, all_time_minor, ccy = cards.policy_limits(db)
    print(f"policy: per transaction {money_str(limit_minor, ccy)}, "
          f"all time {money_str(all_time_minor, ccy)}")

    iss = Issuing(client)
    email = "cards@kanocraft.example"
    try:
        with db.session_scope() as s:
            try:
                holder = iss.create_cardholder(s, email=email,
                                               request_id=f"ch-{BOOK}")
            except Exception as exc:                      # noqa: BLE001
                # SECOND RUN OF THE SAME SCRIPT: the email is already a
                # cardholder. Reuse it rather than inventing a new person, so
                # the probe is idempotent and a demo does not leave a trail of
                # half-made cardholders behind.
                if "already associated" not in str(exc):
                    raise
                existing = client.call("GET", "/api/v1/issuing/cardholders")
                items = (existing.get("items") or existing.get("data")
                         if isinstance(existing, dict) else existing) or []
                holder = next((h for h in items
                               if str(h.get("email")).lower() == email.lower()), {})
                print("reusing the existing cardholder for", email)
        print("cardholder:", json.dumps(_clean(holder), indent=2)[:600])
        # LIVE SHAPE: /issuing/cardholders/create returns `cardholder_id`, not
        # `id` (unlike most Airwallex creates, which return a bare object with
        # `id`). Reading only `id` refused a cardholder that had just been made.
        holder_id = holder.get("id") or holder.get("cardholder_id")
        if not holder_id:
            print("REFUSED: no cardholder id in the response")
            return 1

        with db.session_scope() as s:
            made = iss.create_card(
                s, cardholder_id=holder_id, currency=ccy,
                per_transaction_limit_minor=limit_minor,
                all_time_limit_minor=all_time_minor,
                created_by="Closebook demo",
                # request_id is an IDEMPOTENCY key: reusing it after the first
                # creation is refused ("already associated with a previous
                # request"), which is the guard doing its job. A probe that
                # wants a second card must ask with a new one.
                request_id=f"card-{BOOK}-{uuid.uuid4().hex[:8]}")
        print("card:", json.dumps(_clean(made), indent=2)[:800])
        card_id = made.get("id") or made.get("card_id")
        status = made.get("card_status") or made.get("status")
        print(f"card_id={card_id} card_status={status}")
        if not card_id:
            print("REFUSED: no card id in the response")
            return 1

        # ---- wait for the bank to make it usable
        for i in range(20):
            if str(status).upper() in ("ACTIVE", "OPEN"):
                break
            time.sleep(3)
            try:
                got = client.call("GET", f"/api/v1/issuing/cards/{card_id}")
            except Exception as exc:                      # noqa: BLE001
                print(f"  read {i}: {type(exc).__name__}: {exc}"[:300])
                continue
            status = got.get("card_status") or got.get("status") or status
            print(f"  poll {i}: card_status={status}")
        print(f"card_status after polling: {status}")

        # ---- inside the limit, then over it
        within = iss.authorise(card_id, amount=str(limit_minor / 100),
                               currency=ccy, limit_minor=limit_minor,
                               merchant="Acme Packaging")
        print("INSIDE THE LIMIT ->",
              json.dumps(_clean(within["raw"]), indent=2)[:500])
        over = iss.authorise(card_id, amount=str((limit_minor + 1) / 100),
                             currency=ccy, limit_minor=limit_minor,
                             merchant="Acme Packaging")
        print("OVER THE LIMIT ->",
              json.dumps(_clean(over["raw"]), indent=2)[:500])

        cards.record_decision(db, over["decision"])
        cards.record_decision(db, within["decision"])
        print("recorded both decisions for the AI page")

        ok = (not within["declined"]) and over["declined"]
        print(f"\ncard_id={card_id} card_status={status}")
        print(f"inside the limit: {'APPROVED' if not within['declined'] else 'DECLINED'}")
        print(f"over the limit:   {'DECLINED' if over['declined'] else 'APPROVED'} "
              f"reason={over['bank_reason']}")
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    except Exception as exc:                              # noqa: BLE001
        from app.airwallex.client import ApiError

        if isinstance(exc, ApiError):
            print("API REFUSED:", exc)
            payload = getattr(exc, "payload", None)
            if payload:
                print(json.dumps(_clean(payload), indent=2)[:1200])
        else:
            print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
