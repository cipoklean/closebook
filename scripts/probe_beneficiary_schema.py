"""Live probe: beneficiary schema for the demo corridors.

Run: .venv/bin/python scripts/probe_beneficiary_schema.py

Prints the REAL required fields per corridor. Do not hand-write a beneficiary
from memory — the schema differs per country/currency/rail and Airwallex
returns a bare error code naming a field you did NOT send.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.airwallex.beneficiaries import Beneficiaries  # noqa: E402
from app.airwallex.client import AirwallexClient  # noqa: E402

CORRIDORS = [
    ("US", "USD", "LOCAL"),    # Atlanta Packaging, PO-7003
    ("NL", "EUR", "SWIFT"),    # Rotterdam Freight, PO-7001
    ("GB", "GBP", "LOCAL"),    # Bristol Boxes
    ("CN", "USD", "SWIFT"),    # Shenzhen Weave, PO-7002
]


def flatten(schema_payload: dict, key: str = "required") -> list[tuple]:
    """The schema endpoint returns a FLAT dotted map, not a nested tree.

    Verified live 2026-10-07: the body is
      {"errors": {}, "schema": {"create_beneficiary_request.beneficiary.x": {...}}}
    so walking for nested "properties" (the assumption in my first draft)
    silently returns nothing at all.
    """
    rows = []
    for path, meta in (schema_payload.get("schema") or {}).items():
        if not isinstance(meta, dict):
            continue
        if not meta.get("required"):
            continue
        if meta.get("schema_unknown_reason"):
            continue
        short = path.split("beneficiary.", 1)[-1] \
            if "beneficiary." in path else path.split(".", 1)[-1]
        rows.append((short, meta.get("type"),
                     meta.get("schema_unknown_reason") or "",
                     (meta.get("validation_functions") or [])))
    return rows


def main() -> int:
    c = AirwallexClient()
    ben = Beneficiaries(c)
    for country, ccy, method in CORRIDORS:
        print(f"\n{'='*70}\n{country}/{ccy} via {method}\n{'='*70}")
        try:
            payload = ben.schema(country, ccy, method)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            continue
        rows = flatten(payload)
        if not rows:
            print("  (no required fields returned)")
        for short, typ, unknown, validations in rows:
            note = f"  depends={unknown}" if unknown else ""
            vf = ""
            if validations:
                names = [v.get("function_name") if isinstance(v, dict) else str(v)
                         for v in validations]
                vf = f"  checks={names}"
            print(f"  {short:<48}{str(typ):<10}{note}{vf}")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())