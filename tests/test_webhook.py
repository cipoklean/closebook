"""Webhook receiver: signature verify + dedupe + ordering.

These encode the acceptance criteria for §6/Phase 4 ("webhook replay tests
pass") so the verify+dedupe logic is locked in before the live webhook is
wired. Airwallex's documented scheme is honoured here:
  x-signature = hex( HMAC-SHA256( secret,  x-timestamp + raw_body ) )
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from app.db import DB
from app.airwallex.webhooks import WebhookHandler


def _sig(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).hexdigest()


def _db(tmp_path):
    return DB(f"sqlite:///{tmp_path}/wh.sqlite3")


def _now() -> str:
    """x-timestamp is a unix epoch string, and must be inside the replay
    window. The original tests used ISO dates, which the freshness check
    (correctly) rejects."""
    return str(int(time.time()))


def test_verify_rejects_bad_signature(tmp_path):
    db = _db(tmp_path)
    h = WebhookHandler(db, "whsec_test")
    body = json.dumps({"id": "evt_1", "name": "payout.transfer.paid"}).encode()
    ts = _now()
    assert h.verify(body, ts, "deadbeef") is False
    assert h.verify(body, ts, _sig("wrong_secret", ts, body)) is False


def test_verify_accepts_good_signature(tmp_path):
    db = _db(tmp_path)
    h = WebhookHandler(db, "whsec_test")
    body = json.dumps({"id": "evt_1", "name": "payout.transfer.paid"}).encode()
    ts = _now()
    assert h.verify(body, ts, _sig("whsec_test", ts, body)) is True


def test_client_secret_key_cannot_replace_the_shared_secret(tmp_path, monkeypatch):
    """This test USED TO assert the opposite.

    It pinned the bug: the handler was constructed with one secret and the
    test passed a DIFFERENT one in the header, and verification still
    succeeded — because the header value was used as the HMAC key. An attacker
    could sign anything with anything.

    Now the configured secret is the only key. Full coverage of the flag
    semantics lives in tests/test_webhook_security.py.
    """
    monkeypatch.delenv("AWX_ALLOW_TEST_EVENTS", raising=False)
    db = _db(tmp_path)
    h = WebhookHandler(db, "configured_secret")
    body = b'{"id":"evt_x","name":"issuing.transaction.failed"}'
    ts = _now()
    assert h.verify(body, ts, _sig("attacker_chosen", ts, body),
                    client_secret_key="attacker_chosen") is False


def test_dedupe_replay_is_noop(tmp_path):
    db = _db(tmp_path)
    h = WebhookHandler(db, "s")
    body = json.dumps({"id": "evt_dup", "name": "payout.transfer.paid",
                       "created_at": "2026-11-01T00:00:00Z"}).encode()
    ts = _now()
    assert h.receive(body, ts, _sig("s", ts, body)) == 200
    # second delivery of the SAME event id must be accepted but not re-recorded
    assert h.receive(body, ts, _sig("s", ts, body)) == 200
    evts = h.pending_events()
    assert len(evts) == 1, f"expected 1 pending, got {len(evts)}"


def test_bad_signature_returns_400(tmp_path):
    db = _db(tmp_path)
    h = WebhookHandler(db, "s")
    body = b'{"id":"evt_bad"}'
    assert h.receive(body, "123", "notarealhash") == 400


def test_ordering_oldest_first(tmp_path):
    db = _db(tmp_path)
    h = WebhookHandler(db, "s")
    events = [
        ("evt_c", "2026-11-01T00:02:00Z"),
        ("evt_a", "2026-11-01T00:00:00Z"),
        ("evt_b", "2026-11-01T00:01:00Z"),
    ]
    for eid, ts_iso in events:
        body = json.dumps({"id": eid, "name": "payout.transfer.paid",
                           "created_at": ts_iso}).encode()
        # x-timestamp (signature freshness) is epoch; created_at (ordering) is
        # the ISO value. They are different fields.
        h.receive(body, _now(), _sig("s", _now(), body))
    order = [e.event_id for e in h.pending_events()]
    assert order == ["evt_a", "evt_b", "evt_c"], order


def test_mark_processed(tmp_path):
    db = _db(tmp_path)
    h = WebhookHandler(db, "s")
    body = json.dumps({"id": "evt_m", "name": "payout.transfer.paid",
                       "created_at": "2026-11-01T00:00:00Z"}).encode()
    ts = _now()
    h.receive(body, ts, _sig("s", ts, body))
    assert len(h.pending_events()) == 1
    h.mark_processed("evt_m")
    assert len(h.pending_events()) == 0
