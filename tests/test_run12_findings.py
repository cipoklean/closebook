"""TestSprite run 12 findings.

1. A bill added through the form never moves. `/setup/payables` committed the
   row and said "the planner will look at it on the next event", and if nothing
   else ever happened the bill sat at Checked forever. A thing the operator just
   did IS an event.
2. The invoice form's Customer select had no selected option, so the browser
   submitted the first customer in the list. The book, not the person, chose who
   was invoiced.
3. "Short by X for supplier bills and the Y safety cushion" mentions supplier
   bills when nothing is promised, and Home carried the free-to-spend figure
   twice.
"""
from __future__ import annotations

import re
from urllib.parse import unquote_plus

import pytest
from fastapi.testclient import TestClient

from app.db import get_default_db, reset_default_db, session_scope
from app.domain.ledger import post
from app.domain.settings_store import set_paused
from app.domain.workspace import (
    create_payable, create_supplier, record_beneficiary,
)

@pytest.fixture(autouse=True)
def _isolated():
    reset_default_db()
    yield
    reset_default_db()


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------------- #
# a fake transport, so "the transfer exists" is a fact and not a network call
# --------------------------------------------------------------------------- #
class FakeTransfers:
    ALL: dict = {}

    def create(self, db_session, **kw):
        rid = kw.get("request_id")
        if rid in self.ALL:
            return self.ALL[rid]
        obj = {"id": f"tr_{len(self.ALL) + 1}", "status": "SENT"}
        self.ALL[rid] = obj
        return obj

    def find_by_request_id(self, request_id):
        return self.ALL.get(request_id)

    def status_of(self, transfer_id):
        return "PROCESSING"


class _FakeClient:
    def __getattr__(self, name):
        raise AssertionError(f"the fake client was used for {name}")


@pytest.fixture
def wire(monkeypatch):
    """Point the conductor at a fake transport WITHOUT faking the planner.

    The real planner, policy and executor still run: the only invented thing is
    Airwallex. A test that faked the executor too would pass even if nothing was
    ever planned.
    """
    from app.domain import conductor
    from app.domain.executor import Executor

    FakeTransfers.ALL.clear()

    class FakeExecutor(Executor):
        def __init__(self, *a, **kw):
            kw["client"] = _FakeClient()
            kw["transfers"] = FakeTransfers()
            super().__init__(*a, **kw)

    monkeypatch.setattr(conductor, "Executor", FakeExecutor)
    yield FakeTransfers
    FakeTransfers.ALL.clear()


def _cash(minor: int = 900_000) -> None:
    with session_scope() as s:
        post(s, "opening:USD:r12",
             [("cash:USD", minor, 0, "USD"), ("opening_balance", 0, minor, "USD")])


def _supplier_with_beneficiary(name: str = "Atlanta Packaging") -> int:
    with session_scope() as s:
        sup = create_supplier(s, name=name, country="US", currency="USD",
                              payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"seeded": True},
                           beneficiary_id="ben-r12", status="CREATED")
        return sup.id


def _add_bill(client, supplier_id: int, number: str, amount: str):
    return client.post("/setup/payables", data={
        "supplier_id": str(supplier_id), "number": number,
        "amount": amount, "currency": "USD"}, follow_redirects=False)


# --------------------------------------------------------------------------- #
# 1. adding a bill is an event
# --------------------------------------------------------------------------- #
class TestANewBillMoves:
    def test_a_bill_inside_every_rule_is_sent_without_another_event(
            self, client, wire):
        _cash()
        sup = _supplier_with_beneficiary()

        resp = _add_bill(client, sup, "PO-9", "1000.00")

        assert wire.ALL, "no transfer was created by adding the bill"
        # the flash rides in the redirect's query string, so it is URL-encoded
        flash = unquote_plus(str(resp.headers.get("location") or "")) + str(resp.text)
        assert "PO-9" in flash, flash[:300]
        assert "Sent to the bank" in flash, (
            "the bill was saved but the message does not say what happened: "
            + flash[:300])

    def test_a_bill_over_the_limit_is_held_with_the_reason(self, client, wire):
        _cash()
        sup = _supplier_with_beneficiary()

        resp = _add_bill(client, sup, "PO-10", "3000.00")

        assert not wire.ALL, "an over-limit bill was sent without an approval"
        # the flash rides in the redirect's query string, so it is URL-encoded
        flash = unquote_plus(str(resp.headers.get("location") or "")) + str(resp.text)
        assert "Held" in flash, flash[:300]
        assert "2,500.00" in flash, (
            "the hold does not say which limit it broke: " + flash[:300])
        assert "your OK" in flash, flash[:300]

    def test_a_paused_ai_does_not_send_the_new_bill(self, client, wire):
        _cash()
        sup = _supplier_with_beneficiary()
        with session_scope() as s:
            set_paused(s, True)

        resp = _add_bill(client, sup, "PO-11", "1000.00")

        assert not wire.ALL, "the bill was sent while the AI was paused"
        # the flash rides in the redirect's query string, so it is URL-encoded
        flash = unquote_plus(str(resp.headers.get("location") or "")) + str(resp.text)
        assert "paused" in flash.lower(), flash[:300]


# --------------------------------------------------------------------------- #
# 2. a form must not pick the customer for you
# --------------------------------------------------------------------------- #
class TestTheCustomerSelect:
    def _selected(self, html: str, field: str) -> str:
        block = html[html.index(f'id="f-{field}"') - 400:]
        block = block[:block.index("</select>")]
        m = re.findall(r'<option value="([^"]*)"[^>]*selected', block)
        return m[0] if m else ""

    def test_the_newest_customer_is_preselected(self, client):
        from app.domain.workspace import create_customer

        with session_scope() as s:
            first = create_customer(s, name="Older Customer")
            second = create_customer(s, name="Newest Customer")
        html = client.get("/app?tab=setup").text
        selected = self._selected(html, "customer_id")
        assert selected == str(second.id), (
            f"the form preselects {selected!r}, not the newest customer "
            f"({second.id})")
        assert selected != str(first.id)

    def test_the_newest_supplier_is_preselected(self, client):
        with session_scope() as s:
            s.add(__import__("app.models", fromlist=["Supplier"]).Supplier(
                name="Older Supplier", country="US", currency="USD",
                payout_method="LOCAL", beneficiary_status="CREATED"))
            s.commit()
            sup = create_supplier(s, name="Newest Supplier", country="US",
                                  currency="USD", payout_method="LOCAL")
        html = client.get("/app?tab=setup").text
        selected = self._selected(html, "supplier_id")
        assert selected == str(sup.id), (
            f"the bill form preselects {selected!r}, not the newest supplier")


# --------------------------------------------------------------------------- #
# 3. one figure, and the right words
# --------------------------------------------------------------------------- #
class TestFreeToSpendOneFact:
    def test_home_has_exactly_one_free_to_spend_figure(self, client):
        html = client.get("/app?tab=home").text
        assert html.count('data-figure="free_to_spend"') == 1, (
            "Home carries the free-to-spend figure more than once")

    def test_with_nothing_promised_the_shortfall_only_names_the_cushion(
            self, client):
        from app.ui import tile_text
        from app.dashboard import build_context

        html = client.get("/app?tab=home").text
        # nothing owed and nothing in the bank: the only thing to be short of
        # is the cushion itself
        assert "for supplier bills" not in html or "USD 0.00" not in html
        ctx = build_context([])
        text = tile_text(ctx)["free_to_spend"]
        if "Short by" in text:
            assert "safety cushion" in text
            assert "supplier bills" not in text, text
