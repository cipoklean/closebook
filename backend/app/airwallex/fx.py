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
        data = self._c.call(
            "GET", "/api/v1/fx/rates/current", query={"base": base, "quote": quote_ccy}
        )
        # defensive: shape may be {"data":{"rate":...}} or {"data":[{"rate":...}]}
        d = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(d, dict):
            return Decimal(str(d.get("rate")))
        if isinstance(d, list) and d:
            return Decimal(str(d[0].get("rate")))
        raise ValueError("unexpected fx rates shape: " + repr(data))

    def create_quote(self, *, sell_currency: str, buy_currency: str,
                    sell_amount_minor: Optional[int] = None,
                    buy_amount_minor: Optional[int] = None,
                    sell_amount_major: Optional[Decimal] = None) -> Quote:
        body: dict = {"sell_currency": sell_currency, "buy_currency": buy_currency}
        amounts: dict[str, tuple[int, str]] = {}
        if sell_amount_minor is not None:
            body["sell_amount"] = "placeholder"  # replaced at the money boundary
            amounts["sell_amount"] = (sell_amount_minor, sell_currency)
        if buy_amount_minor is not None:
            body["buy_amount"] = "placeholder"
            amounts["buy_amount"] = (buy_amount_minor, buy_currency)
        data = self._c.call("POST", "/api/v1/fx/quotes/create", json_body=body,
                            money_amounts_minor=amounts)
        d = data.get("data", data) if isinstance(data, dict) else data
        return Quote(
            quote_id=d.get("id") or d.get("quote_id"),
            client_rate=Decimal(str(d.get("client_rate", "0"))),
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
