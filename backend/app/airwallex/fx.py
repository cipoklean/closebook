"""FX: rates, quotes, conversions.

Hard rules (closebook.md §5.4/§6):
- GET /fx/rates/current -> read `rate` (indicative).
- POST /fx/quotes/create -> read `client_rate` and `quote_id`. A QUOTE is
  single-use: book it once with one request_id; fetch a fresh quote to
  convert again. `client_rate` lives on the quote, NOT on a rate check.
- POST /fx/conversions/create -> REST only (no MCP tool). Omit `x-api-version`
  on ALL FX calls (Airwallex returns incorrect_version for a stale value).
- Conversions settle immediately (status SETTLED, no pending state).
- Insufficient funds on FX surfaces as `insufficient_funds` (transfers use
  `balance_insufficient`).

All amounts in this module are passed as integer minor units; conversion to
major-unit API strings happens at the single boundary in client.call().
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .client import AirwallexClient, _reserve_request_id


@dataclass
class Quote:
    quote_id: str
    client_rate: Decimal
    buy_currency: str
    sell_currency: str


class FX:
    def __init__(self, client: AirwallexClient):
        self._c = client

    def current_rate(self, base: str, quote_ccy: str) -> Decimal:
        """Indicative rate.

        Verified live 2026-10-06: the query params are `buy_currency` and
        `sell_currency`. Passing base/quote returns
        400 invalid_argument "'buy_currency' is not specified".
        """
        data = self._c.call(
            "GET", "/api/v1/fx/rates/current",
            query={"buy_currency": quote_ccy, "sell_currency": base},
        )
        # Shape is unverified against the live API yet (the 400 above masked
        # it); accept {"rate": ...}, {"items": [{"rate": ...}]}, {"data": ...}.
        d = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(d, dict):
            rate = d.get("rate")
            if rate is None and isinstance(d.get("items"), list) and d["items"]:
                rate = d["items"][0].get("rate")
            if rate is None:
                raise ValueError("no rate field in fx response: " + repr(data)[:200])
            return Decimal(str(rate))
        if isinstance(d, list) and d:
            return Decimal(str(d[0].get("rate")))
        raise ValueError("unexpected fx rates shape: " + repr(data)[:200])

    def create_quote(self, *, sell_currency: str, buy_currency: str,
                    sell_amount_minor: Optional[int] = None,
                    buy_amount_minor: Optional[int] = None,
                    sell_amount_major: Optional[Decimal] = None) -> Quote:
        body: dict = {"sell_currency": sell_currency, "buy_currency": buy_currency}
        # Verified live 2026-10-06 via the Airwallex docs MCP: `validity` is a
        # REQUIRED enum, not a duration string. Allowed: MIN_1, MIN_15, MIN_30,
        # HR_1, HR_4, HR_8, HR_24. HR_1 is the default for most flows; we use
        # HR_1 because a quote is SINGLE_USE and must be booked promptly.
        body["validity"] = "HR_1"
        amounts: dict[str, tuple[int, str]] = {}
        if sell_amount_minor is not None:
            body["sell_amount"] = "placeholder"  # replaced at the money boundary
            amounts["sell_amount"] = (sell_amount_minor, sell_currency)
        if buy_amount_minor is not None:
            body["buy_amount"] = "placeholder"
            amounts["buy_amount"] = (buy_amount_minor, buy_currency)
        data = self._c.call("POST", "/api/v1/fx/quotes/create", json_body=body,
                            money_amounts_minor=amounts)
        # Verified live 2026-10-06 (docs MCP + 400-error probing): the quote
        # response is a BARE object with `id`, `awx_rate`, `buy_amount`,
        # `sell_amount`, `usage` (SINGLE_USE/MULTI_USE) and `valid_to_at`.
        # The builder guide's "client_rate" is the *developer MCP* field name;
        # REST exposes awx_rate. Accept either.
        d = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
        rate = None
        for key in ("client_rate", "awx_rate", "rate"):
            if isinstance(d, dict) and d.get(key) is not None:
                rate = Decimal(str(d[key]))
                break
        if rate is None:
            raise ValueError("no rate field in quote response: " + repr(data)[:200])
        return Quote(
            quote_id=(d.get("id") or d.get("quote_id")) if isinstance(d, dict) else None,
            client_rate=rate,
            buy_currency=buy_currency,
            sell_currency=sell_currency,
        )

    def create_conversion(self, db_session, *, sell_currency: str, buy_currency: str,
                         sell_amount_minor: int, quote_id: Optional[str] = None,
                         request_id: Optional[str] = None, plan_step=None) -> dict:
        """Book a conversion. Single-use quote; omit x-api-version."""
        rid = request_id or _reserve_request_id(db_session, plan_step)
        body: dict = {
            "sell_currency": sell_currency,
            "buy_currency": buy_currency,
            "sell_amount": "placeholder",
        }
        if quote_id:
            body["quote_id"] = quote_id
        amounts = {"sell_amount": (sell_amount_minor, sell_currency)}
        return self._c.call("POST", "/api/v1/fx/conversions/create", json_body=body,
                            money_amounts_minor=amounts, request_id=rid, is_simulation=False)


__all__ = ["FX", "Quote"]
