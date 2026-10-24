"""Two workspaces must never see each other, and prod must not offer one.

Run 5 was 63 tests against a single book. One test's commitments held another
test's payment, bill numbers collided, and a reset wiped a neighbour's evidence.
Isolation here is a separate SQLite FILE per `?ws=` suffix, so settings, cash,
invoices, bills, approvals and plan steps are separate by construction rather
than by every query remembering a filter.

The last class is the important one for production: an instance without the flag
must REFUSE a `?ws=` request rather than quietly serving the operator's book to
a caller who asked for a different one.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))


@pytest.fixture()
def workspaces(tmp_path, monkeypatch):
    """Enable per-test books on a temp root, the way the tester instance does."""
    monkeypatch.setenv("CLOSEBOOK_WORKSPACES", "1")
    monkeypatch.setenv("CLOSEBOOK_WORKSPACE_ROOT", str(tmp_path / "ws"))
    yield tmp_path / "ws"


@pytest.fixture()
def off(monkeypatch):
    monkeypatch.delenv("CLOSEBOOK_WORKSPACES", raising=False)


def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _url(ws: str, path: str) -> str:
    """Append the workspace to a path that may already carry a query string.

    `f"{path}?ws="` on `/app?tab=setup` produced `/app?tab=setup?ws=...`, which
    is a tab called "setup?ws=..." and no workspace at all.
    """
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}ws={ws}"


def post(ws: str, path: str, body: dict):
    return client().post(_url(ws, path), json=body,
                         headers={"Accept": "application/json"})


def get(ws: str, path: str):
    return client().get(_url(ws, path))


class TestTwoBooks:
    def test_rows_written_in_one_are_invisible_in_the_other(self, workspaces):
        post("aaa", "/setup/customers", {"name": "Only In A"})
        assert "Only In A" in get("aaa", "/app?tab=setup").text
        assert "Only In A" not in get("bbb", "/app?tab=setup").text

    def test_each_book_has_its_own_settings(self, workspaces):
        # the route takes MAJOR-unit strings under these names
        r = post("aaa", "/setup/settings", {"budget_currency": "USD",
                                            "budget": "40000",
                                            "reserve_floor": "5000",
                                            "auto_pay_limit": "9000"})
        assert r.status_code < 500
        a = get("aaa", "/state").json()
        b = get("bbb", "/state").json()
        assert a != b or True          # the settings are read below, explicitly
        from app.workspaces import book
        from app.domain.settings_store import read

        s = book("aaa").session()
        try:
            ra = read(s)
        finally:
            s.close()
        s = book("bbb").session()
        try:
            rb = read(s)
        finally:
            s.close()
        assert ra.auto_pay_limit_minor == 900_000, ra.auto_pay_limit_minor
        assert rb.auto_pay_limit_minor != 900_000, (
            "one book's policy leaked into another")

    def test_an_approval_in_one_does_not_release_a_payment_in_the_other(self, workspaces):
        """The money case, asserted where the money decision is made.

        Same supplier, same bill number, same amount in two books. Book A holds
        an APPROVED approval. Book B must not honour it: `valid_approval` is the
        one reader of a stored approval, so if it answers None in B the payment
        cannot be cleared by A's decision.
        """
        from app.models import Approval, ApprovalStatus, PlanStep, StepStatus, StepType
        from app.workspaces import book

        def seed(ws, *, approved: bool):
            s = book(ws).session()
            try:
                s.add(PlanStep(type=StepType.TRANSFER, status=StepStatus.APPROVED,
                               step_key="PAY:SHARED-BILL",
                               payload_json='{"payable_number": "SHARED-BILL", '
                                            '"action": "PAY", "amount_minor": 400000, '
                                            '"currency": "USD", '
                                            '"supplier": "Atlanta Packaging"}'))
                s.flush()
                if approved:
                    s.add(Approval(plan_step_id=1, bound_hash="h",
                                   amount_minor=400_000, currency="USD",
                                   counterparty="Atlanta Packaging",
                                   status=ApprovalStatus.APPROVED,
                                   decided_by="tester"))
                s.commit()
            finally:
                s.close()

        seed("aaa", approved=True)
        seed("bbb", approved=False)

        from app.domain.plan_store import valid_approval

        for ws, expected in (("aaa", True), ("bbb", False)):
            s = book(ws).session()
            try:
                got = valid_approval(s, step_key="PAY:SHARED-BILL",
                                     amount_minor=400_000, currency="USD",
                                     counterparty="Atlanta Packaging")
            finally:
                s.close()
            assert (got is not None) is expected, (
                f"book {ws}: valid_approval returned {got!r}, expected approved={expected}")

        # and B holds no approval of its own
        s = book("bbb").session()
        try:
            assert s.query(Approval).count() == 0
        finally:
            s.close()

    def test_a_first_request_creates_it_empty(self, workspaces):
        from app.models import PlanStep
        from app.workspaces import book

        s = book("fresh-one").session()
        try:
            assert s.query(PlanStep).count() == 0
        finally:
            s.close()

    def test_the_suffix_travels_back_in_a_header(self, workspaces):
        r = get("aaa", "/healthz")
        assert r.headers.get("x-closebook-workspace") == "aaa"


class TestProdRefusesIt:
    def test_a_workspace_query_is_refused_without_the_flag(self, off):
        r = client().get("/app?ws=anything")
        assert r.status_code == 404, (
            "an instance without per-test books served a ?ws= request anyway")

    def test_the_default_request_is_untouched(self, off):
        assert client().get("/healthz").status_code == 200

    def test_a_bad_suffix_is_rejected(self, workspaces):
        assert client().get("/app?ws=../etc/passwd").status_code == 400
        assert client().get("/app?ws=" + "x" * 60).status_code == 400

    def test_new_is_refused_without_the_flag(self, off):
        r = client().post("/workspaces/new", headers={"Accept": "application/json"})
        assert r.status_code < 500
        assert "does not offer" in r.text
