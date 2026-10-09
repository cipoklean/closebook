"""Webhook receiver: verify, dedupe, order, ack-fast.

Verified against the Airwallex docs ("Listen for webhook events"):
- x-signature = hex(HMAC-SHA256( AWX_WEBHOOK_SECRET,  x-timestamp + raw_body ))
  computed on the RAW request body, BEFORE JSON parsing — re-serializing the
  payload changes whitespace and breaks the signature.
- Test events carry the secret in a `client-secret-key` header instead of a
  shared secret (sandbox convenience); we honour that when present.
- Endpoint must return 200 IMMEDIATELY and process in the background
  (Airwallex retries on anything but 200/timeout).
- Delivery is at-least-once: dedupe on event id, order by created_at.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime
from typing import Optional

from ..timeutils import utcnow as _utcnow

from .client import AirwallexClient
from ..models import WebhookEvent

log = logging.getLogger("closebook.webhooks")

# How long a signed webhook stays acceptable. A captured body+signature pair is
# otherwise replayable forever; dedupe on event_id does not help because an
# event id we have never seen looks brand new.
REPLAY_WINDOW_SECONDS = 300  # 5 minutes


class WebhookHandler:
    EVENTS_OF_INTEREST = {
        "payout.transfer.paid",
        "payout.transfer.failed",
        "payout.transfer.cancelled",
        "issuing.transaction.succeeded",
        "issuing.transaction.failed",
    }

    def __init__(self, db, secret: str, client: Optional[AirwallexClient] = None):
        self._db = db
        self._secret = secret  # kept as str; encoded at verify time
        self._client = client

    # ---- verification ---------------------------------------------------
    def verify(self, raw_body: bytes, x_timestamp: str,
               x_signature: str, client_secret_key: Optional[str] = None) -> bool:
        """Constant-time compare of HMAC-SHA256(secret, x-timestamp + raw_body).

        The shared secret is the ONLY key. `client_secret_key` is a sandbox
        test-event convenience, and treating it as the HMAC key was an
        authentication bypass: an attacker could sign a forged webhook with any
        value they liked and it would verify, because the "key" they supplied
        was compared against a signature they also supplied.

        It is honoured only when BOTH are true:
          * `AWX_ALLOW_TEST_EVENTS=true` (explicit opt-in, default off), and
          * the header value equals the configured `AWX_WEBHOOK_SECRET`.

        An empty configured secret rejects everything. There is no fallback.
        """
        secret = self._secret.encode()
        if not self._secret:
            # No shared secret configured and test events not explicitly
            # enabled -> nothing can be authenticated, so reject everything.
            log.error("rejecting webhook: AWX_WEBHOOK_SECRET is not configured")
            return False

        # Replay window: a captured (body, signature) pair is valid forever
        # otherwise, so an attacker who ever sees one can resend it indefinitely.
        # Deduping on event_id limits the damage but does not prevent it — an
        # event we have never seen, replayed inside the window, still arrives
        # looking new. Bound how long a signature is honoured.
        if not self._timestamp_is_fresh(x_timestamp):
            log.warning("rejecting webhook: x-timestamp outside the %ss window",
                        REPLAY_WINDOW_SECONDS)
            return False

        if client_secret_key and self._allow_test_events():
            if not hmac.compare_digest(client_secret_key, self._secret):
                log.warning("rejecting webhook: client-secret-key does not match "
                            "the configured AWX_WEBHOOK_SECRET")
                return False

        message = x_timestamp.encode() + raw_body
        expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, (x_signature or "").lower())

    def _allow_test_events(self) -> bool:
        """Explicit opt-in, read fresh so tests can toggle it."""
        return str(os.environ.get("AWX_ALLOW_TEST_EVENTS", "")).lower() in {
            "1", "true", "yes"}

    @staticmethod
    def _timestamp_is_fresh(x_timestamp: str,
                            window_seconds: int = REPLAY_WINDOW_SECONDS) -> bool:
        """Is `x-timestamp` within +/- `window_seconds` of now?

        A missing or unparseable timestamp is NOT fresh. If it were, an attacker
        could omit the header entirely and skip the replay check.
        """
        try:
            sent = int(float(x_timestamp))
        except (TypeError, ValueError):
            return False
        return abs(int(time.time()) - sent) <= window_seconds

    # ---- intake ---------------------------------------------------------
    def receive(self, raw_body: bytes, x_timestamp: str, x_signature: str,
                client_secret_key: Optional[str] = None) -> int:
        """Ack-fast: verify + persist + return 200/400-equivalent code.

        Returns HTTP-like status: 200 accepted (or deduped), 400 bad sig.
        """
        if not self.verify(raw_body, x_timestamp, x_signature, client_secret_key):
            log.warning("webhook rejected: signature/timestamp verification failed")
            return 400
        event = json.loads(raw_body)
        if not isinstance(event, dict):
            log.warning("webhook rejected: payload is not a JSON object")
            return 400
        event_id = event.get("id") or event.get("event_id")
        if not event_id or not str(event_id).strip():
            # Deduping on event_id is what stops an at-least-once redelivery
            # being processed twice. An event with no id cannot be deduped, so
            # it would be reprocessed on every retry — the exact shape of a
            # double payment. Refuse it instead of storing an unidentifiable row.
            log.warning("webhook rejected: payload carries no event id")
            return 400
        event_id = str(event_id).strip()
        name = event.get("name") or event.get("type")

        with self._db.session() as s:
            existing = s.query(WebhookEvent).filter_by(event_id=event_id).first()
            if existing:  # at-least-once replay — already recorded
                return 200
            s.add(WebhookEvent(
                event_id=event_id,
                name=name,
                payload=raw_body.decode("utf-8", errors="replace"),
                created_at=self._parse_ts(event.get("created_at")),
            ))
            s.commit()
        log.info("webhook received: %s (%s)", name, event_id)
        return 200

    def claim_next_pending(self) -> Optional[WebhookEvent]:
        """Atomically claim ONE unprocessed event for this worker.

        Two concurrent background tasks both calling `pending_events()` would
        each get the same rows and both act on them — for a `transfer.failed`
        that means two incident handlers, and potentially two replacement
        payments for one failure.

        The claim is a conditional UPDATE: `... WHERE processed_at IS NULL`.
        The database decides which worker wins, and the loser's UPDATE matches
        zero rows. Reading a row and then writing `processed_at` afterwards is
        NOT safe — that is check-then-act, and both workers pass the check.
        """
        with self._db.session() as s:
            candidate = (
                s.query(WebhookEvent)
                .filter(WebhookEvent.processed_at.is_(None))
                .order_by(WebhookEvent.created_at.asc(), WebhookEvent.id.asc())
                .first()
            )
            if candidate is None:
                return None
            event_id = candidate.event_id
            claimed = (
                s.query(WebhookEvent)
                .filter(WebhookEvent.event_id == event_id,
                        WebhookEvent.processed_at.is_(None))
                .update({"processed_at": _utcnow()},
                        synchronize_session=False)
            )
            s.commit()
            if not claimed:
                # Another worker claimed it first. Skip, do not process.
                return None
            s.expunge(candidate)
            return candidate

    def claim_all_pending(self) -> list[WebhookEvent]:
        """Claim every currently-pending event, one atomic claim at a time."""
        claimed: list[WebhookEvent] = []
        while True:
            ev = self.claim_next_pending()
            if ev is None:
                return claimed
            claimed.append(ev)

    def pending_events(self) -> list[WebhookEvent]:
        """Unprocessed events ordered by created_at (out-of-order delivery
        guard: process oldest first)."""
        with self._db.session() as s:
            rows = (
                s.query(WebhookEvent)
                .filter(WebhookEvent.processed_at.is_(None))
                .order_by(WebhookEvent.created_at.asc(), WebhookEvent.id.asc())
                .all()
            )
            for r in rows:
                s.expunge(r)  # detached-safe for the caller's loop
            return rows

    def mark_processed(self, event_id: str) -> None:
        with self._db.session() as s:
            s.query(WebhookEvent).filter_by(event_id=event_id).update(
                {"processed_at": _utcnow()}, synchronize_session=False
            )
            s.commit()

    @staticmethod
    def _parse_ts(v) -> Optional[datetime]:
        if not v:
            return None
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None


__all__ = ["WebhookHandler"]
