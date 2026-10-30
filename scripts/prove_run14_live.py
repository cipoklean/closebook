"""Run 14 leftovers, live on the tester, on one fresh book.

1. A bill approved while short, then sent once cash arrives, must not keep a
   planner blocker: no "pay PO-2600 ... below the USD 5,000.00 floor" once the
   row reads "Sent, the bank has scheduled it".
2. No screen may print "reserve floor"; the cushion is called the safety
   cushion.

Usage: .venv/bin/python scripts/prove_run14_live.py [base]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

BASE = (sys.argv[1].rstrip("/") if len(sys.argv) > 1
        else "http://127.0.0.1:8020")
BOOK = "r14-stale"
PIN = os.environ.get("DEMO_APPROVER_PIN") or ""
SHOTS = Path("/tmp/proof/run14")


def _seed() -> None:
    from app.airwallex.client import AirwallexClient
    from app.db import DB
    from app.domain.ledger import post
    from app.domain.settings_store import save
    from app.domain.workspace import (
        create_payable, create_supplier, record_beneficiary,
    )

    got = AirwallexClient().call("GET", "/api/v1/beneficiaries")
    items = (got.get("items") or got.get("data")
             if isinstance(got, dict) else got) or []
    real = str((items or [{}])[0].get("id") or "")
    if not real:
        raise SystemExit("no live beneficiary on the sandbox")

    path = REPO / "data" / "workspaces" / f"{BOOK}.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = DB(f"sqlite:///{path}")
    with db.session_scope() as s:
        save(s, budget_currency="USD", reserve_floor="5000",
             auto_pay_limit="2500", fx_tolerance_pct="0.50")
        # USD 7,000 released as income: a 2,600 bill fits, so it is approved
        # and sent without ever being short. The short case is created below by
        # approving BEFORE the cash is released.
        post(s, "income:USD:r14", [("cash:USD", 200_000, 0, "USD"),
                                   ("opening_balance", 0, 200_000, "USD")])
        sup = create_supplier(s, name="Atlanta Packaging", country="US",
                              currency="USD", payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"seeded": True},
                           beneficiary_id=real, status="CREATED")
        create_payable(s, supplier_id=sup.id, currency="USD",
                       amount="2600.00", number="PO-2600")


def _credit(minor: int) -> None:
    from app.db import DB
    from app.domain.ledger import post

    db = DB(f"sqlite:///{REPO / 'data' / 'workspaces' / (BOOK + '.sqlite3')}")
    with db.session_scope() as s:
        post(s, "income:USD:r14-more", [("cash:USD", minor, 0, "USD"),
                                        ("opening_balance", 0, minor, "USD")])


def main() -> int:
    from playwright.sync_api import sync_playwright

    if not PIN:
        print("DEMO_APPROVER_PIN is not set")
        return 2
    SHOTS.mkdir(parents=True, exist_ok=True)
    _seed()
    bad: list[str] = []

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1280, "height": 1200})
        page = ctx.new_page()
        try:
            # plan, then approve while the cash is short
            page.goto(f"{BASE}/app?ws={BOOK}&tab=money-out", wait_until="load")
            page.wait_for_timeout(1200)
            page.request.post(f"{BASE}/money-out/run?ws={BOOK}")
            page.wait_for_timeout(2500)
            page.goto(f"{BASE}/app?ws={BOOK}&tab=ok", wait_until="load")
            page.wait_for_timeout(1500)
            pin = page.locator('input[name="pin"]').first
            if not pin.count():
                bad.append("no PIN field: nothing to approve")
            else:
                pin.fill(PIN)
                page.locator('button:has-text("Approve")').first.click()
                page.wait_for_timeout(6000)

            # the money arrives and the payment goes out
            _credit(900_000)
            page.request.post(f"{BASE}/money-out/run?ws={BOOK}")
            for _ in range(15):
                page.wait_for_timeout(3000)
                page.goto(f"{BASE}/app?ws={BOOK}&tab=money-out", wait_until="load")
                page.wait_for_timeout(700)
                snapped = " ".join(page.inner_text("body").split())
                if "Sent," in snapped or "bank has scheduled" in snapped:
                    break

            page.evaluate("() => document.querySelectorAll('details')"
                          ".forEach(d => d.open = true)")
            page.wait_for_timeout(300)
            text = " ".join(page.inner_text("body").split())
            page.screenshot(path=str(SHOTS / "money-out-after-send.png"),
                            full_page=True)
            print("money out:", text[:260])
            if "below the" in text:
                bad.append("a sent bill still carries its cash blocker")
            if "reserve floor" in text.lower():
                bad.append("money out still says 'reserve floor'")

            # the copy, on the two screens the finding named
            for tab in ("home", "ok", "setup", "money-in", "ai"):
                page.goto(f"{BASE}/app?ws={BOOK}&tab={tab}", wait_until="load")
                page.wait_for_timeout(1200)
                page.evaluate("() => document.querySelectorAll('details')"
                              ".forEach(d => d.open = true)")
                page.wait_for_timeout(250)
                said = " ".join(page.inner_text("body").split())
                if "reserve floor" in said.lower():
                    bad.append(f"{tab} still says 'reserve floor'")
                if tab in ("home", "ok") and "safety cushion" not in said.lower():
                    bad.append(f"{tab} never names the safety cushion")
            for label, w in (("desktop", 1280), ("phone", 390)):
                c = b.new_context(viewport={"width": w, "height": 1400})
                pg = c.new_page()
                pg.goto(f"{BASE}/app?ws={BOOK}&tab=home", wait_until="load")
                pg.wait_for_timeout(1500)
                pg.evaluate("() => document.querySelectorAll('details')"
                            ".forEach(d => d.open = true)")
                pg.wait_for_timeout(300)
                pg.screenshot(path=str(SHOTS / f"home-{label}.png"),
                              full_page=True)
                c.close()
        finally:
            ctx.close()
            b.close()

    print("\nRESULT:", "PASS" if not bad else "FAIL")
    for b_ in bad:
        print("  -", b_)
    print(f"screenshots in {SHOTS}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
