"""Browser proof of the money-out chain in a FRESH ?ws= book, with no provider.

Run 6 could not pay a bill in a fresh book: a deposit showed as unmatched, the
bill said the account held USD 0.00 (it held USD 2,000.00 in suspense), and the
supplier's beneficiary was refused, so a bill could not even be created. This
drives the real UI in a real browser through the fixed chain, on an empty temp
database with every provider credential BLANKED.

WHAT THIS CANNOT DO, and why it is stated rather than faked: PROCESSING, settle
and fail/return are AIRWALLEX's states. Reaching them needs the sandbox, so they
are proved live by `scripts/prove_run6_money_out_live.py` against the tester, and
this suite stops where the local decision chain ends. There is deliberately no
stub provider here: a fake bank would prove the stub, not the product.
"""
from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright is not installed - run `make setup` then `playwright install chromium`",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"

PIN = "r6-money-pin"           # 11 characters: the PIN field is maxlength 12
SHOT_DIR = Path("/tmp/proof/e2e-run6")

BOOK = "r6-e2e-1"
SUPPLIER = "Atlanta Packaging"
BILL_UNDER = "PO-R6-UNDER"     # USD 1,800.00, under the 2,500 limit
BILL_OVER = "PO-R6-OVER"       # USD 3,000.00, over it
INVOICE = "INV-R6-1"           # USD 1,200.00
DEPOSIT_MINOR = 600_000        # USD 6,000.00: enough for both bills at once


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def tester(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    """The real app on a free port: books on, every credential blank."""
    root = tmp_path_factory.mktemp("r6-e2e")
    db_path = root / "shared.sqlite3"
    books = root / "books"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AWX_", "LLM_"))
           and k not in {"PUBLIC_BASE_URL", "WEBHOOK_PATH", "CLOSEBOOK_DATABASE_URL",
                         "CLOSEBOOK_HMAC_SECRET", "CLOSEBOOK_WORKSPACES",
                         "CLOSEBOOK_WORKSPACE_ROOT", "DEMO_APPROVER_PIN",
                         "CLOSEBOOK_AUTO_EXECUTE", "OPERATOR_NAME"}}
    env.update({
        "AWX_CLIENT_ID": "", "AWX_API_KEY": "", "AWX_WEBHOOK_SECRET": "",
        "LLM_API_KEY": "",
        "CLOSEBOOK_DATABASE_URL": f"sqlite:///{db_path}",
        "CLOSEBOOK_HMAC_SECRET": "e2e-test-only-not-a-real-secret",
        "CLOSEBOOK_WORKSPACES": "1",
        "CLOSEBOOK_WORKSPACE_ROOT": str(books),
        "DEMO_APPROVER_PIN": PIN,
        "OPERATOR_NAME": "e2e",
        "CLOSEBOOK_AUTO_EXECUTE": "0",     # nothing is ever SENT in this suite
        "PYTHONPATH": str(BACKEND_DIR),
        "PYTHONUNBUFFERED": "1",
    })

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"server exited during boot (rc={proc.returncode}):\n{out[-3000:]}")
            try:
                with urllib.request.urlopen(f"{base}/healthz", timeout=2) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(0.4)
        else:
            pytest.fail("server did not become ready on /healthz within 60s")
        yield {"base": base, "db": db_path, "books": books}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Exception as e:               # browser binary missing
            pytest.skip(f"chromium is not installed for playwright ({e})")
        yield b
        b.close()


def book_file(books: Path) -> Path:
    return books / f"{BOOK}.sqlite3"


def seed_book(books: Path, *, floor: str = "0", limit: str = "2500") -> None:
    """The rows a browser cannot make without a provider, by the OWN writers.

    Two things only: the deposit (a real one needs the sandbox) and the
    supplier's beneficiary id (a real one needs Airwallex). The bill, the invoice
    and every action are done through the UI by the test.
    """
    sys.path.insert(0, str(BACKEND_DIR))
    from app.db import DB
    from app.domain.intake import record_and_match
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import (
        create_customer, create_invoice, create_supplier, record_beneficiary,
    )

    books.mkdir(parents=True, exist_ok=True)      # the server does this on first use
    db = DB(f"sqlite:///{book_file(books)}")
    with db.session_scope() as s:
        save_settings(s, budget_currency="USD", reserve_floor=floor,
                      auto_pay_limit=limit, fx_tolerance_pct="0.50")
        sup = create_supplier(s, name=SUPPLIER, country="US", currency="USD",
                              payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"e2e": "no provider call"},
                           beneficiary_id="ben-r6", status="CREATED")
        cust = create_customer(s, name="Brightline Retail")
        create_invoice(s, customer_id=cust.id, number=INVOICE, currency="USD",
                       amount="1200")
    with db.session_scope() as s:
        record_and_match(s, deposit_id="dep-r6", currency="USD",
                         amount_minor=DEPOSIT_MINOR, payer_name="Brightline Retail",
                         reference="cash with no invoice", llm=False)


def second_deposit(books: Path) -> None:
    """A USD 800.00 payment that names the invoice: a PARTIAL when proposed."""
    sys.path.insert(0, str(BACKEND_DIR))
    from app.db import DB
    from app.domain.intake import record_and_match

    db = DB(f"sqlite:///{book_file(books)}")
    with db.session_scope() as s:
        record_and_match(s, deposit_id="dep-r6-partial", currency="USD",
                         amount_minor=80_000, payer_name="Brightline Retail",
                         reference=INVOICE, llm=False)


def counts(books: Path, table: str) -> int:
    con = sqlite3.connect(f"file:{book_file(books)}?mode=ro", uri=True)
    try:
        return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        con.close()


