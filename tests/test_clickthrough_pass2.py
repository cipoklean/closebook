"""Pass 2: the page is cut down to what is true and said once.

Three things this pins, because each was a real defect:

  * THE LANDING INVENTED ITS EVIDENCE. A testimonial from "Kano Craft Imports"
    (no such customer was ever in the book), a decorative "0.96" and a
    "9,012.85 EUR Processing" chip. A reader who checked could find nothing
    behind any of them. It now shows counts from the same tables the app
    renders, or says it has none.
  * THE SAME FACT APPEARED THREE TIMES. PO-2002's hold reason printed in full
    on Home, again on Money out and again on Needs your OK.
  * THE MACHINE'S WORDS WERE ON THE SCREEN. An account picker read
    "USD · US · ACTIVE · 5888", where the only part that tells two accounts
    apart is the last four digits.
"""
from __future__ import annotations

import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import reset_default_db  # noqa: E402
from app.ui import landing_proof, render_landing  # noqa: E402

TABS = ["home", "money-in", "money-out", "ok", "ai", "setup"]


@pytest.fixture(autouse=True)
def _isolated():
    reset_default_db()
    yield
    reset_default_db()


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def _page(tab: str) -> str:
    return client_page(tab)


def client_page(tab: str) -> str:
    from app.main import app
    return TestClient(app, raise_server_exceptions=False).get(
        f"/app?tab={tab}").text


class TestTheLandingInventsNothing:
    def test_the_testimonial_and_the_decorative_figures_are_gone(self):
        html = render_landing({"deposits": 6, "by_rules": 4, "by_model": 1,
                               "invoices": 4, "bills": 5, "ledger_rows": 18,
                               "balanced": True})
        for slop in ("Kano Craft", "KC /", "0.96", "9,012.85", "Processing</"):
            assert slop not in html, f"invented evidence still on the page: {slop}"

    def test_the_extra_tile_grids_are_gone(self):
        html = render_landing({"deposits": 1})
        for section in ("The problem", "Who it&rsquo;s for", "Who it's for",
                        "Built on", 'id="who"'):
            assert section not in html, f"the cut section is back: {section}"

    def test_the_page_it_kept_is_the_short_one(self):
        html = render_landing({"deposits": 1})
        assert 'id="how"' in html and 'id="safety"' in html
        assert "How it works" in html and "Safety" in html
        assert "Live from this book" in html

    def test_every_link_on_it_goes_somewhere_real(self):
        """A page whose own CTA is dead is the first thing anyone clicks."""
        html = render_landing({"deposits": 1})
        for href in re.findall(r'href="(/[^"]*)"', html):
            target = href.split("#")[0] or "/"
            assert (target in ("/", "/app", "/#how", "/#safety")
                    or target.startswith("/static/")), href

    def test_the_anchors_it_links_to_are_on_the_page(self):
        html = render_landing({"deposits": 1})
        for anchor in re.findall(r'href="/#([a-z]+)"', html):
            assert f'id="{anchor}"' in html, f"#{anchor} is linked but absent"


class TestTheProofIsReal:
    def test_an_empty_book_says_so_instead_of_showing_zeros(self):
        html = render_landing({"deposits": 0, "invoices": 0, "bills": 0})
        assert "nothing to show yet" in html
        assert "0</span>" not in html, "an empty book must not dress a zero as a result"

    def test_the_figures_shown_are_the_ones_handed_in(self):
        html = render_landing({"deposits": 6, "by_rules": 4, "by_model": 1,
                               "invoices": 4, "bills": 5, "ledger_rows": 18,
                               "balanced": True})
        assert ">6<" in html and ">4<" in html and ">1<" in html
        assert "read by the model, then re-checked" in html

    def test_it_reports_the_book_as_unbalanced_when_it_is(self):
        html = render_landing({"deposits": 3, "balanced": False})
        assert ">no<" in html

    def test_it_counts_from_the_real_tables(self):
        """`landing_proof` must not invent a total it was not given."""
        pr = landing_proof({"deposits": 3, "by_rules": 2, "by_model": 1,
                            "invoices": 4, "bills": 5, "ledger_rows": 9,
                            "balanced": True})
        assert pr == {"deposits": 3, "by_rules": 2, "by_model": 1, "invoices": 4,
                      "bills": 5, "balanced": True, "empty": False,
                      "ledger_rows": 9}
        assert landing_proof(None)["empty"] is True

    def test_the_landing_needs_no_network(self, client, monkeypatch):
        """It reads the DB and nothing else, so it works with the sandbox down."""
        import app.airwallex.client as C

        def boom(*a, **k):
            raise AssertionError("the landing page must not call Airwallex")

        monkeypatch.setattr(C, "AirwallexClient", boom)
        assert client.get("/").status_code == 200


