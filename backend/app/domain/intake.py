"""Money in, for real: a sandbox deposit becomes a matched receivable.

Before this module the ONLY code that created an `Inflow` was
`seed/live_beats.py` — the scripted demo. The matcher, the ledger and the
models were all complete and tested, but nothing in the running app could take a
real deposit and turn it into one. This is that path.

The chain, in order, every step of it re-verifiable:

    POST /simulation/deposit/create        (sandbox only, from the form)
      -> a DEPOSIT exists at Airwallex
      -> it arrives as a `deposit.*` webhook, or the poller finds it
      -> record_and_match() creates ONE Inflow, keyed to the deposit id
      -> matching.match_inflow() proposes
      -> ledger.apply_inflow_to_receivables() RE-CHECKS the confidence and posts

Two rules this module exists to keep:

1. **One deposit, one Inflow, one posting.** `inflow.airwallex_deposit_id` is
   UNIQUE and every entry point goes through `record_and_match`, so a webhook
   that arrives twice, or a webhook plus a poll, cannot post the cash twice. The
   fallback is not an alternative code path — it is the same function.
2. **The model never gets the last word.** The reference is passed through
   untouched (messy, blank, whatever was typed) and the allocation is re-derived
   in code by the ledger, which enforces the confidence band itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from ..models import Inflow, InvoiceReceivable, MatchStatus, RecStatus
from ..money import to_minor

log = logging.getLogger("closebook")

#: Global accounts Closebook will open for itself if the sandbox has none.
WANTED_ACCOUNTS = {"USD": ("US", "USD"), "GBP": ("GB", "GBP")}


@dataclass
class IntakeResult:
    deposit_id: str
    status: str                 # MATCHED | PARTIAL | OVERPAID | AMBIGUOUS | DUPLICATE | ERROR
    confidence: float = 0.0
    method: str = ""
    applied: list[str] = field(default_factory=list)
    reason: str = ""
    inflow_id: Optional[int] = None

    @property
    def duplicate(self) -> bool:
        return self.status == "DUPLICATE"


# ------------------------------------------------------------------ candidates
def candidates_from_db(session) -> list:
    """Every OPEN or PARTIAL receivable, as the matcher's candidate shape."""
    from .matching import Candidate

    out = []
    for inv in session.query(InvoiceReceivable).all():
        if inv.status in (RecStatus.PAID,):
            continue
        open_minor = max(0, inv.amount_minor - inv.amount_paid_minor)
        if open_minor <= 0:
            continue
        out.append(Candidate(
            invoice_id=inv.id,
            number=inv.number,
            customer_name=(inv.customer.name if inv.customer else ""),
            currency=inv.currency,
            amount_open_minor=open_minor,
            amount_original_minor=inv.amount_minor,
        ))
    return out


# --------------------------------------------------------------------- intake
def record_and_match(session, *, deposit_id: str, currency: str,
                     amount_minor: int, payer_name: str = "",
                     reference: str = "", llm=None) -> IntakeResult:
    """Create the Inflow (once), match it, and post the cash.

    Keyed to `deposit_id`. If that deposit is already in the book this returns
    DUPLICATE and touches NOTHING — which is what makes the webhook and the
    polling fallback safe to run side by side.
    """
    from .ledger import apply_inflow_to_receivables
    from .matching import match_inflow

    if not deposit_id:
        return IntakeResult(deposit_id="", status="ERROR",
                            reason="A deposit with no id cannot be de-duplicated, "
                                   "so it is not recorded.")

    existing = (session.query(Inflow)
                .filter_by(airwallex_deposit_id=str(deposit_id)).first())
    if existing is not None:
        return IntakeResult(
            deposit_id=str(deposit_id), status="DUPLICATE",
            inflow_id=existing.id,
            reason=(f"deposit {deposit_id} is already in the book as inflow "
                    f"{existing.id} ({existing.match_status.value}); "
                    f"nothing was posted twice."))

    inflow = Inflow(
        airwallex_deposit_id=str(deposit_id),
        currency=currency.upper(),
        amount_minor=int(amount_minor),
        payer_name=(payer_name or None),
        reference_raw=(reference or ""),
        match_status=MatchStatus.UNMATCHED,
    )
    session.add(inflow)
    session.flush()

    cands = candidates_from_db(session)
    result = match_inflow(
        inflow.amount_minor, inflow.currency,
        {"reference_raw": reference or "", "payer_name": payer_name or ""},
        cands, llm=llm,
    )

    # The ledger re-checks confidence and posts the cash. It is the ONLY writer
    # of a receivable's paid amount and status.
    matches = apply_inflow_to_receivables(session, inflow, result)

    # Record the verdict ON the inflow. Storing it means the Money-in, Needs
    # your OK and AI activity screens all read the SAME decision instead of
    # re-deriving one, and the reason survives the process that produced it.
    inflow.match_method = result.method
    inflow.match_confidence = float(result.confidence)
    inflow.match_reason = result.reason

    numbers = []
    for m in matches:
        inv = session.get(InvoiceReceivable, m.invoice_receivable_id)
        if inv is not None:
            numbers.append(f"{inv.number} ({inv.status.value})")
    session.flush()
    return IntakeResult(
        deposit_id=str(deposit_id),
        status=result.status,
        confidence=float(result.confidence),
        method=result.method,
        applied=numbers,
        reason=result.reason or "",
        inflow_id=inflow.id,
    )


