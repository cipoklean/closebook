"""Global accounts (sandbox).

Note: creating global accounts is REST-only — the Developer MCP cannot do it.
EUR accounts use country_code NL/DE/FR (BE is rejected); it is the account's
country, not the currency.
"""
from __future__ import annotations

from typing import Optional

from .client import AirwallexClient, ApiError, _reserve_request_id


class GlobalAccounts:
    def __init__(self, client: AirwallexClient):
        self._c = client

    def list(self) -> list[dict]:
        data = self._c.call("GET", "/api/v1/global_accounts")
        return data.get("data", data.get("global_accounts", [])) or []

    def get(self, account_id: str) -> dict:
        return self._c.call("GET", f"/api/v1/global_accounts/{account_id}")

    def create(self, db_session, *, country_code: str, currency: str,
               transfer_method: str = "LOCAL", request_id: Optional[str] = None,
               plan_step=None) -> dict:
        rid = request_id or (_reserve_request_id(db_session, plan_step) if plan_step is not None
                            else AirwallexClient.new_request_id())
        body = {
            "request_id": rid,
            "country_code": country_code,
            "required_features": [{"currency": currency, "transfer_method": transfer_method}],
        }
        return self._c.call("POST", "/api/v1/global_accounts/create", json_body=body,
                            request_id=None, is_simulation=False)


__all__ = ["GlobalAccounts"]
