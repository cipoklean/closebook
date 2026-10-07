"""FastAPI app — webhook receiver + API + static dashboard (Phase 5).

Endpoints wired now:
  GET  /healthz            liveness + sandbox-readiness flag
  GET  /state              read-only snapshot of balances/receivables/payables
  POST {webhook_path}     Airwallex webhook: verify -> 200 fast -> process later

The dashboard HTML (inflow feed, plan board, approval queue, ledger tab,
"run demo step" button) lands in Phase 5; this returns a minimal index today.
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
    s = _db.session()
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
    """
    for ev in _webhooks.pending_events():
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
        _webhooks.mark_processed(ev.event_id)


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
