"""Pass 3 follow-up: label the bar, fold the empty steps, and stop making the
deposit form wait for Airwallex.

Three things, each one something a reader actually hit:

  * the waterfall's currency existed ONLY as `data-wf-ccy`, so with two bars
    side by side there was nothing on screen to tell them apart -- a report of
    the GBP and USD bars named them the wrong way round;
  * a bar padded with 0.00 blocks buries the one term that matters, and when
    nothing but cash is left there is no proportion to draw at all;
  * Money in's first view after a restart spent ~2.8s inside the render because
    the account picker needs a list the page went and fetched.
"""
from __future__ import annotations

import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import reset_default_db, session_scope  # noqa: E402
from app.domain.ledger import post  # noqa: E402
from app.domain.policy import available_cash_minor, cash_waterfall  # noqa: E402
from app.ui import _waterfall_bar  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated():
    reset_default_db()
    yield
    reset_default_db()


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def _cash(ccy: str, minor: int) -> None:
    with session_scope() as s:
        post(s, f"opening:{ccy}", [(f"cash:{ccy}", minor, 0, ccy),
                                   ("opening_balance", 0, minor, ccy)])


def _amounts_in(html: str) -> list:
    """The numeric value of every rendered segment."""
    out = []
    segs = re.findall(r'data-wf="([a-z_]+)"[^>]*?><b>([^<]+)</b>', html)
    for key, raw in segs:
        # "+ 30,370.00" / "\u2212 900.00" / "-8,646.00". The sign is rendered with a
        # SPACE after it, so `float` saw "- 900.00" and raised -- which silently
        # dropped every NEGATIVE segment. A parser that cannot see the negative
        # terms cannot see a zero one either, so the check below was blind on
        # exactly the segments most likely to be wrong.
        txt = (raw.replace("\u2212", "-").replace("+", "")
               .replace(",", "").replace(" ", "").strip())
        if not txt:
            continue
        try:
            out.append((key, float(txt)))
        except ValueError:
            raise AssertionError(f"unparsable segment amount: {raw!r}")
    return out


class TestTheCurrencyIsOnTheBar:
    def test_the_label_is_visible_not_just_a_data_attribute(self):
        html = _waterfall_bar("USD", cash_waterfall(cash_minor=1_000_00,
                                                    committed_this_run_minor=100_00))
        assert 'data-testid="wf-currency"' in html
        assert re.search(r'data-testid="wf-currency">USD<', html)

    def test_the_closing_figure_reads_in_the_tile_format(self):
        """The same string `money_str` puts on the tile, so the bar and the tile
        above it read as the same number."""
        from app.money import money_str

        html = _waterfall_bar("USD", cash_waterfall(cash_minor=1_000_00,
                                                    committed_this_run_minor=1_100_00))
        expected = money_str(-100_00, "USD") + " free to spend"
        assert f'data-testid="wf-currency">USD<' in html
        assert expected in html, expected

    def test_the_currency_is_in_the_aria_label(self):
        """A screen reader gets the currency too, and each term by name."""
        html = _waterfall_bar("GBP", cash_waterfall(
            cash_minor=1_000_00, committed_this_run_minor=250_00, floor_minor=50_00))
        label = re.search(r'aria-label="([^"]+)"', html).group(1)
        assert "GBP" in label
        assert "promised" in label and "reserve floor" in label
        assert "free to spend" in label

    def test_the_label_is_not_confusable_between_bars(self):
        """Two currencies, two labels, each naming itself."""
        a = _waterfall_bar("GBP", cash_waterfall(cash_minor=1_000_00,
                                                 committed_this_run_minor=1_00))
        b = _waterfall_bar("USD", cash_waterfall(cash_minor=2_000_00,
                                                 committed_this_run_minor=1_00))
        assert 'data-wf-ccy="GBP"' in a and "GBP" in a
        assert 'data-wf-ccy="USD"' in b and "USD" in b
        assert 'data-wf-ccy="USD"' not in a


