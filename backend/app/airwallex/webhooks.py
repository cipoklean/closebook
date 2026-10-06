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
from datetime import datetime
from typing import Optional

from .client import AirwallexClient
from ..models import WebhookEvent

log = logging.getLogger("closebook.webhooks")


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

        Test events pass the secret in the `client-secret-key` header instead
        of using the shared secret; honour that when present.
        """
        secret = (client_secret_key or self._secret).encode()
        message = x_timestamp.encode() + raw_body
        expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, (x_signature or "").lower())

    # ---- intake ---------------------------------------------------------
    def receive(self, raw_body: bytes, x_timestamp: str, x_signature: str,
                client_secret_key: Optional[str] = None) -> int:
        """Ack-fast: verify + persist + return 200/400-equivalent code.

        Returns HTTP-like status: 200 accepted (or deduped), 400 bad sig.
        """
        if not self.verify(raw_body, x_timestamp, x_signature, client_secret_key):
            log.warning("webhook signature mismatch")
            return 400
        event = json.loads(raw_body)
        event_id = event.get("id") or event.get("event_id")
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
                {"processed_at": datetime.utcnow()}, synchronize_session=False
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