def _deposit_fields(deposit: dict) -> dict:
    """Airwallex's deposit object -> the fields we store.

    `amount` comes back in MAJOR units as a number or a string; `to_minor`
    handles both, and the currency is the deposit's own (the simulation endpoint
    has no currency field — the money lands in the account's currency).
    """
    ccy = (deposit.get("currency") or "").upper()
    amount = deposit.get("amount")
    if not ccy or amount is None:
        return {}
    try:
        amount_minor = to_minor(amount, ccy)
    except Exception:
        try:
            amount_minor = int(float(amount) * 100)
        except Exception:
            return {}
    return {
        "deposit_id": str(deposit.get("id") or ""),
        "currency": ccy,
        "amount_minor": amount_minor,
        "payer_name": deposit.get("payer_name") or "",
        "reference": deposit.get("reference") or "",
        "status": (deposit.get("status") or "").upper(),
    }


def ingest_deposit(db, deposit: dict, *, llm=None) -> Optional[IntakeResult]:
    """Record one Airwallex deposit object. Safe to call on any deposit twice."""
    f = _deposit_fields(deposit)
    if not f or not f["deposit_id"]:
        return None
    # Only money that actually arrived. PENDING means Airwallex has it but has
    # not settled it, and posting cash we may not have is the failure mode this
    # whole module exists to avoid.
    if f["status"] and f["status"] not in ("SETTLED", "PENDING"):
        log.info("deposit %s is %s — not posting cash", f["deposit_id"], f["status"])
        return None
    with db.session_scope() as s:
        return record_and_match(
            s, deposit_id=f["deposit_id"], currency=f["currency"],
            amount_minor=f["amount_minor"], payer_name=f["payer_name"],
            reference=f["reference"], llm=llm)


def _created_at(deposit: dict):
    """The deposit's create_time as an aware UTC datetime, or None."""
    from datetime import datetime, timezone

    raw = deposit.get("create_time") or deposit.get("created_at")
    if not raw:
        return None
    try:
        txt = str(raw).replace("Z", "+00:00")
        # Airwallex returns things like 2017-03-22T16:08:02+1100 (no colon).
        if len(txt) >= 5 and txt[-5] in "+-" and txt[-4:].isdigit():
            txt = f"{txt[:-2]}:{txt[-2:]}"
        dt = datetime.fromisoformat(txt)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def poll_deposits(db, *, llm=None) -> list[IntakeResult]:
    """The polling FALLBACK for a webhook that is late or never arrives.

    Deliberately the same `record_and_match` the webhook path uses: a fallback
    that reimplements the logic is a second, untested way to post money.

    Skips anything older than the workspace's `deposits_from`. The sandbox
    account accumulates deposits from every previous run, and pulling all of
    them into a fresh workspace turned a brand-new book into USD 225,900 of
    suspense that had nothing to do with the invoices on screen — and applied
    6,000 of it to the only open GBP invoice, reading OVERPAID. History is an
    opening balance, not this workspace's money.
    """
    from ..airwallex.client import AirwallexClient
    from ..airwallex.simulation import Simulation
    from .settings_store import read as read_settings

    with db.session_scope() as s:
        floor = read_settings(s).deposits_from

    client = AirwallexClient()
    try:
        deposits = Simulation(client).list_deposits()
    except Exception as exc:
        log.warning("could not list deposits: %s", exc)
        return []

    out = []
    skipped = 0
    for d in deposits:
        created = _created_at(d) if isinstance(d, dict) else None
        if floor is not None and created is not None:
            want = floor if floor.tzinfo else floor.replace(tzinfo=created.tzinfo)
            if created < want:
                skipped += 1
                continue
        try:
            r = ingest_deposit(db, d, llm=llm)
        except Exception:
            log.exception("intake failed for deposit %s", d.get("id"))
            continue
        if r is not None and not r.duplicate:
            out.append(r)
    if skipped:
        log.info("poll skipped %d deposit(s) older than this workspace", skipped)
    return out


