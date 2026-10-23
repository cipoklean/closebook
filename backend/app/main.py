"""FastAPI app — webhook receiver, API, approval endpoints and the dashboard.

Endpoints:
  GET  /healthz                     liveness + sandbox-readiness flag
  GET  /state                       read-only snapshot of receivables/payables
  GET  /                            the one-page dashboard
  GET  /incidents                   incidents raised by verified webhooks
  GET  /approvals/pending           steps awaiting a human decision
  POST /approvals/{id}/approve      record a HUMAN approval (PIN-gated)
  POST /approvals/{id}/reject       record a human rejection (PIN-gated)
  POST {webhook_path}               Airwallex webhook: verify -> 200 fast -> process later

Approving records a decision; it never moves money. Every payout still goes
through `domain/executor.py`, which is the only module permitted to call Airwallex
for money movement, and which re-verifies the bound approval on every path.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from contextlib import asynccontextmanager
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .airwallex.webhooks import WebhookHandler
from .config import get_settings
from .db import DB, get_db, get_default_db
from .domain.incidents import Incident, Recovery, handle_event, parse_payload
from .models import Inflow, InvoicePayable, InvoiceReceivable

log = logging.getLogger("closebook")
logging.basicConfig(level="INFO", force=True)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(_STATIC_DIR, exist_ok=True)

def _warm_the_account_cache() -> None:
    """Fetch the global accounts once, in the background, at boot.

    The first Money in view after a restart used to spend ~2.8s inside the
    render because the picker needs this list and the render went and got it.
    Warming it here means the list is usually already cached by the time anyone
    looks, and when it is not the page says so and fills itself in rather than
    stalling.

    In a THREAD, deliberately: startup must not wait for the sandbox either, or
    a slow Airwallex becomes a slow boot and then a slow first page anyway.
    """
    import threading

    def _warm() -> None:
        try:
            from .domain.intake import accounts_stale, refresh_accounts

            if accounts_stale():
                refresh_accounts(_database())
        except Exception:
            log.warning("could not warm the account cache at startup")

    threading.Thread(target=_warm, name="warm-accounts", daemon=True).start()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Warm the caches, then serve. LIFESPAN rather than the deprecated
    `@app.on_event`, which is what this started as -- it worked, but it put a
    DeprecationWarning in the test output, and noise in CI output is how a real
    warning gets ignored."""
    _warm_the_account_cache()
    yield


app = FastAPI(title="Closebook", version="0.1.0", lifespan=_lifespan)


@app.get("/money-in/accounts")
def money_in_accounts() -> dict:
    """The global accounts, as JSON. READ-ONLY, and it moves no money.

    The deposit form needs this list and must not block on it. When the cache
    was cold the page rendered without a picker; this is what it calls to fill
    it in, and it is the SAME builder the page uses, so the options are
    identical to the ones a warm cache would have produced.
    """
    from .domain.intake import accounts_cached, accounts_stale, refresh_accounts
    from .ui import account_options_html

    try:
        db = _database()
        if not accounts_cached(db) and accounts_stale():
            # One attempt, on a read the user can see is waiting for it.
            refresh_accounts(db)
        items = accounts_cached(db)
        return {"ok": True, "ready": bool(items),
                "html": account_options_html(items),
                "count": len(items)}
    except Exception as exc:
        log.warning("could not list accounts for the form: %s", exc)
        return {"ok": False, "ready": False, "html": "", "count": 0,
                "error": "Could not reach Airwallex for the account list."}
# Serve the stylesheet. `html=False` is deliberately omitted so the page is
# usable with JS off; the only script is a small enhancement layer.
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
# ONE handle to the book, resolved PER CALL.
#
# `_db = DB()` created a second engine alongside the module-level default, and
# `_db = get_default_db()` at import time captured whatever existed then, so any
# reset of the default (a script, a test) left the routes reading a different
# database than the code they called. In production both pointed at the same
# file so it never showed; in a test the routes saw an empty book and every
# lookup answered "no longer exists".
_settings = get_settings()

#: `/state` is polled by every open page, so it has a budget. Overrunning it is
#: logged with the elapsed time rather than being allowed to become an outage.
STATE_SLOW_SECONDS = 1.0
# The webhook handler is long-lived, so it is rebuilt whenever the default DB
# changes underneath it.
_webhook_db = None
_webhooks = None
# Lazy: resolving the Airwallex client touches .env credentials, and importing
# main.py must not require live credentials (tests import it).
_transfers = None
# Incidents raised by processed webhooks, newest last. In-memory on purpose —
# this is a demo view; incidents are also written to plan_step/transfer state.
INCIDENTS: list[Incident] = []


def _database():
    """The app's single database handle, resolved fresh each time."""
    return get_default_db()


def _webhook_handler() -> WebhookHandler:
    global _webhooks, _webhook_db
    db = _database()
    if _webhooks is None or _webhook_db is not db:
        _webhooks = WebhookHandler(
            db, _settings.awx_webhook_secret.get_secret_value())
        _webhook_db = db
    return _webhooks


def _transfer_client():
    global _transfers
    if _transfers is None:
        from .airwallex.client import AirwallexClient
        from .airwallex.transfers import Transfers
        _transfers = Transfers(AirwallexClient())
    return _transfers


# A transfer's lifecycle, in one place. The dev control, the badge and the
# buttons all read THIS, so the page cannot offer an action the API will refuse.
TERMINAL_TRANSFER_STATES = frozenset({
    "PAID", "COMPLETED", "SUCCESS", "FAILED", "CANCELLED", "CANCELED",
    "EXPIRED", "RETURNED", "REJECTED",
})
IN_FLIGHT_TRANSFER_STATES = frozenset({
    "PROCESSING", "PENDING", "SUBMITTED", "SENT", "CREATED", "SCHEDULED",
    "IN_PROGRESS", "APPROVAL_PENDING", "BOOKED", "READY_FOR_DISPATCH",
    "DISPATCHED", "IN_REVIEW",
})


def _dev_controls_are_offered(status: Optional[str]) -> bool:
    """Whether settle/fail/return can do anything for this transfer.

    Only while the bank says it is still moving: asking a terminal transfer to
    transition is an HTTP 500, and an UNKNOWN status must not produce a button
    that cannot work — a control that fails is worse than no control.
    """
    return (status or "").strip().upper() in IN_FLIGHT_TRANSFER_STATES


# ------------------------------------------------------- the planning loop
def _auto_execute() -> bool:
    """Whether a clear payment may be sent without a human.

    ON by default in sandbox (that is the whole point of the demo), OFF outside
    it, and switchable off entirely with `CLOSEBOOK_AUTO_EXECUTE=0`. The guard is
    the same one that gates every other route which creates a real object: a
    non-sandbox host cannot auto-pay, whatever this returns.
    """
    import os

    if os.environ.get("CLOSEBOOK_AUTO_EXECUTE", "").strip().lower() in (
            "0", "false", "no", "off"):
        return False
    return _require_sandbox() is None


