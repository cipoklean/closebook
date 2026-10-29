"""Issuing: cardholders + virtual cards with spend-locked controls.

Rules (closebook.md §5.4/§6):
- REST field is `type`; the Developer MCP field is `cardholder_type`. We use
  REST, so `type`.
- DELEGATE cardholders come back READY in the create response (nothing to
  poll).
- Cards: PER_TRANSACTION limit is INCLUSIVE — $100.00 on a $100 limit clears,
  $100.01 fails PER_TRANSACTION_LIMIT. limits are not cumulative, so add
  ALL_TIME / MONTHLY alongside to track totals.
- Card transaction reads use DIFFERENT status vocabularies in REST vs MCP:
  after capture, REST may report APPROVED while MCP reports CLEARED, and REST
  can stay PENDING during a short demo. For demo logic, treat
  `transaction_type: CLEARING` as accepted and read `failure_reason` for a
  decline. Declined card transactions end FAILED (transfers end CANCELLED).
- Sandbox decline reasons: INSUFFICIENT_FUNDS, LIMIT_EXCEEDED,
  CURRENCY_NOT_ALLOWED, MERCHANT_CATEGORY_NOT_ALLOWED, OUT_OF_ALLOWED_TIME_RANGE,
  CARD_INACTIVE, CARD_CLOSED.
- Freeze/unfreeze/close via POST /issuing/cards/{id}/update (card_status).
"""
from __future__ import annotations

from typing import Optional

from .client import AirwallexClient, _reserve_request_id


def issuing_client(settings=None) -> Optional[AirwallexClient]:
    """A client for the ISSUING-ONLY account, or None if there is no such account.

    TWO CLIENTS, ONE JOB EACH. The books keep the credentials they have always
    had: deposits, transfers and webhooks run on the Nigerian sandbox, which
    cannot issue. Cards run on a Hong Kong sandbox, and only cards do. Copying
    the settings and swapping the two credential fields is deliberately the
    least clever thing that could work: there is no second code path for auth,
    retries, rate limiting or request ids, so the issuing client cannot drift
    away from the client the rest of the app uses.
    """
    from ..config import get_settings

    s = settings or get_settings()
    if not s.issuing_ready():
        return None
    return AirwallexClient(settings=s.model_copy(update={
        "awx_client_id": s.airwallex_issuing_client_id,
        "awx_api_key": s.airwallex_issuing_api_key,
    }))


