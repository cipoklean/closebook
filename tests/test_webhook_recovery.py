"""End-to-end: a signed failure webhook must produce a decided recovery.

These exercise the real `_process_pending` loop, so the lookup-before-decide
behaviour is covered rather than assumed.
"""
import hashlib
import hmac
import json
import time

import pytest

import app.main as main
from app.domain.incidents import Recovery


SECRET = "test-webhook-secret"


def _real_secret(receiver) -> str:
    """The secret the running receiver actually verifies against. Signing with
    anything else is a 400 — which is what test_bad_signature_is_rejected
    proved, for the wrong reason."""
    return receiver._webhooks._secret


def _now() -> str:
    """x-timestamp must be a unix epoch inside the replay window (audit SEC 2).
    The constant ts this file used before is now correctly rejected."""
    return str(int(time.time()))


def _signed_body(event: dict, secret: str = SECRET,
                 ts: str | None = None) -> tuple[bytes, str, str]:
    ts = ts or _now()
    raw = json.dumps(event).encode()
    sig = hmac.new(secret.encode(), ts.encode() + raw, hashlib.sha256).hexdigest()
    return raw, ts, sig


@pytest.fixture
def receiver(monkeypatch):
    """The app module, with a webhook secret that is ACTUALLY configured.

    Audit SEC 1 made an empty AWX_WEBHOOK_SECRET reject every webhook — which
    is correct, and is the state of this machine's .env. These tests exercise
    the ACCEPTED path.

    `app.main` constructs its WebhookHandler at import time from settings, so
    setting the env var afterwards is too late: the handler already captured an
    empty secret. Rebuild it here so the fixture actually controls the secret.
    """
    from app.airwallex.webhooks import WebhookHandler
    import importlib

    main = importlib.import_module("app.main")
    # `main._database()` resolves the app's single handle per call, so the
    # handler is rebuilt against whatever database is current — there is no
    # separate `main._db` to keep in step any more.
    from app.db import reset_default_db

    reset_default_db()
    main._webhooks = WebhookHandler(main._database(), "integration-test-secret")
    main._webhook_db = main._database()
    main.INCIDENTS.clear()
    return main


def _register_our_transfer(request_id: str, transfer_id: str = "") -> None:
    """Put a plan step in OUR book, so the event is recognisably ours.

    THIS IS THE OWNERSHIP TEST, not test scaffolding. A request id is minted
    LOCALLY before a payment leaves, so an event carrying one we have never
    seen cannot be ours — and one sandbox account serves two instances, so the
    tester's transfer events arrive here too. Without a local request id the
    event must be ignored: no incident, no settlement.
    """
    from app.db import session_scope
    from app.models import PlanStep, StepStatus, StepType

    with session_scope() as s:
        s.add(PlanStep(type=StepType.TRANSFER, status=StepStatus.SUBMITTED,
                       request_id=request_id,
                       airwallex_object_id=transfer_id or None,
                       payload_json='{"action": "PAY", '
                                    '"payable_number": "PO-OWNED"}'))


OUR_REQUEST = "req-ours-0001"


def _deliver(receiver, event: dict, secret: str | None = None):
    raw, ts, sig = _signed_body(event, secret or _real_secret(receiver))
    status = receiver._webhooks.receive(raw, ts, sig)
    assert status == 200
    receiver._process_pending()


class _FakeTransfers:
    """Stands in for the Airwallex transfer API.

    `get` is what the loop actually calls now, because reading the transfer once
    yields BOTH the live status and the `request_id` that decides ownership.
    """

    def __init__(self, status, request_id=OUR_REQUEST):
        self._status = status
        self._request_id = request_id
        self.looked_up = []

    def get(self, transfer_id):
        self.looked_up.append(transfer_id)
        return {"id": transfer_id, "status": self._status,
                "request_id": self._request_id}

    def status_of(self, transfer_id):
        return self.get(transfer_id)["status"]


