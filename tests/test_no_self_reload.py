"""The page must not rebuild itself under the person reading it.

WHAT HAPPENED
    An automated tester failed four tests — Launch app, Back to site, How it
    works, Safety — all with "stale or non-interactable element". The links were
    fine. Every page, the public landing included, ran:

        setTimeout(refresh, 5000) -> window.location.reload()

    The whole DOM was rebuilt every five seconds, so anything a test had just
    located was stale by the time it clicked; and a real visitor got yanked back
    mid-scroll or mid-read. A page that reloads on its own is a page that fights
    everyone using it.

THE RULE NOW
    * Public pages get NO reload and NO polling — only their toggles.
    * The dashboard polls a cheap stamp and OFFERS a refresh instead of taking
      one, and never even offers while someone is mid-task.
"""
from __future__ import annotations

import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import get_default_db, reset_default_db, session_scope  # noqa: E402

LANDING_ROUTES = ["/"]
APP_ROUTES = ["/app", "/app?tab=money-in", "/app?tab=money-out", "/app?tab=ok",
              "/app?tab=ai", "/app?tab=setup"]


@pytest.fixture(autouse=True)
def _isolated():
    reset_default_db()
    yield
    reset_default_db()


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


# ---- the landing page must be still ---------------------------------------
class TestThePublicPagesNeverReloadThemselves:
    def test_the_landing_has_no_reload_script(self, client):
        html = client.get("/").text
        assert "window.location.reload" not in html, \
            "a landing page that reloads itself loses the reader AND the test"
        assert "setTimeout(refresh" not in html
        assert "location.href" not in html

    def test_the_landing_does_not_poll_anything(self, client):
        html = client.get("/").text
        assert "/state/version" not in html, \
            "the public page has nothing to keep current"
        assert "setInterval" not in html

    def test_the_landing_still_gets_its_toggles(self, client):
        """The split must not strip what the page legitimately needs."""
        html = client.get("/").text
        assert "data-toggle" in html
        assert "addEventListener" in html, "the FAQ toggles need a script"

    def test_the_dashboard_does_have_the_poller_and_the_pill(self, client):
        html = client.get("/app").text
        assert "/state/version" in html
        assert 'data-testid="new-activity"' in html
        assert "midTask" in html, "the pause rules must be in the shipped script"

    def test_the_dashboard_never_reloads_unprompted(self, client):
        """The one reload left must be the pill's own click handler."""
        html = client.get("/app").text
        assert "window.location.reload" in html, "the pill needs it"
        assert "setTimeout(refresh" not in html, \
            "no timer may reload the page"
        # and it must be reachable only from the pill
        assert 'pill.addEventListener("click"' in html


# ---- the version endpoint --------------------------------------------------
class TestTheVersionStamp:
    def test_it_exists_and_is_cheap(self, client):
        started = time.monotonic()
        r = client.get("/state/version")
        elapsed = time.monotonic() - started
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert isinstance(body["version"], str) and body["version"]
        assert elapsed < 0.5, f"the poll target took {elapsed:.2f}s"

    def test_reading_the_page_does_not_move_it(self, client):
        """A page view must never change the version it is watching, or the
        poller would tell every reader the book changed the moment they looked
        at it."""
        before = client.get("/state/version").json()["version"]
        for route in APP_ROUTES + LANDING_ROUTES:
            client.get(route)
        client.get("/state")
        assert client.get("/state/version").json()["version"] == before

    def test_a_ledger_entry_moves_it(self, client):
        before = client.get("/state/version").json()["version"]
        from app.domain.ledger import post

        with session_scope() as s:
            post(s, "txn-moves-the-version",
                 [("cash:USD", 1_000, 0, "USD"),
                  ("opening_balance", 0, 1_000, "USD")])
        assert client.get("/state/version").json()["version"] != before

    def test_a_new_inflow_moves_it(self, client):
        before = client.get("/state/version").json()["version"]
        from app.domain import intake as I

        I.ingest_deposit(get_default_db(), {
            "id": "dep-version-1", "currency": "USD", "amount": 10,
            "payer_name": "Someone", "reference": "hello", "status": "SETTLED"})
        assert client.get("/state/version").json()["version"] != before

    def test_it_is_one_query_and_does_not_leak_a_connection(self, tmp_path,
                                                            monkeypatch):
        from sqlalchemy import event
        from app.db import DB, make_engine

        engine = make_engine(f"sqlite:///{tmp_path / 'v.sqlite3'}")
        live: list[int] = []

        @event.listens_for(engine, "checkout")
        def _out(dbapi_conn, _rec, _proxy):      # noqa: ANN001
            live.append(id(dbapi_conn))

        @event.listens_for(engine, "checkin")
        def _in(dbapi_conn, _rec):               # noqa: ANN001
            if id(dbapi_conn) in live:
                live.remove(id(dbapi_conn))

        import app.main as M

        # monkeypatch, NOT a bare assignment: a module-level patch here stays
        # installed for every later test file and points them all at this
        # throwaway database.
        monkeypatch.setattr(M, "_database", lambda: DB(str(engine.url)))
        c = TestClient(M.app, raise_server_exceptions=False)
        for _ in range(12):
            assert c.get("/state/version").status_code == 200
        assert live == [], f"{len(live)} connection(s) still checked out"

    def test_it_never_raises_a_raw_500(self, monkeypatch):
        import app.main as M

        class Boom:
            def session(self):
                raise RuntimeError("disk I/O error")

        monkeypatch.setattr(M, "_database", lambda: Boom())
        c = TestClient(M.app, raise_server_exceptions=False)
        r = c.get("/state/version")
        assert r.status_code == 200
        assert r.json()["ok"] is False


# ---- the navigation --------------------------------------------------------
class TestTheNavigationWorksOnEveryPage:
    def _landing(self, client):
        return client.get("/").text

    def test_the_landing_links_go_where_they_say(self, client):
        html = self._landing(client)
        assert 'href="/app"' in html, "Launch app"
        assert 'href="/#how"' in html, "How it works"
        assert 'href="/#safety"' in html, "Safety"

    def test_every_nav_link_has_a_stable_hook(self, client):
        html = self._landing(client)
        for hook in ("launch-app", "launch-app-hero", "how-it-works", "safety"):
            assert f'data-testid="{hook}"' in html, hook

    def test_the_anchors_exist_on_the_page(self, client):
        """A nav link to /#how is only useful if #how is actually there."""
        html = self._landing(client)
        for anchor in ('id="how"', 'id="safety"'):
            assert anchor in html, anchor

    def test_the_dashboard_links_back_to_the_site(self, client):
        for route in ("/app", "/app?tab=setup", "/app?tab=money-out"):
            html = client.get(route).text
            assert 'data-testid="back-to-site"' in html, route
            assert 'href="/"' in html, route

    def test_both_sets_of_tabs_are_hooked(self, client):
        from app.ui import NAV

        html = client.get("/app").text
        for key, *_ in NAV:
            assert f'data-testid="tab-{key}"' in html, f"desktop tab {key}"
            assert f'data-testid="tab-mobile-{key}"' in html, f"mobile tab {key}"

    def test_the_hooks_are_on_the_links_themselves(self, client):
        """A testid on a wrapper would not be clickable."""
        html = client.get("/app").text
        import re

        for m in re.finditer(r'<a[^>]*data-testid="tab-[^"]*"[^>]*>', html):
            assert "href=" in m.group(0), m.group(0)