def _run_loop(source: str, budget_s: float = 12.0) -> str:
    """Re-plan from live state after an event, and execute whatever is clear.

    Returns ONE plain sentence for the flash message. Never raises: a failure
    here must not lose the event that triggered it (a deposit that arrived is
    still a deposit that arrived).
    """
    from .domain.conductor import on_event

    # NO MODEL IS EVER RUN FROM HERE. `conductor.on_event` builds a plan and
    # sends clear steps; it has no language-model import and no path to one. The
    # model's only job is reading a bank reference, which happens on the way IN
    # (intake), never while an operator waits on a button.

    try:
        run = on_event(_database(), source=source, budget_s=budget_s,
                       execute=True, dry_run=not _auto_execute())
    except Exception as exc:                       # noqa: BLE001
        log.exception("the planning loop failed after %s", source)
        return f" Re-planning failed ({type(exc).__name__}); nothing was sent."

    bits = []
    for v in run.verdicts:
        if v.outcome == "EXECUTED":
            bits.append(f"sent {v.action} {v.payable_number or ''} "
                        f"({(v.object_id or '')[:8]})".strip())
        elif v.outcome == "ALREADY_DONE":
            bits.append(f"{v.payable_number} already has a live payment")
        else:
            bits.append(f"{v.action} {v.payable_number or ''} held".strip())
    body = "; ".join(bits) if bits else "nothing to pay"
    tail = "" if _auto_execute() else " (automatic execution is off)"
    return f" Re-planned: {body}{tail}."


@app.post("/money-out/run")
async def money_out_run(request: Request):
    """Re-plan and execute the clear payments, on demand."""
    return _back(request, tab="money-out", ok=f"Money out.{_run_loop('run')}")


@app.post("/dev/transfers/{transfer_id}/transition")
async def dev_transfer_transition(transfer_id: str, request: Request):
    """Sandbox dev control: settle / fail / return an in-flight transfer.

    The status change is then read back the SAME way production would see it —
    a live lookup on Airwallex, never the local row and never the request — and
    only then does the incident logic and the settlement posting run. A repeat is
    harmless: settlement is keyed to the Airwallex object id, so a second PAID
    posts nothing.
    """
    guard = _require_sandbox()
    if guard:
        return _back(request, tab="money-out", err=guard)

    b = await _read_body(request)
    what = str(b.get("what") or "").strip().lower()
    # FAILED needs a failure_type, and the two reasons are different incidents:
    # a beneficiary the bank rejected is fixable, a bank RETURN is money coming
    # back and needs a new approval before it is sent again.
    plan_for = {
        "settle": ("PAID", None),
        "fail": ("FAILED", "INVALID_ACCOUNT_NAME_OR_NUMBER"),
        "return": ("FAILED", "BENEFICIARY_BANK_RETURNED"),
    }
    if what not in plan_for:
        return _back(request, tab="money-out",
                     err="Pick settle, fail or return.")
    next_status, failure_type = plan_for[what]

    # A transfer the bank has already finished is READ, never asked to move
    # again: the sandbox returns a 500 for that, and a 500 on something already
    # where we wanted it is not a failure.
    terminal = TERMINAL_TRANSFER_STATES

    already = ""
    try:
        from .airwallex.client import AirwallexClient
        from .airwallex.simulation import Simulation
        from .airwallex.transfers import Transfers

        client = AirwallexClient()
        already = (Transfers(client).status_of(transfer_id) or "").upper()
        if already not in terminal:
            Simulation(client).transition_transfer(
                transfer_id, next_status, failure_type=failure_type)
    except Exception as exc:                       # noqa: BLE001
        log.exception("transition failed for %s", transfer_id)
        # A SENTENCE, NOT A BLOB. This used to put Airwallex's raw JSON on the
        # query string, so the operator read braces and a status code where they
        # needed to know whether their books had changed.
        from .domain.plain_words import settle_failure

        sentence, detail = settle_failure(exc)
        if "application/json" in (request.headers.get("accept") or ""):
            from fastapi.responses import JSONResponse
            return JSONResponse(_action_body("money-out", "", sentence,
                                             detail=detail))
        return _back(request, tab="money-out", err=sentence)

    pre_note = (f"Airwallex already reported {already}, so nothing was asked of "
                f"it and the existing state was settled instead."
                if already in terminal else "")

    # Read the truth back off Airwallex rather than repeating what we asked for.
    live = None
    settle_note = ""
    try:
        from .airwallex.transfers import Transfers as _T
        from .settle import settle_transfer

        live = _T(client).status_of(transfer_id)
        if live:
            # WE JUST ASKED, so the page must not keep showing the old answer:
            # the response below says PAID and the pill said PROCESSING.
            try:
                from .dashboard import note_live_status
                note_live_status(transfer_id, live)
            except Exception:                      # noqa: BLE001
                pass
            with _database().session_scope() as sess:
                outcomes = settle_transfer(sess, transfer_id, live)
            posted = [o for o in outcomes if o.posted]
            if posted:
                settle_note = f" Ledger: {len(posted)} posting(s) written."
            elif outcomes:
                # Say what settlement ACTUALLY decided. "already settled" was
                # printed for a CANCELLED transfer that never settled at all —
                # the opposite claim, on the screen that exists to be trusted.
                settle_note = f" Ledger: nothing written — {outcomes[0].reason}"
            else:
                settle_note = (" Ledger: no plan step holds this transfer, so "
                               "there was nothing to post.")
    except Exception as exc:                       # noqa: BLE001
        log.exception("settlement after transition failed")
        settle_note = f" Settlement could not run: {type(exc).__name__}."

    try:
        from .settle import settle_all
        settle_all(_database())
    except Exception:                              # noqa: BLE001
        log.exception("sweep settlement failed")

    name = {"settle": "Settle", "fail": "Fail", "return": "Return"}[what]
    return _back(request, tab="money-out", ok=(
        f"{name}: Airwallex now reports {live or '(unreadable)'} for "
        f"{transfer_id[:8]}…. {pre_note}{settle_note}"
        f"{_run_loop('dev ' + what)}"))


@app.get("/healthz")
def healthz() -> dict:
    return {
        "ok": True,
        "sandbox_ready": _settings.sandbox_ready(),
        "webhook_url": _settings.webhook_url,
    }


@app.get("/state/version")
def state_version() -> dict:
    """A cheap stamp of everything the dashboard renders. ONE query, no ORM.

    WHY THIS EXISTS. The page used to reload itself every five seconds to stay
    current, which rebuilt the DOM under whoever was reading it and made any
    element a test had just located stale by the time it clicked. Polling a
    stamp instead means the page can know the book changed WITHOUT touching the
    page, and offer a refresh rather than taking one.

    It is deliberately the CHEAPEST thing that can answer "did anything
    change": max row id per table the view shows, in a single statement. It
    cannot return stale-because-cached, because it is not cached; and it does
    not write, so reading it does not change it — a page view must never move
    the version it is watching.
    """
    from sqlalchemy import text

    tables = ("ledger_entry", "inflow", "match", "approval", "plan_step",
              "invoice_receivable", "invoice_payable", "ignored_event",
              "webhook_event")
    sql = "SELECT " + ", ".join(
        f"(SELECT COALESCE(MAX(id),0) FROM {t})" for t in tables)
    try:
        with _database().session() as s:
            row = s.execute(text(sql)).one()
    except Exception:                              # never a raw 500 here either
        log.exception("could not read the state version")
        return {"ok": False, "version": "unreadable"}
    version = "-".join(str(int(v or 0)) for v in row)
    return {"ok": True, "version": version}


