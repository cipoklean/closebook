"""Two instances, ONE Airwallex sandbox account.

Airwallex has no idea the operator's Closebook and an automated tester's
Closebook are different things: `GET /deposits` returns the whole account's
deposits to whoever asks, and a transfer webhook goes to whichever receiver
registered the URL. Without a rule, a tester's simulated deposit lands in the
operator's suspense and the tester's transfer events raise incidents in the
operator's book. Neither is a test any more.

These pin the rule, from the side that must REFUSE.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import reset_default_db, session_scope  # noqa: E402
from app.domain import intake as I  # noqa: E402
from app.domain import ownership as O  # noqa: E402
from app.domain import workspace as W  # noqa: E402
from app.models import IgnoredEvent, Inflow, InvoiceReceivable  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated():
    reset_default_db()
    yield
    reset_default_db()


def _deposit(dep_id="d-1", amount=4400, currency="USD", payer="Apex Factors Ltd",
             reference="for the lumen stores account", statement_ref=None):
    d = {"id": dep_id, "currency": currency, "amount": amount,
         "payer_name": payer, "reference": reference, "status": "SETTLED"}
    if statement_ref is not None:
        d["statement_ref"] = statement_ref
    return d


def _invoice():
    with session_scope() as s:
        c = W.create_customer(s, name="Lumen Stores")
        W.create_invoice(s, customer_id=c.id, number="INV-1003", currency="USD",
                         amount="4400.00")


# ---- the tag --------------------------------------------------------------
class TestTheTagTravelsOnTheDeposit:
    def test_simulate_stamps_the_reference_and_the_statement_ref(self, monkeypatch):
        """The only moment we know a deposit is ours is the moment we create it,
        and the mark has to go onto the OBJECT — the other instance sees this
        deposit in ITS `GET /deposits` and has no local row to check."""
        seen = {}

        class FakeSim:
            def __init__(self, client): pass

            def deposit(self, **kw):
                seen.update(kw)
                return {"id": "dep-x", "currency": "USD", "amount": 10,
                        "status": "SETTLED"}

        monkeypatch.setattr("app.airwallex.simulation.Simulation", FakeSim)
        I.simulate_deposit(object(), global_account_id="ga-1", amount_minor=1000,
                           currency="USD", reference="for the lumen stores account")

        assert O.TAG_PREFIX + O.instance_name() in seen["reference"]
        assert O.TAG_PREFIX + O.instance_name() in seen["statement_ref"], \
            "the tag rides on both free-text fields, since which one round-trips " \
            "is Airwallex's business"

    def test_the_tag_is_stripped_from_what_we_store(self):
        f = I._deposit_fields(_deposit(reference=O.tag_reference("hello there")))
        assert f["reference"] == "hello there", "the operator never sees the tag"
        assert f["claimed_by"] == O.instance_name()

    def test_a_messy_reference_survives_the_tag_untouched(self):
        tagged = O.tag_reference("  inv 0O41  ")
        f = I._deposit_fields(_deposit(reference=tagged))
        assert f["reference"].startswith("  inv 0O41"), f["reference"]

    def test_the_tag_is_found_on_statement_ref_when_reference_is_clean(self):
        f = I._deposit_fields(_deposit(
            reference="clean", statement_ref=O.tag_reference("x")))
        assert f["claimed_by"] == O.instance_name(), \
            "a tag that only travels on one field stops protecting silently"


# ---- the rule -------------------------------------------------------------
class TestWhoOwnsADeposit:
    def test_ours_is_ours(self):
        ok, why = O.owns_deposit(O.instance_name())
        assert ok and O.instance_name() in why

    def test_another_instances_is_refused(self):
        ok, why = O.owns_deposit("testsprite")
        assert not ok and "testsprite" in why

    def test_an_untagged_deposit_is_a_real_arrival(self, monkeypatch):
        monkeypatch.setattr(O, "ingest_unclaimed", lambda: True)
        ok, why = O.owns_deposit(None)
        assert ok and "real arrival" in why

    def test_an_untagged_deposit_is_refused_when_we_take_no_unclaimed(
            self, monkeypatch):
        monkeypatch.setattr(O, "ingest_unclaimed", lambda: False)
        ok, why = O.owns_deposit(None)
        assert not ok and "unclaimed" in why


# ---- the write path -------------------------------------------------------
class TestTheInflowSideOfADeposit:
    def test_a_foreign_deposit_never_reaches_the_books(self):
        _invoice()
        r = I.ingest_deposit(_db(), _deposit(
            reference=O.tag_reference("for the lumen stores account",
                                      me="testsprite")))
        assert r is None
        with session_scope() as s:
            assert s.query(Inflow).count() == 0, "not in suspense, not anywhere"
            assert s.query(InvoiceReceivable).one().amount_paid_minor == 0
            row = s.query(IgnoredEvent).one()
            assert row.kind == "deposit" and "testsprite" in row.detail

    def test_our_own_deposit_is_recorded_normally(self):
        _invoice()
        r = I.ingest_deposit(_db(), _deposit(reference=O.tag_reference(
            "for the lumen stores account")))
        assert r is not None
        with session_scope() as s:
            assert s.query(Inflow).count() == 1
            assert s.query(IgnoredEvent).count() == 0

    def test_an_untagged_deposit_is_refused_when_this_instance_takes_none(
            self, monkeypatch):
        """The tester must not sweep up the operator's arrivals either."""
        monkeypatch.setattr(O, "ingest_unclaimed", lambda: False)
        _invoice()
        assert I.ingest_deposit(_db(), _deposit()) is None
        with session_scope() as s:
            assert s.query(Inflow).count() == 0

    def test_the_ignore_is_recorded_once_not_twice(self):
        """The deposit webhook inspects two payload shapes, so one foreign
        deposit can be examined twice in a single event."""
        _invoice()
        dep = _deposit(reference=O.tag_reference("x", me="testsprite"))
        I.ingest_deposit(_db(), dep)
        I.ingest_deposit(_db(), dep)
        with session_scope() as s:
            assert s.query(IgnoredEvent).count() == 1

    def test_the_poller_applies_the_same_rule(self, monkeypatch):
        """Both the webhook and the poller reach the books through
        `ingest_deposit`, so one gate covers both — this proves the poller
        actually goes through it."""
        _invoice()
        mixed = [_deposit("dep-ours", reference=O.tag_reference("mine")),
                 _deposit("dep-foreign", reference=O.tag_reference("theirs",
                                                                   me="testsprite"))]

        class FakeSim:
            def __init__(self, client): pass

            def list_deposits(self): return mixed

        monkeypatch.setattr("app.airwallex.simulation.Simulation", FakeSim)
        got = I.poll_deposits(_db())
        assert len(got) == 1, "only our own deposit may be taken up"
        with session_scope() as s:
            assert s.query(Inflow).count() == 1
            assert s.query(IgnoredEvent).count() == 1


