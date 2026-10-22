"""Create a REAL Airwallex beneficiary from what the operator typed.

The corridor rules are not guessed here — they were derived live and are
recorded in `airwallex/beneficiaries.py`. What this module adds is:

  * a form-shaped input (what a human can actually type) turned into the nested
    `beneficiary` object the API wants, with `payment_methods` at the TOP level
    (sibling of `beneficiary`, verified live 2026-10-07);
  * `validate()` FIRST, then `create()` — validate takes the INNER beneficiary
    and wraps it itself, while create needs the body to already contain
    `beneficiary`; getting that backwards returns error 001 naming fields that
    are present;
  * a PLAIN refusal. Error 001 names the field you did NOT send, so the useful
    sentence is built from `payload.details.errors[].source`, not from
    `str(exc)` which is a generic message that helps nobody.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .workspace import WorkspaceRefused

#: LOCAL routing type by currency, lowercase. Airwallex rejects "SORT_CODE"
#: (code 011) and wants "sort_code".
ROUTING_TYPE_BY_CCY = {"USD": "aba", "GBP": "sort_code", "AUD": "bsb", "CAD": "transit_no"}

#: Currencies whose SWIFT corridor wants an explicit `iban`, not `account_number`.
IBAN_COUNTRIES = {"NL", "DE", "FR", "ES", "IT", "IE", "BE", "AT", "PT", "FI",
                  "GR", "LU", "PL", "SE", "DK", "NO", "CH", "GB"}


@dataclass
class BeneficiaryOutcome:
    ok: bool
    status: str                      # VERIFIED | FAILED
    beneficiary_id: Optional[str] = None
    error: Optional[str] = None      # ONE plain sentence, for the screen
    request: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


def build_payload(f: dict) -> dict:
    """Form fields -> the exact body `POST /beneficiaries/create` wants.

    Only fields with a value are sent. Airwallex's schema marks fields
    `rules_known: false / DEPENDENCY_NOT_PROVIDED` that turn out to be REQUIRED
    once their dependency is supplied, so sending an empty string is worse than
    sending nothing: it reads as "provided, and blank".
    """
    entity = (f.get("entity_type") or "COMPANY").strip().upper()
    if entity not in ("PERSONAL", "COMPANY"):
        raise WorkspaceRefused(
            f"Entity type has to be PERSONAL or COMPANY — Airwallex rejects "
            f"anything else with error 011. Got {entity!r}.")

    country = (f.get("country") or "").strip().upper()
    if len(country) != 2:
        raise WorkspaceRefused("Bank country has to be a 2-letter code, e.g. US.")
    ccy = (f.get("currency") or "").strip().upper()
    if len(ccy) != 3:
        raise WorkspaceRefused("Currency has to be a 3-letter code, e.g. USD.")
    method = (f.get("payout_method") or "LOCAL").strip().upper()
    if method not in ("LOCAL", "SWIFT"):
        raise WorkspaceRefused("Payout method has to be LOCAL or SWIFT.")

    name = (f.get("name") or "").strip()
    if not name:
        raise WorkspaceRefused("The beneficiary needs a name.")
    acct_name = (f.get("account_name") or "").strip() or name
    acct_number = (f.get("account_number") or "").strip()
    if not acct_number:
        raise WorkspaceRefused(
            "The account number is required. For a SWIFT corridor in an IBAN "
            "country, this is the IBAN itself.")

    address = {}
    for key in ("street_address", "city", "state", "postcode"):
        v = (f.get(key) or "").strip()
        if v:
            address[key] = v
    address["country_code"] = country

    bank_details: dict = {
        "account_currency": ccy,
        "bank_country_code": country,
        "account_name": acct_name,
    }
    bank_name = (f.get("bank_name") or "").strip()
    if bank_name:
        bank_details["bank_name"] = bank_name

    iban = (f.get("iban") or "").strip()
    if method == "SWIFT":
        swift = (f.get("swift_code") or "").strip()
        if not swift:
            raise WorkspaceRefused(
                "A SWIFT payment needs the bank's SWIFT/BIC code, e.g. INGBNL2A.")
        bank_details["swift_code"] = swift
        if iban:
            bank_details["iban"] = iban
            bank_details["account_number"] = iban
        elif country in IBAN_COUNTRIES:
            # Measured live: EUR corridors want an explicit `iban`; omitting it
            # returns error 001 naming `iban`. A bad checksum fails here, at
            # creation, with code 083 — which is the good time to find out.
            bank_details["iban"] = acct_number
            bank_details["account_number"] = acct_number
        else:
            bank_details["account_number"] = acct_number
    else:
        bank_details["account_number"] = acct_number
        rtype = (f.get("routing_type") or "").strip() \
            or ROUTING_TYPE_BY_CCY.get(ccy, "")
        rvalue = (f.get("routing_value") or "").strip()
        if rtype and rvalue:
            # `account_routing_value1`, NOT account_routing_number. Verified
            # live: the wrong name returns error 001 naming the field you did
            # NOT send, which sends you looking for the wrong problem.
            bank_details["account_routing_type1"] = rtype.lower()
            bank_details["account_routing_value1"] = rvalue
        elif rtype:
            raise WorkspaceRefused(
                f"A LOCAL payment to {country} needs the routing number "
                f"({rtype}). Leave the payout method as SWIFT if there is none.")
        if ccy == "USD":
            cat = (f.get("bank_account_category") or "Checking").strip()
            if cat not in ("Checking", "Savings"):
                raise WorkspaceRefused(
                    'For USD the account category has to be exactly "Checking" '
                    'or "Savings" — other casing returns error 016.')
            bank_details["bank_account_category"] = cat

    beneficiary: dict = {
        "entity_type": entity,
        "name": name,
        "address": address,
        "bank_details": bank_details,
    }
    first = (f.get("first_name") or "").strip()
    last = (f.get("last_name") or "").strip()
    if entity == "PERSONAL" and (first or last):
        beneficiary["first_name"] = first
        beneficiary["last_name"] = last

    return {
        "payment_methods": [method],
        "beneficiary": beneficiary,
        "_meta": {"country": country, "currency": ccy, "method": method},
    }


def plain_error(exc: Exception) -> str:
    """Airwallex's refusal, in one actionable sentence.

    The top-level `message` for a validation failure is generic ("Invalid
    issuance details", "a required field was missing or invalid"); the field
    that actually failed lives in `payload.details.errors[].source`. A money
    path that logs only the exception string gives an operator nothing to act
    on, so the field is pulled out and named.
    """
    payload = getattr(exc, "payload", None) or {}
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)

    details: list[str] = []
    if isinstance(payload, dict):
        errs = ((payload.get("details") or {}).get("errors")
                or payload.get("errors") or [])
        if isinstance(errs, list):
            for e in errs:
                if not isinstance(e, dict):
                    continue
                src = e.get("source") or e.get("field") or ""
                msg = e.get("message") or e.get("reason") or ""
                c = e.get("code") or ""
                bit = " ".join(str(x) for x in (src, msg) if x).strip()
                if c:
                    bit = f"{bit} (code {c})" if bit else f"code {c}"
                if bit:
                    details.append(bit)
        message = payload.get("message")
    else:
        message = None

    head = str(message or code or exc)
    if details:
        return f"Airwallex refused: {head} — {'; '.join(details)}."
    if status is not None:
        return f"Airwallex refused (HTTP {status}): {head}."
    return f"Airwallex refused: {head}."


def validate_and_create(db_session, f: dict, *, beneficiaries, request_id=None):
    """Validate, then create. Returns a BeneficiaryOutcome, never raises for an
    API refusal — the refusal IS the outcome and has to be storable."""
    from ..airwallex.client import ApiError

    try:
        body = build_payload(f)
    except WorkspaceRefused as exc:
        return BeneficiaryOutcome(ok=False, status="FAILED", error=str(exc),
                                  request={"_rejected": str(exc)})

    meta = body.pop("_meta")
    inner = body["beneficiary"]
    try:
        # validate() wraps its argument itself — pass the INNER beneficiary.
        beneficiaries.validate(inner, [meta["method"]])
    except ApiError as exc:
        return BeneficiaryOutcome(ok=False, status="FAILED",
                                  error=plain_error(exc), request=body,
                                  raw=getattr(exc, "payload", None) or {})
    except Exception as exc:                     # transport, DNS, timeout
        return BeneficiaryOutcome(ok=False, status="FAILED",
                                  error=f"Could not reach Airwallex: {exc}",
                                  request=body)

    try:
        created = beneficiaries.create(
            db_session, country=meta["country"], currency=meta["currency"],
            transfer_method=meta["method"], beneficiary=inner,
            request_id=request_id)
    except ApiError as exc:
        return BeneficiaryOutcome(ok=False, status="FAILED",
                                  error=plain_error(exc), request=body,
                                  raw=getattr(exc, "payload", None) or {})
    except Exception as exc:
        return BeneficiaryOutcome(ok=False, status="FAILED",
                                  error=f"Could not reach Airwallex: {exc}",
                                  request=body)

    bid = created.get("id") if isinstance(created, dict) else None
    if not bid:
        return BeneficiaryOutcome(
            ok=False, status="FAILED",
            error="Airwallex accepted the request but returned no beneficiary "
                  "id, so this supplier cannot be paid yet.",
            request=body, raw=created if isinstance(created, dict) else {})
    return BeneficiaryOutcome(
        ok=True, status=str(created.get("status") or "CREATED"),
        beneficiary_id=str(bid), request=body,
        raw=created if isinstance(created, dict) else {})


__all__ = ["BeneficiaryOutcome", "IBAN_COUNTRIES", "ROUTING_TYPE_BY_CCY",
           "build_payload", "plain_error", "validate_and_create"]
