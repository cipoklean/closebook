"""The book a test opens must STAY open for the whole browser session.

Run 5 gave every test its own SQLite file behind `?ws=<suffix>`, and it still
did not isolate anything: a test opened its book, clicked a tab or submitted a
form, and was back in the shared one. The suffix lived only in the query string,
so it survived exactly one request. Every fetch, every redirect and every link
that did not repeat it silently resolved to the DEFAULT book, which is the
collision per-test books were built to end.

The fix has two halves, and both are asserted here:

1. **The cookie.** `?ws=qa-7f31` sets `closebook_ws=qa-7f31` for the session and
   every later request resolves the workspace from the query param FIRST, then
   the cookie. That is what makes a POST, its 303 redirect, a PIN approval, the
   `/state/*` polls and `/money-in/accounts` land in the same book without each
   one remembering to carry anything.
2. **The links and the fetches carry it too.** The page names its own book on
   every tab, form action, formaction, fetch and on the landing page's link back
   to `/app`, so a client that drops cookies still stays put.

Prod is asserted unchanged in the same file: without `CLOSEBOOK_WORKSPACES=1`
there is no cookie, no `Book:` line, and `?ws=` is still a 404.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.models import (  # noqa: E402
    Approval, Customer, InvoicePayable, InvoiceReceivable, PlanStep, Supplier,
)

COOKIE = "closebook_ws"
PIN = "ws-session-pin-2468"
COUNTED = (Customer, InvoiceReceivable, Supplier, InvoicePayable, Approval, PlanStep)
TABS = ("home", "money-in", "money-out", "ok", "ai", "setup")


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def workspaces(tmp_path, monkeypatch):
    """Per-test books enabled on a temp root, the way the tester runs."""
    monkeypatch.setenv("CLOSEBOOK_WORKSPACES", "1")
    monkeypatch.setenv("CLOSEBOOK_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("DEMO_APPROVER_PIN", PIN)
    from app import workspaces as ws
    from app.config import _settings_cached

    _settings_cached.cache_clear()
    # A CACHED HANDLE OUTLIVES ITS TEMP ROOT. Without this, book "aaa" in the
    # next test is the PREVIOUS test's file: its rows, its approvals and its
    # already-approved steps all come back as this test's mystery. Called only
    # if it exists, so this file also runs (and fails honestly) against a tree
    # that predates it.
    forget = getattr(ws, "forget_cached_books", None)
    if forget:
        forget()
    yield tmp_path / "ws"
    if forget:
        forget()
    _settings_cached.cache_clear()


@pytest.fixture()
def off(monkeypatch):
    monkeypatch.delenv("CLOSEBOOK_WORKSPACES", raising=False)


# ------------------------------------------------------------------- helpers
def session():
    """ONE client, so it keeps the cookie jar a browser would keep."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def url(path: str, ws: str) -> str:
    """Append the workspace to a path that may already carry a query string."""
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}ws={ws}"


def counts(suffix: str = "") -> dict:
    """Row counts for one book, read from the database itself."""
    from app import workspaces
    from app.db import get_default_db

    db = workspaces.book(suffix) if suffix else get_default_db()
    with db.session() as s:
        return {m.__tablename__: s.query(m).count() for m in COUNTED}


def write_customer(ws: str, name: str):
    return session().post(url("/setup/customers", ws), json={"name": name},
                          headers={"Accept": "application/json"})


def opened(ws: str):
    """A client that has opened book `ws` exactly once, cookie and all."""
    c = session()
    r = c.get(url("/app?tab=home", ws))
    assert r.status_code == 200, r.status_code
    return c


