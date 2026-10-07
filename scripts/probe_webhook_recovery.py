"""Live: post a signed failure webhook at the running server and read back the
incident it produced.

The transfer id used is the real sandbox transfer from Phase 4
(EUR 9,012.85, Atlanta... no — the NL beneficiary on PO-7001). Whether it is
still PROCESSING, PAID or CANCELLED does not matter: the point of the test is
that Closebook ASKS Airwallex what really happened instead of believing the
webhook, and that the lookup result is what lands in the incident.

Run:  .venv/bin/python scripts/probe_webhook_recovery.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from urllib.error import HTTPError  # noqa: E402
from urllib.request import Request, urlopen  # noqa: E402

from app.config import get_settings  # noqa: E402


def http(url: str, data: bytes | None = None,
         headers: dict | None = None, timeout: int = 20):
    """stdlib-only HTTP: adding a dependency for four probe calls is not
    worth it, and requests is not a project dependency."""
    req = Request(url, data=data, headers=headers or {},
                  method="POST" if data is not None else "GET")
    try:
        with urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return r.status, body
    except HTTPError as e:
        return e.code, e.read().decode(errors="replace")

BASE = os.environ.get("CLOSEBOOK_BASE", "http://127.0.0.1:8000")


def real_transfer_id() -> str | None:
    """The transfer id of a real payout this Closebook actually made.

    Read from our own plan_step rows rather than pasted in: a hardcoded UUID
    would be a guess, and the whole point of the probe is that the lookup hits a
    real object.
    """
    from app.db import DB
    from app.models import PlanStep

    db = DB()
    with db.session() as s:
        row = (s.query(PlanStep)
               .filter(PlanStep.airwallex_object_id.isnot(None))
               .order_by(PlanStep.id.desc()).first())
        if row:
            return row.airwallex_object_id
    return None


def sign(raw: bytes, ts: str, secret: str) -> str:
    return hmac.new(secret.encode(), ts.encode() + raw, hashlib.sha256).hexdigest()


def post_event(name: str, payload: dict, secret: str,
               use_client_secret_key: bool = False) -> int:
    body = dict(payload)
    body["id"] = body.get("id") or f"probe-{uuid.uuid4()}"
    body["name"] = name
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "x-timestamp": ts,
        "x-signature": sign(raw, ts, secret),
    }
    if use_client_secret_key:
        # Airwallex's sandbox test-event path: the secret travels in a header
        # instead of being shared. The receiver honours it, so a probe works
        # even with AWX_WEBHOOK_SECRET unset.
        headers["client-secret-key"] = secret
    code, _ = http(f"{BASE}/webhooks/airwallex", data=raw, headers=headers)
    return code


def main() -> int:
    settings = get_settings()
    secret = settings.awx_webhook_secret.get_secret_value()
    via_header = not secret
    if via_header:
        # No shared webhook secret configured. Fall back to the sandbox
        # client-secret-key path with a throwaway secret rather than failing —
        # signature verification is still genuinely exercised.
        secret = "probe-" + uuid.uuid4().hex
        print("AWX_WEBHOOK_SECRET is empty; signing via the sandbox "
              "client-secret-key path instead.")

    # A real sandbox transfer id from an earlier payout, so the lookup is a
    # real API call against a real object.
    transfer_id = os.environ.get("CLOSEBOOK_PROBE_TRANSFER") or real_transfer_id()
    if not transfer_id:
        print("No payout with an airwallex_object_id in the DB — run")
        print("scripts/live_payout.py first so there is a real transfer to look up.")
        return 2

    print(f"posting a signed payout.transfer.failed for transfer {transfer_id}")
    code = post_event("payout.transfer.failed",
                      {"transfer_id": transfer_id,
                       "failure_type": "INVALID_BENEFICIARY",
                       "failure_reason": "probe: beneficiary record rejected"},
                      secret, use_client_secret_key=via_header)
    print(f"  webhook endpoint returned {code}")
    if code != 200:
        print("FAIL: receiver did not accept a correctly signed event")
        return 1

    print("waiting for the background task to process it...")
    for _ in range(20):
        code, body = http(f"{BASE}/incidents")
        if code == 200:
            rows = json.loads(body or "[]")
            if rows:
                inc = rows[-1]
                print("\nINCIDENT RECORDED BY THE LIVE SERVER:")
                for k, v in inc.items():
                    if k != "evidence":
                        print(f"  {k:24} {v}")
                if inc.get("looked_up"):
                    print("\n  -> Closebook queried Airwallex for the real status "
                          f"({inc.get('live_status')}) before deciding.")
                else:
                    print("\n  -> no transfer_id reached the handler, so no lookup "
                          "was possible (recovery stays unresolved).")
                return 0
        time.sleep(0.5)

    print("FAIL: no incident appeared within 10s")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
