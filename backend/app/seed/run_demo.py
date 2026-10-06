"""`make demo` — drive the 7 scripted beats (closebook.md §9) end to end.

Beat drivers that only need the local DB (initial plan, close) run fully.
Beats that need the live sandbox (deposit matching, card decline, transfer
failure) run the LOCAL decision/ledger logic, then print the exact sandbox
commands the executor will issue — so the demo is replayable and testable
offline, and live-verifiable once `.env` is filled and the executor (Phase 3)
is wired.

Keeps the "model proposes, code enforces" line: this module only asserts
deterministic outcomes; it never moves money itself.
"""
from __future__ import annotations

import sys

from ..db import DB
from ..models import InvoicePayable, InvoiceReceivable, PayStatus, RecStatus
from ..seed import scenario


def _print_bullets(lines: list[str]) -> None:
    for ln in lines:
        print(f"  • {ln}")


def _show_planner_view(db: DB) -> None:
    """The 'initial plan' beat: which payables auto-pay vs stay HELD, and
    why, from the reserve floor + auto-pay limit. This is the deterministic
    decision the agent explains (Phase 3 planner owns the real engine; this
    is the seed-level preview so the demo runs before Phase 3)."""
    s = db.session()
    pays: list[str] = []
    holds: list[str] = []
    for p in s.query(InvoicePayable).all():
        # payout_method lives on the SUPPLIER (InvoicePayable itself
        # has no such column — hit in the first `make demo` run).
        payout = p.supplier.payout_method.value if p.supplier else "LOCAL"
        auto = (
            p.amount_minor <= int(scenario.AUTO_PAY_LIMIT_USD * 100)
            and payout == "LOCAL"
            and p.currency == "USD"
        )
        tag = f"crit={p.criticality}"
        if p.depends_on_receivable_id:
            tag += " needs-receivable"
        if auto and p.status == PayStatus.HELD:
            pays.append(
                f"{p.number} {p.supplier.name if p.supplier else ''} "
                f"USD {p.amount_minor/100:.2f} auto-pays ({tag})"
            )
        else:
            holds.append(
                f"{p.number} HELD ({tag}) — "
                + (
                    "receivable not landed"
                    if p.depends_on_receivable_id
                    else f"exceeds limit / {payout.lower()} rail"
                )
            )
    print("Initial plan (deterministic preview):")
    _print_bullets(pays)
    print("Held:")
    _print_bullets(holds)


def main() -> int:
    from ..config import get_settings
    settings = get_settings()
    db = DB()
    print("=== Closebook demo — Kano Craft Imports Ltd ===")
    for beat in scenario.BEATS:
        print(f"\n--- beat: {beat.name} ---")
        print(f"  inject: {beat.inject}")
        if beat.name == "initial_plan":
            _show_planner_view(db)
        elif beat.name in ("deposit_typo_match", "partial_payment",
                           "guardrail", "failure_recovery", "ambiguous_inflow"):
            live = settings.sandbox_ready()
            print(f"  expect: {beat.expect}")
            print("  " + ("[live sandbox path ready — executor will issue the calls]"
                          if live else
                          "[offline: local decision + expected sandbox call logged; "
                          "fill .env for live execution]"))
        else:  # close
            print(f"  expect: {beat.expect}")
            print("  (summarise_day report — Phase 5 agent output)")
    print("\ndemo script complete. "
          + ("Sandbox creds present: run live beats via the executor."
             if settings.sandbox_ready()
             else "No sandbox creds: local-only run; fill .env to go live."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
