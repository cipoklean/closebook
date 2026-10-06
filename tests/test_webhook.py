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

from app.db import DB
from app.airwallex.webhooks import WebhookHandler


def _sig(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).hexdigest()


def _db(tmp_path):
    return DB(f"sqlite:///{tmp_path}/wh.sqlite3")


def test_verify_rejects_bad_signature(tmp_path):
    db = _db(tmp_path)
    h = WebhookHandler(db, "whsec_test")
    body = json.dumps({"id": "evt_1", "name": "payout.transfer.paid"}).encode()
    ts = "2026-11-01T00:00:00Z"
    assert h.verify(body, ts, "deadbeef") is False
    assert h.verify(body, ts, _sig("wrong_secret", ts, body)) is False


def test_verify_accepts_good_signature(tmp_path):
    db = _db(tmp_path)
    h = WebhookHandler(db, "whsec_test")
    body = json.dumps({"id": "evt_1", "name": "payout.transfer.paid"}).encode()
    ts = "2026-11-01T00:00:00Z"
    assert h.verify(body, ts, _sig("whsec_test", ts, body)) is True


def test_test_event_uses_client_secret_key_header(tmp_path):
    """Sandbox test events carry the secret in `client-secret-key`."""
    db = _db(tmp_path)
    h = WebhookHandler(db, "shipped_but_ignored_for_this_event")
    body = b'{"id":"evt_x","name":"issuing.transaction.failed"}'
    ts = "1730000000"
    assert h.verify(body, ts, _sig("real_test_secret", ts, body),
                    client_secret_key="real_test_secret") is True


def test_dedupe_replay_is_noop(tmp_path):
    db = _db(tmp_path)
    h = WebhookHandler(db, "s")
    body = json.dumps({"id": "evt_dup", "name": "payout.transfer.paid",
                       "created_at": "2026-11-01T00:00:00Z"}).encode()
    ts = "2026-11-01T00:00:00Z"
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
        h.receive(body, ts_iso, _sig("s", ts_iso, body))
    order = [e.event_id for e in h.pending_events()]
    assert order == ["evt_a", "evt_b", "evt_c"], order


def test_mark_processed(tmp_path):
    db = _db(tmp_path)
    h = WebhookHandler(db, "s")
    body = json.dumps({"id": "evt_m", "name": "payout.transfer.paid",
                       "created_at": "2026-11-01T00:00:00Z"}).encode()
    ts = "2026-11-01T00:00:00Z"
    h.receive(body, ts, _sig("s", ts, body))
    assert len(h.pending_events()) == 1
    h.mark_processed("evt_m")
    assert len(h.pending_events()) == 0
