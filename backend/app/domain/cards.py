"""Cards as a control the CODE enforces, and the record of what the bank said.

The product's whole claim is that a spend limit belongs in something that can
refuse, not in a prompt. A virtual card is the only part of Closebook where the
refusal happens outside our process: Airwallex declines the authorisation and
the money does not move.

Beat 4 used to report CLOSEBOOK'S OWN VERDICT, because the sandbox account is
registered in Nigeria and could not issue a card at all. Airwallex confirmed the
cause was the business country and opened a second sandbox in Hong Kong, so the
beat can now be a real decline with the bank's own reason.

THE RULE THIS MODULE EXISTS FOR: a verdict is not evidence. `caveat_wanted()`
stays True until a decision that is BOTH live and a decline has been recorded,
so the "policy verdict" caveat cannot be quietly retired by a code-only check
that never reached the bank.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Optional


@contextmanager
def _session(obj):
    """Work with a Session or with a DB.

    Both arrive at this module: a request has a session in hand, and a script
    has a DB. Making the caller unwrap one of them is how two call sites end up
    disagreeing about which row holds the policy.
    """
    if hasattr(obj, "session_scope"):
        with obj.session_scope() as s:
            yield s
    else:
        yield obj

from ..money import money_str

#: How many decisions are kept. Enough for a demo beat and a restart, small
#: enough that the row stays a page fragment rather than a log.
KEEP = 10


def _load(row) -> list[dict]:
    raw = getattr(row, "card_decisions_json", None)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)]


def decisions(session) -> list[dict]:
    """Newest first. Reads the ONE row the app already keeps its policy in."""
    from .settings_store import get_or_create

    with _session(session) as s:
        return _load(get_or_create(s))


def record_decision(session, decision: dict) -> dict:
    """Append a decision, newest first, and return it."""
    from .settings_store import get_or_create

    with _session(session) as s:
        row = get_or_create(s)
        items = _load(row)
        items.insert(0, decision)
        row.card_decisions_json = json.dumps(items[:KEEP])
        s.add(row)
        s.commit()
    return decision


def policy_limits(session) -> tuple[int, int, str]:
    """(per transaction, all time, currency) from the operator's own policy.

    The per-transaction limit IS the auto-pay limit: the amount Closebook may
    pay without asking is the amount a card may spend without asking. One
    number, one meaning, and a change in Settings moves both.
    """
    from .settings_store import get_or_create

    with _session(session) as s:
        row = get_or_create(s)
    limit = int(row.auto_pay_limit_minor)
    # ALL_TIME is four times the per-transaction limit: one number from policy,
    # multiplied, rather than a second policy invented in a card module. It
    # exists so a card that is never rotated cannot spend without bound.
    all_time = limit * 4
    return limit, all_time, str(row.budget_currency)


def live_decline(session) -> Optional[dict]:
    """The most recent decision that was BOTH live and a decline, if any."""
    for d in decisions(session):
        if d.get("live") and str(d.get("result") or "").lower().startswith("declin"):
            return d
    return None


def caveat_wanted(session) -> bool:
    """Should beat 4 still say "policy verdict, not a live decline"?"""
    return live_decline(session) is None


def caveat_sentence(session) -> str:
    """What beat 4 says when there is nothing live to point at."""
    if not caveat_wanted(session):
        d = live_decline(session) or {}
        amount = money_str(int(d.get("amount_minor") or 0),
                           str(d.get("currency") or "USD"))
        return (f"LIVE DECLINE from Airwallex: {amount} was refused by the card "
                f"with reason {d.get('bank_reason') or 'unspecified'}.")
    return ("POLICY VERDICT only: no card has been authorised on this "
            "workspace, so this is Closebook's own arithmetic and not a bank "
            "decline.")
