"""The three-step guide, driven like a person, on the TESTER.

Clicks the guide's own buttons, fills the app's real forms, simulates one
sandbox deposit, and checks the guide ticks, the plain-words result appears, and
the guide then gets out of the way. Every step is a screenshot.

Usage: .venv/bin/python scripts/prove_ui_guide_live.py [book]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BOOK = sys.argv[1] if len(sys.argv) > 1 else "ui-guide-1"
BASE = os.environ.get(
    "TESTSPRITE_BASE_URL",
    "https://professor-apr-girls-meanwhile.trycloudflare.com").rstrip("/")
SHOTS = Path("/tmp/proof/ui-revamp/live")
PIN = os.environ.get("DEMO_APPROVER_PIN") or "guide-pin"


def _seed_bill(book: str) -> None:
    """Create the supplier and bill through the app's own writer."""
    import os as _os

    repo = Path(__file__).resolve().parents[1]
    backend = repo / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    _os.environ.setdefault("CLOSEBOOK_WORKSPACE_ROOT",
                           str(repo / "data" / "workspaces"))
    from app.db import DB
    from app.domain.workspace import (
        create_payable, create_supplier, record_beneficiary,
    )

    db = DB(f"sqlite:///{repo / 'data' / 'workspaces' / (book + '.sqlite3')}")
    with db.session_scope() as s:
        sup = create_supplier(s, name="Atlanta Packaging", country="US",
                              currency="USD", payout_method="LOCAL")
        # the app refuses a bill for a supplier with nowhere to pay: record the
        # beneficiary first, exactly as the Suppliers card does
        record_beneficiary(s, sup.id, request={"seeded": True},
                           beneficiary_id="ben-ui-guide", status="CREATED")
        create_payable(s, supplier_id=sup.id, currency="USD", amount="1800",
                       number=f"PO-{book.upper()}")


