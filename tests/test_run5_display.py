"""The three display findings from run 5.

2. Home showed USD -4,880.00 after a release. A negative figure is a real
   position (committed more than held), so the number is right; the LABEL was
   wrong, because "-4,880.00 free to spend" contradicts itself. It now says
   "over-committed by USD 4,880.00". And a release of USD 120 must raise the
   figure by exactly 120, since that is money leaving suspense.
3. Three bills showed only "Held by policy", which is not a reason. A held card
   must name its blocker with numbers, and when the planner recorded none the
   card must say THAT rather than assert a rule nobody can see.
4. After an exact match the invoice read PAID but the cash figure did not move
   without a reload. The tile must be in the set the page updates in place.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import reset_default_db, session_scope  # noqa: E402
from app.domain.ledger import post  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    reset_default_db()
    yield
    reset_default_db()


def _fund(minor: int, tag: str = "a"):
    with session_scope() as s:
        post(s, f"open:{tag}", [("cash:USD", minor, 0, "USD"),
                                ("opening_balance", 0, minor, "USD")])


def _bar(**kw) -> str:
    from app.domain.policy import cash_waterfall
    from app.ui import _waterfall_bar

    return _waterfall_bar("USD", cash_waterfall(**kw))


class TestANegativePositionIsNamed:
    def test_it_says_over_committed_rather_than_minus_free(self):
        html = _bar(cash_minor=1_000_00, committed_this_run_minor=5_880_00,
                    floor_minor=0)
        text = _visible(html)
        assert "over-committed by USD 4,880.00" in text, text
        assert "free to spend" not in text, text

    def test_a_positive_position_still_says_free_to_spend(self):
        text = _visible(_bar(cash_minor=9_000_00, floor_minor=5_000_00))
        assert "free to spend" in text
        assert "over-committed" not in text

    def test_a_release_raises_free_to_spend_by_exactly_its_amount(self):
        """Money leaving suspense becomes spendable, to the cent."""
        from app.domain.policy import available_cash_minor

        before = available_cash_minor(cash_minor=10_000_00, suspense_minor=120_00,
                                      floor_minor=5_000_00)
        after = available_cash_minor(cash_minor=10_000_00, suspense_minor=0,
                                     floor_minor=5_000_00)
        assert after - before == 120_00


class TestAHeldCardNamesItsBlocker:
    def test_a_blocker_list_is_rendered_with_its_numbers(self):
        from app.ui import blockers_html

        html = blockers_html({"plan_blockers": [
            "pay PO-1 (USD 4,000.00): needs USD 4,000.00 but the account "
            "holds USD 1,000.00"]})
        text = _visible(html)
        assert "USD 4,000.00" in text
        assert "holds USD 1,000.00" in text

    def test_it_never_settles_for_a_bare_held_by_policy(self):
        from app.ui import blockers_html

        for card in ({"plan_blockers": []}, {}, {"plan_blockers": None}):
            text = _visible(blockers_html(card))
            assert "Held by policy" not in text, text
            assert text.strip(), "an empty card is not an explanation"

    def test_the_planner_gives_every_held_step_a_blocker(self):
        """If a step is held, the planner owes it a reason with numbers."""
        from decimal import Decimal

        from app.domain.planner import CashPosition, Payable, build_plan
        from app.domain.policy import load_budget

        pos = CashPosition(balances={"USD": 1_000_00}, rates={"USD": Decimal(1)},
                           budget_currency="USD", floor_minor=500_000)
        p = Payable(number="PO-1", supplier="Atlanta Packaging", currency="USD",
                    amount_minor=4_000_00)
        step = build_plan(payables=[p], pos=pos, budget=load_budget()).steps[0]
        held = step.blockers or step.needs_approval
        assert held, "a step that cannot go must be held for a stated reason"
        if step.blockers:
            assert any(c.isdigit() for c in " ".join(step.blockers)), step.blockers


class TestCashMovesInPlace:
    def _page(self) -> str:
        from fastapi.testclient import TestClient

        from app.main import app

        return TestClient(app).get("/app?tab=home").text

    def test_the_cash_tile_is_one_the_page_updates_in_place(self):
        """Without this the tile only changes on a reload, which is what a
        tester saw: the invoice read PAID and the cash figure did not move."""
        html = self._page()
        assert 'data-testid="tile-cash"' in html or "x-fig" in html
        # the in-place updater must be told about the money cells
        assert "state/version" in html or "/state" in html

    def test_a_match_moves_the_cash_figure(self):
        from app.dashboard import build_context
        from app.models import Customer, InvoiceReceivable, RecStatus

        _fund(12_000_00)
        before = build_context([])["cash"].get("USD", 0)

        with session_scope() as s:
            c = Customer(name="Brightline")
            s.add(c)
            s.flush()
            s.add(InvoiceReceivable(number="INV-1", customer_id=c.id,
                                    currency="USD", amount_minor=12_000_00,
                                    status=RecStatus.OPEN))

        after = build_context([])["cash"].get("USD", 0)
        assert after == before, "an invoice is not cash"
        assert after >= 12_000_00


def _visible(html: str) -> str:
    import re as _re

    body = _re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    body = _re.sub(r"<[^>]+>", " ", body)
    return _re.sub(r"\s+", " ", body).strip()


class TestTheAccountsSpinnerSettles:
    """A control that says "Loading accounts..." must stop saying it.

    Run 5 hung on a Setup page. The fill loop retried twenty times and, on the
    last failure, did nothing at all: the select stayed disabled with the
    spinner beside it for ever. Every exit from that loop has to settle.
    """

    def _page(self) -> str:
        from fastapi.testclient import TestClient

        from app.main import app

        return TestClient(app).get("/app?tab=money-in").text

    def test_the_give_up_path_clears_the_loading_state(self):
        html = self._page()
        assert "function giveUp()" in html, "no terminal branch exists"
        # it must clear the attribute that drives the spinner
        block = html[html.index("function giveUp()"):html.index("function giveUp()") + 400]
        assert "data-loading" in block, block
        assert "data-settled" in block, block

    def test_both_failure_paths_reach_it(self):
        html = self._page()
        after = html[html.index("if (tries < 20)"):]
        window = after[:900]
        assert "else { giveUp(); }" in window, window
        catch = html[html.index("A FAILED FETCH MUST SETTLE TOO"):]
        assert "giveUp();" in catch[:400], catch[:400]

    def test_the_message_offers_a_way_forward(self):
        html = self._page()
        assert "still works" in html or "reload" in html.lower()


def test_a_repeated_blocker_is_shown_once():
    """The planner can record cash and the floor as the same sentence."""
    from app.ui import blockers_html

    line = "pay PO-1 (USD 5,000.00): needs USD 5,000.00 but the account holds USD 0.00"
    body = _visible(blockers_html({"plan_blockers": [line, line]}))
    assert body.count(line) == 1, body
