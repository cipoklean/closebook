"""ALL sandbox-only simulation calls behind ONE interface (closebook.md §2/§5).

The demo seeds the entire scenario through simulation endpoints. Keeping
them behind a single seam means the domain decision logic never depends on
sandbox mechanics — a real-money build swaps this module out without
touching the planner/executor.

Key behaviours (closebook.md §6 + builder guide):
- POST /simulation/deposit/create -> returns PENDING, but the balance
  updates IMMEDIATELY. Re-read balances before recalculating anything.
  There is no `currency` field: the deposit lands in the Global Account's
  own currency (send one and Airwallex silently discards it).
- POST /simulation/transfers/{id}/transition -> to SENT before PAID or
  FAILED. A chosen failure_type applies only after SENT.
- POST /simulation/issuing/create + /capture + /reverse + /refund.
- POST /simulation/accounts/{id}/update_status -> force-activate a
  connected account (kits 5-8).

All of these are marked `is_simulation=True` on the client so a 5xx/timeout
there raises a retryable error, NOT AmbiguousSubmit.
"""
from __future__ import annotations

from typing import Optional

from .client import AirwallexClient


class Simulation:
    """The one place the build talks to sandbox-only endpoints."""

    def __init__(self, client: AirwallexClient):
        self._c = client

    # --- deposits -------------------------------------------------------
    def deposit(self, *, global_account_id: str, amount_minor: int,
                currency: str, payer_name: Optional[str] = None) -> dict:
        """Simulate a deposit into a global account. Returns PENDING status,
        but the balance is already credited — re-read before deciding."""
        from ..money import to_major_str
        body: dict = {
            "global_account_id": global_account_id,
            "amount": to_major_str(amount_minor, currency),
        }
        if payer_name:
            body["payer_name"] = payer_name
        # NOTE: no currency field — it lands in the account's currency.
        return self._c.call("POST", "/api/v1/simulation/deposit/create",
                            json_body=body, is_simulation=True)

    def list_deposits(self) -> list[dict]:
        data = self._c.call("GET", "/api/v1/deposits", is_simulation=True)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("items") or data.get("data") or []
        return []

    # --- transfer outcomes ---------------------------------------------
    def transition_transfer(self, transfer_id: str, next_status: str,
                            failure_type: Optional[str] = None) -> dict:
        """Move a transfer through its state machine.
        next_status in {SENT, PAID, FAILED, CANCELLED}. failure_type is only
        meaningful after SENT (a bank-return style reason)."""
        body: dict = {"next_status": next_status}
        if failure_type:
            body["failure_type"] = failure_type
        return self._c.call(
            "POST", f"/api/v1/simulation/transfers/{transfer_id}/transition",
            json_body=body, is_simulation=True,
        )

    # --- issuing outcomes ------------------------------------------------
    def simulate_issuing_authorization(self, card_id: str, *, currency: str,
                                       amount_minor: int, outcome: str = "CLEARING",
                                       test_card_number: str = "4035501000000008",
                                       merchant_name: str = "Test Merchant") -> dict:
        """Create a synthetic authorization against a card.
        outcome in {CLEARING, FAILED}. Use failure_reason to stage a decline
        (e.g. LIMIT_EXCEEDED)."""
        from ..money import to_major_str
        body: dict = {
            "card_id": card_id,
            "amount": to_major_str(amount_minor, currency),
            "currency": currency,
            "card_number": test_card_number,
            "merchant_name": merchant_name,
            "transaction_type": outcome,
        }
        return self._c.call("POST", "/api/v1/simulation/issuing/create",
                            json_body=body, is_simulation=True)

    def capture_issuing(self, transaction_id: str) -> dict:
        return self._c.call("POST",
                            f"/api/v1/simulation/issuing/{transaction_id}/capture",
                            json_body={}, is_simulation=True)

    def reverse_issuing(self, transaction_id: str) -> dict:
        return self._c.call("POST",
                            f"/api/v1/simulation/issuing/{transaction_id}/reverse",
                            json_body={}, is_simulation=True)

    def refund_issuing(self, card_id: str, *, currency: str,
                       amount_minor: int, transaction_id: Optional[str] = None) -> dict:
        from ..money import to_major_str
        body: dict = {
            "card_id": card_id,
            "amount": to_major_str(amount_minor, currency),
            "currency": currency,
        }
        if transaction_id:
            body["transaction_id"] = transaction_id
        return self._c.call("POST", "/api/v1/simulation/issuing/refund",
                            json_body=body, is_simulation=True)

    # --- connected-account activation (kits 5-8, later phases) ----------
    def activate_connected_account(self, account_id: str) -> dict:
        return self._c.call(
            "POST", f"/api/v1/simulation/accounts/{account_id}/update_status",
            json_body={"next_status": "ACTIVE", "force": True}, is_simulation=True,
        )


__all__ = ["Simulation"]