class TestZeroSegmentsAreFolded:
    def test_a_zero_subtraction_renders_no_segment(self):
        html = _waterfall_bar("USD", cash_waterfall(
            cash_minor=1_000_00, committed_this_run_minor=100_00,
            suspense_minor=0, refunds_owed_minor=0, in_flight_minor=0, floor_minor=0))
        keys = [k for k, _ in _amounts_in(html)]
        assert "in_flight" not in keys
        assert "unmatched" not in keys
        assert "refunds_owed" not in keys
        assert "floor" not in keys
        assert "promised" in keys

    def test_no_segment_is_ever_zero_on_the_page(self, client):
        _cash("USD", 1_000_00)
        _cash("GBP", 500_00)
        html = client.get("/app").text
        zeroes = [(k, v) for k, v in _amounts_in(html) if v == 0.0]
        assert not zeroes, f"zero segments rendered: {zeroes}"

    def test_the_label_describes_what_is_drawn(self):
        """The label and the picture must agree: it names the terms that are on
        the bar, in order, so a screen reader hears the same bar everyone sees."""
        html = _waterfall_bar("USD", cash_waterfall(
            cash_minor=1_000_00, committed_this_run_minor=100_00,
            suspense_minor=0, refunds_owed_minor=0, in_flight_minor=0, floor_minor=0))
        label = re.search(r'aria-label="([^"]+)"', html).group(1)
        assert "promised" in label
        assert "unmatched" not in label, "a folded term must not be described"
        assert "ending at" in label


class TestTheSingleLineForm:
    def test_nothing_set_aside_renders_one_sentence(self):
        """Cash and nothing else: there is no proportion to draw."""
        html = _waterfall_bar("GBP", cash_waterfall(cash_minor=4_800_00))
        assert 'data-testid="wf-simple"' in html
        assert "GBP 4,800.00, nothing set aside, all free to spend" in html

    def test_it_renders_no_segments_at_all(self):
        html = _waterfall_bar("GBP", cash_waterfall(cash_minor=4_800_00))
        assert "x-wf-seg" not in html
        assert 'class="x-wf-bar"' not in html

    def test_it_still_names_its_currency(self):
        html = _waterfall_bar("GBP", cash_waterfall(cash_minor=4_800_00))
        assert 'data-testid="wf-currency">GBP<' in html
        assert 'data-wf-ccy="GBP"' in html

    def test_one_live_term_is_enough_to_draw_the_bar(self):
        """The sentence is for a book with NOTHING set aside, not for a quiet one."""
        html = _waterfall_bar("GBP", cash_waterfall(cash_minor=4_800_00,
                                                    floor_minor=500_00))
        assert 'data-testid="wf-simple"' not in html
        assert "x-wf-seg" in html

    def test_and_it_still_agrees_with_the_basis(self):
        w = cash_waterfall(cash_minor=4_800_00)
        assert w["free_minor"] == w["total_minor"] == available_cash_minor(
            cash_minor=4_800_00)
        assert w["free_minor"] == 4_800_00