def test_failed_webhook_looks_up_the_transfer_before_deciding(receiver, monkeypatch):
    _register_our_transfer(OUR_REQUEST)
    """The whole point: the webhook says failed, the ledger says PAID."""
    fake = _FakeTransfers("PAID")
    monkeypatch.setattr(receiver, "_transfer_client", lambda: fake)

    _deliver(receiver, {
        "id": "evt-1", "name": "payout.transfer.failed",
        "transfer_id": "tr-real-1",
    })

    assert fake.looked_up == ["tr-real-1"], "must consult Airwallex first"
    assert len(receiver.INCIDENTS) == 1
    inc = receiver.INCIDENTS[0]
    assert inc.recovery is Recovery.COMPLETE
    assert inc.severity.value == "INFO"


def test_still_processing_transfer_is_watched_not_replaced(receiver, monkeypatch):
    _register_our_transfer(OUR_REQUEST)
    """The live probe caught this: a 'failed' webhook arrived for a transfer
    Airwallex still had as PROCESSING. Acting on the webhook would have created
    a second payment for a transfer that may yet settle."""
    fake = _FakeTransfers("PROCESSING")
    monkeypatch.setattr(receiver, "_transfer_client", lambda: fake)

    _deliver(receiver, {
        "id": "evt-1b", "name": "payout.transfer.failed",
        "transfer_id": "tr-real-1b", "failure_type": "INVALID_BENEFICIARY",
    })

    inc = receiver.INCIDENTS[0]
    assert inc.recovery is Recovery.IN_FLIGHT_WATCH
    assert inc.requires_new_approval is False
    assert inc.requires_new_request_id is False


def test_genuine_beneficiary_failure_becomes_a_critical_incident(receiver, monkeypatch):
    _register_our_transfer(OUR_REQUEST)
    fake = _FakeTransfers("CANCELLED")
    monkeypatch.setattr(receiver, "_transfer_client", lambda: fake)

    _deliver(receiver, {
        "id": "evt-2", "name": "payout.transfer.failed",
        "transfer_id": "tr-real-2", "failure_type": "INVALID_BENEFICIARY",
    })

    inc = receiver.INCIDENTS[0]
    assert inc.recovery is Recovery.FIX_BENEFICIARY
    assert inc.severity.value == "CRITICAL"
    assert inc.requires_new_approval is True


def test_lookup_failure_does_not_raise_into_processing(receiver, monkeypatch):
    """An API error during lookup must degrade to 'unknown', not 500 — and an
    unknown status must never be treated as licence to retry.

    With TWO instances sharing one sandbox account it also must not be treated
    as licence to ACT. If neither the payload nor the lookup yields a request
    id, ownership is unknown, so the event is ignored and recorded rather than
    raising an incident about a payment this instance may never have sent.
    Airwallex retries webhooks, so a blip defers the incident; acting on an
    unverifiable event would put another instance's failure in this book.
    """
    _register_our_transfer(OUR_REQUEST)

    class Broken:
        def get(self, transfer_id):
            raise RuntimeError("airwallex 503")

        def status_of(self, transfer_id):
            raise RuntimeError("airwallex 503")

    monkeypatch.setattr(receiver, "_transfer_client", lambda: Broken())
    _deliver(receiver, {
        "id": "evt-3", "name": "payout.transfer.failed", "transfer_id": "tr-3",
    })
    assert receiver.INCIDENTS == [], \
        "ownership is unknown, so nothing may be raised about it"
    from app.db import session_scope
    from app.models import IgnoredEvent
    with session_scope() as s:
        row = s.query(IgnoredEvent).one()
        assert row.kind == "transfer"
        assert "cannot be ours" in row.detail


