"""Webhook authentication: forged signatures, replay, and missing ids.

These are the tests that matter most in the whole suite. Every one corresponds
to a way an attacker could otherwise inject a fake payment failure and make
Closebook create a replacement payout.

Fixed behaviours (audit items SEC 1-3):
  * the shared secret is the only HMAC key; the `client-secret-key` header
    cannot stand in for it, and only with an explicit opt-in flag
  * `x-timestamp` outside +/- 5 minutes is rejected (replay protection)
  * a payload with no event id is rejected, so dedupe cannot be bypassed
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from app.airwallex.webhooks import WebhookHandler
from app.db import DB

SECRET = "shared-secret-under-test"


def _db(tmp_path):
    return DB(f"sqlite:///{tmp_path}/whsec.sqlite3")


def _sig(secret: str, ts: str, body: bytes) -> str:
    return hmac.new(secret.encode(), ts.encode() + body, hashlib.sha256).hexdigest()


def _now() -> str:
    return str(int(time.time()))


def _body(**over) -> bytes:
    payload = {"id": "evt-1", "name": "payout.transfer.failed",
               "transfer_id": "tr-1"}
    payload.update(over)
    return json.dumps(payload).encode()


# --- SEC 1: the client-secret-key bypass ------------------------------
def test_attacker_chosen_client_secret_key_cannot_sign_a_forgery(tmp_path, monkeypatch):
    """THE BUG: the header value used to BE the HMAC key, so anyone could
    supply both the key and a matching signature. Now the shared secret is the
    only key and the header cannot replace it."""
    monkeypatch.delenv("AWX_ALLOW_TEST_EVENTS", raising=False)
    h = WebhookHandler(_db(tmp_path), SECRET)
    body, ts = _body(), _now()
    evil = "attacker-chosen"
    forged = _sig(evil, ts, body)

    assert h.verify(body, ts, forged, client_secret_key=evil) is False
    assert h.receive(body, ts, forged, client_secret_key=evil) == 400


def test_client_secret_key_is_ignored_even_when_it_would_verify(tmp_path, monkeypatch):
    """With the flag ON, a header value that is not the real secret must not
    authenticate — otherwise the flag is just the bypass renamed."""
    monkeypatch.setenv("AWX_ALLOW_TEST_EVENTS", "true")
    h = WebhookHandler(_db(tmp_path), SECRET)
    body, ts = _body(), _now()
    assert h.verify(body, ts, _sig("not-the-secret", ts, body),
                    client_secret_key="not-the-secret") is False


def test_client_secret_key_honoured_when_it_equals_the_real_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("AWX_ALLOW_TEST_EVENTS", "true")
    h = WebhookHandler(_db(tmp_path), SECRET)
    body, ts = _body(), _now()
    assert h.verify(body, ts, _sig(SECRET, ts, body),
                    client_secret_key=SECRET) is True


def test_header_cannot_make_a_forged_signature_verify(tmp_path, monkeypatch):
    """Without the flag the header is simply ignored, so it cannot rescue a
    forged signature. A GENUINE signature still verifies with or without the
    header — that is correct, and is what the last test asserts."""
    monkeypatch.delenv("AWX_ALLOW_TEST_EVENTS", raising=False)
    h = WebhookHandler(_db(tmp_path), SECRET)
    body, ts = _body(), _now()
    # Forged with the right secret but a wrong body/signature pairing.
    forged = _sig(SECRET, ts, b'{"id":"other","name":"payout.transfer.paid"}')
    assert h.verify(body, ts, forged, client_secret_key=SECRET) is False


def test_empty_secret_rejects_everything(tmp_path, monkeypatch):
    monkeypatch.delenv("AWX_ALLOW_TEST_EVENTS", raising=False)
    h = WebhookHandler(_db(tmp_path), "")
    body, ts = _body(), _now()
    assert h.verify(body, ts, _sig(SECRET, ts, body)) is False
    assert h.receive(body, ts, _sig(SECRET, ts, body)) == 400


def test_empty_secret_with_attacker_header_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("AWX_ALLOW_TEST_EVENTS", raising=False)
    h = WebhookHandler(_db(tmp_path), "")
    body, ts = _body(), _now()
    assert h.verify(body, ts, _sig("anything", ts, body),
                    client_secret_key="anything") is False


def test_correct_signature_still_works(tmp_path, monkeypatch):
    monkeypatch.delenv("AWX_ALLOW_TEST_EVENTS", raising=False)
    h = WebhookHandler(_db(tmp_path), SECRET)
    body, ts = _body(), _now()
    assert h.verify(body, ts, _sig(SECRET, ts, body)) is True


# --- SEC 2: replay protection ----------------------------------------
def test_stale_timestamp_is_rejected(tmp_path):
    h = WebhookHandler(_db(tmp_path), SECRET)
    body = _body()
    old = str(int(time.time()) - 600)          # 10 minutes ago
    assert h.verify(body, old, _sig(SECRET, old, body)) is False


def test_future_timestamp_is_rejected(tmp_path):
    h = WebhookHandler(_db(tmp_path), SECRET)
    body = _body()
    ahead = str(int(time.time()) + 600)
    assert h.verify(body, ahead, _sig(SECRET, ahead, body)) is False


def test_timestamp_inside_the_window_is_accepted(tmp_path):
    h = WebhookHandler(_db(tmp_path), SECRET)
    body, ts = _body(), _now()
    assert h.verify(body, ts, _sig(SECRET, ts, body)) is True


def test_missing_or_unparseable_timestamp_is_rejected(tmp_path):
    """Otherwise an attacker omits the header and skips the check entirely."""
    h = WebhookHandler(_db(tmp_path), SECRET)
    body = _body()
    assert h.verify(body, "", _sig(SECRET, "", body)) is False
    assert h.verify(body, "not-a-number", "deadbeef") is False


def test_replayed_event_is_still_deduped_by_id(tmp_path):
    """Replay protection bounds the window; dedupe on event_id is the real
    defence. Both must hold."""
    h = WebhookHandler(_db(tmp_path), SECRET)
    body, ts = _body(), _now()
    sig = _sig(SECRET, ts, body)
    assert h.receive(body, ts, sig) == 200
    assert h.receive(body, ts, sig) == 200
    assert len(h.pending_events()) == 1


# --- SEC 3: an event must carry an id --------------------------------
@pytest.mark.parametrize("payload", [
    {"name": "payout.transfer.failed", "transfer_id": "tr-1"},
    {"id": "", "name": "payout.transfer.failed"},
    {"id": None, "name": "payout.transfer.failed"},
    {"id": "   ", "name": "payout.transfer.failed"},
    {"event_id": None, "name": "payout.transfer.failed"},
])
def test_event_without_an_id_is_rejected(tmp_path, payload):
    """Without a unique id, dedupe cannot work and a replayed event would be
    reprocessed as a new one — the exact shape of a double payment."""
    h = WebhookHandler(_db(tmp_path), SECRET)
    body = json.dumps(payload).encode()
    ts = _now()
    assert h.receive(body, ts, _sig(SECRET, ts, body)) == 400


def test_non_object_payload_is_rejected(tmp_path):
    h = WebhookHandler(_db(tmp_path), SECRET)
    body = b'["not", "an", "object"]'
    ts = _now()
    assert h.receive(body, ts, _sig(SECRET, ts, body)) == 400
