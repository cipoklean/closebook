"""Screenshot the four screens a judge meets, at phone and desktop widths.

One book that is EMPTY and one with real rows, both seeded through the app's own
writers, so the shots are of the product and not of a fixture nobody will see.

Usage:
    .venv/bin/python scripts/shoot_ui.py before
    .venv/bin/python scripts/shoot_ui.py after
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
ROOT = Path("/tmp/proof/ui-revamp")
EMPTY_BOOK = "shot-empty"
DATA_BOOK = "shot-data"
TABS = ("home", "money-in", "ok", "ai")
WIDTHS = {"phone": 390, "desktop": 1440}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def seed(books: Path) -> None:
    """A supplier with a bill, a customer with an invoice, and a payment."""
    sys.path.insert(0, str(BACKEND))
    from app.db import DB
    from app.domain.intake import record_and_match
    from app.domain.ledger import post
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import (
        create_customer, create_invoice, create_payable, create_supplier,
        record_beneficiary,
    )

    import shutil
    shutil.rmtree(books, ignore_errors=True)
    books.mkdir(parents=True, exist_ok=True)
    # the empty book still needs its settings row, or every figure is unknown
    db = DB(f"sqlite:///{books / (EMPTY_BOOK + '.sqlite3')}")
    with db.session_scope() as s:
        save_settings(s, budget_currency="USD", reserve_floor="5000",
                      auto_pay_limit="2500", fx_tolerance_pct="0.50")

    db = DB(f"sqlite:///{books / (DATA_BOOK + '.sqlite3')}")
    with db.session_scope() as s:
        save_settings(s, budget_currency="USD", reserve_floor="5000",
                      auto_pay_limit="2500", fx_tolerance_pct="0.50")
        sup = create_supplier(s, name="Atlanta Packaging", country="US",
                              currency="USD", payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"shot": "no provider"},
                           beneficiary_id="ben-shot", status="CREATED")
        create_payable(s, supplier_id=sup.id, currency="USD", amount="1800",
                       number="PO-7003")
        create_payable(s, supplier_id=sup.id, currency="USD", amount="3200",
                       number="PO-7004")
        cust = create_customer(s, name="Brightline Retail")
        create_invoice(s, customer_id=cust.id, number="INV-123", currency="USD",
                       amount="1200")
        create_invoice(s, customer_id=cust.id, number="INV-124", currency="USD",
                       amount="800")
    with db.session_scope() as s:
        post(s, "opening:USD:shot", [("cash:USD", 9_000_00, 0, "USD"),
                                     ("opening_balance", 0, 9_000_00, "USD")])
    with db.session_scope() as s:
        # a payment that names the invoice: the good story
        record_and_match(s, deposit_id="dep-shot-1", currency="USD",
                         amount_minor=120_000, payer_name="Brightline Retail",
                         reference="INV-123", llm=False)
    with db.session_scope() as s:
        # and one nobody can place: the "needs you" story
        record_and_match(s, deposit_id="dep-shot-2", currency="USD",
                         amount_minor=70_000, payer_name="Apex Factors Ltd.",
                         reference="cash with no invoice", llm=False)


def main() -> int:
    which = (sys.argv[1] if len(sys.argv) > 1 else "before").strip().lower()
    out = ROOT / ("before" if which == "before" else "after")
    out.mkdir(parents=True, exist_ok=True)

    root = ROOT / f"run-{which}"
    books = root / "books"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWX_", "LLM_"))}
    env.update({
        "AWX_CLIENT_ID": "", "AWX_API_KEY": "", "LLM_API_KEY": "",
        "CLOSEBOOK_DATABASE_URL": f"sqlite:///{root / 'shared.sqlite3'}",
        "CLOSEBOOK_HMAC_SECRET": "shots-only-not-a-secret",
        "CLOSEBOOK_WORKSPACES": "1",
        "CLOSEBOOK_WORKSPACE_ROOT": str(books),
        "DEMO_APPROVER_PIN": "shot-pin",
        "OPERATOR_NAME": "shots",
        "CLOSEBOOK_AUTO_EXECUTE": "0",
        "PYTHONPATH": str(BACKEND),
    })
    seed(books)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
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
            for label, width in WIDTHS.items():
                ctx = b.new_context(viewport={"width": width,
                                              "height": 1400 if width < 500 else 1100})
                page = ctx.new_page()
                for book in (EMPTY_BOOK, DATA_BOOK):
                    for tab in TABS:
                        url = f"{base}/app?ws={book}&tab={tab}"
                        page.goto(url, wait_until="load")
                        page.wait_for_timeout(900)
                        # open every fold so a screenshot shows the content
                        page.evaluate("() => document.querySelectorAll('details')"
                                      ".forEach(d => d.open = true)")
                        page.wait_for_timeout(300)
                        name = (f"{'empty' if book == EMPTY_BOOK else 'data'}"
                                f"-{tab}-{label}.png")
                        page.screenshot(path=str(out / name), full_page=True)
                        print("shot", name)
                ctx.close()
            b.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"screenshots in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
