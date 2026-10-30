"""Browser proof that a test's book STAYS its book, end to end.

Run 5 gave every test a separate SQLite file behind `?ws=<suffix>` and it still
leaked: the suffix lived in one query string, so the tab a test clicked, the
form it submitted, the 303 that answered the form and every `/state/*` poll
arrived WITHOUT it and resolved to the shared book. A test opened its own book,
pressed something, and was silently back in everybody's.

This drives a REAL browser against a REAL server, on an empty temp database with
NO provider credentials, and asserts after EVERY step that:

  * the page still says `Book: a` (the footer label, read off the rendered DOM),
  * the shared book's row counts have not moved,
  * and a fresh navigation with no `?ws=` at all is still the same book.

Then a SECOND browser context opens `?ws=b` and must see none of a's rows.

Provider-free by construction: the credentials are blanked (empty strings, so a
developer's `.env` cannot put them back), `CLOSEBOOK_AUTO_EXECUTE=0` so nothing
clear is ever sent, and the one Airwallex-dependent step — creating a supplier,
which creates a real beneficiary — is asserted for its refusal and then given
the beneficiary id Airwallex WOULD have returned, through the app's own
`record_beneficiary` writer. No provider call is needed for the property under
test, which is where the browser lands, not what the sandbox answers.

Run it:  make test-e2e      (needs a one-time `playwright install chromium`)
"""
from __future__ import annotations

import json
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

TABS = ("home", "money-in", "money-out", "ok", "ai", "setup")
#: 12 characters exactly: the approval field carries `maxlength="12"`, so a
#: longer test PIN is truncated at the input and refused as a wrong PIN.
PIN = "e2e-pin-9137"

CUSTOMER = "E2E Only In A"
INVOICE = "INV-E2E-A"
SUPPLIER = "E2E Supplier A"
BILL = "PO-E2E-A"

#: The tables a step of this test could possibly write. Counted in the SHARED
#: book: none of them may move while a test is working in its own.
COUNTED = ("customer", "invoice_receivable", "supplier", "invoice_payable",
           "approval", "plan_step")

SHOT_DIR = Path("/tmp/proof/e2e-workspaces")

#: Handed between the two tests: the context and the rows book a created.
STATE: dict = {}


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _env(db_path: Path, books: Path) -> dict[str, str]:
    """The app as CI sees it: empty DB, no credentials, books enabled."""
    env = {
        k: v for k, v in _environ().items()
        if not k.startswith(("AWX_", "LLM_"))
        and k not in {"PUBLIC_BASE_URL", "WEBHOOK_PATH", "CLOSEBOOK_DATABASE_URL",
                      "CLOSEBOOK_HMAC_SECRET", "CLOSEBOOK_WORKSPACES",
                      "CLOSEBOOK_WORKSPACE_ROOT", "DEMO_APPROVER_PIN",
                      "CLOSEBOOK_AUTO_EXECUTE", "OPERATOR_NAME"}
    }
    env.update({
        # Credentials EMPTY rather than removed: pydantic-settings gives the real
        # environment priority over `.env`, so a blank is what actually produces
        # a credential-free boot.
        "AWX_CLIENT_ID": "", "AWX_API_KEY": "", "AWX_WEBHOOK_SECRET": "",
        "LLM_API_KEY": "",
        "CLOSEBOOK_DATABASE_URL": f"sqlite:///{db_path}",
        "CLOSEBOOK_HMAC_SECRET": "e2e-test-only-not-a-real-secret",
        # Per-test books, exactly as scripts/testsprite.sh turns them on.
        "CLOSEBOOK_WORKSPACES": "1",
        "CLOSEBOOK_WORKSPACE_ROOT": str(books),
        "DEMO_APPROVER_PIN": PIN,
        "OPERATOR_NAME": "e2e",
        # Nothing clear is ever sent, whatever the planner concludes.
        "CLOSEBOOK_AUTO_EXECUTE": "0",
        "PYTHONPATH": str(BACKEND_DIR),
        "PYTHONUNBUFFERED": "1",
    })
    return env


def _environ() -> dict[str, str]:
    import os

    return dict(os.environ)


@pytest.fixture(scope="module")
def tester(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    """The real app on a free port, per-test books on, credentials blank."""
    root = tmp_path_factory.mktemp("ws-e2e")
    db_path = root / "shared.sqlite3"
    books = root / "books"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND_DIR), env=_env(db_path, books),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
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
        b = None
        try:
            b = p.chromium.launch(headless=True)
        except Exception as e:               # browser binary missing
            pytest.skip(f"chromium is not installed for playwright ({e}); "
                        f"run `playwright install chromium`")
        assert b is not None
        yield b
        b.close()


