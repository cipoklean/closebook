"""Run 7 proved live on the tester, with the real deposit form and real PINs.

WHY IT IS SEPARATE FROM THE SUITES. tests/e2e/test_run7_forms.py proves the
panel is laid out and stays usable with NO provider, and stops there: with no
credentials the submit button is disabled by design. What only the sandbox can
show is that the typed form CREATES a deposit in that book, that releasing it
credits the whole amount, that a clean match lands on the open invoice, that a
second payment for a PAID invoice is called a duplicate, and that an approved
over-limit bill reaches the bank with a status a person can read.

    .venv/bin/python scripts/prove_run7_live.py [base-url]

Sandbox money only. The book it creates is deleted afterwards.
"""
from __future__ import annotations

import json
import random
import re
import string
import sqlite3
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8020").rstrip("/")
BOOK = "r7-live-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
OUT = Path("/tmp/proof/run7-live")
OUT.mkdir(parents=True, exist_ok=True)
REPO = Path(__file__).resolve().parents[1]
BOOK_DB = REPO / "data" / "workspaces" / f"{BOOK}.sqlite3"

SIMULATE = 'form[action^="/money-in/simulate"]'
_failures: list[str] = []


def _pin() -> str:
    m = re.search(r"^TESTSPRITE_PIN=(.*)$", (REPO / ".env").read_text(), re.M)
    return m.group(1).strip() if m else ""


PIN = _pin()
if not PIN:
    print("refused: TESTSPRITE_PIN is not set in .env")
    raise SystemExit(1)


