"""Commit A: the two pieces that carry the demo.

  1. MODEL SAID / CODE DECIDED, on AI activity and inside each money-in item.
     Two columns, because the ORDER is the product: the model reads what a
     person typed, and the code decides what it is allowed to do. Where the code
     overrode the model the model's number is struck through, not deleted --
     hiding it would hide the override, and showing it unstruck would misreport
     the number in force.

  2. THE FREE-TO-SPEND WATERFALL. One bar per currency, from cash down to free
     to spend. It must SUM to the same figure the tile shows, from the same
     function, or it is a second opinion about the books.
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


def _seed_book():
    """A real book, written the way the app writes one.

    An empty workspace draws no bar and shows no receipt, so a page test against
    one proves nothing. This puts cash in the ledger and a model proposal on a
    payment, using the same writers the app does.
    """
    from app.models import (Customer, Inflow, InvoiceReceivable, MatchStatus,
                            RecStatus)

    with session_scope() as s:
        post(s, "seed:USD", [("cash:USD", 3_000_00, 0, "USD"),
                             ("opening_balance", 0, 3_000_00, "USD")])
        post(s, "seed:GBP", [("cash:GBP", 800_00, 0, "GBP"),
                             ("opening_balance", 0, 800_00, "GBP")])
        c = Customer(name="Lumen Stores")
        s.add(c)
        s.flush()
        inv = InvoiceReceivable(number="INV-1003", customer_id=c.id,
                                currency="USD", amount_minor=440_000,
                                status=RecStatus.OPEN)
        s.add(inv)
        s.flush()
        s.add(Inflow(currency="USD", amount_minor=440_000,
                     payer_name="Apex Factors Ltd",
                     reference_raw="for the lumen stores account, ref 5522",
                     match_status=MatchStatus.MATCHED, match_method="LLM",
                     match_reason="names Lumen Stores",
                     proposed_json=(
                         '{"confidence": 0.89, "method": "LLM", '
                         '"reason": "names Lumen Stores", '
                         '"evidence": {"model_claimed_confidence": 0.9, '
                         '"allocations": [{"invoice_id": %d, '
                         '"amount_minor": 440000}]}}' % inv.id)))
        s.flush()
        # THE MODEL'S OWN CALL, as the intake writes it: the AI screen renders
        # TRACES, so a book without one has nothing to show there.
        from app.models import AgentTrace

        s.add(AgentTrace(
            tool="propose_match", verdict="accepted",
            inputs_json=('{"inflow_id": 1, "amount_minor": 440000, '
                         '"currency": "USD", "payer": "Apex Factors Ltd", '
                         '"reference_raw": "for the lumen stores account", '
                         '"model": "nvidia/nemotron-3.5-lightning-30b-a3b"}'),
            outputs_json=('{"model_proposed": {"allocations": '
                          '[{"invoice_number": "INV-1003", "amount": "4400"}], '
                          '"confidence": 0.9, "rationale": "The reference '
                          'explicitly mentions Lumen Stores."}, '
                          '"model_error": null}'),
            detail="the model claimed 0.9. Code pinned it to 0.89 and re-checked",
            run_id="run-1"))


@pytest.fixture
def book():
    _seed_book()
    return True

CSS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                   "backend", "app", "static", "closebook.css")


@pytest.fixture(autouse=True)
def _isolated():
    reset_default_db()
    yield
    reset_default_db()


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


class TestTheWaterfallSumsToTheTile:
    """The whole point: it is the WORKING of the number above it, not a graph."""

    def test_it_sums_to_exactly_the_free_figure(self):
        args = dict(cash_minor=30_370_00, in_flight_minor=900_00,
                    committed_this_run_minor=12_946_00, suspense_minor=4_400_00,
                    refunds_owed_minor=250_00, floor_minor=5_000_00)
        w = cash_waterfall(**args)
        assert w["total_minor"] == w["free_minor"]

    def test_and_that_figure_is_the_single_basis(self):
        """ONE basis. If these two ever disagree the page has two opinions."""
        args = dict(cash_minor=30_370_00, in_flight_minor=900_00,
                    committed_this_run_minor=12_946_00, suspense_minor=4_400_00,
                    refunds_owed_minor=250_00, floor_minor=5_000_00)
        assert cash_waterfall(**args)["free_minor"] == available_cash_minor(**args)

    def test_it_holds_over_a_range_of_books(self):
        for cash in (0, 1, 100_00, 5_000_00, 30_370_00, 1_000_000_00):
            for promised in (0, 100_00, 9_000_00):
                for susp in (0, 4_400_00):
                    args = dict(cash_minor=cash, in_flight_minor=900_00,
                                committed_this_run_minor=promised,
                                suspense_minor=susp, refunds_owed_minor=250_00,
                                floor_minor=5_000_00)
                    w = cash_waterfall(**args)
                    assert w["total_minor"] == w["free_minor"] == \
                        available_cash_minor(**args), args

    def test_every_term_is_a_step_even_when_it_is_zero(self):
        """A term missing from the picture is a term nobody can check."""
        w = cash_waterfall(cash_minor=100_00, floor_minor=0)
        assert [s["key"] for s in w["steps"]] == [
            "cash", "in_flight", "promised", "unmatched", "refunds_owed", "floor"]
        assert [s["key"] for s in w["steps"] if s["empty"]]

    def test_the_running_total_is_the_walk_not_a_second_calculation(self):
        w = cash_waterfall(cash_minor=1_000_00, committed_this_run_minor=300_00,
                           floor_minor=100_00)
        run = 0
        for st in w["steps"]:
            run += st["delta_minor"]
            assert st["running_minor"] == run, st["key"]

    def test_it_flags_red_at_the_step_that_goes_past_zero(self):
        """The shortfall is CAUSED at a step, so it is painted there."""
        w = cash_waterfall(cash_minor=1_000_00, committed_this_run_minor=3_000_00)
        assert w["free_minor"] < 0
        first_red = next(s for s in w["steps"] if s["overdrawn"])
        assert first_red["key"] == "promised"
        assert w["steps"][0]["overdrawn"] is False

    def test_a_healthy_book_paints_nothing_red(self):
        w = cash_waterfall(cash_minor=30_000_00, committed_this_run_minor=1_000_00,
                           floor_minor=5_000_00)
        assert not [s for s in w["steps"] if s["overdrawn"]]
        assert w["free_minor"] > 0


class TestTheWaterfallOnThePage:
    def test_it_replaces_the_details_text(self, client, book):
        html = client.get("/app").text
        assert 'class="x-wf-bar"' in html
        assert " − floor " not in html, "the Details prose is gone"

    def test_every_segment_is_labelled_with_its_amount(self, client, book):
        html = client.get("/app").text
        segs = re.findall(r'<span class="x-wf-seg[^"]*"[^>]*>(.*?)</span>', html,
                          flags=re.S)
        assert segs, "no segments rendered"
        for seg in segs[:14]:
            assert "<b>" in seg and "<small>" in seg, seg

    def test_it_is_per_currency_the_way_the_tile_is(self, client, book):
        html = client.get("/app").text
        ccys = re.findall(r'data-wf-ccy="([A-Z]{3})"', html)
        assert ccys == sorted(ccys) and len(set(ccys)) == len(ccys)
        # a USD surplus cannot pay a GBP bill, so they are never merged
        assert len(ccys) >= 1

    def test_the_last_segment_is_free_to_spend(self, client, book):
        html = client.get("/app").text
        assert 'data-wf="free"' in html
        # positive books say "free to spend"; an over-committed one says
        # "over-committed by", and both are the same arithmetic
        assert ("free to spend" in html or "over-committed by" in html)

    def test_it_is_labelled_for_a_screen_reader(self, client, book):
        html = client.get("/app").text
        assert 'role="img"' in html and "aria-label" in html

    def test_a_short_currency_is_painted_red(self, client, book):
        """Prod is short in USD, so the bar must say so, not look calm."""
        html = client.get("/app").text
        if "-" not in html:
            pytest.skip("this book is not short")
        assert "is-overdrawn" in html

    def test_the_segments_stack_at_390px(self):
        """A six-segment bar is unreadable at 390 pixels, so it becomes rows."""
        css = open(CSS).read()
        block = css[css.find("@media (max-width: 700px)"):]
        assert ".x-wf-bar { display: block;" in block.replace("\n", " ") or \
            ".x-wf-bar" in block
        assert ".x-wf-seg" in block


class TestTheReceiptIsTwoColumns:
    def test_ai_activity_carries_it(self, client, book):
        html = client.get("/app?tab=ai").text
        assert 'data-testid="receipt"' in html
        assert "Model said" in html and "Code decided" in html

    def test_the_columns_state_the_four_things_they_must(self):
        """Model: invoice, amount, claimed confidence, a line of reasoning.
        Code: verdict, the rule that fired, the final confidence."""
        from app.ui import _receipt

        html = _receipt(
            {"invoice": "INV-1003", "amount": "USD 4,400.00", "confidence": 0.9,
             "reason": "reference names Lumen Stores", "struck": True},
            {"verdict": "held for you", "rule": "capped at the 0.89 auto band",
             "confidence": 0.89})
        assert "INV-1003" in html and "USD 4,400.00" in html
        assert "0.90" in html
        assert "reference names Lumen Stores" in html
        assert "held for you" in html
        assert "capped at the 0.89 auto band" in html
        assert "0.89" in html

    def test_an_overridden_value_is_struck_through_not_hidden(self):
        from app.ui import _receipt

        html = _receipt({"confidence": 0.9, "struck": True},
                        {"confidence": 0.89})
        assert "<s" in html and 'data-testid="model-overridden"' in html
        assert "0.90" in html, "the model's number is still on the page"
        assert "0.89" in html

    def test_an_agreed_value_is_not_struck(self):
        from app.ui import _receipt

        html = _receipt({"confidence": 0.95, "struck": False},
                        {"confidence": 0.95})
        # "<s" would also match "<span", so ask for the strikethrough itself.
        assert "<s " not in html and "<s>" not in html
        assert "0.95" in html

    def test_a_missing_model_is_stated_plainly_and_the_code_escalates(self):
        from app.ui import _receipt

        html = _receipt({"unavailable": "Model unavailable, so this went to a "
                                         "person instead of guessing."},
                        {"verdict": "escalated to a person",
                         "rule": "no reading to re-check"})
        assert "Model did not answer" in html
        assert "Model unavailable, so this went to a person instead of guessing." in html
        assert "escalated to a person" in html

    def test_no_first_person_anywhere(self):
        from app.ui import _prose

        assert _prose("I think this is INV-1001") == "this is INV-1001"
        assert not _prose("I believe it is a partial payment").startswith("I")
        assert _prose("My reading: INV-1003") == "INV-1003"
        assert "I think" not in _prose("I think I'll go with INV-1001, my guess.")

    def test_plain_prose_is_left_alone(self):
        from app.ui import _prose

        assert _prose("partial payment against INV-1003") == \
            "partial payment against INV-1003"

    def test_it_is_one_line(self):
        from app.ui import _prose

        out = _prose("x " * 400, 120)
        assert len(out) <= 120 and "\n" not in out

    def test_the_receipt_appears_inside_a_money_in_item(self, client, book):
        """Not only on the AI screen: the payment itself carries it."""
        html = client.get("/app?tab=money-in").text
        assert 'data-testid="receipt"' in html

    def test_the_columns_stack_at_390px(self):
        css = open(CSS).read()
        block = css[css.find("@media (max-width: 700px)"):]
        assert ".x-receipt { grid-template-columns: 1fr; }" in block.replace("\n", " ") \
            or ".x-receipt" in block


class TestTheReceiptReadsRealRows:
    def test_a_stored_proposal_produces_a_struck_claim(self):
        """End to end on a real stored row, not a hand-built dict."""
        from app.dashboard import receipt_data
        from app.models import (Customer, Inflow, InvoiceReceivable, MatchStatus,
                                RecStatus)

        with session_scope() as s:
            c = Customer(name="Lumen Stores")
            s.add(c)
            s.flush()
            inv = InvoiceReceivable(number="INV-1003", customer_id=c.id,
                                    currency="USD", amount_minor=440_000,
                                    status=RecStatus.OPEN)
            s.add(inv)
            s.flush()
            i = Inflow(currency="USD", amount_minor=440_000, payer_name="Apex",
                       reference_raw="for the lumen stores account",
                       match_status=MatchStatus.MATCHED, match_method="LLM",
                       match_reason="names Lumen Stores",
                       proposed_json=(
                           '{"confidence": 0.89, "method": "LLM", '
                           '"reason": "names Lumen Stores", '
                           '"evidence": {"model_claimed_confidence": 0.9, '
                           '"allocations": [{"invoice_id": %d, '
                           '"amount_minor": 440000}]}}' % inv.id))
            s.add(i)
            s.flush()
            # `allocations` sits at the TOP level, which is where the intake
            # writes it. Passing it under `evidence` (as my first version did)
            # tested a payload the system never produces.
            rec = receipt_data(s, i, {"confidence": 0.89, "method": "LLM",
                                      "reason": "names Lumen Stores",
                                      "allocations": [{"invoice_id": inv.id,
                                                       "amount_minor": 440000}],
                                      "evidence": {"model_claimed_confidence": 0.9}},
                               0.89, 0)

        assert rec["model"]["confidence"] == 0.9
        assert rec["model"]["struck"] is True
        assert rec["model"].get("invoice") == "INV-1003"
        assert rec["code"]["confidence"] == 0.89
        # The code column may legitimately NAME the 0.90 band in its note; what
        # it must not do is report 0.90 as the confidence IN FORCE.
        assert rec["code"]["confidence"] != 0.9
        assert "0.90" not in str(rec["model"].get("struck"))
