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

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .airwallex.webhooks import WebhookHandler
from .config import get_settings
from .db import DB, get_db, get_default_db
from .domain.incidents import Incident, Recovery, handle_event, parse_payload
from .models import InvoicePayable, InvoiceReceivable

log = logging.getLogger("closebook")
logging.basicConfig(level="INFO", force=True)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(_STATIC_DIR, exist_ok=True)

app = FastAPI(title="Closebook", version="0.1.0")
# Serve the stylesheet. `html=False` is deliberately omitted so the page is
# usable with JS off; the only script is a small enhancement layer.
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
_db = DB()
_settings = get_settings()
_webhooks = WebhookHandler(_db, _settings.awx_webhook_secret.get_secret_value())
# Lazy: resolving the Airwallex client touches .env credentials, and importing
# main.py must not require live credentials (tests import it).
_transfers = None
# Incidents raised by processed webhooks, newest last. In-memory on purpose —
# this is a demo view; incidents are also written to plan_step/transfer state.
INCIDENTS: list[Incident] = []


def _transfer_client():
    global _transfers
    if _transfers is None:
        from .airwallex.client import AirwallexClient
        from .airwallex.transfers import Transfers
        _transfers = Transfers(AirwallexClient())
    return _transfers


@app.get("/healthz")
def healthz() -> dict:
    return {
        "ok": True,
        "sandbox_ready": _settings.sandbox_ready(),
        "webhook_url": _settings.webhook_url,
    }


@app.get("/state")
def state() -> dict:
    """Read-only snapshot for the dashboard (balances come from the cache,
    not a live API call, so the view stays deterministic)."""
    with _db.session() as s:
        recs = s.query(InvoiceReceivable).all()
        pays = s.query(InvoicePayable).all()
        return {
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
    }


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

    with _db.session() as s:
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


@app.post("/approvals/{step_id}/approve")
async def approve(step_id: int, request: Request) -> dict:
    """Record a HUMAN approval. Does not move money.

    Execution still re-verifies the bound hash, amount, counterparty and FX
    rate through the executor — approving here only records the decision.

    Accepts JSON or a form post so the dashboard's plain form works.
    """
    from .domain.approval_api import ApprovalRefused, approve_step

    body = await _read_body(request)
    pin = str(body.get("pin") or "")
    by = str(body.get("approved_by") or "dashboard")
    # A missing SecretStr is not an empty one; treat both as "closed".
    _pin_field = _settings.demo_approver_pin
    configured = (_pin_field.get_secret_value()
                   if _pin_field is not None else "")
    if not configured:
        return {"detail": "DEMO_APPROVER_PIN is not configured — the approval "
                          "endpoint is closed rather than open."}
    try:
        return approve_step(_db, step_id, pin=pin, approved_by=by,
                            configured_pin=configured)
    except ApprovalRefused as exc:
        return {"detail": str(exc)}


@app.post("/approvals/{step_id}/reject")
async def reject(step_id: int, request: Request) -> dict:
    from .domain.approval_api import ApprovalRefused, reject_step

    body = await _read_body(request)
    pin = str(body.get("pin") or "")
    by = str(body.get("rejected_by") or "dashboard")
    reason = str(body.get("reason") or "")
    # A missing SecretStr is not an empty one; treat both as "closed".
    _pin_field = _settings.demo_approver_pin
    configured = (_pin_field.get_secret_value()
                   if _pin_field is not None else "")
    if not configured:
        return {"detail": "DEMO_APPROVER_PIN is not configured — the rejection "
                          "endpoint is closed rather than open."}
    try:
        return reject_step(_db, step_id, pin=pin, rejected_by=by,
                           reason=reason, configured_pin=configured)
    except ApprovalRefused as exc:
        return {"detail": str(exc)}


