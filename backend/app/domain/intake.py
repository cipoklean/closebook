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

from dataclasses import replace

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
    #: What the MATCHER read: MATCHED | PARTIAL | OVERPAID | PROPOSED | AMBIGUOUS
    #: | DUPLICATE | ERROR. This is a reading, not an outcome.
    status: str
    confidence: float = 0.0
    method: str = ""
    applied: list[str] = field(default_factory=list)
    reason: str = ""
    inflow_id: Optional[int] = None
    #: What HAPPENED: APPLIED (the ledger moved the invoice), PROPOSED (stored
    #: and waiting for a human), HELD (nothing usable), DUPLICATE, ERROR.
    #: Reported separately because a PARTIAL reading that was NOT applied is a
    #: completely different state from one that was, and the two used to share
    #: a single field.
    disposition: str = ""
    review_state: str = ""

    @property
    def duplicate(self) -> bool:
        return self.status == "DUPLICATE"

    @property
    def proposed(self) -> bool:
        return self.disposition == "PROPOSED"


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


def _reference_invoices(session, reference: str, currency: str):
    """Invoices the reference NAMES, split into (settled, open).

    Same word-safe containment rule as the matcher, applied to every invoice,
    so a reference quoting two bills is read as naming two bills. The narrow
    duplicate rule needs both sides: what the money COULD have been for, and
    which of those is already closed.
    """
    from .matching import normalise, normalise_invoice

    ref_norm = normalise(reference or "")
    if not ref_norm:
        return [], []
    paid, open_ = [], []
    for inv in session.query(InvoiceReceivable).all():
        if currency and inv.currency and inv.currency.upper() != currency.upper():
            continue
        num = normalise_invoice(inv.number)
        if len(num) < 3 or num not in ref_norm:
            continue
        if inv.status in (RecStatus.PAID, RecStatus.OVERPAID):
            paid.append(inv)
        elif inv.status in (RecStatus.OPEN, RecStatus.PARTIAL):
            open_.append(inv)
    return paid, open_


def _closed_invoice_note(session, reference: str, currency: str):
    """A sentence for a payment that names an invoice which is ALREADY SETTLED.

    `candidates_from_db` only ever offers OPEN or PARTIAL invoices, so a second
    payment quoting a settled one matched NOTHING and landed as "no confident
    match; a human decides" — a sentence that hides the two facts which decide
    what to do with the money: that it is a duplicate, and of WHICH bill. Run 7
    saw exactly that, with the invoice number sitting in the reference. The cash
    is still real and still needs a person; the sentence has to say what the
    person is deciding about.

    Returns (sentence, evidence) or None. The containment rule is the matcher's
    own (the invoice number appears in the reference, word-safe, with at least
    three digits), so this can never claim a match `try_exact` would refuse.
    """
    from .matching import normalise, normalise_invoice

    ref_norm = normalise(reference or "")
    if not ref_norm:
        return None
    for inv in session.query(InvoiceReceivable).all():
        if inv.status not in (RecStatus.PAID, RecStatus.OVERPAID):
            continue
        # A "duplicate" in another currency is not evidence of anything: the
        # same number against a settled bill in a different currency is a
        # coincidence, not a repeat payment.
        if currency and inv.currency and inv.currency.upper() != currency.upper():
            continue
        num = normalise_invoice(inv.number)
        # The WHOLE number, word-safe, is required — not a loose digit run. The
        # matcher's ">= 3 digits" guard exists to stop a SHORT digit run matching
        # anything; here the evidence is the entire invoice number appearing in
        # the reference, and a real one can be short (the tester's own
        # INV-DP7KQ2 carries two digits). Length of the NUMBER is the honest
        # guard: "invd" alone must not count as a duplicate.
        if len(num) < 3 or num not in ref_norm:
            continue
        return (f"{inv.number} is already paid; this looks like a duplicate, "
                f"refund or hold",
                {"rule": "reference names a settled invoice",
                 "invoice_number": inv.number,
                 "invoice_status": inv.status.value,
                 "invoice_id": inv.id})
    return None


