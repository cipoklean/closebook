"""Settle every planned payout and conversion that Airwallex says is done.

LIVE. Reads only (two GETs per unsettled object) and writes only to Closebook's
own ledger.

    cd ~/closebook
    .venv/bin/python scripts/settle_payouts.py

Safe to run repeatedly: every posting is keyed to the Airwallex object id, so a
second run reports "already posted" and moves nothing. Exits non-zero only if a
step could not be checked at all.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

# Refuse to touch the demo book unless told to. Settlement MUTATES the ledger,
# and the earlier lesson stands: point a diagnostic at a temp database.
if "--demo" not in sys.argv:
    os.environ.setdefault(
        "CLOSEBOOK_DATABASE_URL",
        "sqlite:///" + os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                    "data", "settle-scratch.sqlite3"))

from app.db import get_default_db  # noqa: E402
from app.domain.ledger import reconcile  # noqa: E402
from app.settle import settle_all  # noqa: E402


def main() -> int:
    db = get_default_db()
    print(f"database: {os.environ.get('CLOSEBOOK_DATABASE_URL') or 'data/closebook.sqlite3'}")
    outcomes = settle_all(db)
    if not outcomes:
        print("nothing to check — no plan step holds an Airwallex object")
    for o in outcomes:
        print(f"  {o.kind:10s} {o.key[:18]}…  {'POSTED' if o.posted else 'no posting'}  {o.reason}")

    rep = reconcile(db.session())
    print()
    print("cash        :", {k: f"{v/100:,.2f}" for k, v in sorted(rep.cash_by_currency.items())})
    print("outflow     :", {k: f"{v/100:,.2f}" for k, v in sorted(rep.outflow_by_account.items())})
    print("balanced    :", rep.balanced)
    from app.domain.settlement import paid_payouts
    paid = paid_payouts(db.session())
    print("settled payouts:", [(p["payable_number"], f"{p['amount_minor']/100:,.2f} {p['currency']}")
                               for p in paid] or "none")
    return 0 if all("could not be checked" not in o.reason for o in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
