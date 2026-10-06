"""Money handling at the Airwallex API boundary.

Airwallex amounts are **major units** ("100" = one hundred dollars), never
cents. Internally Closebook stores and computes in **integer minor units**
and converts only when building/parsing API payloads. This module owns that
conversion so a bug here cannot silently scale a payment.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

ZERO = Decimal("0")

# ISO 4217 exponent. Only a subset is needed for Closebook's corridors, but
# the table is the source of truth. CNY is special-cased: 1 CNY = 10 jiao =
# 100 fen (exponent 2), so it is NOT "exponent 1" like KPW or UYI.
CURRENCY_EXPONENT: dict[str, int] = {
    "USD": 2, "EUR": 2, "GBP": 2, "CAD": 2, "AUD": 2, "NZD": 2,
    "JPY": 0, "HKD": 2, "SGD": 2, "CNY": 2, "INR": 2, "ZAR": 2,
}


def minor_for(currency: str) -> int:
    """Number of minor units per major unit (100 for USD, 1 for JPY)."""
    try:
        return 10 ** CURRENCY_EXPONENT[currency.upper()]
    except KeyError as exc:
        raise ValueError(f"unsupported currency: {currency}") from exc


def to_major(amount_minor: int, currency: str) -> Decimal:
    """Integer minor units -> Decimal major units, as Airwallex expects."""
    return Decimal(amount_minor) / minor_for(currency)


def to_major_str(amount_minor: int, currency: str) -> str:
    """API-ready string in major units (no trailing-zero surprises)."""
    return str(to_major(amount_minor, currency).normalize())


def to_minor(amount_major: Decimal | str, currency: str) -> int:
    """Decimal/string major units -> integer minor units (banker-safe HALF_UP)."""
    d = Decimal(str(amount_major))
    factor = Decimal(minor_for(currency))
    return int((d * factor).to_integral_value(rounding=ROUND_HALF_UP))


def check_supported(currency: str) -> None:
    minor_for(currency)


__all__ = ["CURRENCY_EXPONENT", "minor_for", "to_major", "to_major_str", "to_minor", "check_supported", "ZERO"]
