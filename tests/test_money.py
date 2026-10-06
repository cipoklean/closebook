"""Unit tests for the money boundary (closebook.md §1.3 + §5 required test).

Covers the exact conversion cases the spec calls out:
- USD major/minor round-trip ($100.00 -> 10000 minor -> back)
- JPY exponent 0 (1 JPY == 1 minor unit, no fractional)
- CNY exponent 2 (1 CNY == 100 fen) — NOT exponent 1
- A round-trip property: to_minor(to_major(x)) == x for every currency we use
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.money import (
    CURRENCY_EXPONENT, minor_for, to_major, to_major_str, to_minor,
)


def test_minor_factor_usd():
    assert minor_for("USD") == 100


def test_minor_factor_jpy_zero_exponent():
    # 1 JPY is the smallest unit; no subunits
    assert minor_for("JPY") == 1


def test_minor_factor_cny_is_two_not_one():
    # 1 CNY = 10 jiao = 100 fen
    assert minor_for("CNY") == 100


def test_to_major_usd_hundred_dollars():
    # 10000 minor == $100.00
    assert to_major(10000, "USD") == Decimal("100")


def test_to_major_str_usd():
    assert to_major_str(10000, "USD") == "100"


def test_to_minor_from_major_string():
    assert to_minor("100.00", "USD") == 10000
    assert to_minor(Decimal("0.99"), "USD") == 99


def test_to_minor_jpy_identity():
    assert to_minor("500", "JPY") == 500
    assert to_major(500, "JPY") == Decimal("500")


def test_to_minor_cny():
    assert to_minor("1.25", "CNY") == 125


def test_round_trip_property():
    for ccy, minor in [
        ("USD", 10000), ("EUR", 900000), ("GBP", 600000),
        ("JPY", 12345), ("CNY", 70000),
    ]:
        assert to_minor(str(to_major(minor, ccy)), ccy) == minor


def test_unsupported_currency_raises():
    with pytest.raises(ValueError):
        minor_for("KWD")


def test_currency_table_has_expected_entries():
    for c in ("USD", "EUR", "GBP", "JPY", "CNY"):
        assert c in CURRENCY_EXPONENT