class TestMoneyInDoesNotBlockOnAColdCache:
    def test_it_renders_with_no_accounts_in_the_cache(self, client, monkeypatch):
        """The regression was ~2.8s inside the render. Nothing in the render may
        go and fetch."""
        import app.domain.intake as I

        monkeypatch.setattr(I, "accounts_cached", lambda *a, **k: [])

        def boom(*a, **k):
            raise AssertionError("the render must not fetch the account list")

        monkeypatch.setattr(I, "refresh_accounts", boom)
        r = client.get("/app?tab=money-in")
        assert r.status_code == 200

    def _cold(self, monkeypatch, *, sandbox_ready=True):
        """A cache that is empty, with the sandbox either configured or not.

        READINESS MATTERS AND IS EASY TO FORGET: without credentials
        `sandbox_ready()` is False and "none found" is the CORRECT branch, so a
        test that only empties the cache passes locally (where .env exists) and
        fails on CI (where it does not). Both branches are pinned below.
        """
        import app.domain.intake as I
        import app.main as M

        monkeypatch.setattr(I, "accounts_cached", lambda *a, **k: [])
        monkeypatch.setattr(I, "refresh_accounts", lambda *a, **k: [])
        monkeypatch.setattr(M, "healthz", lambda: {
            "ok": True, "sandbox_ready": sandbox_ready, "database": "ok"})

    def test_it_says_it_is_loading_rather_than_nothing(self, client, monkeypatch):
        """`none found` is a CLAIM about the account setup and would be wrong."""
        self._cold(monkeypatch, sandbox_ready=True)
        html = client.get("/app?tab=money-in").text
        assert 'data-testid="accounts-loading"' in html
        assert "Loading accounts" in html
        assert "none found" not in html

    def test_the_placeholder_keeps_the_control_and_its_testid(self, client, monkeypatch):
        """The form must be usable the moment it fills in, so the ids stand."""
        self._cold(monkeypatch, sandbox_ready=True)
        html = client.get("/app?tab=money-in").text
        assert 'id="f-global_account_id"' in html
        assert 'name="global_account_id"' in html
        assert 'data-loading="1"' in html

    def test_with_no_sandbox_configured_it_says_none_found(self, client, monkeypatch):
        """The other side of the same branch: no credentials means no accounts,
        and that is a fact about the setup rather than a page still loading."""
        self._cold(monkeypatch, sandbox_ready=False)
        html = client.get("/app?tab=money-in").text
        assert "none found" in html
        # Asserted on the MARKUP, not the words: the page's own script contains
        # the literal "Loading accounts" in its filler code, so a text search
        # finds the script rather than the control.
        assert 'data-loading="1"' not in html
        assert 'data-testid="accounts-loading"' not in html

    def test_the_json_endpoint_serves_the_same_options(self, client, monkeypatch):
        """The late-arriving options must be the ones a warm cache would give."""
        import app.domain.intake as I

        accts = [{"id": "a1", "currency": "GBP", "country": "GB",
                  "status": "ACTIVE", "account_number": "07754504",
                  "usable": True}]
        monkeypatch.setattr(I, "accounts_cached", lambda *a, **k: accts)
        monkeypatch.setattr(I, "accounts_stale", lambda *a, **k: False)
        j = client.get("/money-in/accounts").json()
        assert j["ok"] and j["ready"] and j["count"] == 1
        assert "GBP account …4504" in j["html"]

    def test_the_endpoint_is_read_only(self, client):
        """It moves no money and writes nothing: it is the same builder."""
        import inspect

        from app import main as M

        src = inspect.getsource(M.money_in_accounts)
        for banned in ("create", "transfer", "execute", "commit", "flush"):
            assert banned not in src, banned

    def test_the_endpoint_reports_failure_rather_than_raising(self, client, monkeypatch):
        import app.domain.intake as I

        def boom(*a, **k):
            raise RuntimeError("sandbox down")

        monkeypatch.setattr(I, "accounts_cached", boom)
        monkeypatch.setattr(I, "accounts_stale", lambda *a, **k: False)
        r = client.get("/money-in/accounts")
        assert r.status_code == 200
        j = r.json()
        assert j["ok"] is False and j["ready"] is False

    def test_the_page_carries_the_script_that_fills_it_in(self, client, monkeypatch):
        import app.domain.intake as I

        monkeypatch.setattr(I, "accounts_cached", lambda *a, **k: [])
        monkeypatch.setattr(I, "refresh_accounts", lambda *a, **k: [])
        html = client.get("/app?tab=money-in").text
        assert "/money-in/accounts" in html

    def test_the_public_pages_do_not_carry_it(self, client):
        """The landing page runs a different script and polls nothing."""
        from app.ui import _LANDING_JS

        assert "/money-in/accounts" not in _LANDING_JS

    def test_the_cache_is_warmed_at_startup_off_the_request_path(self):
        """In a THREAD: startup must not wait for the sandbox either."""
        import inspect

        from app import main as M

        src = inspect.getsource(M._warm_the_account_cache)
        assert "threading" in src and "daemon=True" in src
        assert "refresh_accounts" in src