@app.get("/state/tiles")
def state_tiles() -> dict:
    """The Home figures as TEXT, so an action can patch them in place.

    The figures come from `ui.tile_text`, the SAME function the Home render
    uses. If an action formatted them itself the screen and the server would
    drift apart the first time a format changed, and the operator would be
    reading two versions of their own money.
    """
    try:
        from .dashboard import build_context
        from .ui import needs_you, tile_text

        c = build_context(INCIDENTS)
        return {"ok": True, "tiles": tile_text(c), "needs_you": needs_you(c),
                "version": state_version().get("version", "")}
    except Exception as exc:                       # noqa: BLE001
        log.exception("could not read tiles")
        return {"ok": False, "tiles": {},
                "error": (f"Could not read the figures ({type(exc).__name__}). "
                          f"Nothing was changed by this request.")}


@app.get("/state")
def state() -> dict:
    """Read-only snapshot for the dashboard. DB ONLY, and never a raw 500.

    THREE RULES, each learned from a real failure:

    * **No network, no planner, no model.** This is polled every few seconds by
      every open page. Anything slow belongs behind `BackgroundTasks` or on its
      own route, because an inline Airwallex call here would make the whole UI
      depend on a third party's latency.
    * **Bounded.** It is timed; anything over the budget is logged with the
      elapsed time so a slowly-growing query is visible BEFORE it becomes an
      outage rather than after.
    * **Never a raw 500.** A failed read returns a JSON body with a reason the
      page can print. An unhandled exception here means the operator sees
      "Internal Server Error" and has no idea whether the books are safe — and
      "the app could not read state" is exactly what a bot reported.
    """
    started = time.monotonic()
    try:
        with _database().session() as s:
            recs = s.query(InvoiceReceivable).all()
            pays = s.query(InvoicePayable).all()
            body = {
                "ok": True,
                "receivables": [
                    {"number": r.number, "currency": r.currency,
                     "amount_minor": r.amount_minor, "paid_minor": r.amount_paid_minor,
                     "status": r.status.value}
                    for r in recs
                ],
                "payables": [
                    {"number": p.number, "currency": p.currency,
                     "amount_minor": p.amount_minor, "status": p.status.value,
                     "criticality": p.criticality}
                    for p in pays
                ],
                # What this instance DELIBERATELY did not act on, and who it
                # belonged to. Two instances share one sandbox account, so each
                # sees the other's arrivals; they are neither suspense nor
                # incidents, and this is where that decision is visible rather
                # than merely greppable in a log.
                "ignored_events": _ignored_events(),
            }
    except Exception as exc:                       # noqa: BLE001
        log.exception("could not read state")
        return {
            "ok": False,
            "error": (f"Could not read the books ({type(exc).__name__}). The "
                      f"database is the only thing this page reads, so the "
                      f"usual cause is a query that timed out or a file that "
                      f"is not writable. Nothing was changed by this request."),
            "receivables": [], "payables": [],
            "ignored_events": {"count": 0, "recent": []},
        }

    elapsed = time.monotonic() - started
    if elapsed > STATE_SLOW_SECONDS:
        log.warning("GET /state took %.2fs (budget %.2fs) — it must stay a "
                    "fast, database-only read", elapsed, STATE_SLOW_SECONDS)
    return body


def _ignored_events(limit: int = 20) -> dict:
    """Foreign deposits and transfers this instance refused, newest first."""
    from .models import IgnoredEvent

    try:
        with _database().session() as s:
            rows = (s.query(IgnoredEvent)
                    .order_by(IgnoredEvent.id.desc()).limit(limit).all())
            total = s.query(IgnoredEvent).count()
            return {"count": total, "recent": [
                {"kind": r.kind, "external_id": r.external_id,
                 "instance": r.instance, "detail": r.detail}
                for r in rows]}
    except Exception:
        return {"count": 0, "recent": []}


@app.get("/incidents")
def incidents() -> list[dict]:
    """Open incidents raised by verified webhooks, newest last.

    Surfaced on the dashboard: an incident that is not visible is an incident
    nobody acts on. Each carries its decided recovery, so the UI never has to
    re-derive the policy.
    """
    return [i.to_dict() for i in INCIDENTS]


@app.get("/approvals/pending")
def pending_approvals() -> list[dict]:
    """Steps awaiting a human decision, with the terms they would bind."""
    from .models import Approval, ApprovalStatus, PlanStep

    with _database().session() as s:
        steps = (s.query(PlanStep)
                 .filter(PlanStep.airwallex_object_id.is_(None))
                 .order_by(PlanStep.id.desc()).limit(25).all())
        out = []
        for st in steps:
            row = s.query(Approval).filter_by(plan_step_id=st.id).first()
            out.append({
                "step_id": st.id,
                "type": st.type.value,
                "status": st.status.value,
                "reason": st.reason,
                "already_approved": (row.status == ApprovalStatus.APPROVED
                                     if row else False),
            })
        return out


async def _read_body(request: Request) -> dict:
    """Read the request body as JSON or as a form.

    The dashboard posts a plain HTML form; API clients post JSON. Accepting both
    means the choice of UI is never the reason an approval silently fails.
    """
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            return await request.json()
        except ValueError:
            return {}
    if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
        form = await request.form()
        return {k: v for k, v in form.items()}
    try:
        return await request.json()
    except ValueError:
        return {}


def _state_version() -> str:
    """The same stamp `/state/version` serves, for an action's reply."""
    try:
        return state_version().get("version", "")
    except Exception:
        return ""


def _action_body(tab: str, ok: str, err: str, detail: str = "") -> dict:
    """THE REPLY EVERY HUMAN ACTION RETURNS. One shape, always.

    `ok` is a BOOLEAN and `message` is the sentence, so a caller can never
    mistake a sentence for a success flag or the other way round. Before this,
    the approve route answered with its own object shape that had no `ok` key at
    all, so the page fell through to its last resort and told the operator

        "The server did not say what happened. Reload to see the books."

    on a click that had SUCCEEDED and whose approval was already recorded. A
    successful action reported as unknown is worse than a failure reported as
    one: it invites the operator to do it again.

    `state_version` rides along so the page can tell whether the book moved
    without asking a second question.
    """
    success = bool(ok) and not err
    return {
        "tab": tab,
        "ok": success,
        "message": ok or err or "",
        "err": "" if success else (err or ok or ""),
        # The vendor's own words, kept for the person who has to report the
        # problem, and rendered behind a toggle rather than shouted on screen.
        "detail": detail,
        "state_version": _state_version(),
        "done": True,
    }


