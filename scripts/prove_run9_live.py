"""Run 9, live on the TESTER: the resolve form works, and 1,200.00 means 1,200.00.

Two run-9 items, one pass over the live tester:

  1. The money: a USD 12,000.00 deposit quoting a USD 1,200.00 invoice, approved
     with the PIN. The confirmation said "USD 1,200.00 has been applied" and the
     invoice then showed USD 120,000.00 paid, because the overpayment was added
     to the invoice's paid amount as well as the allocation.
  2. The form: on Money in the resolve card lives inside a collapsed deposit row,
     so its fields were in the DOM and unreachable -- five tester tests timed out
     waiting for an element they could never click.

The book is seeded with the APP'S OWN writers (a real sandbox deposit needs a
provider for the intake shape, and the invoice is data, not behaviour); every
action after that is a real browser on the live tester.

Usage: .venv/bin/python scripts/prove_run9_live.py [book]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
BOOK = sys.argv[1] if len(sys.argv) > 1 else "r9-live-9"
PIN = os.environ.get("TESTSPRITE_PIN") or "r9livepin"
BASE = os.environ.get("TESTSPRITE_BASE_URL",
                      "https://professor-apr-girls-meanwhile.trycloudflare.com")
SHOTS = Path("/tmp/proof/run9-live")
CUSTOMER = "Brightline Retail"
INVOICE = "INV-R9-1001"
INVOICE_MINOR = 120_000          # USD 1,200.00
DEPOSIT_MINOR = 12_000_000       # USD 120,000.00: 100x, so a unit bug is loud


def seed() -> None:
    """A customer, an invoice, and a payment that quotes it (a proposal)."""
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("CLOSEBOOK_WORKSPACE_ROOT", str(REPO / "data" / "workspaces"))
    from app.db import DB
    from app.domain.intake import record_and_match
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import create_customer, create_invoice

    path = REPO / "data" / "workspaces" / f"{BOOK}.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = DB(f"sqlite:///{path}")
    with db.session_scope() as s:
        save_settings(s, budget_currency="USD", reserve_floor="0",
                      auto_pay_limit="2500", fx_tolerance_pct="0.50")
        cust = create_customer(s, name=CUSTOMER)
        create_invoice(s, customer_id=cust.id, number=INVOICE, currency="USD",
                       amount=str(INVOICE_MINOR / 100))
    with db.session_scope() as s:
        record_and_match(s, deposit_id="dep-r9-live", currency="USD",
                         amount_minor=DEPOSIT_MINOR, payer_name="Apex Factors Ltd.",
                         reference=INVOICE, llm=False)


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
        ctx = b.new_context(viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("response", lambda r: console.append(f"HTTP {r.status} {r.url}")
                if r.status >= 400 else None)
        try:
            # ---- 2. the form on MONEY IN, where the tester looked -----------
            url = f"{BASE}/app?ws={BOOK}&tab=money-in"
            page.goto(url, wait_until="load")
            page.wait_for_timeout(1500)
            forms = page.eval_on_selector_all(
                'form[data-action][action*="/resolve"]',
                "fs => fs.map(f => f.getAttribute('action'))")
            if not forms:
                problems.append("no resolve form on Money in")
            else:
                fid = f'form[action="{forms[0]}"]'
                for node in page.query_selector_all(
                        f'{fid} input, {fid} select'):
                    if not node.is_visible():
                        problems.append(
                            f"{node.get_attribute('id')} is in the DOM and not "
                            f"visible to a person")
                options = page.eval_on_selector_all(
                    f'{fid} select[name=what] option',
                    "os => os.map(o => o.value)")
                print("options:", options)
                if "match" in options:
                    page.select_option(f"{fid} select[name=what]", "match")
                    page.click(f"{fid} input[name=pin]")
                    page.keyboard.type(PIN)
                    visible_pin = page.input_value(f"{fid} input[name=pin]")
                    print("the PIN landed:", visible_pin == PIN)
                    if visible_pin != PIN:
                        problems.append("the PIN did not reach the field")
                    page.click(f"{fid} button[type=submit]")
                    page.wait_for_timeout(3500)
                    reply = " ".join(page.inner_text("body").split())
                    print("reply:", reply[:220])
                    if "USD 1,200.00 has been applied" not in reply:
                        problems.append("the reply does not name USD 1,200.00")
                    if "USD 118,800.00" not in reply:
                        problems.append("the overpayment is not reported")
                else:
                    problems.append(f"the resolve form offers no match: {options}")
            page.screenshot(path=str(SHOTS / "21-resolved-on-money-in.png"),
                            full_page=True)

            # ---- 1. the books: 1,200.00 paid, not 120,000.00 ----------------
            page.goto(f"{BASE}/app?ws={BOOK}&tab=money-in", wait_until="load")
            page.wait_for_timeout(1500)
            body = " ".join(page.inner_text("body").split())
            page.screenshot(path=str(SHOTS / "22-the-invoice-after.png"),
                            full_page=True)
            # THE BOOKS THEMSELVES, read from the tester's own database: the
            # page legitimately shows the USD 120,000.00 DEPOSIT, so a
            # whole-page search cannot tell the two figures apart, and the
            # question here is what the invoice's paid amount actually IS.
            import sqlite3

            db = REPO / "data" / "workspaces" / f"{BOOK}.sqlite3"
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "select number, currency, amount_minor, amount_paid_minor, "
                    "status from invoice_receivable where number = ?",
                    (INVOICE,)).fetchone()
                ledger = con.execute(
                    "select coalesce(sum(credit_minor), 0) from ledger_entry "
                    "where account = 'receivable_clearing'").fetchone()[0]
            finally:
                con.close()
            print("invoice row from the books:", row, "| clearing credited:", ledger)
            if row is None:
                problems.append("no invoice row to judge")
            else:
                _n, ccy, total, paid, status = row
                if paid != INVOICE_MINOR:
                    problems.append(
                        f"the invoice's paid amount is {paid} minor, not "
                        f"{INVOICE_MINOR} (the whole {DEPOSIT_MINOR} deposit was "
                        f"{DEPOSIT_MINOR / max(paid, 1):.0f}x it)")
                if paid > total:
                    problems.append(
                        f"the invoice is paid {paid} on a total of {total}")
                if ledger != INVOICE_MINOR:
                    problems.append(
                        f"the ledger cleared {ledger} while {INVOICE_MINOR} was "
                        f"applied")
                print(f"paid {paid} of {total} {ccy}, status {status}")
        finally:
            ctx.close()
            b.close()

    for line in console[-8:]:
        print("   ", line)
    print("\nRESULT:", "PASS" if not problems else "FAIL")
    for p_ in problems:
        print("  -", p_)
    print(f"screenshots in {SHOTS}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
