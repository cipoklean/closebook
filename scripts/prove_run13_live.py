"""Run 13's two fixes, live on the tester, on one fresh book each.

1. Two open invoices of the same amount: a deposit that fits both must be HELD,
   with both named, and neither invoice touched.
2. A bill approved and sent must lose its "Sends once ... is free to spend"
   caption, and nothing on the pages may still say "reserve floor".

Usage: .venv/bin/python scripts/prove_run13_live.py [base]
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

BASE = (sys.argv[1].rstrip("/") if len(sys.argv) > 1
        else "http://127.0.0.1:8020")
BOOK_A = "r13-tie"
BOOK_B = "r13-caption"
PIN = os.environ.get("DEMO_APPROVER_PIN") or ""
SHOTS = Path("/tmp/proof/run13")


def _db(book: str):
    from app.db import DB

    path = REPO / "data" / "workspaces" / f"{book}.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return DB(f"sqlite:///{path}")


def _policy(s) -> None:
    from app.domain.settings_store import save

    save(s, budget_currency="USD", reserve_floor="5000",
         auto_pay_limit="2500", fx_tolerance_pct="0.50")


def _seed_tie() -> None:
    os.environ.setdefault("CLOSEBOOK_WORKSPACE_ROOT",
                          str(REPO / "data" / "workspaces"))
    from app.domain.intake import record_and_match
    from app.domain.workspace import create_customer, create_invoice

    db = _db(BOOK_A)
    with db.session_scope() as s:
        _policy(s)
        cust = create_customer(s, name="Lumen Stores")
        for number in ("INV-3100", "INV-3101"):
            create_invoice(s, customer_id=cust.id, number=number,
                           currency="USD", amount="4200.00")
    with db.session_scope() as s:
        record_and_match(s, deposit_id=f"dep-{BOOK_A}", currency="USD",
                         amount_minor=420_000, payer_name="Lumen Stores",
                         reference="payment for last month thanks", llm=False)


def _seed_caption() -> str:
    """Cash short of the bill, and a REAL beneficiary so the send works."""
    from app.airwallex.client import AirwallexClient
    from app.domain.ledger import post
    from app.domain.workspace import (
        create_payable, create_supplier, record_beneficiary,
    )

    got = AirwallexClient().call("GET", "/api/v1/beneficiaries")
    items = (got.get("items") or got.get("data")
             if isinstance(got, dict) else got) or []
    real = str((items or [{}])[0].get("id") or "")
    if not real:
        raise SystemExit("no live beneficiary on the sandbox")

    db = _db(BOOK_B)
    with db.session_scope() as s:
        _policy(s)
        post(s, "income:USD:r13", [("cash:USD", 600_000, 0, "USD"),
                                   ("opening_balance", 0, 600_000, "USD")])
        sup = create_supplier(s, name="Atlanta Packaging", country="US",
                              currency="USD", payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"seeded": True},
                           beneficiary_id=real, status="CREATED")
        create_payable(s, supplier_id=sup.id, currency="USD",
                       amount="2600.00", number="PO-2600")
    return real


def _credit(book: str, minor: int) -> None:
    """Cash lands: the same ledger posting a released deposit makes."""
    from app.domain.ledger import post

    db = _db(book)
    with db.session_scope() as s:
        post(s, "income:USD:r13-more",
             [("cash:USD", minor, 0, "USD"), ("opening_balance", 0, minor, "USD")])


def _invoices(book: str) -> dict:
    con = sqlite3.connect(f"file:{REPO / 'data' / 'workspaces' / (book + '.sqlite3')}?mode=ro",
                          uri=True)
    try:
        return {r[0]: (r[1], r[2]) for r in con.execute(
            "select number, amount_paid_minor, status from invoice_receivable")}
    finally:
        con.close()


def main() -> int:
    from playwright.sync_api import sync_playwright

    if not PIN:
        print("DEMO_APPROVER_PIN is not set")
        return 2
    SHOTS.mkdir(parents=True, exist_ok=True)
    bad: list[str] = []
    _seed_tie()
    _seed_caption()

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1280, "height": 1100})
        page = ctx.new_page()
        try:
            # ---- 1. the tie
            page.goto(f"{BASE}/app?ws={BOOK_A}&tab=money-in", wait_until="load")
            page.wait_for_timeout(2000)
            page.evaluate("() => document.querySelectorAll('details')"
                          ".forEach(d => d.open = true)")
            page.wait_for_timeout(300)
            text = " ".join(page.inner_text("body").split())
            page.screenshot(path=str(SHOTS / "1-tie.png"), full_page=True)
            for want in ("INV-3100", "INV-3101", "which one?"):
                if want not in text:
                    bad.append(f"the tie page does not say {want!r}")
            if "USD 4,200.00 fits" not in text:
                bad.append("the tie reason does not name the amount")
            after = _invoices(BOOK_A)
            if any(paid for paid, _st in after.values()):
                bad.append(f"an invoice was paid on a coin flip: {after}")

            # ---- 2. the caption
            # THE PLAN HAS TO EXIST BEFORE THERE IS ANYTHING TO APPROVE.
            # /money-out/run is the operator's own "look at it now" button: the
            # bill is over the limit, so it is listed for approval and not sent.
            page.goto(f"{BASE}/app?ws={BOOK_B}&tab=money-out", wait_until="load")
            page.wait_for_timeout(1200)
            page.request.post(f"{BASE}/money-out/run?ws={BOOK_B}")
            page.wait_for_timeout(3000)
            # plan first: the bill is over the limit and cash is short
            page.goto(f"{BASE}/app?ws={BOOK_B}&tab=ok", wait_until="load")
            page.wait_for_timeout(1500)
            before = " ".join(page.inner_text("body").split())
            if "PO-2600" not in before:
                bad.append("the bill is not on the Approve tab at all")
            page.screenshot(path=str(SHOTS / "2-before-approve.png"),
                            full_page=True)
            pin_field = page.locator('input[name="pin"]').first
            if pin_field.count():
                pin_field.fill(PIN)
            else:
                bad.append("no PIN field on the Approve tab")
            approve = page.locator('button:has-text("Approve")').first
            if not approve.count():
                bad.append("no Approve button on the page")
            else:
                approve.click()
                page.wait_for_timeout(9000)

            # THE CAPTION SHOULD EXIST HERE: approved, and the cash is short.
            page.goto(f"{BASE}/app?ws={BOOK_B}&tab=money-out", wait_until="load")
            page.wait_for_timeout(2500)
            page.evaluate("() => document.querySelectorAll('details')"
                          ".forEach(d => d.open = true)")
            page.wait_for_timeout(300)
            midway = " ".join(page.inner_text("body").split())
            print("after approval:", midway[:200])
            if "Sends once" not in midway:
                bad.append("the promise was never shown, so this proves nothing")
            page.screenshot(path=str(SHOTS / "3-approved-waiting.png"),
                            full_page=True)

            # the money arrives, and the operator asks the plan to look again
            _credit(BOOK_B, 900_000)
            page.request.post(f"{BASE}/money-out/run?ws={BOOK_B}")
            # POLL, DO NOT SAMPLE: the send is asynchronous, and a page read
            # while the transfer is still in flight is telling the truth.
            for _ in range(15):
                page.wait_for_timeout(3000)
                page.goto(f"{BASE}/app?ws={BOOK_B}&tab=money-out",
                          wait_until="load")
                page.wait_for_timeout(800)
                snapshot = " ".join(page.inner_text("body").split())
                if "Sends once" not in snapshot:
                    break
            page.evaluate("() => document.querySelectorAll('details')"
                          ".forEach(d => d.open = true)")
            page.wait_for_timeout(300)
            out = " ".join(page.inner_text("body").split())
            page.screenshot(path=str(SHOTS / "3-after-approve.png"),
                            full_page=True)
            print("money out:", out[:300])
            if "Sends once" in out:
                bad.append("the caption survived the send: " + out[:300])
            if "reserve floor" in out:
                bad.append("money out still says 'reserve floor'")
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
