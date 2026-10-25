"""The narrow duplicate rule, live on the TESTER: case (b).

A reference naming a SETTLED invoice and an OPEN one must pay the open one and
say the settled one was set aside. Before the narrowing it was all-or-nothing:
either the money was held as credit that had an obvious home, or the settled
bill was reported as matched and nothing was applied.

Usage: .venv/bin/python scripts/prove_run12_narrow_live.py [book]
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
BOOK = sys.argv[1] if len(sys.argv) > 1 else "r12-live-1"
BASE = os.environ.get("TESTSPRITE_BASE_URL",
                      "https://professor-apr-girls-meanwhile.trycloudflare.com")
SHOTS = Path("/tmp/proof/run12-live")
PAID_INV = "INV-R12-1001"
OPEN_INV = "INV-R12-1002"
DEPOSIT_MINOR = 80_000          # exactly what the open invoice owes


def _db() -> Path:
    return REPO / "data" / "workspaces" / f"{BOOK}.sqlite3"


def seed() -> None:
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("CLOSEBOOK_WORKSPACE_ROOT",
                          str(REPO / "data" / "workspaces"))
    from app.db import DB
    from app.domain.intake import record_and_match
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import create_customer, create_invoice

    path = _db()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = DB(f"sqlite:///{path}")
    with db.session_scope() as s:
        save_settings(s, budget_currency="USD", reserve_floor="0",
                      auto_pay_limit="2500", fx_tolerance_pct="0.50")
        cust = create_customer(s, name="Brightline Retail")
        create_invoice(s, customer_id=cust.id, number=PAID_INV, currency="USD",
                       amount="1200")
        create_invoice(s, customer_id=cust.id, number=OPEN_INV, currency="USD",
                       amount="800")
    # the first invoice is paid, through the app's own intake
    with db.session_scope() as s:
        record_and_match(s, deposit_id="dep-r12-paid", currency="USD",
                         amount_minor=120_000, payer_name="Brightline Retail",
                         reference=PAID_INV, llm=False)
    # and now ONE payment naming BOTH: it belongs to the open invoice
    with db.session_scope() as s:
        record_and_match(s, deposit_id="dep-r12-both", currency="USD",
                         amount_minor=DEPOSIT_MINOR,
                         payer_name="Brightline Retail",
                         reference=f"{PAID_INV} and {OPEN_INV}", llm=False)


def state() -> list[tuple]:
    con = sqlite3.connect(f"file:{_db()}?mode=ro", uri=True)
    try:
        return con.execute(
            "select number, amount_paid_minor, status from invoice_receivable "
            "order by number").fetchall()
    finally:
        con.close()


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:                                    # pragma: no cover
        print(f"playwright unavailable: {exc}")
        return 2

    seed()
    SHOTS.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    print("books before reading:", state())
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1440, "height": 1100})
        page = ctx.new_page()
        try:
            page.goto(f"{BASE}/app?ws={BOOK}&tab=money-in", wait_until="load")
            page.wait_for_timeout(2000)
            # A DEPOSIT ROW IS A COLLAPSED <details>, and the reason lives in its
            # body: rendered text skips a closed fold, so open them first.
            page.evaluate("() => document.querySelectorAll('details')"
                          ".forEach(d => d.open = true)")
            page.wait_for_timeout(500)
            body = " ".join(page.inner_text("body").split())
            print("page says:", body[:320])
            want = f"{PAID_INV} is already paid, so it was ignored"
            if want not in body:
                problems.append("the paid invoice is not named as set aside")
            if OPEN_INV not in body:
                problems.append("the open invoice is not named")
            if "already paid; USD" in body and want not in body:
                problems.append("the payment was still treated as a duplicate")
            rows = state()
            print("books after:", rows)
            by = {n: (paid, st) for n, paid, st in rows}
            if by.get(PAID_INV) != (120_000, "PAID"):
                problems.append(f"the settled invoice moved: {by.get(PAID_INV)}")
            if by.get(OPEN_INV) != (80_000, "PAID"):
                problems.append(f"the open invoice was not paid: {by.get(OPEN_INV)}")
            page.screenshot(path=str(SHOTS / "narrow-both-named.png"),
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
