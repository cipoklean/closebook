"""Read a booked FX conversion back from Airwallex. LIVE, READ-ONLY.

Why this exists: `create_conversion` returns the actual rate and the actual
bought/sold amounts, and the executor currently keeps only the object id — the
real numbers are discarded. Settling a payout needs the ACTUAL amounts, never
the planned ones, so the question "can we still get them afterwards?" has to be
answered against the real API rather than assumed.

This script only ever issues a GET. It writes nothing to Airwallex and, per the
project's rule, touches no database at all.

Usage:
    cd ~/closebook && .venv/bin/python scripts/probe_fx_conversion.py [conversion_id]

Masks the bearer token. Prints the full conversion body, because a conversion
carries no card or credential data.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

# Belt and braces: even though this script opens no DB, refuse to run against
# the demo file if anything downstream decides to.
os.environ.setdefault(
    "CLOSEBOOK_DATABASE_URL",
    "sqlite:///" + os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "data", "probe-scratch.sqlite3"),
)

from app.airwallex.client import AirwallexClient  # noqa: E402

DEFAULT_CONVERSION = "40eaec35-e348-40c7-81cd-a6e6fd8dfb98"  # PO-7001's


def main() -> int:
    conv_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONVERSION
    client = AirwallexClient()
    try:
        body = client.call("GET", f"/api/v1/fx/conversions/{conv_id}")
    except Exception as exc:
        print(f"GET /fx/conversions/{conv_id} FAILED: {type(exc).__name__}: {exc}")
        return 1

    print(f"GET /api/v1/fx/conversions/{conv_id}")
    print(json.dumps(body, indent=2, default=str))
    print()
    print("--- the fields settlement would read ---")
    for k in ("id", "status", "conversion_id", "client_rate", "awx_rate",
              "sell_amount", "buy_amount", "sell_currency", "buy_currency",
              "settlement_date", "created_at"):
        if k in body:
            print(f"  {k} = {body[k]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