@app.post("/approvals/{step_id}/approve")
async def approve(step_id: int, request: Request) -> dict:
    """Record a HUMAN approval. Does not move money.

    Execution still re-verifies the bound hash, amount, counterparty and FX
    rate through the executor — approving here only records the decision.

    Accepts JSON or a form post so the dashboard's plain form works.
    """
    from .domain.approval_api import ApprovalRefused, approve_step

    from .money import money_str

    body = await _read_body(request)
    pin = str(body.get("pin") or "")
    by = str(body.get("approved_by") or get_settings().operator_name or "dashboard")
    # A missing SecretStr is not an empty one; treat both as "closed".
    _pin_field = get_settings().demo_approver_pin
    configured = (_pin_field.get_secret_value()
                   if _pin_field is not None else "")
    if not configured:
        return _action_body("ok", "", "The approval PIN is not configured, so "
                                      "this endpoint is closed rather than open.")
    try:
        res = approve_step(_database(), step_id, pin=pin, approved_by=by,
                           configured_pin=configured)
    except ApprovalRefused as exc:
        return _action_body("ok", "", str(exc))
    amount = money_str(int(res.get("amount_minor") or 0),
                       str(res.get("currency") or "USD"))
    who = res.get("counterparty") or "the supplier"

    # RUN A CYCLE NOW, not at the next unrelated event. Approving used to
    # record the decision and stop, so the payment waited for a deposit or a
    # manual re-plan that nobody knew to press. The approval is what makes the
    # step clear, so the send belongs to the same press. `_run_loop` is the
    # same path every other event uses, so an approval cannot take a route no
    # other decision does.
    tail = _run_loop("approve", budget_s=20.0)

    return _action_body("ok", f"Approved {amount} to {who}.{tail}", "")


@app.post("/approvals/{step_id}/reject")
async def reject(step_id: int, request: Request) -> dict:
    from .domain.approval_api import ApprovalRefused, reject_step

    body = await _read_body(request)
    pin = str(body.get("pin") or "")
    by = str(body.get("rejected_by") or get_settings().operator_name or "dashboard")
    reason = str(body.get("reason") or "")
    # A missing SecretStr is not an empty one; treat both as "closed".
    _pin_field = get_settings().demo_approver_pin
    configured = (_pin_field.get_secret_value()
                   if _pin_field is not None else "")
    if not configured:
        return _action_body("ok", "", "The approval PIN is not configured, so "
                                      "this endpoint is closed rather than open.")
    try:
        res = reject_step(_database(), step_id, pin=pin, rejected_by=by,
                          reason=reason, configured_pin=configured)
    except ApprovalRefused as exc:
        return _action_body("ok", "", str(exc))
    return _action_body(
        "ok",
        "Rejected. Nothing was sent, and the payment will not be re-planned on "
        "its own — it needs a fresh decision.", "")


@app.post(_settings.webhook_path)
async def webhook(request: Request, background: BackgroundTasks) -> Response:
    """Ack-fast (200) so Airwallex never retries on a slow handler, then
    process in the background. Bad signature -> 400, also returned fast."""
    raw = await request.body()
    ts = request.headers.get("x-timestamp", "")
    sig = request.headers.get("x-signature", "")
    client_secret = request.headers.get("client-secret-key")  # test events
    status = _webhook_handler().receive(raw, ts, sig, client_secret)
    if status != 200:
        return Response("failed to verify the signature", status_code=400)
    # Defer the actual event handling; the demo's polling fallback covers the
    # case where a webhook is slow/lost (closebook.md §6).
    background.add_task(_process_pending)
    return Response("200", status_code=200)


def _process_pending() -> None:
    """Process unprocessed webhook events oldest-first.

    Every failure event is looked up on Airwallex BEFORE a recovery is chosen:
    a `payout.transfer.failed` webhook is a claim about a transfer, not proof of
    one, and treating the claim as fact is how you pay twice.

    Each event is CLAIMED atomically (`claim_all_pending`), not merely read, so
    two background tasks cannot both process the same event. Airwallex retries
    on a slow handler, and a duplicate `transfer.failed` processed twice is a
    duplicate incident — and potentially a duplicate replacement payment.
    """
    for ev in _webhook_handler().claim_all_pending():
        log.info("processing webhook %s (%s)", ev.event_id, ev.name)
        payload = parse_payload(ev.payload)
        live_status = None
        transfer_id = None
        if ev.name in {"payout.transfer.failed", "payout.transfer.cancelled",
                       "payout.transfer.paid"}:
            # DIG, don't index. A real Airwallex payload nests the whole
            # transfer under `data`, so `payload.get("transfer_id")` is empty
            # and this whole ownership check would have been SKIPPED on exactly
            # the payload shape that actually arrives. Same reader the incident
            # handler uses, so the two cannot disagree about what a field means.
            from .domain.incidents import _dig

            transfer_id = _dig(payload, "transfer_id", "transaction_id")
            # WHOSE TRANSFER IS THIS? One sandbox account serves two instances,
            # so an event about the tester's payment arrives here too. The
            # transfer's own `request_id` is the test: a request id is minted
            # LOCALLY before a payment leaves, so one we have never seen cannot
            # be ours. Reading the transfer once gives us both the live status
            # and the request id.
            # The webhook's `data` object is documented to be the same as the
            # body of `GET /transfers/{id}`, so a well-formed event tells us the
            # request id WITHOUT a lookup — which is what keeps our own failed
            # payment recognisable even when the read API is having a bad day.
            request_id = str(_dig(payload, "request_id", default="") or "")
            if transfer_id:
                try:
                    obj = _transfer_client().get(transfer_id)
                    live_status = (obj.get("status") or "").upper() or None
                    request_id = request_id or str(obj.get("request_id") or "")
                except Exception:
                    log.warning("lookup failed for %s", transfer_id)

            from .domain.ownership import owns_transfer, record_ignored

            with _database().session() as s:
                step, why = owns_transfer(s, request_id)
                if step is None:
                    log.info("ignoring transfer event %s: %s", ev.event_id, why)
                    record_ignored(s, kind="transfer",
                                   external_id=request_id or (transfer_id or ""),
                                   detail=why)
                    s.commit()
                    continue      # NO incident and NO settlement for a payment we did not send

        # A deposit arriving by PUSH goes through the same intake the poller
        # uses, so the two paths cannot disagree about what a deposit means or
        # post the cash twice (the intake is keyed to the deposit id).
        if "deposit" in (ev.name or "").lower():
            try:
                from .domain.intake import ingest_deposit
                blob = payload if isinstance(payload, dict) else {}
                inner = blob.get("data") if isinstance(blob.get("data"), dict) else None
                for candidate in (inner, blob):
                    if not candidate or not candidate.get("id"):
                        continue
                    res = ingest_deposit(_database(), candidate)
                    if res is not None:
                        log.info("deposit webhook intake: %s %s (%.2f)",
                                 res.status, res.applied, res.confidence)
                        break
            except Exception:
                log.exception("deposit intake failed for %s", ev.event_id)

        incident = handle_event(ev.name, payload, event_id=ev.event_id,
                                live_status=live_status)
        if incident.recovery is not Recovery.IGNORE:
            log.warning("INCIDENT %s", incident.describe())
            INCIDENTS.append(incident)

        # A VERIFIED, LOOKED-UP terminal status also settles the books. The
        # lookup above is what makes this safe: a `failed` webhook on a
        # still-PROCESSING transfer leaves live_status non-terminal, so
        # settle_transfer posts nothing rather than removing money that has not
        # moved. Keyed to the transfer id, so a retried webhook posts nothing
        # the second time.
        if transfer_id and live_status:
            try:
                from .settle import settle_transfer
                outcomes = settle_transfer(get_default_db().session(),
                                           transfer_id, live_status)
                for o in outcomes:
                    log.info("settlement: %s", o)
            except Exception:
                log.exception("settlement failed for %s", transfer_id)