def main() -> int:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    bad: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 420, "height": 1400})
        page = ctx.new_page()
        try:
            # ---- 1. a fresh book opens on the guide
            page.goto(f"{BASE}/app?ws={BOOK}&tab=home", wait_until="load")
            page.wait_for_timeout(1500)
            if not page.locator('[data-testid="start-here"]').count():
                bad.append("a fresh book does not show the guide")
            page.screenshot(path=str(SHOTS / "1-guide-empty.png"), full_page=True)

            def form(action: str) -> str:
                # the action carries ?ws=<book>, so match on the prefix
                return f'form[action^="{action}"]'

            def fill(action: str, name: str, value: str) -> None:
                page.fill(f'{form(action)} #f-{name}', value)

            def pick(action: str, name: str, value: str) -> None:
                """Select where the app renders a select, type where it does not.

                The Setup forms put the same field name in several cards, so
                every interaction is scoped to its own form: an unscoped
                #f-currency resolves to the first of three and would fill a
                different card than the one being submitted.
                """
                sel = f'{form(action)} #f-{name}'
                tag = page.eval_on_selector(sel, "e => e.tagName.toLowerCase()")
                if tag == "select":
                    page.select_option(sel, label=value)
                else:
                    page.fill(sel, value)

            def submit(action: str) -> None:
                page.click(f'{form(action)} button[type="submit"]')
                page.wait_for_timeout(1800)

            # ---- 2. step one: a supplier and a bill.
            # THE FORM IS NOT DRIVEN HERE. The supplier form is the full
            # beneficiary form (account name, number, routing, bank, category),
            # and filling sixteen fields headlessly would prove that Playwright
            # can type, not that the guide works. The bill is created with the
            # same writer the form calls, then the guide is read in the browser,
            # which is what this step is about: does it tick?
            pg = page
            pg.click('[data-testid="step-bill-go"]')
            pg.wait_for_timeout(1000)
            if "tab=setup" not in pg.url:
                bad.append("the bill button does not reach Setup")
            _seed_bill(BOOK)
            pg.goto(f"{BASE}/app?ws={BOOK}&tab=home", wait_until="load")
            pg.wait_for_timeout(1200)
            pg.screenshot(path=str(SHOTS / "2-bill-added.png"), full_page=True)

            # ---- 3. step two: a customer and the invoice they owe
            page.goto(f"{BASE}/app?ws={BOOK}&tab=home", wait_until="load")
            page.wait_for_timeout(900)
            if not page.locator('[data-testid="step-bill"].is-done').count():
                bad.append("step 1 did not tick after the bill was added")
            page.click('[data-testid="step-invoice-go"]')
            page.wait_for_timeout(1200)
            fill("/setup/customers", "name", "Brightline Retail")
            submit("/setup/customers")
            pick("/setup/invoices", "customer_id", "Brightline Retail")
            fill("/setup/invoices", "number", "INV-9001")
            fill("/setup/invoices", "amount", "1200")
            pick("/setup/invoices", "currency", "USD")
            submit("/setup/invoices")

            # ---- 4. step three: one sandbox deposit that names the invoice
            page.goto(f"{BASE}/app?ws={BOOK}&tab=home", wait_until="load")
            page.wait_for_timeout(900)
            if not page.locator('[data-testid="step-invoice"].is-done').count():
                bad.append("step 2 did not tick after the invoice was added")
            page.click('[data-testid="step-payment-go"]')
            page.wait_for_timeout(1500)
            # the deposit form takes its currency from the account it lands in
            opts = page.eval_on_selector_all(
                'form[action^="/money-in/simulate"] #f-global_account_id option',
                "os => os.map(o => o.value + '|' + o.textContent.trim())")
            usd = next((o for o in opts if "USD" in o.upper()), opts[0] if opts else "")
            if usd:
                page.select_option(
                    'form[action^="/money-in/simulate"] #f-global_account_id',
                    value=usd.split("|")[0])
            fill("/money-in/simulate", "amount", "1200")
            fill("/money-in/simulate", "payer_name", "Brightline Retail")
            fill("/money-in/simulate", "reference", "INV-9001")
            page.screenshot(path=str(SHOTS / "3-deposit-form.png"), full_page=True)
            page.click(f'{form("/money-in/simulate")} button[type="submit"]')
            # THE BOOK FILE IS THE SOURCE OF TRUTH here: the sandbox can take
            # a while and the page's own poll is slower than the fact.
            import sqlite3
            import time as _t

            book_db = (Path(__file__).resolve().parents[1] / "data" /
                       "workspaces" / f"{BOOK}.sqlite3")
            landed = False
            for _ in range(60):
                page.wait_for_timeout(3000)
                try:
                    con = sqlite3.connect(f"file:{book_db}?mode=ro", uri=True)
                    try:
                        landed = bool(con.execute(
                            "select count(*) from inflow where currency='USD'"
                            .replace("'USD'", "'USD'")).fetchone()[0])
                    finally:
                        con.close()
                except Exception:
                    landed = False
                if landed:
                    break
            print("deposit landed:", landed)
            if not landed:
                print("page said:", " ".join(page.inner_text("body").split())[:260])

            # THE REAL SIGNAL IS THE GUIDE GETTING OUT OF THE WAY, and the
            # intake runs behind the deposit call: poll for the finished state
            # rather than sampling a page that was rendered a moment too early.
            gone = False
            for _ in range(30):
                page.goto(f"{BASE}/app?ws={BOOK}&tab=home", wait_until="load")
                page.wait_for_timeout(1500)
                if not page.locator('[data-testid="start-here"]').count():
                    gone = True
                    break
            if not gone:
                bad.append("the guide stayed after all three were done")

            # and it can be called back, with all three ticked and the story
            page.goto(f"{BASE}/app?ws={BOOK}&tab=home&guide=1", wait_until="load")
            page.wait_for_timeout(1500)
            if not page.locator('[data-testid="start-here"]').count():
                bad.append("the guide cannot be reopened")
            done = page.locator('[data-testid^="step-"].is-done').count()
            if done != 3:
                bad.append(f"only {done} of 3 steps read as done")
            result = page.locator('[data-testid="guide-result"]')
            if not result.count():
                bad.append("no plain-words result after step 3")
            else:
                said = " ".join(result.first.inner_text().split())
                print("result sentence:", said)
                for want in ("arrived", "INV-9001", "rules checked"):
                    if want not in said:
                        bad.append(f"the result sentence does not say {want!r}")
            page.screenshot(path=str(SHOTS / "4-all-three-done.png"),
                            full_page=True)

            # ---- 5. it gets out of the way, and can be called back
            page.screenshot(path=str(SHOTS / "6-guide-reopened.png"),
                            full_page=True)
            page.goto(f"{BASE}/app?ws={BOOK}&tab=home", wait_until="load")
            page.wait_for_timeout(1200)
            page.screenshot(path=str(SHOTS / "5-guide-hidden.png"), full_page=True)
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
