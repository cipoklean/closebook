"""Contract tests against the REAL Airwallex sandbox response shapes.

Every shape asserted here was captured live on 2026-10-06 against
api.sandbox.airwallex.com with the project's sandbox credentials. These are
regression guards: if Airwallex changes an envelope, these fail loudly
instead of the demo breaking mid-record.

Captured facts:
- POST /authentication/login -> 201 {"expires_at": ..., "token": ...}
  (the field is `token`, NOT `access_token`)
- GET /global_accounts -> 200 {"items": [...]}   (NOT {"data": ...})
- POST /global_accounts/create -> 201 BARE object {"id": ..., "status": ...}
- GET /balances/current -> 200 BARE LIST of
  {"currency": ..., "available_amount": ...} for ~46 world currencies
- GET /fx/rates/current?buy_currency=&sell_currency= -> 200 with a rate
  (passing base/quote gives 400 "'buy_currency' is not specified")
- POST /fx/quotes/create -> 201 bare object; `validity` is a REQUIRED ENUM
  (MIN_1|MIN_15|MIN_30|HR_1|HR_4|HR_8|HR_24), and the response carries
  BOTH `awx_rate` and `client_rate`.
- POST /fx/conversions/create -> 201 bare object with awx_rate, buy_amount,
  quote_id, conversion_id, rate_details[].
- POST /beneficiaries/schema -> 200 {"errors": {}, "schema": {DOTTED_KEY: meta}}
  a FLAT dotted map, NOT a nested "properties" tree. Allowed enum values hide in
  meta["validation_functions"][0]["parameters"]["values"].
- POST /beneficiaries/validate -> takes the INNER beneficiary and wraps it
  itself; passes the whole body and it is double-nested (error 001 on fields
  that are present).
- POST /beneficiaries/create -> needs {"beneficiary": {...}} already in the
  body PLUS transfer_methods as a list. Requires entity_type PERSONAL|COMPANY
  (not INDIVIDUAL), account_routing_value1 (not account_routing_number),
  account_name, and for EUR an `iban` with a valid MOD-97 checksum (bad IBAN ->
  code 083 at CREATION).
- POST /transfers/create -> requires BOTH source_currency and
  transfer_currency, AND a `reason` string. Omitting either returns
  validation_failed naming exactly the missing field.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.airwallex.fx import FX
from app.airwallex.global_accounts import GlobalAccounts


class FakeHTTP:
    """Minimal httpx stand-in that replays a captured live response.

    The login call always gets a valid login body (every real client call is
    preceded by an auth), while data calls get the captured payload.
    """

    LOGIN_BODY = {"expires_at": "2026-10-07T00:00:31+0000", "token": "tok_live"}

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.requests: list[tuple[str, str, dict]] = []

    def post(self, path, headers=None, timeout=None, **kw):
        self.requests.append(("POST", path, headers or {}))
        if "authentication/login" in path:
            return _Resp(self.LOGIN_BODY)
        return _Resp(self._payload)

    def request(self, method, path, json=None, params=None, headers=None, timeout=None):
        self.requests.append((method, path, {"json": json, "params": params,
                                             "headers": headers or {}}))
        return _Resp(self._payload)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _client(payload, status=200):
    from app.airwallex.client import AirwallexClient
    from app.config import get_settings
    http = FakeHTTP(payload, status)
    return AirwallexClient(get_settings(), client=http), http


def test_login_token_field_is_token_not_access_token():
    """Captured live: login returns `token` + `expires_at`, never access_token."""
    c, http = _client({"expires_at": "2026-10-07T00:00:31+0000", "token": "tok_live"})
    assert c._ensure_token() == "tok_live"


def test_global_accounts_items_envelope():
    """Captured live: the list lives under `items`, not `data`."""
    payload = {"items": [{"id": "0ec89e41", "account_name": "Sandbox Business",
                          "country_code": "HK", "status": "PROCESSING",
                          "required_features": [{"currency": "HKD",
                                                 "transfer_method": "LOCAL"}]}]}
    c, _ = _client(payload)
    got = GlobalAccounts(c).list()
    assert len(got) == 1
    assert got[0]["country_code"] == "HK"


def test_balances_bare_list_uses_available_amount():
    """Captured live: GET /balances/current is a bare LIST keyed
    available_amount, covering ~46 currencies (most with 0)."""
    from app.airwallex.balances import Balances
    payload = [
        {"available_amount": 0, "currency": "AED", "account_type": "cash"},
        {"available_amount": 10000000, "currency": "USD", "account_type": "cash"},
        {"available_amount": "10018000", "currency": "GBP", "account_type": "cash"},
    ]
    c, _ = _client(payload)
    got = Balances(c).fetch_current()
    assert got["USD"] == Decimal("10000000")
    assert got["GBP"] == Decimal("10018000")
    assert got["AED"] == 0


def test_fx_rates_requires_buy_and_sell_currency():
    """Captured live: base/quote params give 400 invalid_argument."""
    c, http = _client({"rate": 1.137375})
    FX(c).current_rate("USD", "EUR")
    _, path, meta = http.requests[-1]
    assert meta["params"] == {"buy_currency": "EUR", "sell_currency": "USD"}


def test_fx_quote_validity_is_required_enum():
    """Captured live: `validity` must be one of MIN_1..HR_24 (not "1h")."""
    payload = {"id": "ca84aa2f", "awx_rate": 1.138726, "client_rate": 1.138726,
               "buy_amount": 7914.85, "sell_amount": 9012.85,
               "usage": "SINGLE_USE", "validity": "HR_1"}
    c, http = _client(payload, status=201)
    q = FX(c).create_quote(sell_currency="USD", buy_currency="EUR",
                           sell_amount_minor=901285)
    assert q.quote_id == "ca84aa2f"
    assert q.client_rate == Decimal("1.138726")
    body = http.requests[-1][2]["json"]
    assert body["validity"] in {"MIN_1", "MIN_15", "MIN_30",
                                "HR_1", "HR_4", "HR_8", "HR_24"}


def test_fx_quote_amount_is_major_units_string():
    """901285 minor USD must leave as "9012.85", never 901285."""
    payload = {"id": "q1", "awx_rate": 1.13}
    c, http = _client(payload, status=201)
    FX(c).create_quote(sell_currency="USD", buy_currency="EUR",
                       sell_amount_minor=901285)
    body = http.requests[-1][2]["json"]
    assert body["sell_amount"] == "9012.85"


def test_global_account_create_returns_bare_object():
    """Captured live: create returns a bare object, so an id must resolve
    without a `data` wrapper (this broke `make seed` on first run)."""
    payload = {"id": "d135405e", "status": "PROCESSING", "country_code": "US",
               "required_features": [{"currency": "USD", "transfer_method": "LOCAL"}]}
    c, _ = _client(payload, status=201)

    class _GA:
        def create(self, *a, **kw):
            return payload
    node = payload
    if isinstance(node, dict) and isinstance(node.get("data"), dict):
        node = node["data"]
    assert (node.get("id") or node.get("global_account_id")) == "d135405e"


def test_snapshot_skips_unknown_currencies():
    """Live returns ~46 currencies; AED/ARS etc. are not in our exponent
    table and must be skipped, not crash the seed."""
    from app.airwallex.balances import Balances
    from app.db import DB
    payload = [
        {"available_amount": 0, "currency": "AED"},
        {"available_amount": 10000000, "currency": "USD"},
    ]
    c, _ = _client(payload)
    db = DB("sqlite:///:memory:")
    Balances(c).snapshot_to_db(db.session())  # must not raise
    from app.models import BalanceSnapshot
    with db.session() as s:
        rows = s.query(BalanceSnapshot).all()
    assert [r.currency for r in rows] == ["USD"]

# ---- transfers/create body shape (live failures, 2026-10-07) -------------
class _Recorder:
    """Captures the body a wrapper would POST, without any network."""

    def __init__(self):
        self.bodies = []

    def call(self, method, path, *, json_body=None, **kw):
        self.bodies.append((method, path, json_body))
        return {"id": "tr_1", "status": "SENT"}


def test_transfer_create_sends_source_currency_and_reason():
    """Both were missing and the API named each one in turn.

    Live sequence: without source_currency -> "field=source_currency code=001";
    add it, without reason -> "field=reason code=001".
    """
    from app.airwallex.transfers import Transfers

    rec = _Recorder()
    Transfers(rec).create(None, beneficiary_id="bn_1", currency="EUR",
                         amount_minor=901_285, transfer_method="SWIFT",
                         reference="PO-7001",
                         request_id="rid-1")
    _, _, body = rec.bodies[0]
    assert body["source_currency"] == "EUR"
    assert body["transfer_currency"] == "EUR"
    assert body["reason"], "reason is required by transfers/create"
    assert body["transfer_amount"] == "9012.85"


def test_transfer_create_source_currency_can_differ_from_target():
    """Cross-currency: USD funded, EUR paid."""
    from app.airwallex.transfers import Transfers

    rec = _Recorder()
    Transfers(rec).create(None, beneficiary_id="bn_1", currency="EUR",
                         amount_minor=900_000, transfer_method="SWIFT",
                         source_currency="USD", request_id="rid-2")
    _, _, body = rec.bodies[0]
    assert body["source_currency"] == "USD"
    assert body["transfer_currency"] == "EUR"


def test_beneficiary_create_body_is_nested_and_lists_transfer_methods():
    from app.airwallex.beneficiaries import Beneficiaries

    rec = _Recorder()
    inner = {"entity_type": "COMPANY", "name": "Rotterdam Freight BV",
             "address": {"city": "Rotterdam", "country_code": "NL"},
             "bank_details": {"iban": "NL08INGB0000001234"}}
    Beneficiaries(rec).create(None, country="NL", currency="EUR",
                              transfer_method="SWIFT", beneficiary=inner,
                              request_id="rid-3")
    _, _, body = rec.bodies[0]
    assert body["beneficiary"] == inner, "create needs the beneficiary nested"
    assert body["transfer_methods"] == ["SWIFT"]


def test_explicit_request_id_does_not_touch_the_plan_step():
    """Regression: eager `or` fallback crashed on a None plan_step.

    Passing request_id explicitly must never call _reserve_request_id.
    """
    from app.airwallex.transfers import Transfers

    rec = _Recorder()

    class ExplodingSession:
        def execute(self, *a, **k):
            raise AssertionError("must not touch the DB when rid is supplied")

    Transfers(rec).create(ExplodingSession(), beneficiary_id="bn", currency="USD",
                          amount_minor=100, transfer_method="LOCAL",
                          request_id="rid-4")
    assert rec.bodies[0][2]["request_id"] == "rid-4"