@app.post("/inflows/{inflow_id}/approve")
async def approve_inflow_route(inflow_id: int, request: Request):
    """Apply a PROPOSED match. This is where a below-band match becomes real."""
    from .domain.ledger import LedgerError, approve_inflow
    from .money import money_str

    b = await _read_body(request)
    pin = str(b.get("pin") or "")
    by = str(b.get("approved_by") or "dashboard")
    refusal = _check_pin(pin)
    if refusal:
        return _back(request, tab="ok", err=refusal)

    with _database().session_scope() as s:
        inflow = s.get(Inflow, inflow_id)
        if inflow is None:
            return _back(request, tab="ok", err="That payment no longer exists.")
        try:
            matches = approve_inflow(s, inflow, approved_by=by)
        except LedgerError as exc:
            return _back(request, tab="ok", err=str(exc))
        applied = ", ".join(str(m.invoice_receivable_id) for m in matches)
        amount = inflow.amount_minor
        ccy = inflow.currency
    return _back(request, tab="ok", ok=(
        f"Approved. {money_str(amount, ccy)} has been applied to invoice "
        f"id(s) {applied}; the invoice and the ledger have both moved."
        + _run_loop("approval")))


def _check_pin(pin: str) -> Optional[str]:
    """The shared gate for anything a human has to authorise.

    Closed rather than open when no PIN is configured: an approval endpoint with
    no secret is worse than no endpoint.

    Reads `get_settings()` at CALL time, not the `_settings` snapshot taken at
    import. The snapshot is right for things fixed at boot and wrong for the one
    value an operator edits and then immediately tests — the PIN was typed
    correctly into the page and refused, because the process had loaded `.env`
    ten minutes earlier.
    """
    field = get_settings().demo_approver_pin
    configured = (field.get_secret_value() if field is not None else "")
    if not configured:
        return ("DEMO_APPROVER_PIN is not configured — the approval endpoint is "
                "closed rather than open.")
    if pin != configured:
        return "That PIN is not right."
    return None


@app.post("/inflows/{inflow_id}/reask")
async def reask_inflow_route(inflow_id: int, request: Request):
    """Ask the model about this payment again. NO PIN, and no money moves.

    Deliberately un-gated: it changes no balance, applies nothing and sends
    nothing — it replaces a stored READING. Requiring a PIN to retry a failed
    reading would train people to type their PIN at things that do not need it.
    """
    from .domain.intake import reask_model

    try:
        result, note = reask_model(_database(), inflow_id)
    except Exception as exc:                       # noqa: BLE001
        log.exception("re-ask failed for inflow %s", inflow_id)
        return _back(request, tab="ok", err=(
            f"Could not ask the model again ({type(exc).__name__}). The stored "
            f"reading is unchanged."))

    if result is None:
        return _back(request, tab="ok", err=note)

    conf = float(result.confidence or 0.0)
    # A RE-ASK NEVER APPLIES, so say that rather than echoing a status that
    # reads as though it did. The rule comes from intake, not from here.
    from .domain.intake import reading_is_a_proposal

    if reading_is_a_proposal(result):
        return _back(request, tab="ok", ok=(
            f"Asked again. Read as {result.status} via {result.method} at "
            f"{conf:.2f} — asking again never applies anything, so this is a "
            f"proposal waiting for your decision. {result.reason}"))
    if result.method == "LLM" and conf == 0:
        return _back(request, tab="ok", ok=(
            f"Asked again. The model still could not place this one, so it stays "
            f"in suspense. {result.reason}"))
    return _back(request, tab="ok", ok=(
        f"Asked again. Read as {result.status} via {result.method} at "
        f"{conf:.2f}. {result.reason}"))