# ------------------------------------------------------------------- helpers
def shared_counts(db_path: Path) -> dict:
    """Row counts in the SHARED book, read from its file."""
    con = sqlite3.connect(str(db_path))
    try:
        out = {}
        for table in COUNTED:
            out[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out
    finally:
        con.close()


def book_in(db_path: Path, suffix: str) -> dict:
    con = sqlite3.connect(str(db_path))
    try:
        out = {}
        for table in COUNTED:
            out[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out
    finally:
        con.close()


def assert_still_in_book(page, suffix: str, tester: dict, before: dict) -> None:
    """The page names its book, and the shared book has not moved."""
    page.wait_for_selector('[data-testid="active-book"]', timeout=20_000)
    label = page.inner_text('[data-testid="active-book"]').strip()
    assert label == f"Book: {suffix}", (
        f"the page left book {suffix}: it says {label!r} at {page.url}")
    assert shared_counts(tester["db"]) == before, (
        f"the shared book changed while working in {suffix}: "
        f"{shared_counts(tester['db'])} != {before}")


def submit(page, form_action_prefix: str, values: dict) -> None:
    """Fill a plain (non-fetch) form and let the browser follow the 303."""
    form = page.locator(f'form[action^="{form_action_prefix}"]')
    for name, value in values.items():
        field = form.locator(f'[name="{name}"]')
        if field.evaluate("el => el.tagName.toLowerCase()") == "select":
            field.select_option(value=value)
        else:
            field.fill(value)
    form.locator('button[type="submit"]').first.click()
    page.wait_for_load_state("load")


def seed_plan_step(books: Path, suffix: str) -> int:
    """Put one approvable plan step in a book, through the REAL writer."""
    from app.db import DB
    from app.domain.plan_store import new_run_id, persist_plan
    from app.domain.planner import ACT_PAY, Step

    db = DB(f"sqlite:///{books / (suffix + '.sqlite3')}")
    with db.session() as s:
        rows, _skipped = persist_plan(s, new_run_id(), [
            Step(action=ACT_PAY, reason="seeded by the workspace e2e",
                 payable_number=BILL, supplier=SUPPLIER,
                 amount_minor=180_000, currency="USD")])
        assert rows, "the seed wrote no plan step"
        return rows[0].id


def stub_beneficiary(books: Path, suffix: str, supplier_name: str) -> None:
    """Give a supplier the beneficiary id Airwallex would have returned.

    The provider cannot be called in a credential-free run, and a bill cannot be
    created without a beneficiary, so this stands in for that ONE external fact.
    Written through `record_beneficiary`, the same writer the route uses, so the
    row is the shape the app really produces.
    """
    from app.db import DB
    from app.domain.workspace import record_beneficiary
    from app.models import Supplier

    db = DB(f"sqlite:///{books / (suffix + '.sqlite3')}")
    with db.session_scope() as s:
        sup = s.query(Supplier).filter_by(name=supplier_name).first()
        assert sup is not None, f"{supplier_name} is not in book {suffix}"
        record_beneficiary(s, sup.id, request={"stub": "no provider in this run"},
                           beneficiary_id="e2e-stub-beneficiary",
                           status="VERIFIED")


# --------------------------------------------------------------------- tests
class TestABookStaysItsBook:
    """One click-through: every step must stay inside the opened book."""

    def test_the_click_through(self, tester, browser):
        base = tester["base"]
        before = shared_counts(tester["db"])
        ctx_a = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx_a.new_page()
        console: list[str] = []
        page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)

        # ---- open the book
        page.goto(f"{base}/app?ws=a", wait_until="load")
        assert_still_in_book(page, "a", tester, before)

        # ---- every tab
        for tab in TABS:
            page.click(f'[data-testid="tab-{tab}"]')
            page.wait_for_load_state("load")
            # THE TAB LINK ITSELF carries the book, asserted independently of the
            # footer label: the old links were plain `/app?tab=<key>` and every
            # click after the first dropped the test into the shared book.
            assert "ws=a" in page.url, (
                f"the {tab} tab link dropped the book: {page.url}")
            assert_still_in_book(page, "a", tester, before)

        page.click('[data-testid="tab-setup"]')
        page.wait_for_load_state("load")

        # ---- a customer
        submit(page, "/setup/customers", {"name": CUSTOMER, "country": "NG"})
        assert_still_in_book(page, "a", tester, before)
        # A FRESH navigation with NO ?ws= at all must still be the same book.
        page.goto(f"{base}/app?tab=setup", wait_until="load")
        assert_still_in_book(page, "a", tester, before)
        assert CUSTOMER in page.inner_text("body"), "the customer is not in book a"

        # ---- an invoice
        form = page.locator('form[action^="/setup/invoices"]')
        form.locator('[name="customer_id"]').select_option(index=0)
        form.locator('[name="number"]').fill(INVOICE)
        form.locator('[name="amount"]').fill("12000")
        form.locator('[name="currency"]').fill("USD")
        form.locator('button[type="submit"]').first.click()
        page.wait_for_load_state("load")
        assert_still_in_book(page, "a", tester, before)
        assert INVOICE in page.inner_text("body"), "the invoice is not in book a"

        # ---- a supplier (the beneficiary call is refused: no credentials)
        submit(page, "/setup/suppliers",
               {"name": SUPPLIER, "country": "US", "currency": "USD",
                "pin": PIN})
        assert_still_in_book(page, "a", tester, before)
        page.goto(f"{base}/app?tab=setup", wait_until="load")
        assert SUPPLIER in page.inner_text("body"), "the supplier is not in book a"

        # ---- a bill (needs a beneficiary: the one external fact is stubbed)
        stub_beneficiary(tester["books"], "a", SUPPLIER)
        page.goto(f"{base}/app?tab=setup", wait_until="load")
        form = page.locator('form[action^="/setup/payables"]')
        form.locator('[name="supplier_id"]').select_option(index=0)
        form.locator('[name="number"]').fill(BILL)
        form.locator('[name="amount"]').fill("1800")
        form.locator('[name="currency"]').fill("USD")
        form.locator('button[type="submit"]').first.click()
        page.wait_for_load_state("load")
        assert_still_in_book(page, "a", tester, before)
        assert BILL in page.inner_text("body"), "the bill is not in book a"

        # ---- approve with the PIN
        seed_plan_step(tester["books"], "a")
        page.goto(f"{base}/app?tab=ok", wait_until="load")
        assert_still_in_book(page, "a", tester, before)
        pin_box = page.locator('form[action^="/approvals/"] input[name="pin"]')
        assert pin_box.count() == 1, "the approval form is not on the page"
        pin_box.fill(PIN)
        page.locator('form[action^="/approvals/"] button[type="submit"]').first.click()
        page.wait_for_selector("#x-toast.is-on", timeout=40_000)
        toast = page.inner_text("#x-toast").strip()
        assert_still_in_book(page, "a", tester, before)
        assert book_in(tester["books"] / "a.sqlite3", "a")["approval"] == 1, (
            f"the PIN approval was not recorded in book a (the page said {toast!r})")
        assert shared_counts(tester["db"])["approval"] == 0, (
            "the approval was recorded against the SHARED book")

        # ---- a sandbox control
        page.goto(f"{base}/app?tab=money-in", wait_until="load")
        assert_still_in_book(page, "a", tester, before)
        sandbox = page.locator('form[action^="/money-in/simulate"]')
        assert sandbox.count() == 1, "the sandbox deposit control is not on the page"
        action = sandbox.get_attribute("action")
        assert action == "/money-in/simulate?ws=a", (
            f"the sandbox control's action dropped the book: {action!r}")
        before_b = book_in(tester["books"] / "a.sqlite3", "a")
        # The button is DISABLED without credentials, which is honest: pressing it
        # would invent a real sandbox deposit. So the POST the control makes is
        # issued with the page's own session, and what is asserted is that the
        # control's press lands in this book: the refusal is a refusal ABOUT
        # book a, and neither book grows a row.
        answer = page.evaluate(
            """async (act) => {
              const r = await fetch(act, {
                method: "POST",
                headers: {"Content-Type": "application/x-www-form-urlencoded",
                          "Accept": "application/json"},
                body: new URLSearchParams({})
              });
              return {status: r.status, body: await r.text()};
            }""", action)
        assert answer["status"] == 200, answer
        answer_body = json.loads(answer["body"])
        assert answer_body.get("ok") is False, answer_body
        assert answer_body.get("message") or answer_body.get("err"), answer_body
        assert_still_in_book(page, "a", tester, before)
        assert book_in(tester["books"] / "a.sqlite3", "a") == before_b, (
            "a refused sandbox press changed book a")

        page.screenshot(path=str(SHOT_DIR / "a-after-every-step.png"), full_page=True)
        assert not console, f"console errors during the click-through: {console[:5]}"

        # ---- keep context A for the isolation check below
        STATE["a"] = {"ctx": ctx_a, "rows": (CUSTOMER, INVOICE, SUPPLIER, BILL)}

    def test_a_second_context_sees_none_of_it(self, tester, browser):
        a = STATE.get("a")
        assert a, "the click-through did not run first"
        base = tester["base"]
        before = shared_counts(tester["db"])

        ctx_b = browser.new_context(viewport={"width": 1280, "height": 900})
        page_b = ctx_b.new_page()
        page_b.goto(f"{base}/app?ws=b", wait_until="load")
        assert_still_in_book(page_b, "b", tester, before)
        page_b.goto(f"{base}/app?tab=setup", wait_until="load")
        assert_still_in_book(page_b, "b", tester, before)
        body = page_b.inner_text("body")
        for row in a["rows"]:
            assert row not in body, f"{row!r} leaked into book b"
        page_b.screenshot(path=str(SHOT_DIR / "b-empty.png"), full_page=True)

        # And book a still has its own rows, through its own cookie.
        page_a = a["ctx"].new_page()
        page_a.goto(f"{base}/app?tab=setup", wait_until="load")
        assert_still_in_book(page_a, "a", tester, before)
        body_a = page_a.inner_text("body")
        for row in a["rows"]:
            assert row in body_a, f"{row!r} was lost from book a"
        page_a.screenshot(path=str(SHOT_DIR / "a-still-there.png"), full_page=True)

        assert shared_counts(tester["db"]) == before, "the shared book was written to"