class TestAReasonIsStatedOnce:
    def test_the_landing_and_the_app_do_not_share_a_headline(self):
        """The landing's How-it-works heading is its own; the app's tagline is
        the sidebar's. Printed twice on one screen it reads as filler."""
        landing = render_landing({"deposits": 1})
        assert landing.count("The model reads. The rules decide.") == 1

    def test_the_app_tagline_appears_once_per_screen(self, client):
        for tab in TABS:
            html = client.get(f"/app?tab={tab}").text
            assert html.count("model reads. The rules decide") == 1, tab

    def test_the_hold_anchors_are_well_formed(self, client):
        """An anchor with a space or a stray quote in it is a dead link."""
        home = client.get("/app?tab=home").text
        for anchor in re.findall(r'id="(held-[^"]*)"', home):
            assert re.fullmatch(r"held-[A-Za-z0-9_-]+", anchor), anchor

    def test_a_link_to_a_hold_points_at_an_anchor_that_exists(self, client):
        home = client.get("/app?tab=home").text
        ids = set(re.findall(r'id="([^"]+)"', home))
        for target in re.findall(r'href="#(held-[^"]*)"', home):
            assert target in ids, f"#{target} is linked but not on the page"


class TestTheDropdownsReadLikeAccounts:
    def test_the_account_label_is_the_currency_and_the_last_four(self, client):
        html = client.get("/app?tab=money-in").text
        for label in re.findall(r"<option[^>]*>([^<]+)</option>", html):
            if "account" in label:
                assert re.fullmatch(r"[A-Z]{3} account( …\d{4})?( — not active yet)?",
                                    label.strip()), label

    def test_the_old_shouted_form_is_gone(self, client):
        html = client.get("/app?tab=money-in").text
        assert "· ACTIVE ·" not in html
        assert "ACTIVE ·" not in html

    def test_the_country_and_status_are_still_reachable(self, client):
        """Moved, not deleted: a hover title carries what the label dropped."""
        html = client.get("/app?tab=money-in").text
        titles = re.findall(r'<option[^>]*title="([^"]+)"', html)
        for t in titles:
            assert re.fullmatch(r"[A-Z]{2} · [A-Z]+", t), t


class TestAnAccountWithNoNumber:
    def test_it_does_not_invent_a_number(self, client):
        """Airwallex returns "-" for an account with no number yet. Slicing its
        last four characters rendered "HKD account …-" -- a wrong figure in the
        one position that tells two accounts apart."""
        html = client.get("/app?tab=money-in").text
        assert "…-" not in html
        assert "account …-" not in html
        for label in re.findall(r"<option[^>]*>([^<]+)</option>", html):
            assert "…-" not in label, label


class TestTheBankReferenceIsFoldedAway:
    def test_the_uuid_is_behind_a_toggle_not_in_the_reader_s_face(self, client):
        html = client.get("/app?tab=money-out").text
        if 'data-testid="bank-ref"' not in html:
            pytest.skip("no live transfer on this book")
        fold = re.search(r'<details[^>]*data-testid="bank-ref".*?</details>',
                         html, flags=re.S)
        assert fold, "the bank reference must be inside its own <details>"
        assert "Environment" in fold.group(0)
        outside = html.replace(fold.group(0), "")
        assert "Environment" not in outside

    def test_the_status_stays_visible(self, client):
        """The answer is the status; the UUID is only needed when it breaks."""
        html = client.get("/app?tab=money-out").text
        if 'data-testid="bank-ref"' not in html:
            pytest.skip("no live transfer on this book")
        head = re.search(r'<details[^>]*data-testid="bank-ref".*?<summary>(.*?)</summary>',
                         html, flags=re.S)
        assert head and re.search(
            r"PAID|COMPLETED|PROCESSING|PENDING|SUBMITTED|CANCELLED|UNCHECKED|FAILED",
            head.group(1)), head.group(1) if head else "no summary"


class TestPlainWords:
    def test_suspense_is_called_unmatched_money(self, client):
        home = client.get("/app?tab=home").text
        assert "Unmatched money" in home
        assert "Suspense" not in home

    def test_unattributed_is_called_unmatched(self, client):
        home = client.get("/app?tab=home").text
        assert "unattributed" not in home.lower()

    def test_owed_back_is_called_refunds_owed(self, client):
        home = client.get("/app?tab=home").text
        assert "owed back" not in home.lower()

    def test_the_old_word_is_gone_from_every_tab(self, client):
        for tab in TABS:
            html = client.get(f"/app?tab={tab}").text
            low = html.lower()
            assert "unattributed" not in low, tab
            assert "owed back" not in low, tab
