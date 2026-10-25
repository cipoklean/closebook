"""Run 9 item 2: the resolve form must keep what is typed into it.

WHAT THE TESTER DID, and it has to be reproduced the same way: open a fresh
?ws= book, get an AMBIGUOUS deposit into it, open the card, choose "release as
income", type into the Note and the PIN with the REAL KEYBOARD (page.keyboard.type,
not fill), wait past a poll interval, and read the fields back. Five run-9 tests
blocked here, and run 8's fix (making both fields visible instead of hiding them
until a radio was chosen) did not stop it.

The deposit is written by the app's OWN writer, because a real sandbox deposit
needs a provider and this suite blanks every credential. Everything after that is
a real browser on the real app.
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

PIN = "r9-pin-1"          # 8 characters: the PIN field is maxlength 12
SHOT_DIR = Path("/tmp/proof/e2e-run9")

BOOK = "r9-resolve-1"
#: The tester's own deposits: none of these quotes an invoice, so all three
#: land AMBIGUOUS and each one puts a resolve card on the page.
DEPOSITS = [("dep-r9-1", 1_200_000), ("dep-r9-2", 700_000), ("dep-r9-3", 680_000)]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def tester(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    """The real app on a free port: books on, every credential blank."""
    root = tmp_path_factory.mktemp("r9-e2e")
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
        except Exception as e:
            pytest.skip(f"chromium is not installed for playwright ({e})")
        yield b
        b.close()


_SEEDED: set = set()


def seed(books: Path) -> None:
    """Three AMBIGUOUS deposits, written by the app's own intake.

    Idempotent, so either test can ask for the book without depending on which
    one pytest happened to run first.
    """
    if str(books) in _SEEDED:
        return
    _SEEDED.add(str(books))
    sys.path.insert(0, str(BACKEND_DIR))
    from app.db import DB
    from app.domain.intake import record_and_match
    from app.domain.settings_store import save as save_settings

    books.mkdir(parents=True, exist_ok=True)
    db = DB(f"sqlite:///{books / f'{BOOK}.sqlite3'}")
    with db.session_scope() as s:
        save_settings(s, budget_currency="USD", reserve_floor="0",
                      auto_pay_limit="2500", fx_tolerance_pct="0.50")
    for dep_id, minor in DEPOSITS:
        with db.session_scope() as s:
            record_and_match(s, deposit_id=dep_id, currency="USD",
                             amount_minor=minor, payer_name="Apex Factors Ltd.",
                             reference="cash with no invoice", llm=False)


def pinned(page, url: str) -> None:
    """Open a page with a loaded signal: never judge a half-rendered document."""
    page.goto(url, wait_until="load")
    page.wait_for_selector("[data-testid='active-book'], .x-todo, .x-card", timeout=15000)


def fields(page) -> list[dict]:
    """Every resolve form on the page, with its own field ids and values."""
    # ONLY THE RESOLVE FORMS: the re-ask form beside each one also carries
    # data-action and has no note or PIN, so counting it made three ambiguous
    # deposits look like six forms.
    return page.eval_on_selector_all(
        'form[data-action][action*="/resolve"]',
        """forms => forms.map(f => {
             const g = n => { const el = f.querySelector(`input[name=${n}]`);
                              return el ? {id: el.id, value: el.value} : null; };
             const sel = f.querySelector('select[name=what]');
             return {action: f.getAttribute('action'),
                     what: sel ? sel.value : null,
                     note: g('note'), pin: g('pin')};
           })""")


class TestTheResolveFormKeepsWhatIsTyped:
    def test_typing_survives_the_page(self, tester, browser):
        seed(tester["books"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        try:
            pinned(page, f"{tester['base']}/app?ws={BOOK}&tab=ok")
            geom = page.eval_on_selector_all(
                'form[data-action][action*="/resolve"] input, '
                'form[data-action][action*="/resolve"] select',
                """els => els.map(e => { const r = e.getBoundingClientRect();
                     const cs = getComputedStyle(e);
                     return {id: e.id, name: e.name, w: Math.round(r.width),
                             h: Math.round(r.height), x: Math.round(r.x),
                             y: Math.round(r.y), display: cs.display,
                             visibility: cs.visibility}; })""")
            print("GEOM", geom)

            before = fields(page)
            assert before, "no resolve form on the page for an ambiguous deposit"
            assert len(before) == len(DEPOSITS), (
                f"{len(before)} resolve form(s) for {len(DEPOSITS)} ambiguous "
                f"deposits")

            for i, form in enumerate(before):
                assert form["note"] and form["note"]["id"], form
                assert form["pin"] and form["pin"]["id"], form
                # THE SAME FIELDS, EVERY TIME: a duplicate id would mean the
                # tester's click lands in another card.
                fid = f'form[action="{form["action"]}"]'
                page.select_option(f"{fid} select[name=what]", "income")
                page.click(f'{fid} input[name=note]')
                page.keyboard.type(f"no invoice will ever claim this {i}")
                page.click(f'{fid} input[name=pin]')
                page.keyboard.type(PIN)

            # PAST ONE POLL INTERVAL: the /state/version poll runs every 20s and
            # must not replace the card under the reader's hands.
            page.wait_for_timeout(6000)

            after = fields(page)
            assert len(after) == len(DEPOSITS), after
            for i, (was, now) in enumerate(zip(before, after)):
                fid = f'form[action="{was["action"]}"]'
                note = page.input_value(f'{fid} input[name=note]')
                pin = page.input_value(f'{fid} input[name=pin]')
                assert note == f"no invoice will ever claim this {i}", (
                    f"form {i}: the note is {note!r} after typing into it and "
                    f"waiting past the poll")
                assert pin == PIN, (
                    f"form {i}: the PIN is {pin!r} after typing into it and "
                    f"waiting past the poll")
                assert now["what"] == "income", (
                    f"form {i}: the choice reset to {now['what']!r}")

            # AND IT SUBMITS. The whole point of the form is the release.
            first = after[0]
            fid = f'form[action="{first["action"]}"]'
            page.click(f"{fid} button[type=submit]")
            page.wait_for_timeout(2500)
            body = " ".join(page.inner_text("body").split())
            assert "Released as income" in body or "released" in body.lower(), (
                "the release did not report anything: " + body[:400])
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SHOT_DIR / "resolve-typed-and-released.png"),
                            full_page=True)
        finally:
            ctx.close()


class TestTheResolveFormOnMoneyInIsReachable:
    """Run 9's five blocked tests: the form is there but a person cannot touch it.

    On Money in every deposit row is a collapsed <details>, and the resolve form
    is rendered INSIDE that body, so the select and both fields are in the DOM
    and NOT VISIBLE: a click waits for actionability and times out, and a human
    sees a row they have to guess to expand. Needs your OK opens its rows, which
    is why the same card works there and why a sweep on an empty book sees
    nothing at all.
    """

    def test_the_form_is_visible_without_expanding_anything(self, tester, browser):
        seed(tester["books"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(4000)      # a red must be quick, not a 30s wait
        try:
            pinned(page, f"{tester['base']}/app?ws={BOOK}&tab=money-in")
            forms = page.eval_on_selector_all(
                'form[data-action][action*="/resolve"]',
                "fs => fs.map(f => f.getAttribute('action'))")
            assert forms, "no resolve form on Money in for an ambiguous deposit"
            for action in forms:
                for node in page.query_selector_all(
                        f'form[action="{action}"] input, '
                        f'form[action="{action}"] select'):
                    assert node.is_visible(), (
                        f"{action}: {node.get_attribute('id')} is in the DOM and "
                        f"not visible to a person")
            # AND IT CAN BE USED, which is what the tester's blocked tests did.
            fid = f'form[action="{forms[0]}"]'
            page.select_option(f"{fid} select[name=what]", "income")
            page.click(f"{fid} input[name=note]")
            page.keyboard.type("released on Money in")
            page.click(f"{fid} input[name=pin]")
            page.keyboard.type(PIN)
            assert page.input_value(f"{fid} input[name=note]") == "released on Money in"
        finally:
            ctx.close()