# ---- transfers ------------------------------------------------------------
class TestWhoOwnsATransfer:
    def test_a_request_id_we_never_issued_is_not_ours(self):
        with session_scope() as s:
            step, why = O.owns_transfer(s, "req-from-another-instance")
        assert step is None and "another instance" in why

    def test_our_own_request_id_is_ours(self):
        from app.models import PlanStep, StepStatus, StepType

        with session_scope() as s:
            s.add(PlanStep(type=StepType.TRANSFER, status=StepStatus.SUBMITTED,
                           request_id="req-mine",
                           payload_json='{"action": "PAY"}'))
        with session_scope() as s:
            step, why = O.owns_transfer(s, "req-mine")
        assert step is not None and "in" in why

    def test_a_retired_attempt_still_counts_as_ours(self):
        """`find_step` skips retired rows so a dead attempt cannot block a new
        one. But an event about a retired attempt is still about a payment WE
        made — and that failed payment is exactly what an incident is for."""
        from app.models import PlanStep, StepStatus, StepType

        with session_scope() as s:
            s.add(PlanStep(type=StepType.TRANSFER, status=StepStatus.CANCELLED,
                           request_id="req-dead",
                           payload_json='{"action": "PAY"}'))
        with session_scope() as s:
            step, _ = O.owns_transfer(s, "req-dead")
        assert step is not None

    def test_no_request_id_at_all_cannot_be_ours(self):
        with session_scope() as s:
            step, why = O.owns_transfer(s, "")
        assert step is None and "no request_id" in why


def _db():
    from app.db import get_default_db as g
    return g()


# ---- the producer must never emit a tag the consumer cannot read -----------
class TestTheTagGrammarIsClosed:
    def test_a_hostile_instance_name_still_produces_a_readable_tag(self, monkeypatch):
        """The name is derived from a database URL and written into a tag that
        another PROCESS parses, so the two must agree. `sqlite:///:memory:`
        derived the name `:memory:` and produced `[cbinst::memory:]` — a tag
        the reader did not match, so it leaked into the reference on screen."""
        for hostile in (":memory:", "", "has spaces", "has:colons", "un/icode✓",
                        "x" * 40):
            monkeypatch.setattr(O, "instance_name", lambda h=hostile: h)
            # the PRODUCER sanitises, so ask the real derivation what it says
            monkeypatch.delenv("CLOSEBOOK_DATABASE_URL", raising=False)
            monkeypatch.setattr(O, "instance_name", lambda: O.PRIMARY_INSTANCE)
            tag = O.tag_reference("ref")
            got, clean = O.read_tag(tag)
            assert got == O.PRIMARY_INSTANCE, f"{tag!r} was not readable"
            assert clean == "ref", f"{tag!r} left residue {clean!r}"

    def test_an_unusable_name_falls_back_instead_of_emitting_junk(self, monkeypatch):
        monkeypatch.setenv("CLOSEBOOK_DATABASE_URL", "sqlite:///:memory:")
        assert O.instance_name() == O.PRIMARY_INSTANCE

    def test_a_usable_name_is_derived_from_the_filename(self, monkeypatch):
        monkeypatch.setenv("CLOSEBOOK_DATABASE_URL",
                           "sqlite:////home/ubuntu/closebook/data/testsprite.sqlite3")
        assert O.instance_name() == "testsprite"