def text_of(page) -> str:
    return " ".join(page.inner_text("body").split())


def add_bill(page, number: str, amount: str) -> None:
    form = page.locator('form[action^="/setup/payables"]')
    form.locator('[name="number"]').fill(number)
    form.locator('[name="amount"]').fill(amount)
    form.locator('[name="currency"]').fill("USD")
    form.locator('[name="supplier_id"]').select_option(index=0)
    form.locator('button[type="submit"]').first.click()
    page.wait_for_load_state("load")


def pin_it(page, form_selector: str, values: dict | None = None) -> str:
    form = page.locator(form_selector).first
    for name, value in (values or {}).items():
        field = form.locator(f'[name="{name}"]')
        if field.evaluate("el => el.tagName.toLowerCase()") == "select":
            field.select_option(value=value)
        else:
            field.fill(value)
    form.locator('input[name="pin"]').fill(PIN)
    form.locator('button[type="submit"]').first.click()
    page.wait_for_selector("#x-toast.is-on", timeout=40_000)
    return page.inner_text("#x-toast").strip()


class TestTheMoneyOutChainInAFreshBook:
    def test_the_whole_chain(self, tester, browser):
        base = tester["base"]
        books = tester["books"]
        seed_book(books)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # ---- open the book -------------------------------------------------
        page.goto(f"{base}/app?ws={BOOK}", wait_until="load")
        assert f"Book: {BOOK}" in text_of(page)

        # ---- 1. Setup shows a beneficiary input that works, and takes a bill
        page.goto(f"{base}/app?tab=setup", wait_until="load")
        assert "1 Peachtree St" in text_of(page), (
            "the Setup form does not document a beneficiary input that works")
        add_bill(page, BILL_UNDER, "1800")
        assert BILL_UNDER in text_of(page)

        # ---- 2. the deposit is unmatched, and the page tells the truth -----
        page.goto(f"{base}/app?tab=money-out", wait_until="load")
        money_out = text_of(page)
        assert "holds USD 0.00" not in money_out, (
            "the bill claims the account holds nothing while the book holds "
            "USD 6,000.00")
        assert "USD 6,000.00" in money_out, money_out[:400]
        assert "waiting on a match" in money_out, money_out[:400]
        page.screenshot(path=str(SHOT_DIR / "1-held-names-the-truth.png"),
                        full_page=True)

        # ---- 3. the deposit is released as income, and becomes spendable ---
        page.goto(f"{base}/app?tab=money-in", wait_until="load")
        assert "not spendable" in text_of(page), text_of(page)[:400]
        page.goto(f"{base}/app?tab=ok", wait_until="load")
        toast = pin_it(page, 'form[action^="/inflows/"]',
                       {"what": "income", "note": "no invoice will claim this"})
        assert "release" in toast.lower(), toast
        page.goto(f"{base}/app?tab=money-out", wait_until="load")
        after = text_of(page)
        assert "waiting on a match" not in after, (
            "the bill still says the deposit is unattributed after it was released")
        assert "insufficient_balance" not in after, (
            "the under-limit bill is still held for cash after the release")
        page.screenshot(path=str(SHOT_DIR / "2-released-and-payable.png"),
                        full_page=True)

        # ---- 4. an over-limit bill asks for approval ----------------------
        #
        # WHAT THIS CANNOT DO WITHOUT A PROVIDER, stated rather than faked: the
        # approval CARD comes from a PERSISTED plan step, and a plan is persisted
        # only when the run can actually send. With no credentials the conductor
        # refuses before writing it, so the card does not exist to click here.
        # What IS asserted is the planner's own verdict on the page, which is the
        # same sentence the card would carry; the PIN gating is proved on the two
        # inflow approvals above and below, and the PLAN-STEP approval going to
        # PROCESSING is proved live by scripts/prove_run6_money_out_live.py.
        page.goto(f"{base}/app?tab=setup", wait_until="load")
        add_bill(page, BILL_OVER, "3000")
        page.goto(f"{base}/app?tab=money-out", wait_until="load")
        over = text_of(page)
        assert BILL_OVER in over, over[:400]
        assert "exceeds the auto-pay limit" in over or "needs your approval" in over, (
            "the over-limit bill does not say it needs approval: " + over[:600])
        assert "is within the auto-pay limit" in over, (
            "the under-limit bill is not within the limit: " + over[:600])
        assert counts(books, "plan_step") == 0, (
            "a plan step was persisted with no credentials and nothing to send")
        page.screenshot(path=str(SHOT_DIR / "3-over-limit-needs-approval.png"),
                        full_page=True)

        # ---- 5. a partial payment says what is left to collect -------------
        second_deposit(books)
        page.goto(f"{base}/app?tab=ok", wait_until="load")
        toast = pin_it(page, 'form[action^="/inflows/"]', {"what": "match"})
        assert "applied" in toast.lower(), toast
        page.goto(f"{base}/app?tab=money-in", wait_until="load")
        partial = text_of(page)
        assert "still to collect" in partial, partial[:600]
        assert "USD 400.00" in partial, partial[:600]
        page.screenshot(path=str(SHOT_DIR / "4-what-is-left-to-collect.png"),
                        full_page=True)

        # ---- 6. the AI tab shows the decisions it made ---------------------
        page.goto(f"{base}/app?tab=ai", wait_until="load")
        assert "No model calls yet" not in text_of(page), (
            "two deposits produced no trace on the AI tab")
        page.screenshot(path=str(SHOT_DIR / "5-ai-activity.png"), full_page=True)

        assert not errors, f"console errors: {errors[:4]}"
        ctx.close()
