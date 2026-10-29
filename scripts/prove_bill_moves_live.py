"""Fix 1, live on the tester: a bill added through the form moves on its own.

TestSprite's finding was that `/setup/payables` saved the row and said "the
planner will look at it on the next event", so a bill inside every rule sat at
Checked forever. This drives the real form on a FRESH book and then reads the
book's own file: the proof is a plan step carrying an Airwallex object id, not
a sentence on a page.

Usage: .venv/bin/python scripts/prove_bill_moves_live.py [book]
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

BOOK = sys.argv[1] if len(sys.argv) > 1 else "r12-fix1"
BASE = os.environ.get(
    "TESTSPRITE_BASE_URL",
    "https://professor-apr-girls-meanwhile.trycloudflare.com").rstrip("/")
SHOTS = Path("/tmp/proof/run12")
BILL = "PO-1200"


def _book_db() -> Path:
    return REPO / "data" / "workspaces" / f"{BOOK}.sqlite3"


def _seed() -> None:
    """Cash and a payable supplier, written the way the app writes them."""
    os.environ.setdefault("CLOSEBOOK_WORKSPACE_ROOT",
                          str(REPO / "data" / "workspaces"))
    from app import workspaces
    from app.domain.ledger import post
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import create_supplier, record_beneficiary

    # A REAL BENEFICIARY, not a made-up id. A fake one is refused by Airwallex
    # with code 045 and the step ends CANCELLED, which looks like "the bill did
    # not move" when the truth is "the supplier cannot be paid".
    from app.airwallex.client import AirwallexClient

    real_id = ""
    try:
        got = AirwallexClient().call("GET", "/api/v1/beneficiaries")
        items = (got.get("items") or got.get("data")
                 if isinstance(got, dict) else got) or []
        usd = [x for x in items
               if str(x.get("transfer_methods") or "").find("LOCAL") >= 0
               or "USD" in str(x.get("bank_details") or {}).upper()]
        real_id = str((usd or items)[0].get("id") or "") if items else ""
        print("live beneficiary:", real_id or "(none on the account)")
    except Exception as exc:                              # noqa: BLE001
        print("could not list beneficiaries:", type(exc).__name__)

    if not real_id:
        raise SystemExit("no live beneficiary on the sandbox: a transfer cannot "
                         "be proved without one")

    with workspaces.book(BOOK).session_scope() as s:
        save_settings(s, budget_currency="USD", reserve_floor="5000",
                      auto_pay_limit="2500", fx_tolerance_pct="0.50")
        post(s, "opening:USD:r12", [("cash:USD", 900_000, 0, "USD"),
                                    ("opening_balance", 0, 900_000, "USD")])
        sup = create_supplier(s, name="Atlanta Packaging", country="US",
                              currency="USD", payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"seeded": True},
                           beneficiary_id=real_id, status="CREATED")


def _steps() -> list[tuple]:
    con = sqlite3.connect(f"file:{_book_db()}?mode=ro", uri=True)
    try:
        return con.execute(
            "select step_key, status, airwallex_object_id from plan_step "
            "order by id").fetchall()
    finally:
        con.close()


def main() -> int:
    from playwright.sync_api import sync_playwright

    _seed()
    SHOTS.mkdir(parents=True, exist_ok=True)
    bad: list[str] = []
    print("before:", _steps())
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1280, "height": 1000})
        page = ctx.new_page()
        try:
            page.goto(f"{BASE}/app?ws={BOOK}&tab=setup", wait_until="load")
            page.wait_for_timeout(1500)
            page.select_option('form[action^="/setup/payables"] #f-supplier_id',
                               label="Atlanta Packaging")
            page.fill('form[action^="/setup/payables"] #f-number', BILL)
            page.fill('form[action^="/setup/payables"] #f-amount', "1000")
            tag = page.eval_on_selector(
                'form[action^="/setup/payables"] #f-currency', "e => e.tagName")
            if tag.lower() == "select":
                page.select_option('form[action^="/setup/payables"] #f-currency',
                                   label="USD")
            else:
                page.fill('form[action^="/setup/payables"] #f-currency', "USD")
            page.click('form[action^="/setup/payables"] button[type="submit"]')
            # a live transfer takes as long as the sandbox takes
            page.wait_for_timeout(9000)
            body = " ".join(page.inner_text("body").split())
            print("page says:", body[:260])
            page.screenshot(path=str(SHOTS / "bill-added.png"), full_page=True)
            if "Sent to the bank" not in body:
                bad.append("the page does not say the bill was sent: "
                           + body[:260])
        finally:
            ctx.close()
            b.close()

    steps = _steps()
    print("after:", steps)
    moved = [s for s in steps if s[2]]
    if not moved:
        bad.append("no plan step holds an Airwallex object id: nothing moved")
    if not any(BILL in (s[0] or "") for s in moved):
        bad.append(f"nothing was sent for {BILL}")

    print("\nRESULT:", "PASS" if not bad else "FAIL")
    for b_ in bad:
        print("  -", b_)
    print(f"screenshots in {SHOTS}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