@app.post("/inflows/{inflow_id}/resolve")
async def resolve_inflow_route(inflow_id: int, request: Request):
    """Resolve a suspended payment. THE ONLY THREE WAYS SUSPENSE CAN LEAVE.

      match  -> apply the stored proposal to an invoice (the existing path)
      refund -> record a liability; the cash stays put and stops being spendable
      income -> release it as ours; it becomes spendable, and it needs a note

    Three things this route must never do, all learned the hard way:

      * **Never build a message inside the transaction.** The first version
        called `approve_inflow` and then formatted the reply using an attribute
        that does not exist. It raised, `session_scope` rolled back, and a
        resolution that had genuinely applied was undone — the operator saw a
        failure and the books showed nothing. The work is committed FIRST, and
        the sentence is built afterwards from plain values.
      * **Never run the model here.** Resolving is a decision about money already
        in the book; asking a language model to read a reference again would put
        a network call with a 45-second timeout inside a button press.
      * **Never hang.** The re-plan runs on a wall-clock budget, so the button
        always ends in a sentence.
    """
    from .domain.ledger import (LedgerError, approve_inflow, resolve_income,
                                resolve_refund, suspense_for)
    from .money import money_str

    try:
        b = await _read_body(request)
        pin = str(b.get("pin") or "")
        by = (str(b.get("by") or "").strip() or "david")
        what = str(b.get("what") or "").strip().lower()
        reason = str(b.get("reason") or "").strip()
        note = str(b.get("note") or "").strip()

        refusal = _check_pin(pin)
        if refusal:
            return _back(request, tab="ok", err=refusal)

        if what not in ("match", "refund", "income", "reject"):
            return _back(request, tab="ok",
                         err="Pick what to do with this money: match it to an "
                             "invoice, record a refund owed, release it as "
                             "income, or leave it in suspense.")

        # ---- the work, in its own transaction, committed before any prose ----
        summary = None
        try:
            with _database().session_scope() as s:
                inflow = s.get(Inflow, inflow_id)
                if inflow is None:
                    return _back(request, tab="ok",
                                 err="That payment no longer exists.")
                ccy = inflow.currency
                if what == "match":
                    matches = approve_inflow(s, inflow, approved_by=by)
                    amount = sum(m.amount_applied_minor for m in matches) or \
                        inflow.amount_minor
                    applied = ", ".join(str(m.invoice_receivable_id)
                                        for m in matches)
                    summary = ("match", amount, ccy, applied)
                elif what == "reject":
                    from .domain.ledger import reject_inflow
                    reject_inflow(s, inflow, rejected_by=by, reason=reason)
                    summary = ("reject", suspense_for(s, inflow), ccy, "")
                elif what == "refund":
                    amount = resolve_refund(s, inflow, reason=reason, by=by)
                    summary = ("refund", amount, ccy, "")
                else:
                    amount = resolve_income(s, inflow, note=note, by=by)
                    summary = ("income", amount, ccy, "")
                # read inside; used outside, after the commit
                remaining = suspense_for(s, inflow)
        except LedgerError as exc:
            return _back(request, tab="ok", err=str(exc))

        kind, amount, ccy, applied = summary
        if kind == "match":
            head = (f"Matched. {money_str(amount, ccy)} has been applied to "
                    f"invoice id(s) {applied}; the invoice and the ledger have "
                    f"both moved.")
        elif kind == "reject":
            head = (f"Left in suspense. {money_str(amount, ccy)} is still in the "
                    f"book and still not spendable — decide where it belongs "
                    f"when you know.")
        elif kind == "refund":
            head = (f"Refund owed: {money_str(amount, ccy)} is now a recorded "
                    f"liability. No transfer was sent, and the money has stopped "
                    f"being spendable.")
        else:
            head = (f"Released as income: {money_str(amount, ccy)} is now ours "
                    f"to spend. The ledger credits other_income and the decision "
                    f"is recorded against {by}.")

        tail = (f" Still unresolved on this payment: "
                f"{money_str(remaining, ccy)}." if remaining else "")

        # The re-plan runs on a budget. If it runs out, the resolution stands and
        # the page says so, because the money decision is already committed.
        return _back(request, tab="ok",
                     ok=head + tail + _run_loop("resolve"))
    except Exception as exc:                       # noqa: BLE001
        # An unexpected failure must still be a sentence, never a bare 500: the
        # operator needs to know whether anything moved.
        log.exception("resolve failed for inflow %s", inflow_id)
        return _back(request, tab="ok", err=(
            f"Something went wrong resolving that payment "
            f"({type(exc).__name__}). Nothing was changed if the message did not "
            f"say it was."))


@app.post("/inflows/{inflow_id}/reject")
async def reject_inflow_route(inflow_id: int, request: Request):
    """Reject a proposal. The cash stays in suspense — we really did receive it."""
    from .domain.ledger import LedgerError, reject_inflow

    b = await _read_body(request)
    pin = str(b.get("pin") or "")
    by = str(b.get("rejected_by") or "dashboard")
    reason = str(b.get("reason") or "")
    _pin_field = get_settings().demo_approver_pin
    configured = (_pin_field.get_secret_value() if _pin_field is not None else "")
    if not configured:
        return _back(request, tab="ok",
                     err="DEMO_APPROVER_PIN is not configured — the rejection "
                         "endpoint is closed rather than open.")
    if pin != configured:
        return _back(request, tab="ok", err="That PIN is not right.")

    with _database().session_scope() as s:
        inflow = s.get(Inflow, inflow_id)
        if inflow is None:
            return _back(request, tab="ok", err="That payment no longer exists.")
        try:
            reject_inflow(s, inflow, rejected_by=by, reason=reason)
        except LedgerError as exc:
            return _back(request, tab="ok", err=str(exc))
    return _back(request, tab="ok", ok=(
        "Rejected. Nothing was applied and the cash stays in suspense, where it "
        "can be matched again or refunded."))


# ------------------------------------------------------------- money in (P2)
# The ONLY route that creates a real deposit, and it is sandbox-gated twice:
# here and inside intake.simulate_deposit(). It cannot pay anyone — there is no
# transfer or conversion call anywhere on this path.

@app.post("/money-in/simulate")
async def money_in_simulate(request: Request):
    """Simulate a customer payment landing in the sandbox.

    The reference is passed through EXACTLY as typed, because a messy one is the
    interesting case. The deposit then goes through the same intake the webhook
    uses, so the fallback and the push path cannot disagree.
    """
    from .domain.intake import (accounts_cached, ingest_deposit, simulate_deposit)
    from .money import money_str, to_minor

    guard = _require_sandbox()
    if guard:
        return _back(request, tab="money-in", err=guard)

    b = await _read_body(request)
    asked_ccy = str(b.get("currency") or "").strip().upper()
    amount = str(b.get("amount") or "").strip()
    payer = str(b.get("payer_name") or "").strip()
    reference = str(b.get("reference") or "")       # NOT stripped: typo'd on purpose
    chosen = str(b.get("global_account_id") or "").strip()

    try:
        from .airwallex.client import AirwallexClient
        client = AirwallexClient()
        accts = accounts_cached(_database(), blocking=True)
        account = next((a for a in accts if a["id"] == chosen), None) if chosen else None
        if account is None:
            # No account chosen: pick the first ACTIVE one for the asked currency.
            account = next((a for a in accts
                            if a.get("usable") and a["currency"] == asked_ccy),
                           None)
        if account is None:
            return _back(request, tab="money-in",
                         err="Pick a global account to deposit into. Nothing "
                             "ACTIVE was available for that currency.")

        # The ACCOUNT decides the currency, not the form. The simulation
        # endpoint has no currency field — the money lands in the account's own
        # currency and a currency sent alongside it is silently discarded — so
        # scaling the amount by the form's default would mis-scale every
        # non-2-exponent currency and mislabel the result.
        ccy = account["currency"] or asked_ccy
        if ccy and asked_ccy and ccy != asked_ccy:
            log.info("deposit: form asked for %s but account %s is %s",
                     asked_ccy, account["id"], ccy)
        ga_id = account["id"]

        try:
            amount_minor = to_minor(amount, ccy)
        except Exception:
            return _back(request, tab="money-in",
                         err=f"Amount has to be a number like 12000. "
                             f"Got {amount!r}.")
        if amount_minor <= 0:
            return _back(request, tab="money-in",
                         err="Amount has to be more than zero.")

        deposit = simulate_deposit(
            client, global_account_id=ga_id, amount_minor=amount_minor,
            currency=ccy, payer_name=payer, reference=reference)
    except Exception as exc:
        log.exception("simulated deposit failed")
        return _back(request, tab="money-in",
                     err=f"Airwallex refused the simulated deposit: {exc}")

    dep_id = (deposit or {}).get("id") if isinstance(deposit, dict) else None
    # The deposit's OWN currency is the last word on what was credited.
    credited = ((deposit or {}).get("currency") or ccy).upper() \
        if isinstance(deposit, dict) else ccy
    try:
        result = ingest_deposit(_database(), deposit) if dep_id else None
    except Exception as exc:
        log.exception("intake failed for deposit %s", dep_id)
        result = None

    head = (f"Deposit {money_str(amount_minor, credited)}"
            f"{' from ' + payer if payer else ''} created in the sandbox "
            f"({credited} account ending "
            f"{(account['account_number'] or '?')[-4:]})")
    if result is None:
        return _back(request, tab="money-in",
                     ok=f"{head}. It will appear once Airwallex reports it.")
    if result.duplicate:
        return _back(request, tab="money-in", ok=f"{head}. {result.reason}")
    if result.disposition == "PROPOSED":
        return _back(request, tab="money-in", ok=(
            f"{head}. Read as {result.status} via {result.method} at "
            f"{result.confidence:.2f} — BELOW the automatic threshold, so "
            f"nothing has been applied. The invoice has not moved and the cash "
            f"is in suspense awaiting your approval. {result.reason}"
            + _run_loop("deposit")))
    applied = f" -> {', '.join(result.applied)}" if result.applied else ""
    return _back(request, tab="money-in", ok=(
        f"{head}. Matched {result.status} via {result.method} at "
        f"{result.confidence:.2f}{applied}. {result.reason}"
        + _run_loop("deposit")))


