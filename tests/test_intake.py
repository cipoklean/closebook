"""Money in: a real sandbox deposit becomes a matched receivable, exactly once.

Before this, the ONLY code that created an `Inflow` was the scripted demo. The
matcher and the ledger were complete and tested; nothing in the running app
could turn a real deposit into one. These tests cover that path.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import get_default_db, reset_default_db, session_scope  # noqa: E402
from app.domain import intake as I  # noqa: E402
from app.domain import workspace as W  # noqa: E402
from app.models import Inflow, InvoiceReceivable, MatchStatus, RecStatus  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db():
    reset_default_db()
    yield
    reset_default_db()


@pytest.fixture
def db():
    return get_default_db()


def _invoice(number: str, ccy: str, minor: int, customer: str = "Brightline"):
    with session_scope() as s:
        c = W.create_customer(s, name=customer)
        return W.create_invoice(s, customer_id=c.id, number=number,
                                currency=ccy, amount=str(minor / 100)).id


def _deposit(dep_id, ccy, minor, payer="", reference="", status="SETTLED"):
    return {"id": dep_id, "currency": ccy, "amount": minor / 100,
            "payer_name": payer, "reference": reference, "status": status}


# ------------------------------------------------------------------- the shape
class TestDepositFields:
    def test_a_major_amount_becomes_minor_units(self):
        f = I._deposit_fields(_deposit("d1", "USD", 1_200_000, reference="x"))
        assert f["amount_minor"] == 1_200_000
        assert f["currency"] == "USD"

    def test_a_string_amount_is_accepted(self):
        f = I._deposit_fields({"id": "d1", "currency": "USD", "amount": "12000.00"})
        assert f["amount_minor"] == 1_200_000

    def test_a_deposit_with_no_id_is_refused(self):
        assert I._deposit_fields({"currency": "USD", "amount": 10})["deposit_id"] == ""

    def test_the_reference_is_kept_uncut(self):
        f = I._deposit_fields(_deposit("d1", "USD", 100, reference="  inv 0O41  "))
        assert f["reference"] == "  inv 0O41  "


# ------------------------------------------------------------------ the intake
class TestIntakeIsExactlyOnce:
    def test_a_deposit_becomes_one_matched_inflow(self, db):
        _invoice("INV-1001", "USD", 1_200_000)
        r = I.ingest_deposit(db, _deposit("dep-1", "USD", 1_200_000,
                                          payer="Brightline Retail",
                                          reference="payment for INV-1001"))
        assert r.status == "MATCHED"
        assert r.method == "EXACT"
        assert r.confidence == 1.0
        assert r.confidence >= 0.9
        assert any("INV-1001" in a for a in r.applied)
        with session_scope() as s:
            assert s.query(Inflow).count() == 1
            inv = s.query(InvoiceReceivable).filter_by(number="INV-1001").one()
            assert inv.status == RecStatus.PAID

    def test_the_SAME_deposit_twice_posts_nothing_the_second_time(self, db):
        """This is what makes a webhook plus a poll safe to run side by side."""
        _invoice("INV-1001", "USD", 1_200_000)
        d = _deposit("dep-1", "USD", 1_200_000, reference="INV-1001")
        first = I.ingest_deposit(db, d)
        second = I.ingest_deposit(db, d)
        assert not first.duplicate
        assert second.duplicate
        with session_scope() as s:
            assert s.query(Inflow).count() == 1, "a second row would double-post"
            inv = s.query(InvoiceReceivable).filter_by(number="INV-1001").one()
            assert inv.amount_paid_minor == 1_200_000, "not 2.4m"

    def test_the_same_deposit_by_a_different_route_is_still_a_duplicate(self, db):
        """The webhook and the poller both call record_and_match, so they cannot
        disagree — a fallback that reimplemented this would be a second,
        untested way to post money."""
        _invoice("INV-1001", "USD", 1_200_000)
        d = _deposit("dep-1", "USD", 1_200_000, reference="INV-1001")
        with session_scope() as s:
            a = I.record_and_match(s, deposit_id="dep-1", currency="USD",
                                   amount_minor=1_200_000, reference="INV-1001")
        with session_scope() as s:
            b = I.record_and_match(s, deposit_id="dep-1", currency="USD",
                                   amount_minor=1_200_000, reference="INV-1001")
        assert not a.duplicate and b.duplicate
        with session_scope() as s:
            assert s.query(Inflow).count() == 1


# ------------------------------------------------------------------- matching
class TestMatchingOnRealReferences:
    def test_a_typoed_invoice_number_is_READ_but_only_PROPOSED(self, db):
        """'1OO1' folds to '1001' — read, but not trusted on its own.

        With no payer to corroborate it, folding is the entire evidence, so the
        match is PROPOSED: the invoice does not move and the cash waits in
        suspense for a human.
        """
        _invoice("INV-1001", "USD", 1_200_000)
        r = I.ingest_deposit(db, _deposit("a", "USD", 1_200_000,
                                          reference="invoce 1OO1 brightline"))
        assert r.disposition == "PROPOSED"
        assert r.confidence < 0.90
        with session_scope() as s:
            inv = s.query(InvoiceReceivable).filter_by(number="INV-1001").one()
            assert inv.amount_paid_minor == 0, "a folded number moves nothing"
            inflow = s.query(Inflow).one()
            assert inflow.review_state == "PENDING"
            assert inflow.proposed_json, "the proposal is stored for approval"

    def test_a_material_part_payment_is_PARTIAL_and_never_auto_applies(self, db):
        """A payer we recognise plus a big shortfall = a deliberate part
        payment: apply it, chase the rest, but CAP the confidence below the
        auto-apply band so a human sees it."""
        # The payer must match the CUSTOMER name for the identity gate to score
        # it — "Harrow & Co" against a customer called Brightline is 0.70, not
        # 0.85, and 0.70 escalates instead of applying.
        _invoice("INV-1002", "GBP", 800_000, customer="Harrow & Co")
        r = I.ingest_deposit(db, _deposit("b", "GBP", 480_000,
                                          payer="Harrow & Co",
                                          reference="part payment invoice 1002"))
        assert r.status == "PARTIAL", "what the matcher read"
        assert r.disposition == "PROPOSED", "and what actually happened"
        assert 0.75 <= r.confidence < 0.90
        with session_scope() as s:
            inv = s.query(InvoiceReceivable).filter_by(number="INV-1002").one()
            assert inv.status == RecStatus.OPEN, "below the band moves NOTHING"
            assert inv.amount_paid_minor == 0
            inflow = s.query(Inflow).one()
            assert inflow.review_state == "PENDING"

    def test_an_exact_amount_against_one_open_invoice_matches_on_amount_alone(self, db):
        """The documented rule, not a guess: an amount-only match is allowed when
        the payer is unrecognised AND the amount lines up exactly against a
        single open invoice. It is capped below the auto band."""
        _invoice("INV-1003", "USD", 500_000)
        r = I.ingest_deposit(db, _deposit("c", "USD", 500_000, reference=""))
        assert r.status in ("MATCHED", "PARTIAL")
        assert r.confidence < 0.90, "amount alone never reaches the auto band"

    def test_an_odd_small_shortfall_is_PROPOSED_rather_than_guessed(self, db):
        """A 2% gap is outside the bank-fee tolerance and below the 10% that
        makes a shortfall deliberate — an odd fee, a wrong invoice and a typo all
        look identical here, so it goes to a human.

        The allocation IS recorded against the invoice, deliberately: the money
        has to be visible and chaseable, and a part-payment releases no
        dependent payable (only a FULLY PAID receivable does). What makes it
        safe is that the inflow is recorded as awaiting approval rather than as
        a settled match, and the confidence is below the auto band.
        """
        _invoice("INV-1004", "USD", 500_000)
        r = I.ingest_deposit(db, _deposit("d", "USD", 490_000,
                                          reference="no idea what this is"))
        assert r.disposition == "PROPOSED"
        assert r.confidence < 0.90, "an ambiguous read must never reach the band"
        with session_scope() as s:
            inflow = s.query(Inflow).one()
            assert inflow.review_state == "PENDING"
            inv = s.query(InvoiceReceivable).filter_by(number="INV-1004").one()
            assert inv.amount_paid_minor == 0, "a guess moves nothing"
            assert inv.status == RecStatus.OPEN

    def test_a_large_shortfall_is_a_part_payment_but_still_needs_a_human(self, db):
        """84% outstanding is not a fee, so it reads as a part payment — and at
        0.70 it is below the band, so it is proposed rather than applied."""
        _invoice("INV-1005", "USD", 500_000)
        r = I.ingest_deposit(db, _deposit("e", "USD", 77_777,
                                          reference="no idea what this is"))
        assert r.disposition == "PROPOSED"
        assert r.confidence < 0.90
        with session_scope() as s:
            inv = s.query(InvoiceReceivable).filter_by(number="INV-1005").one()
            assert inv.amount_paid_minor == 0, "proposed, not applied"
            assert s.query(Inflow).one().review_state == "PENDING"

    def test_the_cash_lands_in_the_right_currency(self, db):
        _invoice("INV-1002", "GBP", 800_000)
        I.ingest_deposit(db, _deposit("e", "GBP", 480_000, reference="INV-1002"))
        with session_scope() as s:
            inflow = s.query(Inflow).one()
            assert inflow.currency == "GBP"
            assert inflow.amount_minor == 480_000


# -------------------------------------------------------------- what not to post
class TestRejectedDeposits:
    def test_a_rejected_deposit_posts_no_cash(self, db):
        _invoice("INV-1001", "USD", 1_200_000)
        r = I.ingest_deposit(db, _deposit("x", "USD", 1_200_000,
                                          reference="INV-1001",
                                          status="REJECTED"))
        assert r is None
        with session_scope() as s:
            assert s.query(Inflow).count() == 0

    def test_a_pending_deposit_is_accepted(self, db):
        """The simulation endpoint credits the balance immediately and reports
        PENDING, so refusing PENDING would hide the deposit the operator just
        created."""
        _invoice("INV-1001", "USD", 1_200_000)
        r = I.ingest_deposit(db, _deposit("y", "USD", 1_200_000,
                                          reference="INV-1001", status="PENDING"))
        assert r is not None and r.status == "MATCHED"


# -------------------------------------------------------------- the accounts
class TestAccountCurrencyIsReadFromWhereItLives:
    """LIVE FINDING 2026-10-22: GET /global_accounts returns NO top-level
    `currency`, even though the documented example shows one. Reading the
    documented field left every account unlabelled."""

    def test_currency_comes_from_required_features(self):
        assert I._account_currency({
            "id": "a", "status": "ACTIVE",
            "required_features": [{"currency": "USD", "transfer_method": "LOCAL"}],
        }) == "USD"

    def test_it_falls_back_to_supported_features(self):
        assert I._account_currency({
            "id": "a", "supported_features": [{"currency": "GBP"}],
        }) == "GBP"

    def test_a_top_level_currency_still_wins_if_present(self):
        assert I._account_currency({"currency": "eur",
                                    "required_features": [{"currency": "USD"}]}) == "EUR"

    def test_an_account_with_no_currency_anywhere_is_blank_not_guessed(self):
        assert I._account_currency({"id": "a", "country_code": "US"}) == ""


# ---------------------------------------------------------------- the guard
class TestSimulationIsSandboxOnly:
    def test_simulate_deposit_refuses_a_non_sandbox_host(self, monkeypatch):
        """The guard lives in the domain, not only on the route: a money-shaped
        simulation must not depend on one caller remembering."""
        from app.config import get_settings
        s = get_settings()
        monkeypatch.setattr(s, "awx_base_url", "https://api.airwallex.com",
                            raising=False)
        with pytest.raises(RuntimeError) as exc:
            I.simulate_deposit(None, global_account_id="a", amount_minor=1,
                               currency="USD")
        assert "sandbox" in str(exc.value)

    def test_the_route_is_a_POST_that_cannot_pay_anyone(self):
        """No transfer or conversion call exists anywhere on the money-in path."""
        import inspect

        from app import main

        src = inspect.getsource(main.money_in_simulate)
        for forbidden in ("create_transfer", "create-transfer",
                          "conversions/create", "transfers/create"):
            assert forbidden not in src


# ------------------------------------------------- a workspace starts from a date
class TestHistoryIsNotThisWorkspacesMoney:
    """A fresh workspace must not inherit the sandbox account's whole history.

    Without a floor, the first poll pulled 12 deposits from previous builds into
    a brand-new book — USD 225,900 of suspense that had nothing to do with the
    invoices on screen — and applied GBP 6,000 of it to the only open GBP
    invoice, reading OVERPAID.
    """

    def test_a_create_time_is_parsed_including_a_colonless_offset(self):
        got = I._created_at({"create_time": "2017-03-22T16:08:02+1100"})
        assert got is not None and got.utcoffset() is not None
        assert I._created_at({"create_time": "2026-10-22T08:00:00Z"}) is not None

    def test_an_unparseable_or_missing_time_is_None_not_now(self):
        """None means "cannot judge", and the caller ingests rather than
        silently dropping real money."""
        assert I._created_at({}) is None
        assert I._created_at({"create_time": "not a date"}) is None

    def test_the_poller_skips_deposits_older_than_the_workspace(
            self, db, monkeypatch):
        from datetime import datetime, timezone

        from app.domain import settings_store as S

        # The workspace began NOW.
        with session_scope() as s:
            row = S.get_or_create(s)
            row.deposits_from = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)

        old = {"id": "old", "currency": "USD", "amount": 40000,
               "reference": "test", "status": "SETTLED",
               "create_time": "2026-10-07T00:00:00Z"}
        new = {"id": "new", "currency": "USD", "amount": 12000,
               "reference": "INV-1001", "status": "SETTLED",
               "create_time": "2026-10-22T13:00:00Z"}

        class FakeSim:
            def __init__(self, *a, **k): pass
            def list_deposits(self): return [old, new]

        monkeypatch.setattr("app.airwallex.simulation.Simulation", FakeSim)
        _invoice("INV-1001", "USD", 1_200_000)

        results = I.poll_deposits(db)
        assert [r.deposit_id for r in results] == ["new"], \
            "the historical deposit must not be pulled into a new workspace"
        with session_scope() as s:
            assert s.query(Inflow).count() == 1

    def test_a_deposit_with_no_create_time_is_ingested_not_dropped(
            self, db, monkeypatch):
        """Cannot judge the age, so it is NOT silently discarded."""
        class FakeSim:
            def __init__(self, *a, **k): pass
            def list_deposits(self):
                return [{"id": "nn", "currency": "USD", "amount": 100,
                         "reference": "", "status": "SETTLED"}]

        monkeypatch.setattr("app.airwallex.simulation.Simulation", FakeSim)
        results = I.poll_deposits(db)
        assert [r.deposit_id for r in results] == ["nn"]

    def test_start_fresh_resets_the_floor(self, tmp_path):
        from datetime import datetime, timezone

        from app.db import DB
        from app.domain import settings_store as S

        dbx = DB(f"sqlite:///{tmp_path / 'w.sqlite3'}")
        with dbx.session_scope() as s:
            row = S.get_or_create(s)
            row.deposits_from = datetime(2020, 1, 1, tzinfo=timezone.utc)
        before = datetime.now(timezone.utc)
        W.archive_and_reset(dbx, confirm="START FRESH", backup_dir=tmp_path)
        with dbx.session_scope() as s:
            after = s.get(type(S.get_or_create(s)), 1).deposits_from
        # SQLite hands back NAIVE datetimes, so an aware comparison raises
        # "can't compare offset-naive and offset-aware datetimes" — the same
        # trap the settlement work hit. Normalise before comparing.
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        assert after >= before, "a fresh workspace starts from now"
        dbx.engine.dispose()


# ------------------------------------------------- what the three tabs read
class TestTheVerdictIsReadableFromTheInflow:
    """The tabs must read ONE recorded decision, not re-derive three.

    Method, confidence and reason are stored on the inflow, so Money in, Needs
    your OK and AI activity cannot disagree with each other or with the code
    that decided.
    """

    def _arrivals(self, db):
        from app.dashboard import arrivals_block
        with session_scope() as s:
            return arrivals_block(s)

    def test_a_settled_match_records_its_tier_and_confidence(self, db):
        _invoice("INV-1001", "USD", 1_200_000)
        I.ingest_deposit(db, _deposit("m1", "USD", 1_200_000,
                                      payer="Brightline Retail",
                                      reference="INV-1001"))
        a = self._arrivals(db)[0]
        assert a["method"] == "EXACT"
        assert a["confidence"] == 1.0
        assert a["confidence"] >= 0.90
        assert a["settled_match"] is True
        assert a["awaiting_human"] is False
        assert a["applied"] and a["applied"][0]["invoice"] == "INV-1001"

    def test_the_raw_reference_is_kept_for_display(self, db):
        _invoice("INV-1001", "USD", 1_200_000)
        I.ingest_deposit(db, _deposit("m2", "USD", 1_200_000,
                                      reference="  inv 0O41   brightline  "))
        a = self._arrivals(db)[0]
        assert a["reference"] == "  inv 0O41   brightline  ", \
            "spaces and typos are the INPUT, not something to tidy away"
        assert a["has_reference"] is True

    def test_a_blank_reference_is_marked_as_blank(self, db):
        _invoice("INV-1001", "USD", 1_200_000)
        I.ingest_deposit(db, _deposit("m3", "USD", 10, reference=""))
        a = self._arrivals(db)[0]
        assert a["has_reference"] is False

    def test_a_part_payment_waits_on_a_HUMAN_as_well_as_being_applied(self, db):
        """'Needs your OK' is not the same question as 'unmatched'. A 0.85 part
        payment has moved the cash and the invoice AND still needs a person."""
        _invoice("INV-1002", "GBP", 800_000, customer="Harrow & Co")
        I.ingest_deposit(db, _deposit("m4", "GBP", 480_000, payer="Harrow & Co",
                                      reference="part payment invoice 1002"))
        a = self._arrivals(db)[0]
        assert a["match_status"] == "PROPOSED"
        assert a["method"] == "RULE"
        assert a["confidence"] < 0.90
        assert "part-payment" in a["reason"]
        assert a["awaiting_human"] is True, "it must surface under Needs your OK"
        assert a["applied"] == [], "nothing is on the invoice yet"
        assert a["review_state"] == "PENDING"

    def test_a_settled_match_does_not_wait_on_anyone(self, db):
        from app.dashboard import arrivals_block
        _invoice("INV-1001", "USD", 1_200_000)
        I.ingest_deposit(db, _deposit("m5", "USD", 1_200_000,
                                      reference="INV-1001"))
        with session_scope() as s:
            waiting = [a for a in arrivals_block(s) if a["awaiting_human"]]
        assert waiting == []

    def test_the_reason_sentence_is_stored_not_invented(self, db):
        _invoice("INV-1004", "USD", 500_000)
        I.ingest_deposit(db, _deposit("m6", "USD", 490_000, reference="junk"))
        a = self._arrivals(db)[0]
        assert a["reason"], "the sentence the code produced is kept"
        assert "INV-1004" in a["reason"]
