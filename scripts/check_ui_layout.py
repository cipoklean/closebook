"""Does the revamped app actually fit a phone, and does the guide exist?

Structural checks on the real pages, at the width the brief names first: 390px.
Not a screenshot diff -- an assertion per requirement, so a regression fails a
test instead of looking slightly off to whoever is watching the demo.

Usage: .venv/bin/python scripts/check_ui_layout.py
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
ROOT = Path("/tmp/proof/ui-revamp/layout")
EMPTY, DATA = "layout-empty", "layout-data"
TABS = ("home", "money-in", "money-out", "ok", "ai", "setup")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _seed(books: Path) -> None:
    sys.path.insert(0, str(BACKEND))
    from app.db import DB
    from app.domain.intake import record_and_match
    from app.domain.ledger import post
    from app.domain.settings_store import save
    from app.domain.workspace import (
        create_customer, create_invoice, create_payable, create_supplier,
        record_beneficiary,
    )

    import shutil
    shutil.rmtree(books, ignore_errors=True)
    books.mkdir(parents=True, exist_ok=True)
    with DB(f"sqlite:///{books / (EMPTY + '.sqlite3')}").session_scope() as s:
        save(s, budget_currency="USD", reserve_floor="5000",
             auto_pay_limit="2500", fx_tolerance_pct="0.50")
    db = DB(f"sqlite:///{books / (DATA + '.sqlite3')}")
    with db.session_scope() as s:
        save(s, budget_currency="USD", reserve_floor="5000",
             auto_pay_limit="2500", fx_tolerance_pct="0.50")
        sup = create_supplier(s, name="Atlanta Packaging", country="US",
                              currency="USD", payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"x": 1},
                           beneficiary_id="ben-layout", status="CREATED")
        create_payable(s, supplier_id=sup.id, currency="USD", amount="1800",
                       number="PO-7003")
        cust = create_customer(s, name="Brightline Retail")
        create_invoice(s, customer_id=cust.id, number="INV-123", currency="USD",
                       amount="1200")
    with db.session_scope() as s:
        post(s, "opening:USD:layout", [("cash:USD", 900_000, 0, "USD"),
                                       ("opening_balance", 0, 900_000, "USD")])
    with db.session_scope() as s:
        record_and_match(s, deposit_id="dep-layout-1", currency="USD",
                         amount_minor=120_000, payer_name="Brightline Retail",
                         reference="INV-123", llm=False)
    with db.session_scope() as s:
        record_and_match(s, deposit_id="dep-layout-2", currency="USD",
                         amount_minor=70_000, payer_name="Apex Factors",
                         reference="nothing to match", llm=False)


def main() -> int:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    root = ROOT
    books = root / "books"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWX_", "LLM_"))}
    env.update({
        "AWX_CLIENT_ID": "", "AWX_API_KEY": "", "LLM_API_KEY": "",
        "CLOSEBOOK_DATABASE_URL": f"sqlite:///{root / 'shared.sqlite3'}",
        "CLOSEBOOK_HMAC_SECRET": "layout-only",
        "CLOSEBOOK_WORKSPACES": "1", "CLOSEBOOK_WORKSPACE_ROOT": str(books),
        "DEMO_APPROVER_PIN": "layout-pin", "OPERATOR_NAME": "layout",
        "CLOSEBOOK_AUTO_EXECUTE": "0", "PYTHONPATH": str(BACKEND),
    })
    _seed(books)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    bad: list[str] = []
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"{base}/healthz", timeout=2)
                break
            except Exception:
                time.sleep(0.4)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            for width in (390, 1440):
                ctx = b.new_context(viewport={"width": width, "height": 900})
                page = ctx.new_page()
                for book in (EMPTY, DATA):
                    for tab in TABS:
                        page.goto(f"{base}/app?ws={book}&tab={tab}",
                                  wait_until="load")
                        page.wait_for_timeout(500)
                        over = page.evaluate(
                            "() => document.documentElement.scrollWidth")
                        if over > width + 1:
                            bad.append(f"{book}/{tab}@{width}: page is {over}px wide")
                        # the new chips explain themselves
                        # only a RAW STATUS in a chip is a defect; a count or
                        # a label explains itself
                        RAW = ["HELD", "READY", "APPROVED", "PROPOSED",
                               "SUBMITTED", "SCHEDULED", "PROCESSING", "SENT",
                               "PAID", "CANCELLED", "FAILED", "RETURNED",
                               "MATCHED", "PARTIAL", "OVERPAID", "AMBIGUOUS",
                               "UNMATCHED", "REJECTED", "PAYABLE"]
                        untipped = page.evaluate(
                            "(raw) => [...document.querySelectorAll('.x-pill')]"
                            ".filter(p => !p.title && !p.dataset.tip)"
                            ".map(p => p.textContent.trim())"
                            ".filter(t => raw.includes(t.toUpperCase()))"
                            ".slice(0, 3)", RAW)
                        if untipped:
                            bad.append(f"{book}/{tab}@{width}: chips with no "
                                       f"explanation {untipped}")
                # ---- the guide, on the empty book
                page.goto(f"{base}/app?ws={EMPTY}&tab=home", wait_until="load")
                page.wait_for_timeout(400)
                if not page.locator('[data-testid="start-here"]').count():
                    bad.append("an empty book has no Start here guide")
                for key in ("bill", "invoice", "payment"):
                    btn = page.locator(f'[data-testid="step-{key}-go"]')
                    if not btn.count():
                        bad.append(f"step {key} has no button")
                        continue
                    box = btn.first.bounding_box() or {}
                    if float(box.get("width", 0)) < 120:
                        bad.append(f"step {key} button is {box.get('width')}px wide")
                    if not btn.first.get_attribute("href", timeout=1000):
                        bad.append(f"step {key} button has no route")
                # three numbers, and the fold is shut until asked
                if page.locator('[data-testid="home-numbers"] .x-big').count() != 3:
                    bad.append("Home does not show exactly three numbers")
                det = page.locator('[data-testid="home-details"]')
                if det.count() and det.first.evaluate("d => d.open"):
                    bad.append("the Details fold is open on arrival")
                # ---- the guide finishes and then gets out of the way
                page.goto(f"{base}/app?ws={DATA}&tab=home", wait_until="load")
                page.wait_for_timeout(400)
                if page.locator('[data-testid="start-here"]').count():
                    bad.append("a book with data still shows the first-run guide")
                if not page.locator('[data-testid="what-just-happened"]').count():
                    bad.append("Home has no What just happened timeline")
                page.goto(f"{base}/app?ws={DATA}&tab=home&guide=1",
                          wait_until="load")
                page.wait_for_timeout(300)
                if not page.locator('[data-testid="start-here"]').count():
                    bad.append("the guide cannot be reopened")
                # ---- the decision shows all three columns
                page.goto(f"{base}/app?ws={DATA}&tab=ai", wait_until="load")
                page.wait_for_timeout(500)
                for want in ("AI suggestion", "Rule checks", "Result"):
                    if not page.get_by_text(want, exact=True).count():
                        bad.append(f"the AI decision has no {want} column")
                ctx.close()
            b.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\nRESULT:", "PASS" if not bad else "FAIL")
    for b_ in bad:
        print("  -", b_)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
