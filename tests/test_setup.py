"""Setup screens: the operator builds the workspace, Closebook invents nothing.

These tests exist because the app previously carried a SEED — hardcoded
invoices, suppliers, POs and balances that appeared in the running product. The
rule now is that every row has an author, so the interesting cases are the
refusals: a duplicate invoice number, a bill whose supplier cannot be paid, a
bill that depends on an invoice that does not exist.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import get_default_db, reset_default_db  # noqa: E402
from app.models import (  # noqa: E402
    Customer, InvoicePayable, InvoiceReceivable, PayStatus, PayoutMethod, RecStatus,
    Supplier, WorkspaceSettings,
)
from app.domain import workspace as W  # noqa: E402
from app.domain.settings_store import (  # noqa: E402
    SettingsRefused, get_or_create, save, workspace_budget,
)


@pytest.fixture(autouse=True)
def _isolated_db():
    reset_default_db()
    yield
    reset_default_db()


@pytest.fixture
def db():
    return get_default_db()


@pytest.fixture
def session(db):
    s = db.session()
    yield s
    s.close()


# ------------------------------------------------------------------- settings
class TestSettingsLiveInTheDatabase:
    def test_the_row_is_created_from_the_file_defaults(self, session):
        row = get_or_create(session)
        assert row.id == 1
        assert row.reserve_floor_minor == 500_000
        assert row.auto_pay_limit_minor == 250_000

    def test_saving_changes_what_the_planner_reads(self, session):
        save(session, budget_currency="USD", reserve_floor="9000",
             auto_pay_limit="1000", fx_tolerance_pct="0.25")
        b = workspace_budget(session)
        assert b.reserve_floor_usd == 9000
        assert b.auto_pay_limit_usd == 1000
        assert b.fx_tolerance_pct == 0.25

    def test_a_junk_floor_is_refused_rather_than_stored_as_zero(self, session):
        """A zero floor would silently disable the control, which is the worst
        possible way to handle a parse failure."""
        with pytest.raises(SettingsRefused):
            save(session, budget_currency="USD", reserve_floor="lots",
                 auto_pay_limit="2500", fx_tolerance_pct="0.5")
        assert get_or_create(session).reserve_floor_minor == 500_000

    def test_a_bad_currency_is_refused(self, session):
        with pytest.raises(SettingsRefused):
            save(session, budget_currency="DOLLARS", reserve_floor="5000",
                 auto_pay_limit="2500", fx_tolerance_pct="0.5")

    def test_the_budget_currency_can_change(self, session):
        save(session, budget_currency="GBP", reserve_floor="4000",
             auto_pay_limit="900", fx_tolerance_pct="0.5")
        b = workspace_budget(session)
        assert b.currency == "GBP"
        assert b.reserve_floor_usd == 4000


# ------------------------------------------------------------------ customers
class TestCustomers:
    def test_a_customer_needs_a_name(self, session):
        with pytest.raises(W.WorkspaceRefused):
            W.create_customer(session, name="   ")

    def test_a_named_customer_is_created(self, session):
        c = W.create_customer(session, name="Brightline", country="ng",
                              email="ap@brightline.example")
        assert c.id and c.country == "NG"


# ------------------------------------------------------------------- invoices
class TestInvoices:
    def test_a_duplicate_number_is_refused(self, session):
        c = W.create_customer(session, name="Brightline")
        W.create_invoice(session, customer_id=c.id, number="INV-1",
                         currency="USD", amount="12000")
        with pytest.raises(W.WorkspaceRefused) as exc:
            W.create_invoice(session, customer_id=c.id, number="INV-1",
                             currency="USD", amount="500")
        assert "already exists" in str(exc.value)

    def test_amounts_are_stored_in_minor_units(self, session):
        c = W.create_customer(session, name="Brightline")
        row = W.create_invoice(session, customer_id=c.id, number="INV-1",
                               currency="USD", amount="12000.50")
        assert row.amount_minor == 1_200_050

    def test_a_bad_amount_is_refused(self, session):
        c = W.create_customer(session, name="Brightline")
        with pytest.raises(W.WorkspaceRefused):
            W.create_invoice(session, customer_id=c.id, number="INV-1",
                             currency="USD", amount="twelve thousand")

    def test_a_bad_due_date_is_refused(self, session):
        c = W.create_customer(session, name="Brightline")
        with pytest.raises(W.WorkspaceRefused):
            W.create_invoice(session, customer_id=c.id, number="INV-1",
                             currency="USD", amount="100", due_date="next tuesday")

    def test_a_good_due_date_is_kept(self, session):
        c = W.create_customer(session, name="Brightline")
        row = W.create_invoice(session, customer_id=c.id, number="INV-1",
                               currency="USD", amount="100", due_date="2026-11-30")
        assert row.due_date.strftime("%Y-%m-%d") == "2026-11-30"

    def test_an_invoice_needs_a_real_customer(self, session):
        with pytest.raises(W.WorkspaceRefused):
            W.create_invoice(session, customer_id=999, number="INV-1",
                             currency="USD", amount="100")


# ------------------------------------------------------------------ suppliers
class TestSuppliers:
    def test_a_supplier_starts_with_no_beneficiary(self, session):
        s = W.create_supplier(session, name="Atlanta Packaging", country="US",
                              currency="USD", payout_method="LOCAL")
        assert s.airwallex_beneficiary_id is None
        assert s.beneficiary_status is None

    def test_a_failed_beneficiary_is_STORED_not_lost(self, session):
        """The whole point: a refusal has to be visible on the supplier row."""
        s = W.create_supplier(session, name="Bad Co", country="US",
                              currency="USD")
        W.record_beneficiary(session, s.id, request={"beneficiary": {"name": "Bad Co"}},
                             beneficiary_id=None, status="FAILED",
                             error="Airwallex refused: code 001 field [beneficiary].")
        assert s.airwallex_beneficiary_id is None
        assert s.beneficiary_status == "FAILED"
        assert "code 001" in s.beneficiary_error
        assert s.beneficiary_request_json

    def test_a_good_beneficiary_records_its_id(self, session):
        s = W.create_supplier(session, name="Atlanta Packaging", country="US",
                              currency="USD")
        W.record_beneficiary(session, s.id, request={},
                             beneficiary_id="9f187b28-x", status="VERIFIED")
        assert s.airwallex_beneficiary_id == "9f187b28-x"

    def test_a_bad_payout_method_is_refused(self, session):
        with pytest.raises(W.WorkspaceRefused):
            W.create_supplier(session, name="X", country="US", currency="USD",
                              payout_method="CARRIER PIGEON")


# ------------------------------------------------------------------- payables
class TestPayables:
    def _supplier(self, session, *, paid: bool):
        s = W.create_supplier(session, name="Atlanta Packaging", country="US",
                              currency="USD")
        if paid:
            s.airwallex_beneficiary_id = "ben-1"
        session.flush()
        return s

    def test_a_bill_for_a_supplier_with_no_beneficiary_is_REFUSED(self, session):
        """Otherwise the planner would try to pay someone Airwallex does not
        know, and the failure would surface at transfer time instead of here."""
        s = self._supplier(session, paid=False)
        with pytest.raises(W.WorkspaceRefused) as exc:
            W.create_payable(session, supplier_id=s.id, currency="USD",
                             amount="1800", number="PO-1")
        assert "beneficiary" in str(exc.value)
        assert "Suppliers card" in str(exc.value)

    def test_a_bill_for_a_payable_supplier_is_created(self, session):
        s = self._supplier(session, paid=True)
        p = W.create_payable(session, supplier_id=s.id, currency="USD",
                             amount="1800", number="PO-1")
        assert p.amount_minor == 180_000
        assert p.status == PayStatus.HELD

    def test_a_dependency_on_a_missing_invoice_is_refused(self, session):
        s = self._supplier(session, paid=True)
        with pytest.raises(W.WorkspaceRefused) as exc:
            W.create_payable(session, supplier_id=s.id, currency="USD",
                             amount="100", number="PO-1", depends_on="INV-9999")
        assert "INV-9999" in str(exc.value)

    def test_a_dependency_on_a_real_invoice_is_linked(self, session):
        s = self._supplier(session, paid=True)
        c = W.create_customer(session, name="Brightline")
        inv = W.create_invoice(session, customer_id=c.id, number="INV-0041",
                               currency="USD", amount="12000")
        p = W.create_payable(session, supplier_id=s.id, currency="USD",
                             amount="100", number="PO-1", depends_on="INV-0041")
        assert p.depends_on_receivable_id == inv.id

    def test_priority_is_clamped_to_1_5(self, session):
        s = self._supplier(session, paid=True)
        assert W.create_payable(session, supplier_id=s.id, currency="USD",
                                amount="10", number="PO-1",
                                criticality=99).criticality == 5
        assert W.create_payable(session, supplier_id=s.id, currency="USD",
                                amount="10", number="PO-2",
                                criticality=-4).criticality == 1


# ------------------------------------------------------------------- snapshot
class TestSnapshot:
    def test_an_empty_workspace_reports_empty(self, session):
        snap = W.snapshot(session)
        assert snap["counts"] == {"customers": 0, "invoices": 0,
                                  "suppliers": 0, "payables": 0}
        assert snap["invoices"] == []

    def test_it_reads_back_what_was_created(self, session):
        c = W.create_customer(session, name="Brightline")
        W.create_invoice(session, customer_id=c.id, number="INV-0041",
                         currency="USD", amount="12000")
        s = W.create_supplier(session, name="Atlanta", country="US", currency="USD")
        s.airwallex_beneficiary_id = "ben-1"
        session.flush()
        W.create_payable(session, supplier_id=s.id, currency="USD",
                         amount="1800", number="PO-7003")
        snap = W.snapshot(session)
        assert snap["counts"] == {"customers": 1, "invoices": 1, "suppliers": 1,
                                  "payables": 1}
        assert snap["invoices"][0]["amount"] == "USD 12,000.00"
        assert snap["payables"][0]["supplier"] == "Atlanta"
        assert snap["suppliers"][0]["beneficiary_id"] == "ben-1"


# ---------------------------------------------------------------- start fresh
class TestStartFresh:
    """These use a FILE-backed database on purpose.

    The suite's default is `sqlite:///:memory:`, and Start fresh correctly
    refuses to archive an in-memory book — there is no file to copy, and
    pretending otherwise would destroy the data it claims to have saved. That
    refusal is asserted separately below.
    """

    @pytest.fixture
    def filedb(self, tmp_path):
        from app.db import DB
        path = tmp_path / "workspace.sqlite3"
        db = DB(f"sqlite:///{path}")
        yield db
        db.engine.dispose()

    def _seed_some(self, session):
        c = W.create_customer(session, name="Brightline")
        W.create_invoice(session, customer_id=c.id, number="INV-0041",
                         currency="USD", amount="12000")
        s = W.create_supplier(session, name="Atlanta", country="US", currency="USD")
        s.airwallex_beneficiary_id = "ben-1"
        session.flush()
        W.create_payable(session, supplier_id=s.id, currency="USD",
                         amount="1800", number="PO-7003")
        session.commit()

    def test_it_refuses_without_the_exact_words(self, filedb, tmp_path):
        """The confirmation is the WORDS, checked case-insensitively — so
        "start fresh" is accepted and everything else is not."""
        s = filedb.session()
        self._seed_some(s)
        for bad in ("", "yes", "START", "FRESH", "START FRESHING",
                    "confirm", "DELETE", "1"):
            with pytest.raises(W.WorkspaceRefused):
                W.archive_and_reset(filedb, confirm=bad, backup_dir=tmp_path)
        assert s.query(Customer).count() == 1, "nothing may be cleared"
        s.close()

    def test_the_words_are_case_insensitive(self, filedb, tmp_path):
        s = filedb.session()
        self._seed_some(s)
        arch = W.archive_and_reset(filedb, confirm="start fresh",
                                   backup_dir=tmp_path)
        s.close()
        assert arch.cleared["customer"] == 1

    def test_it_archives_a_COPY_before_clearing(self, filedb, tmp_path):
        """The archive is a real file that still holds the rows — an archive
        written after the delete would be an empty file with a nice name."""
        import sqlite3

        s = filedb.session()
        self._seed_some(s)
        arch = W.archive_and_reset(filedb, confirm="START FRESH",
                                   backup_dir=tmp_path)
        s.close()
        assert os.path.exists(arch.path)
        assert arch.cleared["customer"] == 1
        assert arch.cleared["invoice_receivable"] == 1
        assert arch.cleared["invoice_payable"] == 1
        assert arch.cleared["supplier"] == 1
        # and the copy really does contain them
        con = sqlite3.connect(arch.path)
        try:
            assert con.execute("select count(*) from customer").fetchone()[0] == 1
            assert con.execute("select count(*) from invoice_payable").fetchone()[0] == 1
        finally:
            con.close()

    def test_it_empties_the_workspace(self, filedb, tmp_path):
        s = filedb.session()
        self._seed_some(s)
        W.archive_and_reset(filedb, confirm="START FRESH", backup_dir=tmp_path)
        s.expire_all()
        assert s.query(Customer).count() == 0
        assert s.query(InvoiceReceivable).count() == 0
        assert s.query(Supplier).count() == 0
        assert s.query(InvoicePayable).count() == 0
        s.close()

    def test_it_KEEPS_the_policy(self, filedb, tmp_path):
        s = filedb.session()
        save(s, budget_currency="USD", reserve_floor="9000",
             auto_pay_limit="1000", fx_tolerance_pct="0.5")
        self._seed_some(s)
        W.archive_and_reset(filedb, confirm="START FRESH", backup_dir=tmp_path)
        s.expire_all()
        assert s.get(WorkspaceSettings, 1).reserve_floor_minor == 900_000
        s.close()

    def test_an_in_memory_book_is_refused_with_a_plain_reason(self, db, session):
        """There is no file to copy, and pretending otherwise would destroy the
        data it claims to have saved."""
        with pytest.raises(W.WorkspaceRefused) as exc:
            W.archive_and_reset(db, confirm="START FRESH")
        assert "in memory" in str(exc.value)


# --------------------------------------------------------------- plain errors
class TestBeneficiaryFormErrors:
    def test_a_plain_refusal_names_the_field(self):
        """Error 001 names the field you did NOT send, so the plain sentence has
        to come from details.errors[].source, not from str(exc)."""
        from app.domain.beneficiary_form import plain_error

        class FakeApiError(Exception):
            status = 400
            code = "invalid_argument"
            payload = {
                "code": "invalid_argument",
                "message": "a required field was missing or invalid",
                "details": {"errors": [
                    {"source": "account_routing_value1", "code": "001",
                     "message": "is required"},
                ]},
            }

        msg = plain_error(FakeApiError())
        assert "account_routing_value1" in msg
        assert "code 001" in msg
        assert "missing or invalid" in msg

    def test_an_unstructured_error_still_produces_a_sentence(self):
        from app.domain.beneficiary_form import plain_error

        class Bare(Exception):
            status = 500
            code = "server_error"
            payload = {}

        assert "server_error" in plain_error(Bare())

    def test_the_payload_puts_payment_methods_at_the_top_level(self):
        """Verified live: `payment_methods` is a SIBLING of `beneficiary`, not
        nested inside it."""
        from app.domain.beneficiary_form import build_payload

        body = build_payload({
            "name": "Marcus Bell", "entity_type": "PERSONAL",
            "country": "US", "currency": "USD", "payout_method": "LOCAL",
            "account_number": "000123456789", "routing_value": "021000021",
            "bank_name": "First National", "city": "Atlanta", "state": "GA",
            "postcode": "30309",
        })
        assert body["payment_methods"] == ["LOCAL"]
        assert "payment_methods" not in body["beneficiary"]
        bd = body["beneficiary"]["bank_details"]
        assert bd["account_routing_type1"] == "aba"        # lowercase
        assert bd["account_routing_value1"] == "021000021"  # not ..._number
        assert bd["bank_account_category"] == "Checking"

    def test_a_gbp_local_beneficiary_uses_sort_code(self):
        from app.domain.beneficiary_form import build_payload
        body = build_payload({
            "name": "Yorkshire Supplies", "entity_type": "COMPANY",
            "country": "GB", "currency": "GBP", "payout_method": "LOCAL",
            "account_number": "12345678", "routing_value": "040004",
            "bank_name": "Barclays",
        })
        assert body["beneficiary"]["bank_details"]["account_routing_type1"] == "sort_code"

    def test_a_swift_beneficiary_in_an_iban_country_gets_an_iban(self):
        from app.domain.beneficiary_form import build_payload
        body = build_payload({
            "name": "Rotterdam Freight BV", "entity_type": "COMPANY",
            "country": "NL", "currency": "EUR", "payout_method": "SWIFT",
            "account_number": "NL08INGB0000001234", "swift_code": "INGBNL2A",
        })
        bd = body["beneficiary"]["bank_details"]
        assert bd["iban"] == "NL08INGB0000001234"
        assert bd["swift_code"] == "INGBNL2A"

    def test_swift_without_a_swift_code_is_refused_here_not_at_the_bank(self):
        from app.domain.beneficiary_form import build_payload
        with pytest.raises(W.WorkspaceRefused) as exc:
            build_payload({"name": "X", "country": "NL", "currency": "EUR",
                           "payout_method": "SWIFT", "account_number": "NL08INGB0"})
        assert "SWIFT" in str(exc.value)

    def test_a_local_payment_with_a_routing_type_but_no_number_is_refused(self):
        from app.domain.beneficiary_form import build_payload
        with pytest.raises(W.WorkspaceRefused):
            build_payload({"name": "X", "country": "US", "currency": "USD",
                           "payout_method": "LOCAL", "account_number": "123"})

    def test_a_bad_entity_type_is_refused_before_the_api(self):
        """Airwallex returns 011 for anything but PERSONAL/COMPANY; catching it
        here saves a round trip and gives a better sentence."""
        from app.domain.beneficiary_form import build_payload
        with pytest.raises(W.WorkspaceRefused) as exc:
            build_payload({"name": "X", "entity_type": "INDIVIDUAL",
                           "country": "US", "currency": "USD",
                           "payout_method": "LOCAL", "account_number": "1"})
        assert "011" in str(exc.value)

    def test_a_bad_usd_account_category_is_refused(self):
        from app.domain.beneficiary_form import build_payload
        with pytest.raises(W.WorkspaceRefused) as exc:
            build_payload({"name": "X", "country": "US", "currency": "USD",
                           "payout_method": "LOCAL", "account_number": "1",
                           "routing_value": "021000021",
                           "bank_account_category": "checking"})
        assert "016" in str(exc.value)


# ------------------------------------------------------- the bill's real state
class TestBillsShowLiveStateNotTheStoredColumn:
    """A payable row is HELD until a payout SETTLES, so `payable.status` is
    stale by design for anything in flight. A bill whose transfer is PROCESSING
    at the bank must not read "HELD" on the Setup screen.
    """

    def _live(self):
        from app.ui import _live_bill_state
        return _live_bill_state

    def test_what_airwallex_says_wins_over_everything(self):
        f = self._live()
        label, tone, note = f({
            "live_status": "PROCESSING", "display_status": "SUBMITTED",
            "plan_action": "PAY", "step_reason": "sent",
        }, "HELD")
        assert label == "PROCESSING"
        assert tone == "info"

    def test_a_bank_failure_is_red(self):
        f = self._live()
        assert f({"live_status": "FAILED"}, "HELD")[1] == "bad"
        assert f({"live_status": "CANCELLED"}, "HELD")[1] == "bad"

    def test_our_newest_step_is_used_when_the_bank_has_not_answered(self):
        f = self._live()
        label, tone, _ = f({"display_status": "SUBMITTED",
                            "plan_action": "PAY"}, "HELD")
        assert label == "SUBMITTED"

    def test_a_stored_HELD_is_not_echoed_when_the_planner_has_a_verdict(self):
        """This is the actual reported bug: HELD shown for a bill the planner
        wants to pay."""
        f = self._live()
        label, tone, _ = f({"display_status": "HELD", "plan_action": "PAY",
                            "plan_needs_approval": False}, "HELD")
        assert label == "READY"
        assert tone == "good"

    def test_over_the_limit_reads_as_needing_the_human(self):
        f = self._live()
        label, tone, note = f({"display_status": "HELD", "plan_action": "PAY",
                               "plan_needs_approval": True,
                               "plan_approval_reason": "EUR 9,012.85 is over the limit"},
                              "HELD")
        assert label == "NEEDS YOUR OK"
        assert tone == "warn"
        assert "over the limit" in note

    def test_a_deferred_bill_carries_the_planner_reason(self):
        f = self._live()
        label, tone, note = f({"display_status": "HELD", "plan_action": "DEFER",
                               "plan_blockers": ["waiting on INV-0042"]}, "HELD")
        assert label == "HELD"
        assert tone == "warn"
        assert note == "waiting on INV-0042"

    def test_no_plan_information_falls_back_to_the_stored_column(self):
        f = self._live()
        assert f(None, "PAID")[0] == "PAID"
        assert f({}, "PARTIAL")[0] == "PARTIAL"

    def test_the_stylesheet_class_is_the_real_one(self):
        """`class="pill good"` is not in the stylesheet; a warning would have
        rendered as invisible plain text."""
        from app.ui import _pill
        assert 'x-pill x-pill--bad' in _pill("X", tone="bad")
        assert "x-pill x-pill--good" in _pill("PAID")
        assert "x-pill x-pill--warn" in _pill("HELD")
