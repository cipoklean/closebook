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


#: How to reach each end state. PROCESSING only accepts SENT as its next
#: status, so the failed ends have to pass THROUGH sent to get there — asking
#: for FAILED or PAID directly is an HTTP 500, not a rejection with a reason.
_TRANSFER_ROUTES: dict[str, tuple[str, ...]] = {
    "SENT": ("SENT",),
    "PAID": ("SENT", "PAID"),
    "FAILED": ("SENT", "FAILED"),
    "CANCELLED": ("SENT", "CANCELLED"),
    # Not every hop is reachable from every state; the caller passes the one it
    # wants and the route is what the sandbox actually accepts.
    "PROCESSING": ("PROCESSING",),
}


class Simulation:
    """The one place the build talks to sandbox-only endpoints."""

    def __init__(self, client: AirwallexClient):
        self._c = client

    # --- deposits -------------------------------------------------------
    def deposit(self, *, global_account_id: str, amount_minor: int,
                currency: str, payer_name: Optional[str] = None,
                reference: Optional[str] = None,
                status: Optional[str] = None) -> dict:
        """Simulate a deposit into a global account.

        `reference` is the free-text the operator typed, and it is the whole
        point of the exercise: a MESSY reference is what makes the matcher earn
        its keep. It is passed through untouched — no trimming, no normalising,
        no "helpfully" filling in a blank one.

        Confirmed against the Airwallex docs (2026-10-22) that the endpoint
        accepts: amount, global_account_id, payer_bankname, payer_country,
        payer_name, reference, statement_ref, status. `status` is one of
        PENDING / REJECTED / SETTLED and defaults to SETTLED, so it is sent
        explicitly and only when asked for.

        NOTE: still no `currency` field — the deposit lands in the global
        account's own currency and a currency sent here is silently discarded.
        """
        from ..money import to_major_str
        body: dict = {
            "global_account_id": global_account_id,
            "amount": to_major_str(amount_minor, currency),
        }
        if payer_name:
            body["payer_name"] = payer_name
        if reference is not None:
            body["reference"] = reference
        if status:
            body["status"] = status
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
        """Move a transfer to its END state, walking the states in between.

        Airwallex accepts only the NEXT valid status: asking for `PAID` on a
        PROCESSING transfer returns HTTP 500 `operation_failed`, and asking for
        `FAILED` does the same. The path from PROCESSING is SENT, so every route
        goes through it. Verified live against the sandbox, both ways.

        `failure_type` is only meaningful on the final FAILED hop; sending it on
        an intermediate SENT is a validation error.
        """
        target = (next_status or "").strip().upper()
        route = _TRANSFER_ROUTES.get(target)
        if route is None:
            raise ValueError(f"cannot simulate next_status={next_status!r}")

        out: dict = {}
        for i, hop in enumerate(route):
            body: dict = {"next_status": hop}
            if failure_type and hop == target:
                body["failure_type"] = failure_type
            out = self._c.call(
                "POST", f"/api/v1/simulation/transfers/{transfer_id}/transition",
                json_body=body, is_simulation=True,
            )
        return out

    def settle_transfer(self, transfer_id: str) -> dict:
        """Convenience: walk a transfer all the way to PAID."""
        return self.transition_transfer(transfer_id, "PAID")

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