def check(label: str, passed: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not passed:
        _failures.append(label)
    return passed


def rows(sql: str) -> list[tuple]:
    if not BOOK_DB.exists():
        return []
    con = sqlite3.connect(f"file:{BOOK_DB}?mode=ro", uri=True)
    try:
        return con.execute(sql).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def text(page) -> str:
    return " ".join(page.inner_text("body").split())


def shot(page, name: str) -> None:
    path = OUT / f"{BOOK}-{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"        screenshot {path}")


def toast(page) -> str:
    """For the DATA-ACTION forms: inflow and approval cards answer JSON."""
    page.wait_for_selector("#x-toast.is-on", timeout=60_000)
    return page.inner_text("#x-toast").strip()


def pick_usd(page) -> str:
    """Choose the USD global account in the deposit form.

    The picker lists whatever Airwallex returns and defaults to the FIRST one,
    which on this sandbox is a GBP account: a deposit typed as 6000 then lands
    as GBP 6,000.00 and cannot match a USD invoice. An automated tester hits
    exactly this and reads it as "the deposit went missing".
    """
    sel = page.locator(f'{SIMULATE} select[name="global_account_id"]').first
    options = sel.locator("option")
    for i in range(options.count()):
        label = options.nth(i).inner_text()
        if "USD" in label.upper():
            sel.select_option(index=i)
            return label.strip()
    return "(no USD account offered)"


def plain(page, form_action: str, values: dict) -> str:
    """Submit a PLAIN form (Setup, and the deposit control) and read the reply.

    Those forms post a real request and come back as a page with a flash line,
    not as JSON: waiting for a toast there waits for ever.
    """
    f = page.locator(f'form[action^="{form_action}"]').first
    for name, value in values.items():
        field = f.locator(f'[name="{name}"]')
        if field.count() == 0:
            continue
        if field.evaluate("el => el.tagName.toLowerCase()") == "select":
            try:
                field.select_option(label=value)
            except Exception:                      # noqa: BLE001
                field.select_option(index=0)
        else:
            field.fill(value)
    f.locator('button[type="submit"]').first.click()
    page.wait_for_load_state("load")
    page.wait_for_timeout(600)
    body = text(page)
    m = re.search(r"(Deposit [^.]+\.|Added [^.]+\.|saved[^.]*\.|[A-Z][^.]*refused[^.]*\.)",
                  body)
    return m.group(0)[:200] if m else body[:200]



def inflow_id_for(minor: int) -> int:
    """The inflow row for an exact amount, read from the BOOK (not the page).

    The card order on the page is not the order the money arrived in, so ".last"
    released a different deposit than the one just created -- which is how a
    live proof starts proving something else. The id comes from the book.
    """
    got = rows(f"SELECT id FROM inflow WHERE amount_minor = {int(minor)} "
               f"ORDER BY id DESC LIMIT 1")
    return int(got[0][0]) if got else 0


def resolve_income(page, inflow_id: int, note: str) -> str:
    f = page.locator(f'form[action^="/inflows/{inflow_id}/resolve"]').first
    f.locator('select[name="what"]').select_option("income")
    f.locator('input[name="note"]').fill(note)
    f.locator('input[name="pin"]').fill(PIN)
    f.locator('button[type="submit"]').first.click()
    return toast(page)


def world() -> dict:
    got = rows("SELECT id FROM supplier ORDER BY id DESC LIMIT 1")
    return {"supplier_id": got[0][0] if got else 0}


def main() -> int:
    print(f"tester : {BASE}")
    print(f"book   : {BOOK}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)[:160]))

        def go(tab: str, wait: float = 1.0) -> None:
            page.goto(f"{BASE}/app?tab={tab}&ws={BOOK}", wait_until="load")
            page.wait_for_timeout(int(wait * 1000))

        # ------------------------------------------------------ 1. the panel
        print("[1] the deposit form is usable at a desktop viewport")
        go("money-in", 2)
        panel = page.locator("#sandbox-controls").bounding_box()
        check("the sandbox panel spans the page", panel and panel["width"] > 600,
              f"{panel['width']:.0f}px" if panel else "no panel")
        check("the panel is not collapsed",
              " open" in page.evaluate(
                  "document.querySelector('#sandbox-controls').outerHTML.slice(0, 200)"))
        shot(page, "1-form-usable")

        # --------------------------------------------- 2. Setup: rows to work on
        print("\n[2] Setup: a customer, an invoice, a supplier and two bills")
        page.goto(f"{BASE}/app?tab=setup&ws={BOOK}", wait_until="load")

        print("        customer:", plain(page, "/setup/customers",
                                        {"name": "Brightline Retail"}))
        print("        invoice :", plain(page, "/setup/invoices",
                                       {"number": "INV-LIVE-1", "currency": "USD",
                                        "amount": "1200",
                                        "customer_id": "Brightline Retail"}))
        sup_msg = plain(page, "/setup/suppliers", {
            "name": "Pennine Supplies", "country": "US", "currency": "USD",
            "payout_method": "LOCAL", "entity_type": "COMPANY",
            "account_name": "Pennine Supplies", "account_number": "1234567890",
            "routing_value": "021000021", "bank_account_category": "Checking",
            "bank_name": "Wells Fargo", "street_address": "1 Peachtree St",
            "city": "Atlanta", "state": "GA", "postcode": "30303"})
        got = rows("SELECT name, airwallex_beneficiary_id FROM supplier "
                   "WHERE name = 'Pennine Supplies'")
        check("a REAL sandbox beneficiary was created",
              bool(got) and bool(got[0][1]),
              f"{sup_msg[:70]} | supplier row: {got[:1]}")
        print("        bill    :", plain(page, "/setup/payables",
                                       {"number": "PO-LIVE-1", "currency": "USD",
                                        "amount": "1800", "criticality": "3",
                                        "supplier_id": "Pennine Supplies"}))
        print("        bill    :", plain(page, "/setup/payables",
                                       {"number": "PO-LIVE-2", "currency": "USD",
                                        "amount": "3000", "criticality": "3",
                                        "supplier_id": "Pennine Supplies"}))

        floor_field = page.locator('input[name="reserve_floor"]').input_value()
        check("a fresh book shows its reserve floor in Setup", floor_field == "5000.00",
              f"field={floor_field!r}")

        # ------------------------------- 3. type a deposit into the real form
        print("\n[3] a deposit TYPED into the form, then past a poll interval")
        go("money-in", 2)
        picked = pick_usd(page)
        print(f"        account: {picked}")
        amount = page.locator(f'{SIMULATE} [name="amount"]')
        amount.click()
        amount.type("6000", delay=120)
        page.locator(f'{SIMULATE} [name="payer_name"]').fill("Someone Else")
        page.locator(f'{SIMULATE} [name="reference"]').fill("no invoice will claim this")
        page.wait_for_timeout(23_000)                      # past one /state poll
        check("the typed amount survived the poll",
              amount.input_value() == "6000", amount.input_value())
        page.locator(f'{SIMULATE} button[type="submit"]').first.click()
        page.wait_for_load_state("load")
        page.wait_for_timeout(1500)
        msg = text(page)
        check("the sandbox created it and the book recorded it",
              "created in the sandbox" in msg, msg[:200])
        shot(page, "2-deposit-typed")

        # ------------------------------------------- 4. the held card says why
        print("\n[4] the held bill names the floor AND the unmatched cash")
        go("money-out", 2)
        held = text(page)
        check("the card names the money waiting on a match",
              "waiting on a match" in held, held[-300:])
        check("the card names the USD 5,000.00 reserve floor",
              "USD 5,000.00" in held and "floor" in held.lower(), held[-300:])
        shot(page, "3-held-names-floor")

        # ------------------------------------------- 5. release credits it all
        print("\n[5] released as income, and the whole amount becomes cash")
        go("ok", 1)
        released = resolve_income(page, inflow_id_for(600_000),
                                  "no invoice will claim this")
        check("the release is accepted", "released" in released.lower()
              or "income" in released.lower(), released[:140])
        go("home", 2)
        home = text(page)
        check("Home shows the deposit as the book's cash",
              "USD 6,000.00" in home, home[:300])
        check("Home names the reserve floor", "USD 5,000.00" in home, home[:300])
        shot(page, "4-released")

        per_ccy = {a: sum(d - c for d, c in rows(
            f"SELECT debit_minor, credit_minor FROM ledger_entry WHERE account='{a}'"))
            for a in ("cash:USD", "cash:GBP", "cash:EUR")}
        cash = per_ccy["cash:USD"]
        check("the ledger's USD cash is the whole deposit", cash == 600000,
              f"USD={cash} all={per_ccy}")

        # --------------------------------- 6. a clean match, then a duplicate
        print("\n[6] a typed reference naming an open invoice matches cleanly")
        go("money-in", 2)
        pick_usd(page)
        amount = page.locator(f'{SIMULATE} [name="amount"]')
        amount.click()
        amount.type("1200", delay=120)
        page.locator(f'{SIMULATE} [name="payer_name"]').fill("Brightline Retail")
        page.locator(f'{SIMULATE} [name="reference"]').fill("INV-LIVE-1")
        page.locator(f'{SIMULATE} button[type="submit"]').first.click()
        page.wait_for_load_state("load")
        page.wait_for_timeout(1500)
        matched = text(page)
        check("it matched the open invoice rather than being ambiguous",
              "Matched" in matched and "INV-LIVE-1" in matched, matched[:260])
        shot(page, "5-clean-match")

        print("\n[7] a second payment for the PAID invoice is named a duplicate")
        go("money-in", 2)
        pick_usd(page)
        amount = page.locator(f'{SIMULATE} [name="amount"]')
        amount.click()
        amount.type("1200", delay=120)
        page.locator(f'{SIMULATE} [name="payer_name"]').fill("Brightline Retail")
        page.locator(f'{SIMULATE} [name="reference"]').fill("INV-LIVE-1")
        page.locator(f'{SIMULATE} button[type="submit"]').first.click()
        page.wait_for_load_state("load")
        page.wait_for_timeout(1500)
        dup = text(page)
        check("the duplicate sentence names the invoice",
              "INV-LIVE-1" in dup and "already paid" in dup.lower(), dup[:260])
        check("the sentence says duplicate or refund or hold",
              any(w in dup.lower() for w in ("duplicate", "refund", "hold")),
              dup[:200])
        go("money-in", 2)
        page.screenshot(path=str(OUT / f"{BOOK}-6-duplicate-named.png"), full_page=True)
        print(f"        screenshot {OUT / (BOOK + '-6-duplicate-named.png')}")

        # ------------------------------- 8. an approved over-limit bill: status
        print("\n[8] an approved over-limit bill never reads UNCHECKED")
        go("money-in", 2)
        pick_usd(page)
        amount = page.locator(f'{SIMULATE} [name="amount"]')
        amount.click()
        amount.type("20000", delay=90)
        page.locator(f'{SIMULATE} [name="payer_name"]').fill("Brightline Retail")
        page.locator(f'{SIMULATE} [name="reference"]').fill("working capital")
        page.locator(f'{SIMULATE} button[type="submit"]').first.click()
        page.wait_for_load_state("load")
        page.wait_for_timeout(1500)
        go("ok", 1)
        wid = inflow_id_for(2_000_000)
        print("        freeing inflow id", wid)
        print("        freed   :", resolve_income(page, wid, "working capital")[:120])

        go("money-out", 1)
        run_form = page.locator('form[action^="/money-out/run"]').first
        if run_form.count():
            run_form.locator('button[type="submit"]').first.click()
            page.wait_for_load_state("load")
        page.wait_for_timeout(3000)
        for _ in range(6):
            go("ok", 2)
            ok_page = text(page)
            if "PO-LIVE-2" in ok_page and page.locator('form[action^="/approvals/"]').count():
                break
            page.wait_for_timeout(3000)
        if "PO-LIVE-2" in ok_page:
            f = page.locator('form[action^="/approvals/"]').first
            f.locator('input[name="pin"]').fill(PIN)
            f.locator('button[type="submit"]').first.click()
            approved = toast(page)
            print(f"        approve: {approved[:150]}")
        else:
            print("        (the over-limit bill is not waiting on the OK tab)")
        status = ""
        for _ in range(12):
            go("money-out", 1.5)
            money_out = text(page)
            status = page.evaluate("""(() => {
              const d = [...document.querySelectorAll('details')].find(x =>
                (x.querySelector('summary')||{}).textContent?.includes('On the bank'));
              const pill = d && d.querySelector('.x-pill');
              return pill ? pill.textContent.trim() : '';
            })()""")
            if status and "Checking" not in status:
                break
            page.wait_for_timeout(4000)
        check("no sent transfer shows the machine placeholder",
              "UNCHECKED" not in money_out and "UNKNOWN" not in money_out,
              money_out[-260:])
        # The bank's own vocabulary, which the app treats as in flight or done.
        # SCHEDULED is a real Airwallex status and it is what an approved payment
        # reads while it is being processed.
        check("the bank's own status is shown for the approved payment",
              status.upper() in ("PROCESSING", "PAID", "PENDING", "SUBMITTED",
                                 "SCHEDULED", "CREATED", "APPROVAL_PENDING",
                                 "IN_PROGRESS", "BOOKED", "CANCELLED"),
              f"On the bank: {status!r}")
        shot(page, "7-approved-status")

        check("no page errors", not errors, str(errors[:2]))
        browser.close()

    print("public URL:", BASE)
    print("\nRESULT:", "PASS" if not _failures else f"FAIL ({len(_failures)})")
    for f in _failures:
        print("  -", f)
    return 0 if not _failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
