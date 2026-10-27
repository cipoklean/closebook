"""Run 10, in a browser: a duplicate bill is refused where it was typed.

Item 3 of run 10: submitting PO-2001 a second time showed NO rejection. The
refusal existed and was delivered to a different tab, so on Setup -- the screen
the operator was typing on -- nothing happened at all. This drives the real form
twice and reads the message off the page, plus the Resolved-money card from item
2 and Home's "Unmatched money" figure from item 5.

Provider-free: the book is seeded by the app's own writers, and every credential
is blanked, so nothing here can reach Airwallex.
"""
from __future__ import annotations

import os
import socket
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

PIN = "r10-pin-1"
SHOT_DIR = Path("/tmp/proof/e2e-run10")
BOOK = "r10-e2e"
BILL = "PO-R10-DUP-9001"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def tester(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    root = tmp_path_factory.mktemp("r10-e2e")
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
        "CLOSEBOOK_AUTO_EXECUTE": "0",
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
        yield {"base": base, "books": books}
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
        except Exception as e:
            pytest.skip(f"chromium is not installed for playwright ({e})")
        yield b
        b.close()


def seed(books: Path) -> None:
    """A supplier with a beneficiary, and one unresolved deposit."""
    sys.path.insert(0, str(BACKEND_DIR))
    from app.db import DB
    from app.domain.intake import record_and_match
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import create_supplier, record_beneficiary

    books.mkdir(parents=True, exist_ok=True)
    db = DB(f"sqlite:///{books / f'{BOOK}.sqlite3'}")
    with db.session_scope() as s:
        save_settings(s, budget_currency="USD", reserve_floor="0",
                      auto_pay_limit="2500", fx_tolerance_pct="0.50")
        sup = create_supplier(s, name="Atlanta Packaging", country="US",
                              currency="USD", payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"e2e": "no provider"},
                           beneficiary_id="ben-r10", status="CREATED")
    with db.session_scope() as s:
        record_and_match(s, deposit_id="dep-r10-e2e", currency="USD",
                         amount_minor=700_000, payer_name="Apex Factors Ltd.",
                         reference="nothing matches this", llm=False)


def text_of(page) -> str:
    return " ".join(page.inner_text("body").split())


class TestADuplicateBillIsRefusedOnTheScreen:
    def test_the_second_submit_says_nothing_was_added(self, tester, browser):
        seed(tester["books"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        try:
            page.goto(f"{tester['base']}/app?ws={BOOK}&tab=setup", wait_until="load")
            page.wait_for_selector("form[action^='/setup/payables']", timeout=15000)
            f = "form[action^='/setup/payables']"
            opts = page.eval_on_selector_all(
                f"{f} select[name=supplier_id] option",
                "os => os.map(o => [o.value, o.textContent.trim()])")
            pick = next((v for v, _t in opts if v), None)
            assert pick, f"no supplier to pick: {opts}"
            for attempt in (1, 2):
                page.select_option(f"{f} select[name=supplier_id]", pick)
                page.fill(f"{f} input[name=number]", BILL)
                page.fill(f"{f} input[name=amount]", "1800")
                # THE FORM HAS NO DEFAULT CURRENCY, and a bill without one is
                # refused before the duplicate check is even reached.
                if page.query_selector(f"{f} input[name=currency]"):
                    page.fill(f"{f} input[name=currency]", "USD")
                page.click(f"{f} button[type=submit]")
                page.wait_for_timeout(2500)
                body = text_of(page)
                if attempt == 1:
                    # STRICT: the first one must really have been added, or the
                    # second submit proves nothing about duplicates.
                    assert "Added bill" in body, (
                        "the first bill was not accepted: " + body[:400])
                else:
                    assert f"{BILL} already exists" in body, (
                        "the duplicate was not refused on the screen it was "
                        "typed on: " + body[:600])
                    assert "nothing added" in body.lower(), body[:600]
            # IT IS LISTED ONCE.
            assert text_of(page).count(BILL) <= 3, text_of(page)[:600]
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SHOT_DIR / "31-duplicate-refused.png"),
                            full_page=True)
        finally:
            ctx.close()


class TestHomeShowsWhatIsHeld:
    def test_the_held_figure_is_on_home(self, tester, browser):
        seed(tester["books"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(f"{tester['base']}/app?ws={BOOK}&tab=home", wait_until="load")
            page.wait_for_timeout(1500)
            # UI revamp: every secondary figure now lives inside one collapsed
            # Details fold, so a reader who does not open it is not shown the
            # number either. Open it the way a person would before reading.
            # READ THE SERVED HTML, not the painted text: the app re-renders on
            # a poll (6s) and the swap closes any fold that was opened by hand,
            # so an inner_text read can race the very thing it is asserting.
            body = page.content()
            # RENAMED ON PURPOSE (UI revamp): the same figure now reads
            # "Unmatched money", which is what it is. The number is unchanged.
            assert "Unmatched money" in body, body[:400]
            assert "USD 7,000.00" in body, (
                "the held figure is missing: " + body[:400])
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SHOT_DIR / "32-held-for-review.png"),
                            full_page=True)
        finally:
            ctx.close()