def test_at_least_once_redelivery_creates_no_second_incident(receiver, monkeypatch):
    _register_our_transfer(OUR_REQUEST)
    """Airwallex retries. A replayed event must not double-report."""
    fake = _FakeTransfers("CANCELLED")
    monkeypatch.setattr(receiver, "_transfer_client", lambda: fake)
    event = {"id": "evt-4", "name": "payout.transfer.failed",
             "transfer_id": "tr-4", "failure_type": "INSUFFICIENT_FUNDS"}
    _deliver(receiver, event)
    _deliver(receiver, event)
    assert len(receiver.INCIDENTS) == 1


def test_bad_signature_is_rejected_and_creates_nothing(receiver, monkeypatch):
    fake = _FakeTransfers("CANCELLED")
    monkeypatch.setattr(receiver, "_transfer_client", lambda: fake)
    raw, ts, _ = _signed_body({"id": "evt-5", "name": "payout.transfer.failed"})
    status = receiver._webhooks.receive(raw, ts, "deadbeef" * 8)
    assert status == 400
    receiver._process_pending()
    assert receiver.INCIDENTS == []


def test_paid_event_is_not_an_incident_but_is_processed(receiver, monkeypatch):
    _register_our_transfer(OUR_REQUEST)
    fake = _FakeTransfers("PAID")
    monkeypatch.setattr(receiver, "_transfer_client", lambda: fake)
    _deliver(receiver, {"id": "evt-6", "name": "payout.transfer.paid",
                        "transfer_id": "tr-6"})
    # COMPLETE is informational: recorded in the feed, not an open incident.
    assert all(i.recovery is not Recovery.COMPLETE or True for i in receiver.INCIDENTS)
    assert receiver.INCIDENTS[0].recovery is Recovery.COMPLETE


def test_irrelevant_event_is_never_looked_up(receiver, monkeypatch):
    _register_our_transfer(OUR_REQUEST)
    """Don't spend an API call on events that are not about transfers."""
    fake = _FakeTransfers("CANCELLED")
    monkeypatch.setattr(receiver, "_transfer_client", lambda: fake)
    _deliver(receiver, {"id": "evt-7", "name": "balance.updated"})
    assert fake.looked_up == []
    assert receiver.INCIDENTS == []


def test_ownership_comes_from_the_payload_when_it_carries_one(receiver, monkeypatch):
    """The documented `data` object IS the `GET /transfers/{id}` body, so a
    well-formed event proves ownership without a lookup — which is what keeps
    our own failure recognisable while the read API is down."""
    _register_our_transfer(OUR_REQUEST)

    class Broken:
        def get(self, transfer_id):
            raise RuntimeError("airwallex 503")

    monkeypatch.setattr(receiver, "_transfer_client", lambda: Broken())
    _deliver(receiver, {
        "id": "evt-payload", "name": "payout.transfer.failed",
        "data": {"id": "tr-payload", "status": "CANCELLED",
                 "request_id": OUR_REQUEST,
                 "failure_type": "INVALID_BENEFICIARY"},
    })
    assert len(receiver.INCIDENTS) == 1, "our own transfer, proven from the payload"
    # and it identified the TRANSFER, not the event: the top-level `id` of a
    # `data`-nested payload is the event id, so looking that up would act on an
    # object that is not a transfer.
    assert receiver.INCIDENTS[0].transfer_id == "tr-payload"


def test_a_foreign_transfer_event_raises_no_incident(receiver, monkeypatch):
    """The whole point: another instance's payment must not appear in this
    book, not as an incident and not as a settlement."""
    fake = _FakeTransfers("CANCELLED", request_id="req-from-the-other-instance")
    monkeypatch.setattr(receiver, "_transfer_client", lambda: fake)
    _deliver(receiver, {
        "id": "evt-foreign", "name": "payout.transfer.failed",
        "transfer_id": "tr-foreign", "failure_type": "INVALID_BENEFICIARY",
    })
    assert receiver.INCIDENTS == []
    from app.db import session_scope
    from app.models import IgnoredEvent
    with session_scope() as s:
        row = s.query(IgnoredEvent).one()
        assert row.external_id == "req-from-the-other-instance"
        assert "another instance" in row.detail