def seed_a_payment(suffix: str) -> int:
    """Write a payable's plan step into a book, through the REAL writer.

    Hand-assembled payloads are how a truncated payload shape survived 161
    tests once; `persist_plan` is the only thing that writes a step.
    """
    from app import workspaces
    from app.domain.plan_store import new_run_id, persist_plan
    from app.domain.planner import ACT_PAY, Step

    db = workspaces.book(suffix)
    with db.session() as s:
        rows, _skipped = persist_plan(s, new_run_id(), [
            Step(action=ACT_PAY, reason="seeded for the session test",
                 payable_number="WS-SEED-1", supplier="Seeded Supplier",
                 amount_minor=400_000, currency="USD")])
        return rows[0].id


# ------------------------------------------------------- 1. the cookie sticks
class TestTheCookieCarriesTheBook:
    def test_opening_a_book_sets_a_session_cookie(self, workspaces):
        r = session().get(url("/app?tab=home", "aaa"))
        cookie = r.headers.get("set-cookie", "")
        assert f"{COOKIE}=aaa" in cookie, cookie
        assert "Path=/" in cookie, cookie
        assert "SameSite=Lax" in cookie, cookie
        # A SESSION cookie: a test's book should not outlive the test's browser.
        assert "Max-Age" not in cookie and "Expires" not in cookie, cookie

    def test_a_later_request_without_the_query_param_is_still_the_same_book(
            self, workspaces):
        c = opened("aaa")
        write_customer("aaa", "Only In A")
        r = c.get("/app?tab=setup")
        assert "Only In A" in r.text, (
            "the cookie did not carry the book: a request with no ?ws= fell "
            "back to the shared book")
        assert r.headers.get("x-closebook-workspace") == "aaa"

    def test_a_client_with_no_cookie_still_gets_the_shared_book(self, workspaces):
        write_customer("aaa", "Only In A")
        assert "Only In A" not in session().get("/app?tab=setup").text

    def test_the_query_param_wins_over_the_cookie(self, workspaces):
        c = opened("aaa")
        r = c.get(url("/state", "bbb"))
        assert r.headers.get("x-closebook-workspace") == "bbb", (
            "a stale cookie overrode an explicit ?ws= in the URL")

    def test_the_cookie_is_refreshed_when_the_url_names_another_book(
            self, workspaces):
        c = opened("aaa")
        r = c.get(url("/app?tab=home", "bbb"))
        assert f"{COOKIE}=bbb" in r.headers.get("set-cookie", ""), r.headers

    def test_a_corrupt_cookie_is_ignored_rather_than_fatal(self, workspaces):
        c = session()
        c.cookies.set(COOKIE, "../../etc/passwd")
        r = c.get("/app?tab=home")
        assert r.status_code == 200, r.status_code
        assert r.headers.get("x-closebook-workspace") is None, (
            "an unusable cookie must fall back to the shared book, not select one")


