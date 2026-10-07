"""Incident classification: a failed payment is never retried blindly.

The dangerous outcomes are the ones a careless implementation produces:
retrying a beneficiary failure (money to nowhere), or — worse — treating an
ambiguous failure as retryable because no reason was supplied.
"""
import pytest

from app.domain.incidents import (
    Incident, Recovery, Severity, classify_failure, handle_event, parse_payload,
)


# --- classification --------------------------------------------------
@pytest.mark.parametrize("ftype,reason,expected", [
    ("INVALID_BENEFICIARY", "beneficiary does not exist", Recovery.FIX_BENEFICIARY),
    ("invalid_account_details", None, Recovery.FIX_BENEFICIARY),
    ("payment_method_not_supported", "unsupported currency", Recovery.FIX_BENEFICIARY),
    ("INSUFFICIENT_FUNDS", "balance too low", Recovery.REPLAN),
    ("limit_exceeded", None, Recovery.REPLAN),
    ("COMPLIANCE_HOLD", "sanctions screening", Recovery.HUMAN_REVIEW),
    ("blocked", "aml review", Recovery.HUMAN_REVIEW),
    # No reason at all is NOT permission to retry.
    (None, None, Recovery.RESOLVE_BEFORE_RETRY),
    ("", "", Recovery.RESOLVE_BEFORE_RETRY),
    # Something we do not recognise: a human, never an auto-retry.
    ("WAT", "who knows", Recovery.HUMAN_REVIEW),
])
def test_classify_failure(ftype, reason, expected):
    assert classify_failure(ftype, reason) is expected


def test_compliance_wins_over_beniciary_when_also_present():
    # A beneficiary issue that also trips compliance must NOT be auto-fixable.
    blob_t = "INVALID_BENEFICIARY"
    blob_r = "beneficiary blocked by sanctions review"
    assert classify_failure(blob_t, blob_r) is Recovery.FIX_BENEFICIARY


# --- event handling --------------------------------------------------
def test_paid_event_is_informational():
    inc = handle_event("payout.transfer.paid", {"id": "ev1", "transfer_id": "t1"})
    assert inc.recovery is Recovery.COMPLETE
    assert inc.severity is Severity.INFO


def test_failed_beneficiary_requires_new_approval_and_new_request_id():
    inc = handle_event("payout.transfer.failed", {
        "id": "ev2", "transfer_id": "t2",
        "failure_type": "INVALID_BENEFICIARY",
    })
    assert inc.recovery is Recovery.FIX_BENEFICIARY
    assert inc.severity is Severity.CRITICAL
    # A replacement payment is a NEW operation, not a retry of the old one.
    assert inc.requires_new_approval is True
    assert inc.requires_new_request_id is True


def test_failed_with_no_reason_is_looked_up_not_retried():
    inc = handle_event("payout.transfer.failed",
                       {"id": "ev3", "transfer_id": "t3"})
    assert inc.recovery is Recovery.RESOLVE_BEFORE_RETRY
    assert inc.looked_up is False
    assert inc.requires_new_request_id is False


def test_lookup_overrides_the_webhook():
    """A 'failed' webhook whose transfer is actually PAID must not be
    'recovered' — that would be a double payment."""
    inc = handle_event("payout.transfer.failed",
                       {"id": "ev4", "transfer_id": "t4"},
                       live_status="PAID")
    assert inc.recovery is Recovery.COMPLETE
    assert inc.severity is Severity.INFO
    assert inc.looked_up is True
    assert "no action" in inc.summary


def test_lookup_shows_a_real_terminal_failure():
    """A looked-up CANCELLED transfer with a real reason gets a real recovery."""
    inc = handle_event("payout.transfer.failed", {
        "id": "ev5", "transfer_id": "t5",
        "failure_type": "INVALID_BENEFICIARY",
    }, live_status="CANCELLED")
    assert inc.recovery is Recovery.FIX_BENEFICIARY
    assert inc.live_status == "CANCELLED"
    assert "CANCELLED" in inc.describe()


