"""End-to-end smoke test: Closebook rendered in a real browser.

Why this exists
---------------
Every other test in this suite asserts on Python objects. Nothing asserted that
the pages a person actually opens *render*: a template can raise, a figure can
print as `None`, a 500 can be served with a friendly body, or a table can push
the layout sideways on a phone — and every unit test stays green.

This test starts the real app (uvicorn) on a free port against an EMPTY temp
database with NO provider credentials, opens `/` and all six `/app` tabs in
headless Chromium at 1280px and 390px, and fails on any of:

  * a console error or an uncaught page exception
  * any 5xx response, including a background fetch
  * raw exception text rendered into the page
  * horizontal overflow at 390px

It deliberately stands up its own instance: it never touches the developer's
running server (port 8000) or the real database file.

Run it
------
    make test-e2e              # after: playwright install chromium

CI installs the browser and runs it; locally it skips (with a clear reason) when
the browser binary is absent, so `make test` stays green for anyone who has only
installed the Python requirements.
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

# Skip cleanly (and loudly) when the browser driver is not installed. CI installs
# it explicitly, so this never silently disables the check there.
pytest.importorskip(
    "playwright.sync_api",
    reason="playwright is not installed - run `make setup` then `playwright install chromium`",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"

TAB_NAMES = ("home", "money-in", "money-out", "ok", "ai", "setup")

# "/" plus the six /app tabs.
PAGES: list[tuple[str, str]] = [("/", "landing")] + [
    (f"/app?tab={t}", f"app-{t}") for t in TAB_NAMES
]

# The two widths the product must survive: a laptop and a phone.
WIDTHS = ((1280, 900), (390, 844))

# Unmistakable evidence that an exception rendered into the page. Deliberately
# narrow: a bare "None" or "undefined" appears legitimately in prose, so those
# are left to the console-error check instead of producing false failures.
EXCEPTION_MARKERS = (
    "Traceback (most recent call last)",
    "HTTPStatusError",
    "Internal Server Error",
    "jinja2.exceptions",
    "sqlalchemy.exc",
    "AttributeError:",
    "KeyError:",
    "TypeError:",
    "ValueError:",
)

SHOT_DIR = Path(os.environ.get("CLOSEBOOK_E2E_SHOTS", "/tmp/proof/e2e"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _subprocess_env(db_path: Path) -> dict[str, str]:
    """The app as CI sees it: an empty DB and no provider credentials.

    The provider variables are set to EMPTY STRINGS rather than merely removed.
    pydantic-settings gives real environment variables priority over the `.env`
    file, so blanking them is what actually guarantees a credential-free boot —
    deleting them would let a developer's `.env` leak back in and quietly make
    this test pass for the wrong reason.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AWX_", "LLM_"))
        and k
        not in {
            "TREASURY_PRIVATE_KEY",
            "RELAYER_PRIVATE_KEY",
            "PUBLIC_BASE_URL",
            "WEBHOOK_PATH",
            "CLOSEBOOK_DATABASE_URL",
            "CLOSEBOOK_HMAC_SECRET",
        }
    }
    # No provider credentials. Empty, so .env cannot put them back.
    env["AWX_CLIENT_ID"] = ""
    env["AWX_API_KEY"] = ""
    env["AWX_WEBHOOK_SECRET"] = ""
    env["LLM_API_KEY"] = ""
    # An empty temp database for this run only.
    env["CLOSEBOOK_DATABASE_URL"] = f"sqlite:///{db_path}"
    # Approval signing needs a key; a fixed test-only value keeps that path
    # renderable without reaching for a real secret.
    env["CLOSEBOOK_HMAC_SECRET"] = "e2e-test-only-not-a-real-secret"
    env["PYTHONPATH"] = str(BACKEND_DIR)
    env["PYTHONUNBUFFERED"] = "1"
    return env


@pytest.fixture(scope="module")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Start the app on a free port, on an empty temp DB, and wait for /healthz."""
    db_path = tmp_path_factory.mktemp("e2e-db") / "e2e.sqlite3"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=str(BACKEND_DIR),
        env=_subprocess_env(db_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:  # died on boot
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
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    """Headless Chromium, or a skip naming exactly what to install."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = None
        try:
            b = p.chromium.launch(headless=True)
        except Exception as e:  # browser binary missing
            pytest.skip(
                f"chromium is not installed for playwright ({e}); "
                "run `playwright install chromium`"
            )
        assert b is not None
        yield b
        b.close()


@pytest.mark.parametrize("width,height", WIDTHS, ids=lambda v: f"{v}px")
@pytest.mark.parametrize("path,page_id", PAGES, ids=[p[1] for p in PAGES])
def test_page_renders_clean(browser, live_server, path, page_id, width, height):
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()

    console_errors: list[str] = []
    page_errors: list[str] = []
    server_errors: list[str] = []

    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    page.on(
        "response",
        lambda r: server_errors.append(f"{r.status} {r.url}") if r.status >= 500 else None,
    )

    try:
        response = page.goto(f"{live_server}{path}", wait_until="load", timeout=30_000)
        page.wait_for_timeout(600)  # let deferred fetches land before judging

        # 1. The document itself.
        assert response is not None, f"{path} returned no response"
        assert response.status == 200, f"{path} returned HTTP {response.status} at {width}px"

        # 2. It is the real page, not an error page wearing a 200.
        body = page.inner_text("body")
        assert "Closebook" in (page.title() or ""), f"{path} title was {page.title()!r}"
        assert len(body.strip()) > 200, f"{path} rendered almost nothing ({len(body)} chars)"

        # 3. No raw exception text in the rendered page.
        leaked = [m for m in EXCEPTION_MARKERS if m in body]
        assert not leaked, f"{path} rendered exception text {leaked} at {width}px"

        # 4. No console errors, no uncaught exceptions.
        assert not console_errors, f"{path} console errors at {width}px: {console_errors[:5]}"
        assert not page_errors, f"{path} page exceptions at {width}px: {page_errors[:5]}"

        # 5. No 5xx anywhere in the page's traffic.
        assert not server_errors, f"{path} server errors at {width}px: {server_errors[:5]}"

        # 6. No horizontal overflow on the phone width.
        if width <= 390:
            scroll_w = page.evaluate("document.documentElement.scrollWidth")
            assert scroll_w <= width, (
                f"{path} overflows horizontally at {width}px: scrollWidth={scroll_w}"
            )

        # Proof screenshot, kept regardless of outcome.
        shot = SHOT_DIR / f"{width}px" / f"{page_id}.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shot))
    finally:
        context.close()
