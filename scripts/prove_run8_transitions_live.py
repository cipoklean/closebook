"""Prove, against the live sandbox, that a transfer can actually be moved.

Run 8: a transfer the bank reported as SCHEDULED could not be Settled or Failed
at all. SCHEDULED is a FUNDING status the sandbox also reports on a transfer,
and Airwallex accepts only the NEXT valid status, so asking for SENT first was
refused and the operator read "Airwallex's sandbox refused this step".

This walks a real in-flight transfer to PAID and a second one to FAILED with a
return reason, printing every hop the sandbox accepted and refused.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

logging.basicConfig(level=logging.INFO, format="    %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

from app.airwallex.client import AirwallexClient  # noqa: E402
from app.airwallex.simulation import Simulation  # noqa: E402
from app.airwallex.transfers import Transfers  # noqa: E402

IN_FLIGHT = ("SCHEDULED", "PENDING", "PROCESSING", "SUBMITTED", "CREATED")


def main() -> int:
    c = AirwallexClient()
    t = Transfers(c)
    sim = Simulation(c)

    rows = t.list()
    by = [(r.get("status"), r.get("id")) for r in rows]
    print(f"\nthe account has {len(rows)} transfer(s):")
    for st, oid in by:
        print(f"  {st:<12} {oid}")

    live = [(st, oid) for st, oid in by if st in IN_FLIGHT]
    print(f"\nin flight: {len(live)}")

    ok = True
    if not live:
        print("\nNO IN-FLIGHT TRANSFER: create one through the app, then re-run.")
        return 2

    st, oid = live[0]
    print(f"\n--- Settle {oid} (starts {st}) -> PAID")
    print(f"    before: {t.status_of(oid)}")
    sim.transition_transfer(oid, "PAID")
    after = t.status_of(oid)
    print(f"    after:  {after}")
    ok &= after in ("PAID", "COMPLETED", "SUCCESS")

    if len(live) < 2:
        print("\nno second in-flight transfer to fail; settling proved only")
    else:
        st2, oid2 = live[1]
        print(f"\n--- Fail {oid2} (starts {st2}) -> FAILED, bank returned")
        print(f"    before: {t.status_of(oid2)}")
        sim.transition_transfer(oid2, "FAILED",
                               failure_type="BENEFICIARY_BANK_RETURNED")
        after2 = t.status_of(oid2)
        obj = t.find_by_request_id("") or {}
        print(f"    after:  {after2}")
        rows2 = [r for r in t.list() if r.get("id") == oid2]
        if rows2:
            r = rows2[0]
            print(f"    failure_type={r.get('failure_type')!r} "
                  f"reason={r.get('failure_reason')!r}")
        ok &= after2 in ("FAILED", "CANCELLED")

    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