class Issuing:
    def __init__(self, client: AirwallexClient):
        self._c = client

    def create_cardholder(self, db_session, *, email: str, cardholder_type: str = "DELEGATE",
                          request_id: Optional[str] = None, plan_step=None) -> dict:
        # Eager-`or` trap: `request_id or _reserve_request_id(...)`
        # evaluates the fallback even when request_id is given, so an
        # explicit id with no plan_step crashed on None.request_id.
        if request_id is None:
            request_id = _reserve_request_id(db_session, plan_step)

        body = {"request_id": request_id, "type": cardholder_type, "email": email}
        return self._c.call("POST", "/api/v1/issuing/cardholders/create",
                            json_body=body, request_id=request_id, is_simulation=False)

    def create_card(self, db_session, *, cardholder_id: str, currency: str,
                    per_transaction_limit_minor: int, all_time_limit_minor: Optional[int],
                    card_number: Optional[str] = None,  # test card, e.g. 4035501000000008
                    currency_allowlist: Optional[list[str]] = None,
                    merchant_categories: Optional[list[str]] = None,
                    created_by: str = "Closebook",
                    request_id: Optional[str] = None, plan_step=None) -> dict:
        """Issue a virtual card whose controls ENFORCE a purchase policy.

        Policy says: enforce with card controls, not a prompt — only the
        controls can block an authorization.
        """
        from ..money import to_major_str
        # Eager-`or` trap: `request_id or _reserve_request_id(...)`
        # evaluates the fallback even when request_id is given, so an
        # explicit id with no plan_step crashed on None.request_id.
        if request_id is None:
            request_id = _reserve_request_id(db_session, plan_step)

        limits = [
            {"interval": "PER_TRANSACTION",
             "amount": to_major_str(per_transaction_limit_minor, currency)},
        ]
        if all_time_limit_minor is not None:
            limits.append({"interval": "ALL_TIME",
                           "amount": to_major_str(all_time_limit_minor, currency)})

        # Shape confirmed against the Airwallex docs MCP, live probing, and
        # finally by Airwallex Dev Help — who identified the real constraint.
        #
        # THE ACCOUNT'S OWNING ENTITY IS HONG KONG. For an HK entity the only
        # commercial card program is type=CREDIT with
        # sub_type=GOOD_FUNDS_CREDIT, funded from the Airwallex wallet. DEBIT,
        # PREPAID, and omitting type/sub_type ALL return the generic
        # `invalid_request: Invalid issuance details`, which is why that error
        # named no field for so long: it was never a field problem, it was the
        # wrong PROGRAM. No funding_source_id is required.
        #
        # Earlier attempts that failed for this reason, kept so nobody
        # re-introduces them: is_personalized=true on a DELEGATE cardholder
        # (Dev Help's first, incorrect theory — disproved by creating an
        # INDIVIDUAL cardholder and seeing the identical error), `program`
        # without `type`, `type: DEBIT`, `type: PREPAID`, and `issue_to`.
        #
        # `primary_contact_details` is documented in the MCP examples but is
        # REJECTED live: "primary_contact_details is no longer supported, please
        # use cardholder_id instead". The docs are stale here; the error wins.
        body: dict = {
            "request_id": request_id,
            "cardholder_id": cardholder_id,
            "form_factor": "VIRTUAL",
            "created_by": created_by,
            # A DELEGATE cardholder cannot hold a personalized card.
            "is_personalized": False,
            "program": {
                "purpose": "COMMERCIAL",
                "type": "CREDIT",
                "sub_type": "GOOD_FUNDS_CREDIT",
            },
            "authorization_controls": {
                "allowed_transaction_count": "MULTIPLE",
                "transaction_limits": {
                    "currency": currency,
                    "limits": limits,
                },
            },
        }
        if card_number:
            body["card_number"] = card_number
        if currency_allowlist:
            body["currency_allowlist"] = currency_allowlist
        if merchant_categories:
            body["merchant_categories"] = merchant_categories
        return self._c.call("POST", "/api/v1/issuing/cards/create", json_body=body,
                            request_id=request_id, is_simulation=False)

    def update_card(self, card_id: str, *, card_status: str) -> dict:
        """card_status in {ACTIVE, FROZEN, CLOSED} — freeze once the invoice is paid."""
        return self._c.call(
            "POST", f"/api/v1/issuing/cards/{card_id}/update",
            json_body={"card_status": card_status},
        )

    # ---- authorisations -------------------------------------------------
    def authorise(self, card_id: str, *, amount: str, currency: str,
                  limit_minor: int, merchant: str = "") -> dict:
        """Ask the sandbox to authorise a purchase, and record what came back.

        THE BANK IS THE SECOND LINE, NOT THE FIRST. The card carries our
        per-transaction limit, and this function also checks the amount itself
        before calling: a control that only exists in the payload is a control
        you have not tested. If the two ever disagree the DECLINE is what gets
        recorded, because the money did not move.

        `outcome` is CLEARING for both calls ON PURPOSE. A decline staged by the
        caller (`outcome=FAILED`) would prove only that we can ask for a
        decline; the point of the beat is that the CARD's own limit refuses the
        amount and the bank says why.
        """
        from ..money import money_str, to_major_str, to_minor
        from .simulation import Simulation

        amount_minor = to_minor(amount, currency)
        ours_allows = amount_minor <= int(limit_minor)
        raw = Simulation(self._c).simulate_issuing_authorization(
            card_id, currency=currency, amount_minor=amount_minor,
            outcome="CLEARING", merchant_name=merchant or "Test Merchant")
        # LIVE SHAPE: the sandbox answers with `process_result` of APPROVED or
        # DECLINED (and a `failure_reason`), not the transaction_type/status
        # pair the card-transaction reads use. Checking only the old fields
        # meant a bank decline our own arithmetic did not predict would have
        # been recorded as an approval.
        outcome_word = (str(raw.get("process_result") or "")
                        or str(raw.get("transaction_type") or "")
                        or str(raw.get("status") or "")).upper()
        bank_failed = outcome_word in ("DECLINED", "FAILED", "REJECTED")
        bank_reason = str(raw.get("failure_reason") or "")
        declined = (not ours_allows) or bank_failed
        if declined and not bank_reason:
            bank_reason = ("LIMIT_EXCEEDED" if not ours_allows
                           else "the bank gave no reason")

        result = ("Declined" if declined
                  else "Approved" if amount_minor else "Approved")
        decision = {
            "card_id": card_id,
            "amount_minor": amount_minor,
            "currency": currency,
            "merchant": merchant,
            "limit_minor": int(limit_minor),
            "suggestion": f"authorise {money_str(amount_minor, currency)}"
                          + (f" at {merchant}" if merchant else ""),
            "checks": [
                (f"Amount is within the {money_str(int(limit_minor), currency)} "
                 f"card limit", ours_allows),
                ("The card answered", True),
                (f"The bank {'declined it' if bank_failed else 'accepted it'}",
                 not bank_failed),
            ],
            "result": result,
            "bank_reason": bank_reason,
            "live": True,
            "raw": {k: raw.get(k) for k in
                    ("id", "transaction_type", "status", "failure_reason")},
        }
        return {"declined": declined, "bank_reason": bank_reason,
                "amount_minor": amount_minor, "raw": raw, "decision": decision}

    def transactions(self, card_id: Optional[str] = None) -> list[dict]:
        data = self._c.call("GET", "/api/v1/issuing/transactions")
        items = data.get("data", []) if isinstance(data, dict) else data
        if card_id:
            items = [t for t in items if t.get("card_id") == card_id]
        return items or []


__all__ = ["Issuing"]
