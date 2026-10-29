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

import logging

log = logging.getLogger(__name__)
from .client import AirwallexClient


#: The statuses a transfer can be walked through, in order, plus the two end
#: states it can be taken to.
#:
#: SCHEDULED IS NOT ONE OF THEM, and that was the bug. It is a FUNDING status
#: (the funding is scheduled for the transfer date) which the sandbox also
#: reports on the transfer itself, and it is not a simulatable TRANSFER status:
#: a transfer sitting on it has to be asked for PROCESSING first. The routes
#: used to start at SENT, so for exactly that transfer the sandbox refused the
#: first hop and Settle and Fail did nothing at all (run 8, twice, and the
#: operator saw "Airwallex's sandbox refused this step").
#:
#: The walk is therefore TOLERANT: ask for each hop in order, skip the ones the
#: bank refuses (it refuses the hops we are already past), and let a refusal of
#: the FINAL hop be the error. A genuine problem still surfaces, with the bank's
#: own reason.
_TRANSFER_WALK = ("PROCESSING", "SENT")

#: Only these can be simulated, per the simulation API's own documentation.
_TRANSFER_TARGETS = ("PROCESSING", "SENT", "PAID", "FAILED", "CANCELLED")


def _hops_to(target: str) -> tuple[str, ...]:
    """The hops to try, in order, to take a transfer to `target`."""
    if target in ("PAID", "FAILED", "CANCELLED"):
        return tuple(_TRANSFER_WALK) + (target,)
    if target in ("PROCESSING", "SENT"):
        return (target,)
    return ()


class Simulation:
    """The one place the build talks to sandbox-only endpoints."""

    def __init__(self, client: AirwallexClient):
        self._c = client

    # --- deposits -------------------------------------------------------
    def deposit(self, *, global_account_id: str, amount_minor: int,
                currency: str, payer_name: Optional[str] = None,
                reference: Optional[str] = None,
                statement_ref: Optional[str] = None,
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
        # The instance tag rides on BOTH free-text fields, because which one the
        # sandbox round-trips on `GET /api/v1/deposits` is its business, and a
        # tag that travels on only one silently stops protecting the day that
        # changes.
        if statement_ref is not None:
            body["statement_ref"] = statement_ref
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
        """Walk a transfer to `next_status`, tolerating the hops it is past.

        Airwallex accepts only the NEXT valid status: asking for `PAID` on a
        PROCESSING transfer is refused, and a transfer the bank reports as
        SCHEDULED has to be asked for PROCESSING before anything else. So the
        route is a ladder, and a refused INTERMEDIATE hop is not an error — it
        means the transfer is already past it. The final hop's refusal IS the
        error, and it is raised with the bank's own words.

        `failure_type` is only meaningful on the final hop; sending it on an
        intermediate one is a validation error.
        """
        from .client import ApiError

        target = (next_status or "").strip().upper()
        hops = _hops_to(target)
        if not hops:
            raise ValueError(f"cannot simulate next_status={next_status!r}")

        out: dict = {}
        refused_final: Optional[ApiError] = None
        for hop in hops:
            body: dict = {"next_status": hop}
            if failure_type and hop == target:
                body["failure_type"] = failure_type
            try:
                out = self._c.call(
                    "POST",
                    f"/api/v1/simulation/transfers/{transfer_id}/transition",
                    json_body=body, is_simulation=True,
                )
            except ApiError as exc:
                if hop == target:
                    refused_final = exc
                    break
                log.info("simulation: hop %s refused for %s, trying the next "
                         "one (%s)", hop, transfer_id, exc)
                continue
        if refused_final is not None:
            raise refused_final
        return out

    def settle_transfer(self, transfer_id: str) -> dict:
        """Convenience: walk a transfer all the way to PAID."""
        return self.transition_transfer(transfer_id, "PAID")

    # --- issuing outcomes ------------------------------------------------
    def simulate_issuing_authorization(self, card_id: str, *, currency: str,
                                       amount_minor: int, outcome: str = "CLEARING",
                                       test_card_number: str = "",
                                       merchant_name: str = "Test Merchant") -> dict:
        """Authorise a purchase against a real card, and let the CARD decide.

        DOCUMENTED SHAPE, read from the Airwallex docs MCP (the live API named
        `transaction_amount` after this module sent `amount`):
          transaction_amount (number), transaction_currency, card_id or
          card_number, merchant_info, single_phase, and an OPTIONAL
          transaction_failure_reason for a staged failure.

        `outcome` is kept for callers, but it no longer sends a
        `transaction_type`: that field is not in the schema, and sending it was
        a guess. A real decline has to come from the CARD's own controls -- ask
        for a failure reason and you have only tested that Airwallex can be told
        to fail.

        `test_card_number` is deliberately NOT defaulted. A card_number
        alongside a card_id TAKES PRIORITY, so the old default authorised the
        sandbox test card instead of the card under test.
        """
        from ..money import to_major_str
        body: dict = {
            "card_id": card_id,
            "transaction_amount": float(to_major_str(amount_minor, currency)),
            "transaction_currency": currency,
            "merchant_info": merchant_name,
            # authorize AND clear in one: the demo wants a settled answer, not
            # a pending authorisation that never resolves
            "single_phase": True,
        }
        if test_card_number:
            body["card_number"] = test_card_number
        if outcome.upper() == "FAILED":
            body["transaction_failure_reason"] = "TRANSACTION_BLOCKED"
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
