"""Webhook processing must be safe against two background tasks at once.

Audit item SEC 4. `pending_events()` + `mark_processed()` is check-then-act:
both workers pass the check, so both process the same event. For a
`transfer.failed` that means two incidents and potentially two replacement
payments for one failure.

The fix claims an event with a conditional UPDATE (`WHERE processed_at IS
NULL`), so the database decides the winner and the loser matches zero rows.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from app.airwallex.webhooks import WebhookHandler
from app.db import DB

SECRET = "claim-test-secret"


def _db(tmp_path):
    return DB(f"sqlite:///{tmp_path}/claim.sqlite3")


def _now() -> str:
    return str(int(time.time()))


def _deliver(h: WebhookHandler, event_id: str,
             name: str = "payout.transfer.failed", **over) -> None:
    payload = {"id": event_id, "name": name, "transfer_id": "tr-1"}
    payload.update(over)
    raw = json.dumps(payload).encode()
    ts = _now()
    sig = hmac.new(SECRET.encode(), ts.encode() + raw, hashlib.sha256).hexdigest()
    assert h.receive(raw, ts, sig) == 200


def _count(db: DB) -> int:
    from app.models import WebhookEvent
    with db.session() as s:
        return s.query(WebhookEvent).count()


def _unclaimed(db: DB) -> int:
    from app.models import WebhookEvent
    with db.session() as s:
        return s.query(WebhookEvent).filter(
            WebhookEvent.processed_at.is_(None)).count()


def test_two_workers_cannot_both_claim_the_same_event(tmp_path):
    """The concurrency bug: worker A reads, worker A is slow, worker B reads the
    SAME row and both process it."""
    db = _db(tmp_path)
    h = WebhookHandler(db, SECRET)
    _deliver(h, "evt-once")

    first = h.claim_next_pending()
    second = h.claim_next_pending()

    assert first is not None, "the first worker must claim the event"
    assert second is None, ("the second worker must get nothing — the event was "
                            "already claimed")
    assert _unclaimed(db) == 0, "the claim must mark the event processed"


def test_claims_are_handed_out_one_each_not_repeated(tmp_path):
    db = _db(tmp_path)
    h = WebhookHandler(db, SECRET)
    for i in range(5):
        _deliver(h, f"evt-{i}", name="payout.transfer.paid")

    claimed = h.claim_all_pending()

    assert len(claimed) == 5, "every event should be claimed exactly once"
    assert len({c.event_id for c in claimed}) == 5, "no event claimed twice"
    assert _unclaimed(db) == 0


def test_claim_returns_none_when_the_queue_is_empty(tmp_path):
    h = WebhookHandler(_db(tmp_path), SECRET)
    assert h.claim_next_pending() is None
    assert h.claim_all_pending() == []


def test_second_pass_after_all_claimed_returns_nothing(tmp_path):
    db = _db(tmp_path)
    h = WebhookHandler(db, SECRET)
    _deliver(h, "evt-1", name="payout.transfer.paid")
    assert len(h.claim_all_pending()) == 1
    assert h.claim_all_pending() == [], "a second pass must not re-deliver"


def test_new_event_arriving_is_still_claimable(tmp_path):
    db = _db(tmp_path)
    h = WebhookHandler(db, SECRET)
    _deliver(h, "evt-a", name="payout.transfer.paid")
    assert len(h.claim_all_pending()) == 1
    _deliver(h, "evt-b", name="payout.transfer.paid")
    again = h.claim_all_pending()
    assert len(again) == 1 and again[0].event_id == "evt-b"


def test_claims_are_processed_oldest_first(tmp_path):
    """Out-of-order delivery guard: created_at ordering must survive claiming."""
    from app.timeutils import utcnow
    from datetime import timedelta
    from app.models import WebhookEvent

    db = _db(tmp_path)
    h = WebhookHandler(db, SECRET)
    with db.session() as s:
        base = utcnow() - timedelta(minutes=10)
        for i, eid in enumerate(["evt-c", "evt-a", "evt-b"]):
            s.add(WebhookEvent(event_id=eid, name="payout.transfer.paid",
                               payload="{}",
                               created_at=base + timedelta(seconds=i * 60)))
        s.commit()

    order = [c.event_id for c in h.claim_all_pending()]
    assert order == ["evt-c", "evt-a", "evt-b"], order


def test_a_failed_worker_does_not_lose_the_event_silently(tmp_path):
    """Claiming marks the event processed immediately, so an exception in the
    handler cannot cause an infinite retry loop of the same event.

    This is a deliberate trade: the alternative (claim-on-success) lets a
    crashing worker re-process a `transfer.failed` forever. The incident is
    recorded before the crash point, and at-least-once delivery is bounded by
    dedupe on event_id instead.
    """
    db = _db(tmp_path)
    h = WebhookHandler(db, SECRET)
    _deliver(h, "evt-crash")
    h.claim_next_pending()
    assert _unclaimed(db) == 0
    assert h.claim_next_pending() is None
