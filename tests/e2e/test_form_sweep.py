"""Every form on /app has to be usable at the viewports a tester uses.

WHY THIS IS A SWEEP AND NOT ONE TEST. Run 7 lost every deposit test to a panel
that was one grid column wide inside a collapsed <details>, and run 8 lost the
resolve form the same way: a form can render, contain the right HTML, pass every
text assertion, and still be impossible to type into. One broken card is a bug;
the family is the bug, and the only way to see the family is to walk every form
on every tab at the two viewports a tester actually uses.

WHAT IT ASSERTS, per visible form field:
  * it has a box at all, and is at least MIN_WIDTH px across;
  * it can be typed into, and the typed text is still there after one poll
    interval (the page polls /state/version every 20s).
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

pytest.importorskip("playwright.sync_api",
                    reason="playwright is not installed - run `make setup`")

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
BOOK = "sweep-1"
MIN_WIDTH = 120
VIEWPORTS = ({"width": 1920, "height": 1080}, {"width": 1280, "height": 900})
TABS = ("home", "money-in", "money-out", "ok", "ai", "setup")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def tester(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    root = tmp_path_factory.mktemp("sweep")
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
        "AWX_CLIENT_ID": "", "AWX_API_KEY": "", "LLM_API_KEY": "",
        "CLOSEBOOK_DATABASE_URL": f"sqlite:///{root / 'shared.sqlite3'}",
        "CLOSEBOOK_HMAC_SECRET": "e2e-test-only-not-a-real-secret",
        "CLOSEBOOK_WORKSPACES": "1",
        "CLOSEBOOK_WORKSPACE_ROOT": str(root / "books"),
        "DEMO_APPROVER_PIN": "sweep-pin",
        "OPERATOR_NAME": "sweep",
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
                pytest.fail(f"server exited during boot (rc={proc.returncode}):\n{out[-2000:]}")
            try:
                with urllib.request.urlopen(f"{base}/healthz", timeout=2) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(0.4)
        else:
            pytest.fail("server did not become ready")
        yield {"base": base, "root": root}
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


_BUILT: set = set()


def build_a_book_with_every_thing(root: Path) -> None:
    """A book that puts EVERY card on the page: an ambiguous deposit, a bill that
    needs approval, an invoice part paid. A sweep over an empty book sweeps
    nothing -- the broken card was the one with content in it."""
    sys.path.insert(0, str(BACKEND_DIR))
    from app.db import DB
    from app.domain.intake import record_and_match
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import (
        create_customer, create_invoice, create_payable, create_supplier,
        record_beneficiary,
    )

    if str(root) in _BUILT:
        return
    _BUILT.add(str(root))
    books = root / "books"
    books.mkdir(parents=True, exist_ok=True)
    db = DB(f"sqlite:///{books / (BOOK + '.sqlite3')}")
    with db.session_scope() as s:
        save_settings(s, budget_currency="USD", reserve_floor="5000",
                      auto_pay_limit="2500", fx_tolerance_pct="0.50")
        sup = create_supplier(s, name="Pennine Supplies", country="US",
                              currency="USD", payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"e2e": "no provider"},
                           beneficiary_id="ben-sweep", status="CREATED")
        create_payable(s, supplier_id=sup.id, currency="USD", amount="3000",
                       number="PO-SWEEP-1")
        create_payable(s, supplier_id=sup.id, currency="USD", amount="1800",
                       number="PO-SWEEP-2")
        cust = create_customer(s, name="Brightline Retail")
        create_invoice(s, customer_id=cust.id, number="INV-SWEEP-1",
                       currency="USD", amount="1200")
    with db.session_scope() as s:
        record_and_match(s, deposit_id="dep-sweep-1", currency="USD",
                         amount_minor=80_000, payer_name="Someone Else",
                         reference="nobody knows", llm=False)
    with db.session_scope() as s:
        record_and_match(s, deposit_id="dep-sweep-2", currency="USD",
                         amount_minor=50_000, payer_name="Brightline Retail",
                         reference="INV-SWEEP-1", llm=False)


def forms_to_check(page) -> list[dict]:
    return page.evaluate("""(() => {
      const out = [];
      document.querySelectorAll('form').forEach((f, fi) => {
        const r = f.getBoundingClientRect();
        if (r.width < 2 && r.height < 2) return;              // not rendered
        if (f.closest('details:not([open])')) return;          // behind a summary
        const fields = [];
        f.querySelectorAll('input:not([type=hidden]), select, textarea').forEach(el => {
          const b = el.getBoundingClientRect();
          const cs = getComputedStyle(el);
          fields.push({name: el.name || el.id || el.tagName.toLowerCase(),
                       id: el.id || '',
                       type: el.type || el.tagName.toLowerCase(),
                       w: Math.round(b.width), h: Math.round(b.height),
                       display: cs.display, visibility: cs.visibility,
                       inViewport: b.width > 0 && b.height > 0});
        });
        if (!fields.length) return;
        out.push({action: f.getAttribute('action') || '(none)',
                  id: f.id || '', fields});
      });
      return out;
    })()""")


class TestEveryFormIsUsable:
    @pytest.mark.parametrize("viewport", VIEWPORTS, ids=["1920", "1280"])
    def test_every_visible_field_is_wide_enough_and_keeps_what_is_typed(
            self, tester, browser, viewport):
        build_a_book_with_every_thing(tester["root"])
        ctx = browser.new_context(viewport=viewport)
        page = ctx.new_page()
        bad_width: list[str] = []
        bad_type: list[str] = []

        for tab in TABS:
            page.goto(f"{tester['base']}/app?tab={tab}&ws={BOOK}", wait_until="load")
            page.wait_for_timeout(900)
            for form in forms_to_check(page):
                for field in form["fields"]:
                    label = (f"{tab} {form['action']} [{field['name']}] "
                             f"{field['w']}x{field['h']}")
                    if not field["inViewport"] or field["w"] < MIN_WIDTH:
                        bad_width.append(label)
        assert not bad_width, (
            "fields narrower than "
            f"{MIN_WIDTH}px at {viewport['width']}px, so no browser can type "
            "into them:\n  " + "\n  ".join(bad_width))

        # --- and nothing that is typed gets wiped by the poll --------------
        # Every form on every tab that has one, at this viewport. The field is
        # addressed by its own ID: NAMES REPEAT (two "by" fields, three
        # "currency" fields), and reading back the first [name=...] on the page
        # reads a different control than the one that was typed into.
        for tab in TABS:
            page.goto(f"{tester['base']}/app?tab={tab}&ws={BOOK}", wait_until="load")
            page.wait_for_timeout(700)
            typed: list[tuple[str, str, str, str]] = []
            for form in forms_to_check(page):
                for field in form["fields"]:
                    if field["type"] not in ("text", "password") or not field["id"]:
                        continue
                    loc = page.locator(
                        f'form[action="{form["action"]}"] '
                        f'[id="{field["id"]}"]').first
                    if loc.count() == 0:
                        continue
                    value = f"t{len(typed)}"
                    try:
                        loc.click()
                        loc.type(value, delay=30)
                    except Exception as e:                # noqa: BLE001
                        bad_type.append(f"{tab} [{field['name']}]: cannot type ({e})")
                        continue
                    typed.append((tab, form["action"], field["id"],
                                  field["name"], value))
            if not typed:
                continue
            page.wait_for_timeout(21_500)                  # past one poll
            for tab_name, action, field_id, name, value in typed:
                # SCOPED TO ITS FORM: the Setup page reuses ids like "f-currency"
                # across four forms, so a page-level lookup reads another card's
                # field (and reports the text as lost when it is not).
                loc = page.locator(f'form[action="{action}"] [id="{field_id}"]').first
                got = loc.input_value() if loc.count() else "(gone)"
                # A pre-filled field (the operator name, "USD", "5000.00") keeps
                # its prefix: what must survive is the text TYPED into it.
                if value not in got:
                    bad_type.append(
                        f"{tab_name} [{name}] id={field_id}: typed {value!r}, "
                        f"after one poll found {got!r}")
        assert not bad_type, (
            f"typed text did not survive a poll at {viewport['width']}px:\n  "
            + "\n  ".join(bad_type))
        ctx.close()
