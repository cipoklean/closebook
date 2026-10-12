"""The card-create payload is ours to get right, so pin it.

Card issuance is blocked on the sandbox at the ACCOUNT level (see
`docs/AIRWALLEX_CARD_ISSUING_REQUEST.md` and the probe scripts), so these tests
cannot assert a card exists. They assert the request we send is the one Airwallex
Dev Help told us to send — that is the part under our control, and the part a
future edit could silently break.

A fake client captures the body instead of calling out, so the suite stays
hermetic (no key, no network) and passing here means nothing about the live
status of the account.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.airwallex.issuing import Issuing
from app.money import to_major_str


class CapturingClient:
    """Records the request instead of sending it."""

    def __init__(self, response=None):
        self.calls = []
        self.response = response if response is not None else {
            "id": "card_test_1", "card_status": "ACTIVE"}

    def call(self, method, path, json_body=None, request_id=None,
             is_simulation=False):
        self.calls.append({"method": method, "path": path,
                           "body": json_body, "request_id": request_id,
                           "is_simulation": is_simulation})
        return self.response

    @property
    def body(self) -> dict:
        return self.calls[-1]["body"]


def _issuing(response=None):
    client = CapturingClient(response)
    return Issuing(client), client


def _card(**kw):
    """A card request with the defaults the demo uses."""
    args = dict(cardholder_id="ch_1", currency="USD",
                per_transaction_limit_minor=700_000,
                all_time_limit_minor=700_000)
    args.update(kw)
    return args


class TestCardPayloadShape:
    """The exact shape Airwallex Dev Help specified."""

    def test_is_personalized_is_false(self):
        """A DELEGATE cardholder cannot hold a PERSONALIZED card. Sending
        is_personalized: true was the reported cause of the 400."""
        issuing, client = _issuing()
        issuing.create_card(object(), request_id="r1", **_card())
        assert client.body["is_personalized"] is False

    def test_issue_to_is_absent_entirely(self):
        """It is not required, and it forced the top-level `purpose` that in
        turn produced 'Purpose can only be set when issue_to is ORGANISATION'."""
        issuing, client = _issuing()
        issuing.create_card(object(), request_id="r1", **_card())
        assert "issue_to" not in client.body
        assert "purpose" not in client.body       # only inside `program`

    def test_program_carries_purpose_and_nothing_else(self):
        issuing, client = _issuing()
        issuing.create_card(object(), request_id="r1", **_card())
        assert client.body["program"] == {"purpose": "COMMERCIAL"}

    def test_cardholder_is_referenced_by_id(self):
        issuing, client = _issuing()
        issuing.create_card(object(), request_id="r1", **_card())
        assert client.body["cardholder_id"] == "ch_1"
        # primary_contact_details is rejected live; it must not creep back.
        assert "primary_contact_details" not in client.body

    def test_spend_controls_are_preserved(self):
        """The controls are the point: policy is enforced by the card, not by
        a prompt, so they must survive any payload edit."""
        issuing, client = _issuing()
        issuing.create_card(object(), request_id="r1", **_card())
        controls = client.body["authorization_controls"]
        assert controls["allowed_transaction_count"] == "MULTIPLE"
        limits = controls["transaction_limits"]
        assert limits["currency"] == "USD"
        intervals = {l["interval"]: l["amount"] for l in limits["limits"]}
        # `to_major_str` strips trailing zeros on purpose (Decimal.normalize()
        # would emit 1E+2, which the API's parser rejects), so 7000.00 is sent
        # as "7000". The value is right; only the formatting is terse.
        assert intervals["PER_TRANSACTION"] == "7000"
        assert intervals["ALL_TIME"] == "7000"
        assert Decimal(intervals["PER_TRANSACTION"]) == Decimal("7000.00")

    def test_the_all_time_limit_is_optional(self):
        issuing, client = _issuing()
        issuing.create_card(object(), request_id="r1",
                            **{**_card(), "all_time_limit_minor": None})
        limits = client.body["authorization_controls"]["transaction_limits"]
        assert [l["interval"] for l in limits["limits"]] == ["PER_TRANSACTION"]

    def test_optional_controls_are_omitted_when_not_asked_for(self):
        issuing, client = _issuing()
        issuing.create_card(object(), request_id="r1", **_card())
        for absent in ("card_number", "currency_allowlist",
                       "merchant_categories"):
            assert absent not in client.body

    def test_swift_style_limits_use_major_units(self):
        """Limits are major-unit strings; a minor-unit value would be 100x."""
        issuing, client = _issuing()
        issuing.create_card(object(), request_id="r1",
                            **{**_card(), "per_transaction_limit_minor": 700_001})
        amount = (client.body["authorization_controls"]
                  ["transaction_limits"]["limits"][0]["amount"])
        assert amount == "7000.01"
        assert to_major_str(700_001, "USD") == "7000.01"


class TestCardholderPayload:
    def test_cardholder_type_is_delegate(self):
        issuing, client = _issuing({"cardholder_id": "ch_1",
                                    "status": "READY", "type": "DELEGATE"})
        issuing.create_cardholder(object(), email="a@b.com",
                                  request_id="r1")
        assert client.body["type"] == "DELEGATE"
        assert client.body["email"] == "a@b.com"

    def test_the_response_id_field_is_cardholder_id(self):
        """The create response returns `cardholder_id`, not `id`. Reading the
        wrong key yielded None and produced 'cardholder_id must be provided'
        on the very next call."""
        issuing, _ = _issuing({"cardholder_id": "ch_live", "status": "READY"})
        r = issuing.create_cardholder(object(), email="a@b.com",
                                      request_id="r1")
        assert (r.get("cardholder_id") or r.get("id")) == "ch_live"


class TestNoAdminOnlyEndpoints:
    """These return 'Admin permissions required' on this account, so calling
    them wastes a round trip and logs an error for nothing."""

    def test_the_module_never_calls_issuing_programs_or_issuing_balances(self):
        import inspect
        src = inspect.getsource(Issuing)
        assert "/issuing/programs" not in src
        assert "/issuing/balances" not in src

    def test_the_repo_never_calls_them_anywhere(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / "backend"
        offenders = []
        for path in root.rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8")
            for needle in ('"/api/v1/issuing/programs"',
                           '"/api/v1/issuing/balances"'):
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        assert not offenders, offenders

    def test_balances_are_read_from_balances_current(self):
        import inspect
        from app.airwallex.balances import Balances
        assert "/api/v1/balances/current" in inspect.getsource(Balances)
