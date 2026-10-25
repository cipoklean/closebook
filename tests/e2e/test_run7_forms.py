"""The run-7 browser checks, in a fresh ?ws= book, with no provider.

Item 1 is the blocker: the "Simulate a customer payment" form could not be
driven at all. The sandbox panel is a direct child of the 12-column `.x-screen`
grid and carried no span, so on a DESKTOP viewport it was laid out one column
wide (57px): every field inside it measured 14-26px across. A real browser
cannot fill that, Playwright reports the field as "not visible", and the tester
saw exactly that: a blank field, nothing created, "No deposits yet".

This is NOT workspace-specific: it reproduces on prod with the flag off. The
narrow-viewport rule `.x-screen > * { grid-column: span 12 }` hid it, which is
why it only bit when a run used a desktop viewport.

What this suite CANNOT do without a provider is assert that the deposit was
created: `sandbox_ready` is false with no credentials, so the submit button is
disabled by design. Creation is proved live on the tester.
"""
from __future__ import annotations

import os
import re
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

BOOK = "r7-e2e-1"
PIN = "r7-money-pin"
SHOT_DIR = Path("/tmp/proof/e2e-run7")

SIMULATE = 'form[action^="/money-in/simulate"]'


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def tester(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    root = tmp_path_factory.mktemp("r7-e2e")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AWX_", "LLM_"))
           and k not in {"PUBLIC_BASE_URL", "WEBHOOK_PATH", "CLOSEBOOK_DATABASE_URL",
                         "CLOSEBOOK_HMAC_SECRET", "CLOSEBOOK_WORKSPACES",
                         "CLOSEBOOK_WORKSPACE_ROOT", "DEMO_APPROVER_PIN",
                         "CLOSEBOOK_AUTO_EXECUTE", "OPERATOR_NAME",
                         "CLOSEBOOK_INSTANCE", "CLOSEBOOK_INGEST_UNCLAIMED"}}
    env.update({
        "AWX_CLIENT_ID": "", "AWX_API_KEY": "", "AWX_WEBHOOK_SECRET": "",
        "LLM_API_KEY": "",
        "CLOSEBOOK_DATABASE_URL": f"sqlite:///{root / 'shared.sqlite3'}",
        "CLOSEBOOK_HMAC_SECRET": "e2e-test-only-not-a-real-secret",
        "CLOSEBOOK_WORKSPACES": "1",
        "CLOSEBOOK_WORKSPACE_ROOT": str(root / "books"),
        "DEMO_APPROVER_PIN": PIN,
        "OPERATOR_NAME": "r7e2e",
        "CLOSEBOOK_INSTANCE": "r7e2e",
        "CLOSEBOOK_INGEST_UNCLAIMED": "0",
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
        yield {"base": base, "books": root / "books"}
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


class TestTheSandboxPanelIsUsable:
    """Item 1: a desktop viewport must be able to type into it."""

    def test_every_field_in_the_panel_is_laid_out_at_a_usable_width(self, tester, browser):
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.goto(f"{tester['base']}/app?tab=money-in&ws={BOOK}", wait_until="load")
        panel = page.locator("#sandbox-controls")
        assert panel.count() == 1, "the sandbox panel is not on Money in"
        width = panel.bounding_box()["width"]
        assert width > 600, (
            f"the sandbox panel is {width:.0f}px wide inside a 12-column grid: it is "
            "spanned one column, so everything inside it is unusable")

        for name in ("amount", "payer_name", "reference"):
            field = page.locator(f'{SIMULATE} [name="{name}"]')
            box = field.bounding_box()
            assert box is not None, f"{name} has no box at all"
            assert box["width"] > 80, (
                f"{name} is {box['width']:.0f}px wide, so no browser can fill it")
            field.fill("123")          # throws if the element is not usable
            assert field.input_value() == "123"
        page.screenshot(path=str(SHOT_DIR / "1-panel-usable.png"), full_page=True)
        page.close()

    def test_the_typed_amount_survives_a_poll_interval(self, tester, browser):
        """The page polls /state/version every 20s; typing must not be disturbed."""
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.goto(f"{tester['base']}/app?tab=money-in&ws={BOOK}", wait_until="load")
        amount = page.locator(f'{SIMULATE} [name="amount"]')
        amount.click()
        amount.type("2500", delay=200)
        assert amount.input_value() == "2500"
        page.wait_for_timeout(23_000)          # past one poll
        assert amount.input_value() == "2500", (
            "the typed amount did not survive a poll interval")
        assert page.locator("[data-testid=active-book]").inner_text().strip() == f"Book: {BOOK}"
        page.close()

class TestTheHeldCardNamesTheFloor:
    """Item 2, in a browser: the card must say what is actually holding it."""

    def test_a_bill_held_by_the_floor_says_so_in_the_page(self, tester, browser):
        books = tester["books"]
        books.mkdir(parents=True, exist_ok=True)
        sys.path.insert(0, str(BACKEND_DIR))
        from app.db import DB
        from app.domain.intake import record_and_match
        from app.domain.settings_store import save as save_settings
        from app.domain.workspace import (
            create_customer, create_invoice, create_payable, create_supplier,
            record_beneficiary,
        )

        db = DB(f"sqlite:///{books / (BOOK + '.sqlite3')}")
        with db.session_scope() as s:
            save_settings(s, budget_currency="USD", reserve_floor="5000",
                          auto_pay_limit="2500", fx_tolerance_pct="0.50")
            sup = create_supplier(s, name="Pennine Supplies", country="US",
                                  currency="USD", payout_method="LOCAL")
            record_beneficiary(s, sup.id, request={"e2e": "no provider"},
                               beneficiary_id="ben-r7", status="CREATED")
            create_payable(s, supplier_id=sup.id, currency="USD",
                           amount="1800", number="PO-R7-FLOOR")
            cust = create_customer(s, name="Brightline Retail")
            create_invoice(s, customer_id=cust.id, number="INV-R7-X",
                           currency="USD", amount="1200")
        with db.session_scope() as s:
            record_and_match(s, deposit_id="dep-r7-floor", currency="USD",
                             amount_minor=600_000, payer_name="Someone Else",
                             reference="no invoice will claim this", llm=False)

        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.goto(f"{tester['base']}/app?tab=money-out&ws={BOOK}", wait_until="load")
        text = " ".join(page.inner_text("body").split())
        assert "PO-R7-FLOOR" in text, text[:400]
        assert "USD 6,000.00" in text, (
            "the card does not name the money the book holds: " + text[:500])
        assert "waiting on a match" in text, text[:500]
        assert "USD 5,000.00" in text, (
            "the card does not name the reserve floor that is ALSO holding it, so "
            "resolving the deposit changes nothing: " + text[:600])
        assert "holds USD 0.00" not in text, text[:400]
        Path(SHOT_DIR).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOT_DIR) / "2-floor-named.png"), full_page=True)
        page.close()
