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
        """Return {'USD': Decimal('123.45'), ...} in major units.

        Verified live 2026-10-06: GET /balances/current returns a BARE JSON
        LIST (not an object) of rows keyed `available_amount`, and it lists
        every currency in the world with zero balances. The old `data`/
        `available_balance` shape does not exist.
        """
        raw = self._c.call("GET", "/api/v1/balances/current")
        items = raw.get("items") if isinstance(raw, dict) else raw
        out: dict[str, Decimal] = {}
        for b in items or []:
            if not isinstance(b, dict):
                continue
            ccy = b.get("currency")
            if not ccy:
                continue
            amt = b.get("available_amount")
            if amt is None:
                amt = b.get("available_balance", "0")
            out[ccy] = Decimal(str(amt))
        return out

    def snapshot_to_db(self, db_session) -> None:
        """Persist the latest balances as integer minor units for the planner.

        Only currencies in `money.CURRENCY_EXPONENT` are stored: the live
        GET /balances/current returns ~46 world currencies (AED, ARS, BDT...)
        and most are not in our exponent table (hit 2026-10-06). Unknown
        currencies are skipped rather than crashing the seed.
        """
        from ..money import CURRENCY_EXPONENT

        major = self.fetch_current()
        for ccy, amt in major.items():
            if ccy not in CURRENCY_EXPONENT:
                continue
            row = db_session.query(BalanceSnapshot).filter(
                BalanceSnapshot.currency == ccy
            ).first()
            if row is None:
                # UPDATE alone is a no-op on first run; insert the row.
                db_session.add(BalanceSnapshot(
                    currency=ccy,
                    available_minor=to_minor(amt, ccy),
                    fetched_at=datetime.utcnow(),
                ))
            else:
                row.available_minor = to_minor(amt, ccy)
                row.fetched_at = datetime.utcnow()
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