# --------------------------------------------------------------------- intake
def matcher_from_settings():
    """The model matcher, or None when nothing is configured.

    Returns `(matcher, why_not)`. The intake used to pass `llm=None`
    unconditionally, so the model tier NEVER RAN — the code was there, the
    prompt was there, and no call site ever supplied an object. That is why AI
    activity showed "0 calls" even for a reference nothing could read.
    """
    try:
        from .llm import LLMConfig, OpenAICompatibleMatcher
    except Exception as exc:                      # pragma: no cover
        return None, f"the model client could not be imported: {exc}"
    cfg = LLMConfig.from_settings()
    if not cfg.available:
        return None, ("no model is configured. LLM_API_KEY is empty, so the "
                      "deterministic tiers decide alone")
    return OpenAICompatibleMatcher(cfg), ""


def _trace_model(session, *, inflow, result, llm, why_not: str) -> None:
    """Record the model call and the code's verdict side by side.

    Written to `agent_trace`, which is the table the AI-activity screen already
    reads, so the proposal and the enforcement appear together rather than one
    of them being implied.
    """
    import json as _json

    from ..models import AgentTrace

    ev = result.evidence or {}
    attempt = ev.get("model_attempt") or {}
    consulted = bool(ev.get("model_consulted")) or result.method == "LLM"
    claimed = attempt.get("claimed", ev.get("model_claimed_confidence"))
    rationale = attempt.get("rationale") or ev.get("rationale") or ""

    if consulted:
        # The tier ran. `None` in last_error means it answered.
        err = (attempt.get("error")
               or (getattr(llm, "last_error", None) if llm is not None else None)
               or why_not)
        session.add(AgentTrace(
            tool="propose_match",
            inputs_json=_json.dumps({
                "inflow_id": inflow.id,
                "amount_minor": inflow.amount_minor,
                "currency": inflow.currency,
                "payer": inflow.payer_name or "",
                "reference_raw": inflow.reference_raw or "",
                "model": getattr(getattr(llm, "cfg", None), "model", "") or "",
            }, default=str)[:4000],
            outputs_json=_json.dumps({
                "model_proposed": {
                    "allocations": [
                        {"invoice_number": a.get("invoice"),
                         "amount": a.get("amount")}
                        for a in (result.evidence or {}).get("allocations", [])
                    ],
                    "confidence": claimed,
                    "rationale": rationale,
                },
                "model_error": (None if err == "" else err),
            }, default=str)[:4000],
            verdict=("accepted" if result.method == "LLM"
                     and result.confidence > 0 else "escalated"),
            detail=(
                f"the model claimed {claimed}; code re-checked it and kept the "
                f"DETERMINISTIC reading instead ({result.method} at "
                f"{result.confidence:.2f}) because it was better grounded: "
                f"{result.reason}"
                if result.method != "LLM" else
                (f"the model claimed {claimed}. Code pinned it to "
                 f"{result.confidence:.2f} and re-checked every number; "
                 f"{result.reason}") if claimed is not None else
                (err or "the model answered and code re-validated it")),
        ))
        session.add(AgentTrace(
            tool="decide_match",
            inputs_json=_json.dumps({"inflow_id": inflow.id}, default=str),
            outputs_json=_json.dumps({
                "confidence": float(result.confidence),
                "band": "auto" if result.confidence >= 0.90 else "proposed",
                "status": result.status,
                "reason": result.reason,
                "evidence": result.evidence,
            }, default=str)[:4000],
            verdict=("accepted" if result.confidence >= 0.90 else "escalated"),
            detail=result.reason,
        ))
        return

    # The tier did NOT run. Say which of the two reasons it was, so the page
    # never reports a bare "0 calls" for a decision nobody was asked about.
    if why_not or (llm is not None and getattr(llm, "last_error", None)):
        session.add(AgentTrace(
            tool="model_unavailable",
            inputs_json=_json.dumps({"inflow_id": inflow.id}, default=str),
            outputs_json=_json.dumps({
                "model_error": why_not or getattr(llm, "last_error", ""),
                "decided_by": result.method,
                "deterministic_confidence": float(result.confidence),
            }, default=str)[:2000],
            verdict="escalated",
            detail=(why_not or getattr(llm, "last_error", "")
                    or "the model was not consulted"),
        ))


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

    # THE MODEL IS ACTUALLY CONSULTED HERE. Passing `llm=None` unconditionally
    # meant the LLM tier could never run, whatever was configured.
    why_not = ""
    if llm is None:
        llm, why_not = matcher_from_settings()

    cands = candidates_from_db(session)
    result = match_inflow(
        inflow.amount_minor, inflow.currency,
        {"reference_raw": reference or "", "payer_name": payer_name or ""},
        cands, llm=llm,
    )
    try:
        _trace_model(session, inflow=inflow, result=result, llm=llm,
                     why_not=why_not)
    except Exception:
        log.exception("could not record the model trace for inflow %s",
                      inflow.id)

    # THE NARROW DUPLICATE RULE. An invoice that is already settled is not a
    # place money can be applied, but a reference can name it ALONGSIDE the bill
    # the customer is actually paying -- "INV-1001 and INV-1002" -- and treating
    # that as a duplicate held money that had an obvious home.
    #
    # So: only the invoice the MATCHER actually chose can make this a duplicate.
    # If it chose an open invoice, that match stands and the settled one is
    # named as ignored; if it chose nothing while the reference names an open
    # bill, that bill is matched deterministically; and anything left over is
    # credit, said out loud. Run 11's case -- a reference naming ONLY the settled
    # bill -- still lands as credit, unchanged.
    from ..money import money_str

    paid_named, open_named = _reference_invoices(session, reference, currency)
    # NOTHING HERE RUNS UNLESS A SETTLED BILL IS NAMED. This is a rule about
    # duplicates, not about matching: an ordinary deposit is left exactly as the
    # matcher scored it, including the deliberate below-band part-payments.
    if paid_named:
        from .ledger import _Alloc      # the ledger's own allocation shape
        from .policy import MATCH_CONFIDENCE_AUTO as _AUTO

        def _credit_sentence(amount_minor: int) -> str:
            return (f"{paid_named[0].number} is already paid; "
                    f"{money_str(amount_minor, inflow.currency)} held as "
                    f"credit, nothing applied.")

        ignored = f"{paid_named[0].number} is already paid, so it was ignored."
        chosen_ids = [a.invoice_id for a in (result.allocations or [])]
        will_apply = (bool(chosen_ids)
                      and float(getattr(result, "confidence", 0) or 0) >= _AUTO
                      and result.status in ("MATCHED", "PARTIAL", "OVERPAID"))

        if not open_named:
            # CASE (a): ONLY A SETTLED BILL IS NAMED. Nothing can be applied to
            # it, whatever the matcher guessed (settled bills are not candidates,
            # so a part-payment tier latches onto some other open invoice).
            result = replace(
                result, status="AMBIGUOUS", confidence=0.0, method="DUPLICATE",
                allocations=[], credit_minor=0, shortfall_minor=0,
                reason=_credit_sentence(inflow.amount_minor),
                evidence={"rule": "reference names only a settled invoice",
                          "invoice_number": paid_named[0].number,
                          "invoice_status": paid_named[0].status.value,
                          "invoice_id": paid_named[0].id})
        else:
            notes = [ignored]
            if will_apply:
                # CASE (b): THE MATCHER ALREADY CHOSE THE OPEN BILL. Its match
                # stands untouched; the settled one is named as set aside.
                applied = sum(a.amount_minor for a in result.allocations)
            else:
                # THE REFERENCE NAMES AN OPEN BILL and the matcher's answer
                # would apply nothing (no allocation, or a below-band guess such
                # as an overpayment). Apply what that bill owes, never more.
                target = open_named[0]
                open_minor = max(0, target.amount_minor - target.amount_paid_minor)
                take = min(inflow.amount_minor, open_minor)
                applied = take
                # The matcher's credit_minor (e.g. an OVERPAID reading) describes
                # the ORIGINAL reading, not this forced, capped one; any leftover
                # is accounted below as credit against the settled invoice.
                result = replace(
                    result, allocations=[_Alloc(target.id, take)], confidence=1.0,
                    method="RULE", credit_minor=0, shortfall_minor=0,
                    status=("MATCHED" if take == open_minor else "PARTIAL"))
            leftover = inflow.amount_minor - applied
            if leftover > 0:
                # CASE (c): what is left over is credit, in the same sentence.
                notes.append(_credit_sentence(leftover))
            result = replace(result, reason=(
                f"{result.reason} " + " ".join(n for n in notes if n)).strip())

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
    disposition = ("APPLIED" if matches
                   else ("PROPOSED" if inflow.proposed_json else "HELD"))
    return IntakeResult(
        deposit_id=str(deposit_id),
        status=result.status,
        confidence=float(result.confidence),
        method=result.method,
        applied=numbers,
        reason=result.reason or "",
        inflow_id=inflow.id,
        disposition=disposition,
        review_state=inflow.review_state or "",
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
    # WHOSE IS THIS? The instance tag travels on the deposit's own free-text
    # fields, because a second instance reading `GET /deposits` sees the same
    # object and has no local row to check it against. The tag is stripped from
    # the reference we store, so matching and the UI never see it.
    from .ownership import read_tag

    claimed_by, clean_reference = read_tag(
        deposit.get("reference"), deposit.get("statement_ref"))
    return {
        "deposit_id": str(deposit.get("id") or ""),
        "currency": ccy,
        "amount_minor": amount_minor,
        "payer_name": deposit.get("payer_name") or "",
        "reference": clean_reference,
        "claimed_by": claimed_by,
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
        log.info("deposit %s is %s: not posting cash", f["deposit_id"], f["status"])
        return None
    # TWO INSTANCES SHARE ONE SANDBOX ACCOUNT, so `GET /deposits` hands each of
    # them the other's arrivals. Both the poller and the deposit webhook reach
    # the books through here, so this is the one place the question has to be
    # asked — and the answer is recorded, not just logged.
    from .ownership import owns_deposit, record_ignored

    mine, why = owns_deposit(f["claimed_by"])
    if not mine:
        log.info("ignoring deposit %s: %s", f["deposit_id"], why)
        with db.session_scope() as s:
            record_ignored(s, kind="deposit", external_id=f["deposit_id"],
                           detail=why)
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

    # TAG IT AS OURS, on the way out. This is the only moment we know for
    # certain the deposit is ours, and the marking has to happen here because
    # the other instance will see this deposit in ITS `GET /deposits` and has no
    # other way to tell whose it is.
    from .ownership import tag_reference

    tagged = tag_reference(reference)
    return Simulation(client).deposit(
        global_account_id=global_account_id,
        amount_minor=int(amount_minor),
        currency=currency.upper(),
        payer_name=payer_name or None,
        reference=tagged,
        statement_ref=tagged,
    )


__all__ = ["IntakeResult", "WANTED_ACCOUNTS", "accounts", "candidates_from_db",
           "ingest_deposit", "poll_deposits", "record_and_match", "simulate_deposit"]


# ------------------------------------------------------- the account list, cached
_ACCOUNTS_CACHE: dict = {"at": 0.0, "items": []}


def accounts_cached(db, *, ttl: float = 60.0, blocking: bool = False) -> list[dict]:
    """The global accounts behind a TTL cache. NON-BLOCKING BY DEFAULT.

    A DASHBOARD THAT BLOCKS ON AIRWALLEX SHOWS NOTHING WHEN AIRWALLEX IS SLOW,
    and that is not a hypothetical: on a cold process the first `/app` render
    waited 1.14 seconds on SSL reads plus a 1.00-second token backoff inside
    `AirwallexClient`, because this function fetched on a cache miss. The render
    now gets whatever was last known and nothing more; the refresh is scheduled
    after the response.

    `blocking=True` is for the one caller that genuinely needs an answer before
    it can proceed — the operator pressing "simulate a deposit" and expecting the
    list of accounts to deposit into.
    """
    import time as _time

    now = _time.time()
    fresh = bool(_ACCOUNTS_CACHE["items"]) and now - _ACCOUNTS_CACHE["at"] < ttl
    if fresh or not blocking:
        return _ACCOUNTS_CACHE["items"]
    return refresh_accounts(db)


def refresh_accounts(db) -> list[dict]:
    """Go and ask Airwallex, and cache the answer. NEVER called from a render."""
    import time as _time

    try:
        from ..airwallex.client import AirwallexClient
        items = accounts(AirwallexClient(), db)
    except Exception as exc:
        log.warning("could not list global accounts: %s", exc)
        return _ACCOUNTS_CACHE["items"]
    if items:
        _ACCOUNTS_CACHE["items"] = items
        _ACCOUNTS_CACHE["at"] = _time.time()
    return items


def accounts_stale(*, ttl: float = 60.0) -> bool:
    """Whether the cached account list wants refreshing."""
    import time as _time

    return not _ACCOUNTS_CACHE["items"] or (
        _time.time() - _ACCOUNTS_CACHE["at"] > ttl)


def forget_accounts_cache() -> None:
    """Called after creating an account so the next render sees it."""
    _ACCOUNTS_CACHE["at"] = 0.0
    _ACCOUNTS_CACHE["items"] = []


def reading_is_a_proposal(result) -> bool:
    """Did this reading find something to allocate?

    ONE rule, used by both the write path and the route that reports it. A
    re-ask never applies money, so a reading that names an allocation is a
    PROPOSAL awaiting a human whatever the matcher called it — a full allocation
    at 0.89 comes back "MATCHED" while sitting below the auto band, and treating
    that as "nothing proposed" both wiped a good proposal and told the operator
    the payment had been read as matched when nothing had moved.
    """
    return bool(result.allocations) and result.status in (
        "MATCHED", "PROPOSED", "PARTIAL", "OVERPAID")


def reask_model(db, inflow_id: int, *, llm=None):
    """Ask the model about an inflow AGAIN, without touching the cash.

    This exists because the model's availability is not the operator's problem:
    inflow 3 was escalated with "model proposed nothing (offline or provider
    unavailable)" while the provider was having a bad afternoon, and from then on
    the page showed a stale sentence about a model that had never actually been
    asked twice. A wrong answer from a down provider is not a fact about the
    payment.

    It re-reads the reference, re-runs the whole matcher (deterministic tiers
    first, then the model), and REPLACES the stored proposal. It posts NO ledger
    entries: the cash was booked when the money arrived, and re-asking is not a
    second receipt. Nothing already applied is undone — an inflow with no
    suspense left is refused, because there is nothing to re-decide.

    Returns `(result, why_not)`.
    """
    from .ledger import suspense_for
    from .matching import match_inflow

    with db.session_scope() as s:
        inflow = s.get(Inflow, inflow_id)
        if inflow is None:
            return None, "That payment no longer exists."
        if suspense_for(s, inflow) <= 0:
            return None, ("Nothing is left unmatched on this payment, so there "
                          "is nothing to re-read.")
        if getattr(inflow, "review_state", None) in ("REFUND_OWED",
                                                     "RELEASED_INCOME"):
            return None, ("This payment has been resolved, so re-reading the "
                          "reference would not change where the money is.")

        candidates = candidates_from_db(s)
        if not candidates:
            return None, ("There are no open invoices to match against. Add the "
                          "invoice first, then ask again.")
        if llm is None:
            llm, why = matcher_from_settings()
            if llm is None:
                return None, f"no model is configured: {why}"

        # A MODEL FAILURE MUST NOT DESTROY THE GOOD READING. If the provider is
        # down, `match_inflow` still returns the deterministic tiers' answer —
        # exactly as it would have when the money first arrived.
        result = match_inflow(inflow.amount_minor, inflow.currency, {
            "payer_name": inflow.payer_name or "",
            "reference_raw": inflow.reference_raw or "",
        }, candidates, llm=llm)

        inflow.match_status = (MatchStatus[result.status]
                               if result.status in MatchStatus.__members__
                               else MatchStatus.AMBIGUOUS)
        inflow.match_method = result.method
        inflow.match_confidence = float(result.confidence or 0.0)
        inflow.match_reason = result.reason
        # RE-ASKING NEVER APPLIES MONEY, so anything it found to allocate is a
        # PROPOSAL awaiting a human — whatever the matcher called it. Deriving
        # this from `result.status` alone was wrong: a full allocation read at
        # 0.89 comes back "MATCHED" (it IS a full match) while sitting BELOW the
        # auto band, so the stored proposal was wiped and the payment was left
        # awaiting a human with no proposal and no explanation.
        if reading_is_a_proposal(result):
            import json as _json
            inflow.proposed_json = _json.dumps({
                "status": result.status,
                "confidence": result.confidence,
                "method": result.method,
                "reason": result.reason,
                "evidence": result.evidence,
                "allocations": [{"invoice_id": a.invoice_id,
                                 "amount_minor": a.amount_minor}
                                for a in result.allocations],
            }, default=str)
            inflow.review_state = "PENDING"
            inflow.reviewed_at = None
            inflow.reviewed_by = None
        else:
            inflow.proposed_json = None
            inflow.review_state = None
        # The REASON survives both branches: it is the only thing telling the
        # operator what the second look found, and clearing the proposal must
        # not also clear the sentence explaining why there isn't one.
        src = getattr(getattr(llm, "cfg", None), "model", "") or ""
        _trace_model(s, inflow=inflow, result=result, llm=llm, why_not="")
        return result, src
