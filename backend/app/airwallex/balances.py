"""Balances (GET /balances/current) + the balance-snapshot cache.

The planner reads from the snapshot, not the API, so a plan diff is
deterministic and doesn't re-hit the sandbox mid-plan.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ..money import to_minor
from ..models import BalanceSnapshot
from .client import AirwallexClient


class Balances:
    def __init__(self, client: AirwallexClient):
        self._c = client

    def fetch_current(self) -> dict:
        """Return {'USD': Decimal('123.45'), ...} in major units."""
        data = self._c.call("GET", "/api/v1/balances/current")
        out: dict[str, Decimal] = {}
        # Defensive: the shape is either {"data": [{currency, available_balance}]}
        # or a bare {currency: {available_balance}}. Normalize both.
        items = data.get("data") if isinstance(data, dict) else data
        if isinstance(items, list):
            for b in items:
                ccy = b.get("currency")
                if ccy:
                    out[ccy] = Decimal(str(b.get("available_balance", "0")))
        elif isinstance(items, dict):
            for ccy, block in items.items():
                if isinstance(block, dict):
                    out[ccy] = Decimal(str(block.get("available_balance", "0")))
        return out

    def snapshot_to_db(self, db_session) -> None:
        """Persist the latest balances as integer minor units for the planner."""
        major = self.fetch_current()
        for ccy, amt in major.items():
            db_session.query(BalanceSnapshot).filter(
                BalanceSnapshot.currency == ccy
            ).update({"available_minor": to_minor(amt, ccy), "fetched_at": datetime.utcnow()},
                     synchronize_session=False)
        db_session.commit()

    def cached_available(self, db_session, currency: str) -> int:
        """Minor units from the latest snapshot (0 if never fetched)."""
        row = (
            db_session.query(BalanceSnapshot)
            .filter(BalanceSnapshot.currency == currency)
            .order_by(BalanceSnapshot.fetched_at.desc())
            .first()
        )
        return row.available_minor if row else 0


__all__ = ["Balances"]