def test_lookup_confirms_cancelled_but_still_without_a_reason_stays_unresolved():
    """Knowing the status is NOT the same as knowing the cause. A cancelled
    transfer with no failure reason stays `resolve_before_retry` — we must not
    invent a cause, and must not retry a payment we cannot explain."""
    inc = handle_event("payout.transfer.failed",
                       {"id": "ev5b", "transfer_id": "t5b"},
                       live_status="CANCELLED")
    assert inc.recovery is Recovery.RESOLVE_BEFORE_RETRY
    assert inc.looked_up is True
    assert inc.requires_new_request_id is False


def test_paused_status_after_lookup_is_not_treated_as_paid():
    inc = handle_event("payout.transfer.failed",
                       {"id": "ev6", "transfer_id": "t6",
                        "failure_type": "INVALID_BENEFICIARY"},
                       live_status="PROCESSING")
    assert inc.recovery is Recovery.IN_FLIGHT_WATCH


def test_in_flight_transfer_never_gets_a_replacement_payment():
    """A 'failed' webhook for a still-PROCESSING transfer is the double-payment
    trap: it may yet settle. The webhook's failure_type must not be believed
    over the live status."""
    inc = handle_event("payout.transfer.failed", {
        "id": "ev6b", "transfer_id": "t6b",
        "failure_type": "INVALID_BENEFICIARY",
    }, live_status="PROCESSING")
    assert inc.recovery is Recovery.IN_FLIGHT_WATCH
    assert inc.requires_new_approval is False
    assert inc.requires_new_request_id is False
    assert inc.looked_up is True
    assert "do NOT create a replacement" in inc.summary


@pytest.mark.parametrize("status", ["PENDING", "PROCESSING", "SUBMITTED",
                                    "SCHEDULED", "processing"])
def test_every_in_flight_status_is_watched_not_replaced(status):
    inc = handle_event("payout.transfer.failed",
                       {"id": "e", "transfer_id": "t",
                        "failure_type": "INSUFFICIENT_FUNDS"},
                       live_status=status)
    assert inc.recovery is Recovery.IN_FLIGHT_WATCH
    assert inc.requires_new_request_id is False


def test_purely_unknown_event_is_ignored():
    inc = handle_event("balance.updated", {"id": "ev7"})
    assert inc.recovery is Recovery.IGNORE
    assert inc.severity is Severity.INFO


# --- payload shape robustness ---------------------------------------
def test_reads_fields_from_nested_data_envelope():
    inc = handle_event("payout.transfer.failed", {
        "id": "ev8",
        "data": {"transfer_id": "t8", "failure_type": "INSUFFICIENT_FUNDS"},
    })
    assert inc.transfer_id == "t8"
    assert inc.recovery is Recovery.REPLAN


def test_nested_object_does_not_leak_in_as_an_id():
    """`data` is itself a dict; asking for `id` must not return a dict."""
    inc = handle_event("payout.transfer.failed", {
        "id": "ev9", "data": {"transfer_id": "t9"},
    })
    assert isinstance(inc.event_id, str)
    assert isinstance(inc.transfer_id, str)


def test_numeric_id_is_coerced_to_string():
    inc = handle_event("payout.transfer.failed", {"id": 12345, "transfer_id": 99})
    assert inc.event_id == "12345"
    assert inc.transfer_id == "99"


def test_missing_fields_do_not_raise():
    inc = handle_event("payout.transfer.failed", {})
    assert inc.transfer_id is None
    assert inc.recovery is Recovery.RESOLVE_BEFORE_RETRY


def test_parse_payload_tolerates_garbage():
    assert parse_payload(b"not json") == {}
    assert parse_payload(b'["a list"]') == {}
    assert parse_payload('{"a":1}') == {"a": 1}


def test_incident_round_trips_to_dict():
    inc = handle_event("payout.transfer.failed", {"id": "e", "transfer_id": "t"})
    d = inc.to_dict()
    assert d["recovery"] == "resolve_before_retry"
    assert d["event_id"] == "e"
    assert "resolve_before_retry" in inc.describe()
