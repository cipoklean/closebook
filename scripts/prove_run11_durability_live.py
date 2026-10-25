"""Run 11, live on the TESTER: a duplicate is credit, and a caption survives.

  1. An invoice is paid, then a second deposit quoting the same invoice is
     posted. The row must read "INV-... is already paid; USD X held as credit,
     nothing applied." -- and the invoice must not move.
  2. A transfer is Failed through the sandbox control, the caption is read, the
     tester is RESTARTED, and the caption is read again from the database alone.

Usage: .venv/bin/python scripts/prove_run11_durability_live.py [book]
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
BOOK = sys.argv[1] if len(sys.argv) > 1 else "r11d-live-1"
PIN = os.environ.get("TESTSPRITE_PIN") or "r11dlivepin"
BASE = os.environ.get("TESTSPRITE_BASE_URL",
                      "https://professor-apr-girls-meanwhile.trycloudflare.com")
SHOTS = Path("/tmp/proof/run11-durability")
INVOICE = "INV-R11D-1001"
BILL = "PO-R11D-FAIL-1"
BENEFICIARY = "159854b9-96d4-49ae-bd2c-0a2709ef6581"


def _db() -> Path:
    return REPO / "data" / "workspaces" / f"{BOOK}.sqlite3"


def seed() -> None:
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("CLOSEBOOK_WORKSPACE_ROOT",
                          str(REPO / "data" / "workspaces"))
    from app.db import DB
    from app.domain.intake import record_and_match
    from app.domain.ledger import post
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import (
        create_customer, create_invoice, create_payable, create_supplier,
        record_beneficiary,
    )

    path = _db()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = DB(f"sqlite:///{path}")
    with db.session_scope() as s:
        save_settings(s, budget_currency="USD", reserve_floor="0",
                      auto_pay_limit="2500", fx_tolerance_pct="0.50")
        cust = create_customer(s, name="Brightline Retail")
        create_invoice(s, customer_id=cust.id, number=INVOICE, currency="USD",
                       amount="1200")
        sup = create_supplier(s, name="Atlanta Packaging", country="US",
                              currency="USD", payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"prove": "real beneficiary"},
                           beneficiary_id=BENEFICIARY, status="CREATED")
        create_payable(s, supplier_id=sup.id, currency="USD", amount="3000",
                       number=BILL)
    with db.session_scope() as s:
        post(s, "opening:USD:r11d", [("cash:USD", 9_000_00, 0, "USD"),
                                     ("opening_balance", 0, 9_000_00, "USD")])
    # the invoice is PAID by a deposit quoting it
    with db.session_scope() as s:
        record_and_match(s, deposit_id="dep-r11d-paid", currency="USD",
                         amount_minor=120_000, payer_name="Brightline Retail",
                         reference=INVOICE, llm=False)
    # and a SECOND payment quoting the same, settled invoice
    with db.session_scope() as s:
        record_and_match(s, deposit_id="dep-r11d-dup", currency="USD",
                         amount_minor=50_000, payer_name="Brightline Retail",
                         reference=INVOICE, llm=False)


def body_text(page, tries: int = 6) -> str:
    """The visible text, reloading if a navigation is still settling.

    A sandbox transition redirects, and reading `body` while that redirect is
    in flight raises a timeout rather than showing the page -- which reads as a
    missing caption when the page simply had not arrived.
    """
    last = ""
    for _ in range(tries):
        try:
            last = " ".join(page.inner_text("body").split())
            if last:
                return last
        except Exception:                                    # noqa: BLE001
            pass
        page.wait_for_timeout(1500)
        try:
            page.goto(f"{BASE}/app?ws={BOOK}&tab=money-out", wait_until="load",
                      timeout=20000)
            page.wait_for_timeout(1200)
        except Exception:                                    # noqa: BLE001
            continue
    return last


def invoice_paid() -> tuple[int, str]:
    con = sqlite3.connect(f"file:{_db()}?mode=ro", uri=True)
    try:
        return con.execute("select amount_paid_minor, status from "
                           "invoice_receivable where number = ?",
                           (INVOICE,)).fetchone()
    finally:
        con.close()


def fail_transfer(page) -> str:
    con = sqlite3.connect(f"file:{_db()}?mode=ro", uri=True)
    try:
        rows = con.execute("select payload_json, coalesce(airwallex_object_id, '') "
                           "from plan_step order by id").fetchall()
    finally:
        con.close()
    oid = next((o for p_, o in rows if BILL in (p_ or "") and o), "")
    if not oid:
        return ""
    page.goto(f"{BASE}/app?ws={BOOK}&tab=money-out", wait_until="load")
    page.wait_for_timeout(1500)
    page.evaluate("() => { const d = document.getElementById('sandbox-controls'); "
                  "if (d) d.open = true; }")
    page.wait_for_timeout(400)
    page.evaluate("""(tid) => {
        const row = document.getElementById('sandbox-row-' + tid);
        const btn = row && Array.from(row.querySelectorAll('button'))
          .find(b => /fail/i.test(b.textContent));
        if (btn) btn.click(); }""", oid)
    page.wait_for_timeout(11000)
    return oid


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:                                    # pragma: no cover
        print(f"playwright unavailable: {exc}")
        return 2

    seed()
    SHOTS.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1440, "height": 1100})
        page = ctx.new_page()
        try:
            # ---- 1. the duplicate is credit, and says so -------------------
            page.goto(f"{BASE}/app?ws={BOOK}&tab=money-in", wait_until="load")
            page.wait_for_timeout(1800)
            body = " ".join(page.inner_text("body").split())
            want = (f"{INVOICE} is already paid; USD 500.00 held as credit, "
                    f"nothing applied.")
            print("money in says:", body[:260])
            if want not in body:
                problems.append("the duplicate sentence is not on Money in")
            if "matched" in body.lower() and want not in body:
                problems.append("the duplicate still reads as matched")
            paid = invoice_paid()
            print("invoice:", paid)
            if paid != (120_000, "PAID"):
                problems.append(f"the settled invoice moved: {paid}")
            page.screenshot(path=str(SHOTS / "1-duplicate-is-credit.png"),
                            full_page=True)

            # ---- 2. fail a transfer, read the caption ----------------------
            page.goto(f"{BASE}/app?ws={BOOK}&tab=ok", wait_until="load")
            page.wait_for_timeout(1200)
            page.request.post(f"{BASE}/money-out/run?ws={BOOK}")
            page.wait_for_timeout(4000)
            # the plan ran AFTER the page was rendered, so the card is not in
            # this DOM yet: reload before looking for it.
            page.goto(f"{BASE}/app?ws={BOOK}&tab=ok", wait_until="load")
            page.wait_for_timeout(1500)
            hit = page.evaluate("""(num) => {
                const f = Array.from(document.querySelectorAll(
                    "form[data-action][action*='/approvals']"))
                  .find(x => (x.closest('.x-card, [data-card]') || x)
                             .textContent.includes(num));
                return f ? f.getAttribute('action') : null; }""", BILL)
            print("approval form:", hit)
            if hit:
                page.click(f'form[action="{hit}"] input[name=pin]')
                page.keyboard.type(PIN)
                page.click(f'form[action="{hit}"] button[type=submit]')
                page.wait_for_timeout(7000)
            oid = fail_transfer(page)
            print("failed transfer:", (oid or "(none)")[:8])
            if not oid:
                problems.append("nothing was sent, so Fail could not be tried")
            else:
                body = body_text(page)
                if "Failed:" not in body:
                    problems.append("no Failed caption before the restart")
                page.screenshot(path=str(SHOTS / "2-caption-before-restart.png"),
                                full_page=True)

                # ---- 3. RESTART the tester, then read it again -------------
                print("restarting closebook-tester…")
                subprocess.run(["sudo", "systemctl", "restart",
                                "closebook-tester"], check=False, timeout=90)
                for _ in range(40):
                    page.wait_for_timeout(500)
                    try:
                        page.goto(f"{BASE}/app?ws={BOOK}&tab=money-out",
                                  wait_until="load", timeout=15000)
                        page.wait_for_timeout(1200)
                        body2 = body_text(page)
                        if "Failed:" in body2 or "Returned:" in body2:
                            break
                    except Exception:
                        continue
                body2 = body_text(page)
                print("after restart:", body2[:260])
                if "Failed:" not in body2 and "Returned:" not in body2:
                    problems.append("the caption is gone after a restart")
                page.screenshot(path=str(SHOTS / "3-caption-after-restart.png"),
                                full_page=True)
        finally:
            ctx.close()
            b.close()

    print("\nRESULT:", "PASS" if not problems else "FAIL")
    for p_ in problems:
        print("  -", p_)
    print(f"screenshots in {SHOTS}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