@app.post("/money-in/poll")
async def money_in_poll(request: Request):
    """The polling fallback for a webhook that is late or never arrives."""
    from .domain.intake import poll_deposits

    guard = _require_sandbox()
    if guard:
        return _back(request, tab="money-in", err=guard)
    try:
        results = poll_deposits(_database())
    except Exception as exc:
        log.exception("deposit poll failed")
        return _back(request, tab="money-in", err=f"Could not poll: {exc}")
    if not results:
        return _back(request, tab="money-in",
                     ok="Checked Airwallex: no new deposits since the last one.")
    bits = "; ".join(f"{r.deposit_id[:8]} {r.status} "
                     f"({r.confidence:.2f}, {r.method})" for r in results)
    return _back(request, tab="money-in",
                 ok=f"Picked up {len(results)} deposit(s) by polling: {bits}")


# --------------------------------------------------------------- setup (P1)
# Every screen here WRITES ONLY THE BOOK. None of these routes can move money:
# there is no call to create-transfer, create-conversion or issuing anywhere
# below. Money still moves only through planner -> policy -> executor.
#
# Errors come back as a redirect carrying a plain sentence, so a refusal is
# visible on the page that caused it rather than in a stack trace.

def _require_sandbox() -> Optional[str]:
    """Sandbox-only guard for anything that creates a REAL Airwallex object."""
    base = (_settings.awx_base_url or "")
    if "sandbox" not in base:
        return (f"Refused: {base} is not a sandbox host. Closebook only ever "
                f"talks to the Airwallex sandbox.")
    return None


def _back(request: Request, tab: str = "setup", ok: str = "", err: str = ""):
    """Return ONE message, in whatever shape the caller can actually read.

    A browser form gets a 303 with the message on the query string: no flash
    state to lose, and it works with JavaScript turned off.

    The page's own forms use `fetch`, and a fetch cannot see through a redirect —
    it follows it, lands on 200 HTML, and a client that judges success by status
    code then reports success for a REFUSAL. So when a caller asks for JSON, it
    gets the message as JSON and decides on the message, not on the status.
    """
    from urllib.parse import urlencode
    from fastapi.responses import JSONResponse, RedirectResponse

    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse(_action_body(tab, ok, err))
    q = {"tab": tab}
    if ok:
        q["ok"] = ok
    if err:
        q["err"] = err
    return RedirectResponse(f"/app?{urlencode(q)}", status_code=303)


def _beneficiary_client():
    from .airwallex.beneficiaries import Beneficiaries
    from .airwallex.client import AirwallexClient
    return Beneficiaries(AirwallexClient())


@app.post("/setup/settings")
async def setup_settings(request: Request):
    """Save the workspace's policy. Database, not .env."""
    from .domain.settings_store import SettingsRefused, save

    b = await _read_body(request)
    try:
        with _database().session_scope() as s:
            save(s, budget_currency=str(b.get("budget_currency") or "USD"),
                 reserve_floor=str(b.get("reserve_floor") or ""),
                 auto_pay_limit=str(b.get("auto_pay_limit") or ""),
                 fx_tolerance_pct=str(b.get("fx_tolerance_pct") or "0.50"))
        return _back(request, ok="Settings saved. The planner and the tiles both "
                                 "read these numbers.")
    except SettingsRefused as exc:
        return _back(request, err=str(exc))


@app.post("/setup/customers")
async def setup_customers(request: Request):
    from .domain.workspace import WorkspaceRefused, create_customer

    b = await _read_body(request)
    try:
        with _database().session_scope() as s:
            row = create_customer(s, name=str(b.get("name") or ""),
                                  email=str(b.get("email") or ""),
                                  country=str(b.get("country") or ""))
            name = row.name
        return _back(request, ok=f"Added customer {name}.")
    except WorkspaceRefused as exc:
        return _back(request, err=str(exc))


@app.post("/setup/invoices")
async def setup_invoices(request: Request):
    from .domain.workspace import WorkspaceRefused, create_invoice

    b = await _read_body(request)
    try:
        with _database().session_scope() as s:
            row = create_invoice(
                s, customer_id=int(b.get("customer_id") or 0),
                number=str(b.get("number") or ""),
                currency=str(b.get("currency") or ""),
                amount=str(b.get("amount") or ""),
                due_date=str(b.get("due_date") or ""))
            num = row.number
        return _back(request, ok=f"Added invoice {num}.")
    except (WorkspaceRefused, ValueError) as exc:
        return _back(request, err=str(exc))


@app.post("/setup/suppliers")
async def setup_suppliers(request: Request):
    """Create the supplier AND its real Airwallex beneficiary.

    The buyer and the API result are stored together, so a supplier whose
    beneficiary was refused is a visible row carrying Airwallex's own words —
    not a missing row somebody has to remember to retry.
    """
    from .domain.beneficiary_form import validate_and_create
    from .domain.workspace import WorkspaceRefused, create_supplier, record_beneficiary

    guard = _require_sandbox()
    if guard:
        return _back(request, err=guard)

    b = await _read_body(request)
    form = {k: (v if isinstance(v, str) else "") for k, v in b.items()}
    try:
        with _database().session_scope() as s:
            sup = create_supplier(
                s, name=str(b.get("name") or ""),
                country=str(b.get("country") or ""),
                currency=str(b.get("currency") or ""),
                payout_method=str(b.get("payout_method") or "LOCAL"))
            supplier_id, supplier_name = sup.id, sup.name
    except WorkspaceRefused as exc:
        return _back(request, err=str(exc))

    outcome = validate_and_create(None, form, beneficiaries=_beneficiary_client())

    with _database().session_scope() as s:
        record_beneficiary(s, supplier_id, request=outcome.request,
                           beneficiary_id=outcome.beneficiary_id,
                           status=outcome.status, error=outcome.error)

    if outcome.ok:
        return _back(request, ok=f"{supplier_name} is payable — Airwallex "
                                 f"beneficiary {outcome.beneficiary_id} "
                                 f"({outcome.status}).")
    return _back(request, err=f"{supplier_name} was saved but its beneficiary "
                              f"was refused. {outcome.error}")