# ------------------------------------------------------------- the accounts
def _account_currency(item: dict) -> str:
    """The account's currency, from where Airwallex actually puts it.

    LIVE FINDING (2026-10-22): `GET /global_accounts` returns NO top-level
    `currency` field, even though the documented example shows one. The real
    location is inside `required_features[].currency` (the primary) or
    `supported_features[].currency`. Reading the documented field gave every
    account an empty currency, so the deposit form listed accounts with a blank
    label and could not tell USD from GBP.
    """
    for key in ("currency",):
        v = (item.get(key) or "").strip().upper()
        if v:
            return v
    for block in ("required_features", "supported_features"):
        feats = item.get(block) or []
        if isinstance(feats, list):
            for f in feats:
                if isinstance(f, dict):
                    v = (f.get("currency") or "").strip().upper()
                    if v:
                        return v
    return ""


def accounts(client, db, *, create_missing: bool = True) -> list[dict]:
    """The sandbox global accounts a deposit can land in.

    Lists first; only creates a USD or GBP account if there is none, because
    `GET /global_accounts` returning `{"items": [...]}` is the live-verified
    shape and a build that always creates would pile up accounts on every run.
    """
    from ..airwallex.global_accounts import GlobalAccounts

    ga = GlobalAccounts(client)
    items = ga.list() or []
    have = {_account_currency(i) for i in items if isinstance(i, dict)}

    if create_missing:
        for ccy, (country, _) in WANTED_ACCOUNTS.items():
            if ccy in have:
                continue
            try:
                created = ga.create(db.session(), country_code=country,
                                    currency=ccy)
                if isinstance(created, dict):
                    items.append(created)
                    have.add(ccy)
                    forget_accounts_cache()
                    log.info("created a %s global account: %s", ccy,
                             created.get("id"))
            except Exception as exc:
                log.warning("could not create a %s global account: %s", ccy, exc)

    out = []
    for i in items:
        if not isinstance(i, dict) or not i.get("id"):
            continue
        ccy = _account_currency(i)
        out.append({
            "id": i.get("id"),
            "currency": ccy,
            "country": (i.get("country_code") or "").upper(),
            "status": i.get("status") or "",
            "name": i.get("account_name") or i.get("nick_name") or "",
            "account_number": i.get("account_number") or "",
            # Only ACTIVE accounts can receive a deposit; others are shown but
            # labelled, so an operator is not surprised by a refusal.
            "usable": (i.get("status") or "").upper() == "ACTIVE",
        })
    out.sort(key=lambda a: (not a["usable"], a["currency"] not in ("USD", "GBP"),
                            a["currency"]))
    return out


def simulate_deposit(client, *, global_account_id: str, amount_minor: int,
                     currency: str, payer_name: str = "",
                     reference: str = "") -> dict:
    """Create a sandbox deposit. Refuses to run against anything but the sandbox.

    The guard is here as well as on the route: the button is one caller, and a
    money-shaped simulation must not depend on one caller remembering.
    """
    from ..airwallex.client import AirwallexClient  # noqa: F401  (type only)
    from ..airwallex.simulation import Simulation
    from ..config import get_settings

    base = get_settings().awx_base_url or ""
    if "sandbox" not in base:
        raise RuntimeError(
            f"Refused: {base} is not a sandbox host. Simulated deposits only "
            f"ever run against the Airwallex sandbox.")

    return Simulation(client).deposit(
        global_account_id=global_account_id,
        amount_minor=int(amount_minor),
        currency=currency.upper(),
        payer_name=payer_name or None,
        reference=reference or None,
    )


__all__ = ["IntakeResult", "WANTED_ACCOUNTS", "accounts", "candidates_from_db",
           "ingest_deposit", "poll_deposits", "record_and_match", "simulate_deposit"]


# ------------------------------------------------------- the account list, cached
_ACCOUNTS_CACHE: dict = {"at": 0.0, "items": []}


def accounts_cached(db, *, ttl: float = 60.0) -> list[dict]:
    """`accounts()` behind a short TTL cache.

    The deposit form shows the global accounts, and rendering it must not make a
    live API call on every page view — a dashboard that blocks on Airwallex is a
    dashboard that shows nothing when Airwallex is slow. Cached for a minute;
    a failure returns the last known list rather than an empty one, and says so
    through the returned list being empty only on a genuinely cold failure.
    """
    import time as _time

    now = _time.time()
    if _ACCOUNTS_CACHE["items"] and now - _ACCOUNTS_CACHE["at"] < ttl:
        return _ACCOUNTS_CACHE["items"]
    try:
        from ..airwallex.client import AirwallexClient
        items = accounts(AirwallexClient(), db)
    except Exception as exc:
        log.warning("could not list global accounts: %s", exc)
        return _ACCOUNTS_CACHE["items"]
    if items:
        _ACCOUNTS_CACHE["items"] = items
        _ACCOUNTS_CACHE["at"] = now
    return items


def forget_accounts_cache() -> None:
    """Called after creating an account so the next render sees it."""
    _ACCOUNTS_CACHE["at"] = 0.0
    _ACCOUNTS_CACHE["items"] = []
