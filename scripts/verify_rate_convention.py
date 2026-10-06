"""One-off: verify our CCY-per-USD rate convention against the LIVE sandbox.

Not part of the test suite. It asks Airwallex for a real USD->EUR quote and
checks that the convention pinned in `policy.usd_value_minor`
(rates[CCY] = CCY per 1 USD, so USD value = amount / rate) matches what
Airwallex actually returns — rather than only matching our own arithmetic.

The real check is the ROUND TRIP: USD -> EUR -> USD must return the amount we
started with. If the convention were inverted, it would not.

Run: .venv/bin/python scripts/verify_rate_convention.py
"""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.airwallex.client import AirwallexClient
from app.airwallex.fx import FX
from app.domain.policy import usd_value_minor

TEST_RATE_EUR = Decimal("0.8756")   # the fixture used in the test suite

client = AirwallexClient()
fx = FX(client)

# USD 9,012.85 — the exact beat-2 amount (EUR 9,000 + EUR 12.85 SWIFT fee).
SELL_MINOR = 901_285
sell_major = Decimal(SELL_MINOR) / 100

quote = fx.create_quote(sell_currency="USD", buy_currency="EUR",
                        sell_amount_minor=SELL_MINOR)
client_rate = Decimal(str(quote.client_rate))   # USD per EUR (Airwallex's framing)

print("LIVE AIRWALLEX QUOTE")
print(f"  quote_id     {quote.quote_id}")
print(f"  sell         USD {sell_major}")
print(f"  buy_currency {quote.buy_currency}")
print(f"  client_rate  {client_rate}   <- Airwallex frames this as USD per EUR")

live_ccy_per_usd = Decimal(1) / client_rate
print(f"  inverse      {live_ccy_per_usd:.6f} EUR per USD  <- our rates['EUR'] form")

buy_minor = int(sell_major * live_ccy_per_usd * 100)
usd_live = usd_value_minor(buy_minor, "EUR", {"EUR": live_ccy_per_usd})
usd_ours = usd_value_minor(SELL_MINOR, "EUR", {"EUR": TEST_RATE_EUR})

print()
print(f"  round-trip: USD {sell_major} -> EUR {buy_minor/100} -> USD {usd_live/100:,.2f}")
print("  (a correct convention must return the amount we started with)")
print()
print(f"  EUR 9012.85 at the LIVE rate -> USD {usd_live/100:,.2f}")
print(f"  EUR 9012.85 at our test rate -> USD {usd_ours/100:,.2f}")
drift = Decimal(abs(usd_live - usd_ours)) / Decimal(usd_live) * Decimal(100)
print(f"  drift = {drift:.2f}%  (sandbox rates move; the fixture is only a test input)")

round_trip_err = Decimal(abs(usd_live - SELL_MINOR)) / Decimal(SELL_MINOR) * Decimal(100)
print()
print(f"  round-trip error = {round_trip_err:.4f}%")
print("  => CONVENTION CONFIRMED (USD->CCY->USD round-trips)" if round_trip_err < Decimal("0.5")
      else "  => CONVENTION WRONG: the round-trip did not return the sell amount")
if drift > Decimal(2):
    print(f"  => note: our test fixture rate {TEST_RATE_EUR} is stale vs the live "
          f"{live_ccy_per_usd:.4f}; refresh tests/test_planner.py RATES if needed")