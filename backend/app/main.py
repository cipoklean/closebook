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
from .models import InvoicePayable, InvoiceReceivable

log = logging.getLogger("closebook")
logging.basicConfig(level="INFO", force=True)

app = FastAPI(title="Closebook", version="0.1.0")
_db = DB()
_settings = get_settings()
_webhooks = WebhookHandler(_db, _settings.awx_webhook_secret.get_secret_value())


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
    """Pull unprocessed webhook events in created_at order and hand them to
    the executor (Phase 3 wires the real handler; for now, mark processed)."""
    for ev in _webhooks.pending_events():
        log.info("processing webhook %s (%s)", ev.event_id, ev.name)
        _webhooks.mark_processed(ev.event_id)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (
        "<html><head><title>Closebook</title></head>"
        "<body style='font-family:system-ui;padding:2rem'>"
        "<h1>Closebook — dashboard (Phase 5)</h1>"
        "<p>Webhook receiver live at "
        f"<code>{_settings.webhook_url}</code>. Sandbox ready: "
        f"{'yes' if _settings.sandbox_ready() else 'no (.env empty)'}</p>"
        "<ul><li><a href='/healthz'>/healthz</a></li>"
        "<li><a href='/state'>/state</a></li></ul>"
        "</body></html>"
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", app_dir=__import__("os").path.dirname(
        __import__("os").path.dirname(__file__.replace("main.py", ""))
    ), port=8000, reload=True)
