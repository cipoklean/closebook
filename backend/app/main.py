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
import time
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from .airwallex.webhooks import WebhookHandler
from .config import get_settings
from .db import DB, get_db
from .domain.incidents import Incident, Recovery, handle_event, parse_payload
from .models import InvoicePayable, InvoiceReceivable

log = logging.getLogger("closebook")
logging.basicConfig(level="INFO", force=True)

app = FastAPI(title="Closebook", version="0.1.0")
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


@app.post("/approvals/{step_id}/approve")
def approve(step_id: int, body: dict) -> dict:
    """Record a HUMAN approval. Does not move money.

    Execution still re-verifies the bound hash, amount, counterparty and FX
    rate through the executor — approving here only records the decision.
    """
    from .domain.approval_api import ApprovalRefused, approve_step

    pin = str(body.get("pin") or "")
    by = str(body.get("approved_by") or "dashboard")
    configured = _settings.demo_approver_pin.get_secret_value()
    if not configured:
        return {"error": "DEMO_APPROVER_PIN is not configured — the approval "
                         "endpoint is closed rather than open."}
    try:
        return approve_step(_db, step_id, pin=pin, approved_by=by,
                            configured_pin=configured)
    except ApprovalRefused as exc:
        return {"error": str(exc)}


@app.post("/approvals/{step_id}/reject")
def reject(step_id: int, body: dict) -> dict:
    from .domain.approval_api import ApprovalRefused, reject_step

    pin = str(body.get("pin") or "")
    by = str(body.get("rejected_by") or "dashboard")
    reason = str(body.get("reason") or "")
    configured = _settings.demo_approver_pin.get_secret_value()
    if not configured:
        return {"error": "DEMO_APPROVER_PIN is not configured — the approval "
                         "endpoint is closed rather than open."}
    try:
        return reject_step(_db, step_id, pin=pin, rejected_by=by,
                           reason=reason, configured_pin=configured)
    except ApprovalRefused as exc:
        return {"error": str(exc)}


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
        if ev.name in {"payout.transfer.failed", "payout.transfer.cancelled",
                       "payout.transfer.paid"}:
            transfer_id = payload.get("transfer_id") or payload.get("transaction_id")
            if transfer_id:
                try:
                    live_status = _transfer_client().status_of(transfer_id)
                except Exception:
                    log.warning("lookup failed for %s", transfer_id)
        incident = handle_event(ev.name, payload, event_id=ev.event_id,
                                live_status=live_status)
        if incident.recovery is not Recovery.IGNORE:
            log.warning("INCIDENT %s", incident.describe())
            INCIDENTS.append(incident)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """The dashboard. Everything on it was decided by the deterministic layer
    before this function ran — the page only presents."""
    from .dashboard import build_context
    from .ui import render
    try:
        ctx = build_context(_db, INCIDENTS)
    except Exception:
        log.exception("dashboard context failed")
        return ("<html><body style='font-family:system-ui;padding:2rem'>"
                "<h1>Closebook</h1><p>The dashboard could not read state. "
                "Try <a href='/state'>/state</a> or <a href='/healthz'>/healthz</a>."
                "</p></body></html>")
    return render(ctx)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", app_dir=__import__("os").path.dirname(
        __import__("os").path.dirname(__file__.replace("main.py", ""))
    ), port=8000, reload=True)