@app.post(_settings.webhook_path)
async def webhook(request: Request, background: BackgroundTasks) -> Response:
    """Ack-fast (200) so Airwallex never retries on a slow handler, then
    process in the background. Bad signature -> 400, also returned fast."""
    raw = await request.body()
    ts = request.headers.get("x-timestamp", "")
    sig = request.headers.get("x-signature", "")
    client_secret = request.headers.get("client-secret-key")  # test events
    status = _webhooks.receive(raw, ts, sig, client_secret)
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
    for ev in _webhooks.claim_all_pending():
        log.info("processing webhook %s (%s)", ev.event_id, ev.name)
        payload = parse_payload(ev.payload)
        live_status = None
        transfer_id = None
        if ev.name in {"payout.transfer.failed", "payout.transfer.cancelled",
                       "payout.transfer.paid"}:
            transfer_id = payload.get("transfer_id") or payload.get("transaction_id")
            if transfer_id:
                try:
                    live_status = _transfer_client().status_of(transfer_id)
                except Exception:
                    log.warning("lookup failed for %s", transfer_id)
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
                    res = ingest_deposit(_db, candidate)
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
        accts = accounts_cached(_db)
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
        result = ingest_deposit(_db, deposit) if dep_id else None
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
    applied = f" -> {', '.join(result.applied)}" if result.applied else ""
    return _back(request, tab="money-in", ok=(
        f"{head}. Matched {result.status} via {result.method} at "
        f"{result.confidence:.2f}{applied}. {result.reason}"))


@app.post("/money-in/poll")
async def money_in_poll(request: Request):
    """The polling fallback for a webhook that is late or never arrives."""
    from .domain.intake import poll_deposits

    guard = _require_sandbox()
    if guard:
        return _back(request, tab="money-in", err=guard)
    try:
        results = poll_deposits(_db)
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
    """Redirect back to the page with ONE message. No flash state to lose."""
    from urllib.parse import urlencode
    from fastapi.responses import RedirectResponse
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
        with _db.session_scope() as s:
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
        with _db.session_scope() as s:
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
        with _db.session_scope() as s:
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
        with _db.session_scope() as s:
            sup = create_supplier(
                s, name=str(b.get("name") or ""),
                country=str(b.get("country") or ""),
                currency=str(b.get("currency") or ""),
                payout_method=str(b.get("payout_method") or "LOCAL"))
            supplier_id, supplier_name = sup.id, sup.name
    except WorkspaceRefused as exc:
        return _back(request, err=str(exc))

    outcome = validate_and_create(None, form, beneficiaries=_beneficiary_client())

    with _db.session_scope() as s:
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
    with _db.session_scope() as s:
        sup = s.get(Supplier, supplier_id)
        if sup is None:
            return _back(request, err="That supplier no longer exists.")
        name = sup.name

    outcome = validate_and_create(None, form, beneficiaries=_beneficiary_client())
    with _db.session_scope() as s:
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
        with _db.session_scope() as s:
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
        arch = archive_and_reset(_db, confirm=str(b.get("confirm") or ""))
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
    """The landing page. Illustrative copy only — no live figures."""
    from .ui import render_landing
    return render_landing()


@app.get("/app", response_class=HTMLResponse)
def app_page(tab: str = "home", ok: str = "", err: str = "") -> str:
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
    except Exception:
        log.exception("dashboard context failed")
        return ("<html><body style='font-family:system-ui;padding:2rem'>"
                "<h1>Closebook</h1><p>The app could not read state. "
                "Try <a href='/state'>/state</a> or <a href='/healthz'>/healthz</a>."
                "</p></body></html>")
    if ok:
        ctx["flash_ok"] = ok
    if err:
        ctx["flash_err"] = err
    health = healthz()
    ctx["sandbox_ready"] = bool(health.get("sandbox_ready"))
    return render_app(ctx, tab=tab, health=health)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", app_dir=__import__("os").path.dirname(
        __import__("os").path.dirname(__file__.replace("main.py", ""))
    ), port=8000, reload=True)