# ------------------------------------------- 2. every route resolves the book
class TestEveryRouteResolvesTheBook:
    def test_state_version_is_the_book_the_cookie_names(self, workspaces):
        c = opened("aaa")
        before = c.get("/state/version").json()["version"]
        shared = session().get("/state/version").json()["version"]
        session().post(url("/setup/customers", "aaa"), json={"name": "V"},
                       headers={"Accept": "application/json"})
        c.post("/setup/invoices", json={"customer_id": 1, "number": "INV-V",
                                        "currency": "USD", "amount": "12000"},
               headers={"Accept": "application/json"})
        after = c.get("/state/version").json()["version"]
        assert after != before, "the version poll read a different book"
        assert session().get("/state/version").json()["version"] == shared, (
            "writing in one book moved the shared book's version")

    def test_state_tiles_are_the_book_the_cookie_names(self, workspaces):
        c = opened("aaa")
        r = c.get("/state/tiles")
        assert r.status_code == 200
        assert r.headers.get("x-closebook-workspace") == "aaa"

    def test_money_in_accounts_resolves_from_the_cookie(self, workspaces):
        c = opened("aaa")
        r = c.get("/money-in/accounts")
        assert r.status_code == 200
        assert r.headers.get("x-closebook-workspace") == "aaa"

    def test_a_post_with_no_ws_in_its_url_lands_in_the_cookie_book(
            self, workspaces):
        c = opened("aaa")
        before = counts()
        r = c.post("/setup/customers", json={"name": "Cookie Carried"},
                   headers={"Accept": "application/json"})
        assert r.status_code == 200
        assert "Cookie Carried" in c.get("/app?tab=setup").text
        assert counts()["customer"] == before["customer"], (
            "a POST with no ?ws= wrote into the shared book")

    def test_the_redirect_after_a_form_post_names_the_book(self, workspaces):
        c = opened("aaa")
        r = c.post("/setup/customers", data={"name": "Redirected"},
                   headers={"Accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303, r.status_code
        assert "ws=aaa" in r.headers["location"], r.headers["location"]
        landing = c.get(r.headers["location"])
        assert "Book: aaa" in landing.text
        assert "Redirected" in landing.text

    def test_a_pin_approval_lands_in_the_cookie_book(self, workspaces):
        step_id = seed_a_payment("aaa")
        before = counts()
        c = opened("aaa")
        r = c.post(f"/approvals/{step_id}/approve", json={"pin": PIN},
                   headers={"Accept": "application/json"})
        assert r.status_code == 200
        assert r.json().get("ok") is True, r.text
        assert counts("aaa")["approval"] == 1, "the approval did not land in aaa"
        assert counts()["approval"] == before["approval"], (
            "an approval POSTed with no ?ws= was recorded against the shared book")

    def test_a_sandbox_control_lands_in_the_cookie_book(self, workspaces):
        before = counts()
        before_a = counts("aaa")
        c = opened("aaa")
        # A real sandbox control route, refused locally (before any network
        # call) so this test stays provider-free. What is under test is WHERE
        # the press landed, not what the sandbox answered.
        r = c.post("/dev/transfers/none/transition", json={"what": "nope"},
                   headers={"Accept": "application/json"})
        assert r.status_code == 200
        assert "Pick settle, fail or return" in r.text, r.text
        assert r.headers.get("x-closebook-workspace") == "aaa"
        assert counts() == before and counts("aaa") == before_a

    def test_the_landing_page_links_back_into_the_book(self, workspaces):
        c = opened("aaa")
        r = c.get("/")
        assert r.status_code == 200
        assert 'href="/app?ws=aaa"' in r.text, (
            "the landing page's Launch app link dropped the book")

    def test_two_books_never_see_each_other(self, workspaces):
        a, b = opened("aaa"), opened("bbb")
        write_customer("aaa", "Only In A")
        assert "Only In A" in a.get("/app?tab=setup").text
        assert "Only In A" not in b.get("/app?tab=setup").text


# --------------------------------------- 3. the page names and carries the book
class TestThePageCarriesTheBook:
    def test_the_footer_names_the_active_book(self, workspaces):
        for tab in TABS:
            r = session().get(url(f"/app?tab={tab}", "qa-7f31"))
            assert "Book: qa-7f31" in r.text, f"{tab} did not name its book"

    def test_the_footer_is_absent_without_a_book(self, workspaces):
        assert "Book:" not in session().get("/app?tab=home").text

    def test_the_tab_links_carry_the_book(self, workspaces):
        c = opened("aaa")
        html = c.get("/app?tab=home").text
        for tab in TABS:
            assert f'href="/app?tab={tab}&ws=aaa"' in html, f"tab {tab} lost the book"
            assert f'data-testid="tab-mobile-{tab}"' in html

    def test_every_form_action_carries_the_book(self, workspaces):
        c = opened("aaa")
        html = c.get("/app?tab=setup").text
        for action in ("/setup/settings", "/setup/customers", "/setup/invoices",
                       "/setup/suppliers", "/setup/payables", "/workspace/reset"):
            assert f'action="{action}?ws=aaa"' in html, f"{action} dropped the book"

    def test_the_sandbox_and_approval_forms_carry_the_book(self, workspaces):
        step_id = seed_a_payment("aaa")
        c = opened("aaa")
        # The sandbox panel's deposit control: a real sandbox-only form, and
        # one that renders in a book with nothing in it.
        money_in = c.get("/app?tab=money-in").text
        assert 'action="/money-in/simulate?ws=aaa"' in money_in
        assert 'formaction="/money-in/poll?ws=aaa"' in money_in
        approve = c.get("/app?tab=ok").text
        assert f'action="/approvals/{step_id}/approve?ws=aaa"' in approve
        assert f'formaction="/approvals/{step_id}/reject?ws=aaa"' in approve

    def test_the_state_fetches_carry_the_book(self, workspaces):
        c = opened("aaa")
        html = c.get("/app?tab=money-in").text
        for path in ("/state/version", "/state/tiles", "/money-in/accounts"):
            assert f'fetch("{path}?ws=aaa"' in html, f"{path} poll dropped the book"

    def test_assets_are_left_alone_and_a_fragment_survives(self, workspaces):
        app_html = session().get(url("/app?tab=home", "aaa")).text
        assert 'href="/static/closebook.css?v=' in app_html, "the asset URL was rewritten"
        # A fragment link on the landing page is a real one, rendered with no
        # data needed: the anchor must still arrive AFTER the workspace.
        land = opened("aaa").get("/").text
        assert 'href="/?ws=aaa#how"' in land, "the fragment was dropped or misplaced"


class TestCarryWorkspaceIsAPureFunction:
    """The rewriter that carries the book, asserted on its own."""

    def _carry(self, html, ws="aaa"):
        from app.ui import carry_workspace

        return carry_workspace(html, ws)

    def test_no_book_is_the_identity(self):
        html = '<a href="/app?tab=home">x</a>'
        assert self._carry(html, "") == html

    def test_it_adds_to_href_action_and_formaction(self):
        html = ('<a href="/app?tab=home">x</a>'
                '<form action="/setup/customers">'
                '<button formaction="/money-in/poll">')
        out = self._carry(html)
        assert 'href="/app?tab=home&ws=aaa"' in out
        assert 'action="/setup/customers?ws=aaa"' in out
        assert 'formaction="/money-in/poll?ws=aaa"' in out

    def test_it_adds_to_a_fetch_literal(self):
        assert 'fetch("/state/version?ws=aaa"' in self._carry(
            'fetch("/state/version", { headers: {} })')

    def test_it_leaves_untouched_things_alone(self):
        html = ('<link rel="stylesheet" href="/static/closebook.css?v=3">'
                '<a href="https://fonts.googleapis.com/css2">f</a>'
                '<a href="//cdn.example.com/x">p</a>'
                '<a href="#sandbox-row-1">j</a>'
                '<img src="/static/logo.png">')
        assert self._carry(html) == html

    def test_it_keeps_a_fragment_and_does_not_double_add(self):
        out = self._carry('<a href="/app?tab=home#held-PO-1">h</a>'
                          '<a href="/app?tab=home&ws=aaa">already</a>')
        assert 'href="/app?tab=home&ws=aaa#held-PO-1"' in out
        assert out.count("ws=aaa") == 2, out


# ------------------------------------------------ 4. prod is untouched by this
class TestProdIsUntouched:
    def test_a_workspace_query_is_still_refused(self, off):
        r = session().get("/app?ws=anything")
        assert r.status_code == 404, r.status_code
        assert "set-cookie" not in {k.lower() for k in r.headers}, r.headers

    def test_a_cookie_alone_changes_nothing(self, off):
        c = session()
        c.cookies.set(COOKIE, "anything")
        r = c.get("/app?tab=home")
        assert r.status_code == 200, r.status_code
        assert "Book:" not in r.text, "prod showed a book label"
        assert "set-cookie" not in {k.lower() for k in r.headers}, r.headers

    def test_no_link_grows_a_ws_without_the_flag(self, off):
        html = session().get("/app?tab=home").text
        assert "ws=" not in html, "prod links carried a workspace"