@app.post("/setup/suppliers/{supplier_id}/beneficiary")
async def setup_supplier_beneficiary(supplier_id: int, request: Request):
    """Retry beneficiary creation for a supplier whose first attempt failed."""
    from .domain.beneficiary_form import validate_and_create
    from .domain.workspace import record_beneficiary
    from .models import Supplier

    guard = _require_sandbox()
    if guard:
        return _back(request, err=guard)

    b = await _read_body(request)
    form = {k: (v if isinstance(v, str) else "") for k, v in b.items()}
    with _database().session_scope() as s:
        sup = s.get(Supplier, supplier_id)
        if sup is None:
            return _back(request, err="That supplier no longer exists.")
        name = sup.name

    outcome = validate_and_create(None, form, beneficiaries=_beneficiary_client())
    with _database().session_scope() as s:
        record_beneficiary(s, supplier_id, request=outcome.request,
                           beneficiary_id=outcome.beneficiary_id,
                           status=outcome.status, error=outcome.error)
    if outcome.ok:
        return _back(request, ok=f"{name} is payable — beneficiary "
                                 f"{outcome.beneficiary_id} ({outcome.status}).")
    return _back(request, err=f"{name}: {outcome.error}")


@app.post("/setup/payables")
async def setup_payables(request: Request):
    from .domain.workspace import WorkspaceRefused, create_payable

    b = await _read_body(request)
    try:
        with _database().session_scope() as s:
            row = create_payable(
                s, supplier_id=int(b.get("supplier_id") or 0),
                currency=str(b.get("currency") or ""),
                amount=str(b.get("amount") or ""),
                number=str(b.get("number") or ""),
                depends_on=str(b.get("depends_on") or ""),
                criticality=int(b.get("criticality") or 3),
                due_date=str(b.get("due_date") or ""))
            num = row.number
        return _back(request, ok=f"Added bill {num}. The planner will look at it "
                                 f"on the next event.")
    except (WorkspaceRefused, ValueError) as exc:
        return _back(request, err=str(exc))


@app.post("/workspace/reset")
async def workspace_reset(request: Request):
    """Archive the book to a file, then empty it. Confirmation required."""
    from .domain.workspace import WorkspaceRefused, archive_and_reset

    b = await _read_body(request)
    try:
        arch = archive_and_reset(_database(), confirm=str(b.get("confirm") or ""))
        total = sum(arch.cleared.values())
        return _back(request, ok=f"Workspace archived to {arch.path} and cleared "
                                 f"({total} rows). Your reserve floor and limit "
                                 f"were kept.")
    except WorkspaceRefused as exc:
        return _back(request, err=str(exc))
    except Exception as exc:
        log.exception("workspace reset failed")
        return _back(request, err=f"Could not reset the workspace: {exc}")


@app.get("/", response_class=HTMLResponse)
def landing() -> str:
    """The landing page, with ONE live proof taken from this instance's book.

    It used to be "illustrative copy only — no live figures", which is how it
    ended up carrying an invented testimonial and a decorative "0.96". A page
    that claims things about a product's results should be able to show them,
    so the figures are counted from the same tables the app renders.
    """
    from .dashboard import landing_stats
    from .ui import render_landing

    stats = None
    try:
        with _database().session() as s:
            stats = landing_stats(s)
    except Exception:
        log.warning("could not read the landing figures")
    return render_landing(stats)


@app.get("/app", response_class=HTMLResponse)
def app_page(background: BackgroundTasks, tab: str = "home",
             ok: str = "", err: str = "") -> str:
    """The operator app. Everything on it was decided by the deterministic layer
    before this function ran — the page only presents.

    `ok`/`err` are the one-line result of a setup POST, carried on the query
    string rather than in a session: a redirect with a query string cannot go
    stale, and there is no flash state to lose.

    Falls back to a plain page with links to the JSON endpoints rather than an
    error, so a rendering fault never hides the state.
    """
    from .dashboard import build_context
    from .ui import render_app

    try:
        ctx = build_context(INCIDENTS)
    except Exception as exc:
        log.exception("dashboard context failed")
        # NAME THE REASON. This page used to say only "The app could not read
        # state", which tells the operator nothing about whether the books are
        # safe, whether to retry, or what to fix — and it is the exact sentence
        # an automated tester reported back. A failure message must carry enough
        # to act on.
        import html as _html

        reason = f"{type(exc).__name__}: {exc}".strip()
        return ("<html><body style='font-family:system-ui;padding:2rem;max-width:44rem'>"
                "<h1>Closebook</h1>"
                "<p><b>The app could not read its state.</b> Nothing was changed "
                "by this request — the books are exactly as they were.</p>"
                f"<p style='font-family:ui-monospace,monospace;background:#F4F2EC;"
                f"padding:.75rem;border-radius:8px;overflow-wrap:anywhere'>"
                f"{_html.escape(reason)}</p>"
                "<p>Retry this page, or read "
                "<a href='/state'>/state</a> and "
                "<a href='/healthz'>/healthz</a> directly.</p>"
                "</body></html>")
    if ok:
        ctx["flash_ok"] = ok
    if err:
        ctx["flash_err"] = err
    health = healthz()
    ctx["sandbox_ready"] = bool(health.get("sandbox_ready"))

    # THE PAGE NEVER WAITS ON AIRWALLEX. The render above read cached transfer
    # statuses; anything stale is refreshed AFTER this response has gone out, so
    # a slow or dead sandbox costs a slightly old label, never a slow page. This
    # is the whole of the 3.1-second render.
    try:
        from .dashboard import refresh_live_statuses, stale_live_status_ids
        with _database().session() as s:
            stale = stale_live_status_ids(s)
        if stale:
            background.add_task(refresh_live_statuses, stale)
        # The global-account list is fetched the same way and for the same
        # reason: it is what made a COLD first render wait on a token fetch.
        from .domain.intake import accounts_stale, refresh_accounts
        # NOBODY WAITS FOR AIRWALLEX. The accounts list is warmed at STARTUP and
        # refreshed after the response; if it is still not here the form renders
        # "Loading accounts…" and fills itself in. Waiting for it inside the
        # render put ~2.8s on the first Money in view after a restart, which is
        # a slow page in exchange for a picker that was going to arrive anyway.
        if accounts_stale():
            background.add_task(refresh_accounts, _database())
    except Exception:
        log.warning("could not schedule the live-status refresh")

    return render_app(ctx, tab=tab, health=health)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", app_dir=__import__("os").path.dirname(
        __import__("os").path.dirname(__file__.replace("main.py", ""))
    ), port=8000, reload=True)
