"""The one-page dashboard — what a judge actually looks at.

Design follows the house style: cream ground (#F9F7F2), Playfair-style serif
headlines against a clean sans body, ONE muted accent, generous whitespace,
pill buttons, and no heavy shadows.

No build step and no JS framework on purpose. The deadline is real, the demo has
to start with `make dev`, and a CDN dependency would be one more thing that
breaks on a hotel wifi. The only JS is a 10-line poller.

Everything rendered here is read from the deterministic layer. The dashboard
never computes a decision — if a number appears on this page, code already
decided it.
"""
from __future__ import annotations

import html
import json
from typing import Any, Iterable

CSS = """
:root{
  --ground:#F9F7F2; --card:#FFFFFF; --ink:#22201C; --muted:#7A756B;
  --line:#E7E1D6; --accent:#5F7A6B; --accent-soft:#EDF2EE;
  --warn:#B07D3A; --warn-soft:#FBF3E7; --crit:#A85751; --crit-soft:#FAEDEC;
  --good:#4F7A5B; --good-soft:#EDF4EE;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  font-size:15px; line-height:1.6;
}
h1,h2,.serif{font-family:"Playfair Display",Georgia,"Times New Roman",serif;
  font-weight:600; letter-spacing:-.01em}
h1{font-size:2rem; margin:0}
h2{font-size:1.05rem; margin:0 0 .75rem; color:var(--ink)}
.wrap{max-width:1180px; margin:0 auto; padding:2.5rem 1.5rem 4rem}
header{display:flex; justify-content:space-between; align-items:flex-end;
  gap:1.5rem; flex-wrap:wrap; margin-bottom:2.25rem}
.sub{color:var(--muted); margin:.35rem 0 0; font-size:.9rem}
.pill{display:inline-block; padding:.3rem .85rem; border-radius:999px;
  font-size:.78rem; font-weight:500; letter-spacing:.02em;
  background:var(--accent-soft); color:var(--accent); border:1px solid var(--line)}
.pill.warn{background:var(--warn-soft); color:var(--warn)}
.pill.crit{background:var(--crit-soft); color:var(--crit)}
.pill.good{background:var(--good-soft); color:var(--good)}
.grid{display:grid; gap:1.25rem; grid-template-columns:repeat(auto-fit,minmax(310px,1fr))}
.card{background:var(--card); border:1px solid var(--line); border-radius:14px;
  padding:1.5rem}
.card.wide{grid-column:1/-1}
.stat{font-family:"Playfair Display",Georgia,serif; font-size:1.9rem;
  line-height:1.15; margin:.15rem 0}
.lbl{color:var(--muted); font-size:.78rem; text-transform:uppercase;
  letter-spacing:.07em}
table{width:100%; border-collapse:collapse; font-size:.88rem}
th{text-align:left; color:var(--muted); font-weight:500; font-size:.74rem;
  text-transform:uppercase; letter-spacing:.06em; padding:.5rem .5rem .5rem 0;
  border-bottom:1px solid var(--line)}
td{padding:.6rem .5rem .6rem 0; border-bottom:1px solid var(--line);
  vertical-align:top}
tr:last-child td{border-bottom:none}
.num{font-variant-numeric:tabular-nums; white-space:nowrap}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.8rem;
  color:var(--muted)}
button{font:inherit; cursor:pointer; border-radius:999px; padding:.4rem 1.1rem;
  border:1px solid var(--line); background:var(--card); color:var(--ink);
  transition:background .15s ease}
button:hover{background:var(--accent-soft)}
button.approve{background:var(--good-soft); border-color:var(--good);
  color:var(--good)}
button.reject{background:var(--crit-soft); border-color:var(--crit);
  color:var(--crit)}
input[type=password]{font:inherit; padding:.35rem .7rem;
  border:1px solid var(--line); border-radius:999px; background:var(--card);
  color:var(--ink); width:9rem}
.terms{font-size:.84rem; line-height:1.7}
.terms b{font-weight:600}
.muted{color:var(--muted)}
.empty{color:var(--muted); font-style:italic; padding:.5rem 0}
.note{background:var(--accent-soft); border-left:3px solid var(--accent);
  padding:.85rem 1rem; border-radius:0 8px 8px 0; font-size:.87rem;
  margin-bottom:1.25rem}
ul.tight{margin:.35rem 0 0; padding-left:1.15rem} ul.tight li{margin:.2rem 0}
a{color:var(--accent)}
footer{margin-top:2.5rem; padding-top:1.25rem; border-top:1px solid var(--line);
  color:var(--muted); font-size:.82rem}
@media(max-width:600px){ .wrap{padding:1.5rem 1rem 3rem} h1{font-size:1.6rem} }
"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _money(minor: Any, currency: str = "") -> str:
    try:
        return f"{int(minor) / 100:,.2f}{(' ' + currency) if currency else ''}"
    except (TypeError, ValueError):
        return _e(minor)


def _pill(text: str, kind: str = "") -> str:
    cls = f"pill {kind}".strip()
    return f'<span class="{cls}">{_e(text)}</span>'


def _status_pill(status: str) -> str:
    s = (status or "").upper()
    good = {"PAID", "MATCHED", "DONE", "COMPLETE", "COMPLETED", "PAID_IN_FULL"}
    warn = {"PARTIAL", "PENDING", "PROCESSING", "HELD", "READY", "AMBIGUOUS",
            "IN_FLIGHT_WATCH", "IN_FLIGHT", "RESOLVE_BEFORE_RETRY",
            "RESOLVE", "SUBMITTED", "UNMATCHED", "NEEDS_APPROVAL", "FIX_BENEFICIARY",
            "REPLAN", "HUMAN_REVIEW"}
    if s in good:
        return _pill(s, "good")
    if s in warn:
        return _pill(s, "warn")
    return _pill(s)


def _table(headers: list[str], rows: Iterable[list[str]],
           empty: str = "Nothing here yet.") -> str:
    rows = list(rows)
    if not rows:
        return f'<p class="empty">{_e(empty)}</p>'
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
                   for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render(ctx: dict[str, Any]) -> str:
    """Build the whole page from a pre-read context dict.

    The caller gathers data; this only presents it. Keeping presentation free of
    queries means the dashboard cannot accidentally disagree with the API.
    """
    budget = ctx.get("budget") or {}
    committed = budget.get("committed_minor")
    cash = ctx.get("cash") or {}
    cash_usd = cash.get("USD", 0)

    stats = f"""
    <div class="grid">
      <div class="card">
        <div class="lbl">Committed cash (policy)</div>
        <div class="stat">{_money(committed, 'USD')}</div>
        <div class="sub">reserve floor {_money(budget.get('reserve_floor_minor'), 'USD')}
          &middot; auto-pay limit {_money(budget.get('auto_pay_limit_minor'), 'USD')}</div>
      </div>
      <div class="card">
        <div class="lbl">Ledger cash</div>
        <div class="stat">{_money(cash_usd, 'USD')}</div>
        <div class="sub">{_e(', '.join(f'{k} {v/100:,.2f}' for k, v in sorted(cash.items())) or 'no postings')}</div>
      </div>
      <div class="card">
        <div class="lbl">Needs a human</div>
        <div class="stat">{len(ctx.get('incidents') or [])}</div>
        <div class="sub">{_e(str(len(ctx.get('pending_approvals') or [])))} approvals waiting
          &middot; {_e(str(len(ctx.get('ambiguous_inflows') or [])))} ambiguous inflows</div>
      </div>
    </div>"""

    plan_rows = []
    for s in (ctx.get("plan") or []):
        action = (s.get("action") or "").upper()
        # An FX step stores what LEAVES in sell_amount_minor; a payment stores
        # what ARRIVES in amount_minor. Showing `amount_minor` for both renders
        # every conversion as 0.00, because an FX row's amount_minor is not the
        # traded amount. Read the field that matches the step type.
        if action == "FX":
            ccy = s.get("sell_currency") or s.get("currency")
            amt = s.get("sell_amount_minor")
            if amt is None:
                amt = s.get("amount_minor")
            extra = (f"buy {s.get('buy_currency')}"
                     if s.get("buy_currency") else "")
        else:
            ccy = s.get("currency")
            amt = s.get("amount_minor")
            extra = ""
        why = (s.get("reason") or "")
        if extra:
            why = f"{why} ({extra})" if why else extra
        plan_rows.append([
            _e(action), _e(s.get("payable_number") or "-"),
            _e(ccy or ""), _money(amt), _status_pill(s.get("status") or "proposed"),
            f'<span class="muted">{_e(why)}</span>',
        ])

    appr_rows = [
        [_e(a.get("payable_number")), _e(a.get("currency")),
         _money(a.get("amount_minor")), _status_pill("needs approval"),
         f'<span class="mono">{_e((a.get("bound_hash") or "")[:16])}</span>',
         f'<span class="muted">{_e(a.get("reason") or "")}</span>']
        for a in (ctx.get("pending_approvals") or [])
    ]

    rec_rows = [
        [_e(r.get("number")), _e(r.get("customer")), _e(r.get("currency")),
         _money(r.get("amount_minor")), _money(r.get("paid_minor")),
         _money(r.get("open_minor")), _status_pill(r.get("status"))]
        for r in (ctx.get("receivables") or [])
    ]

    pay_rows = [
        [_e(p.get("number")), _e(p.get("supplier")), _e(p.get("currency")),
         _money(p.get("amount_minor")), _e(p.get("payout_method")),
         _status_pill(p.get("status")),
         f'<span class="muted">crit {_e(p.get("criticality"))}</span>']
        for p in (ctx.get("payables") or [])
    ]

    inc_rows = [
        [_status_pill(i.get("recovery")), _e(i.get("severity")),
         f'<span class="mono">{_e((i.get("transfer_id") or "-")[:18])}</span>',
         f'<span class="muted">{_e(i.get("summary") or "")}</span>',
         _pill("new id required") if i.get("requires_new_request_id")
         else _pill("no action", "good")]
        for i in (ctx.get("incidents") or [])
    ]

    susp = ctx.get("suspense") or {}
    recon_ok = ctx.get("ledger_balanced")
    recon_note = (f'<div class="note">Every posting balances'
                  if recon_ok else
                  f'<div class="note" style="background:var(--crit-soft);'
                  f'border-color:var(--crit)"><strong>Ledger does not balance.</strong>')

    susp_line = (f'<p class="sub">Unapplied, awaiting a human: '
                 f'{_e(", ".join(f"{k} {v/100:,.2f}" for k, v in sorted(susp.items())))}</p>'
                 if susp else "")

    # --- approval queue: bound terms + Approve/Reject ----------------
    appr_queue = []
    for a in (ctx.get("approval_queue") or []):
        amt = a.get("amount_minor")
        ccy = a.get("currency") or ""
        appr_queue.append(f"""
      <div class="terms">
        <div><b>{_e(a.get("payable_number") or "step %s" % a.get("step_id"))}</b>
          &mdash; {_money(amt, ccy)} to <b>{_e(a.get("counterparty") or "?")}</b>
          {_pill("approved", "good") if a.get("already_approved") else ""}</div>
        <div class="mono">hash {_e(a.get("hash_prefix") or "—")}
          &middot; FX quote {_e(a.get("fx_quote_id") or "—")}
          &middot; rate {_e(a.get("fx_rate") or "—")}</div>
        <div class="muted">{_e((a.get("reason") or "")[:160])}</div>
        <div style="margin-top:.5rem">
          <input type="password" id="pin-{a.get('step_id')}" placeholder="demo PIN">
          <button class="approve" onclick="decide({a.get('step_id')}, 'approve')">Approve</button>
          <button class="reject" onclick="decide({a.get('step_id')}, 'reject')">Reject</button>
        </div>
      </div>""")

    trace_rows = []
    for t in (ctx.get("agent_trace") or []):
        verdict = str(t.get("verdict") or "").upper()
        if verdict == "REFUSED":
            kind = "crit"
        elif verdict in {"REJECTED", "ESCALATED"}:
            kind = "warn"
        else:
            kind = "good"
        inp = t.get("inputs") or {}
        out = t.get("outputs") or {}
        proposed = out.get("model_proposed") if isinstance(out, dict) else None
        trace_rows.append([
            f'<span class="mono">{_e(t.get("tool"))}</span>',
            _pill(verdict, kind),
            f'<span class="mono">{_e(json.dumps(inp, default=str)[:120])}</span>',
            (f'<span class="muted">model proposed: '
             f'{_e(json.dumps(proposed, default=str)[:120])}</span>'
             if proposed else
             f'<span class="muted">{_e((t.get("detail") or "")[:120])}</span>'),
        ])

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Closebook — Kano Craft Imports</title>
<style>{CSS}</style></head><body><div class="wrap">
<header>
  <div>
    <h1>Closebook</h1>
    <p class="sub">Continuous treasury &amp; payments for
      <strong>Kano Craft Imports Ltd</strong> — Lagos importer,
      USD/GBP in, EUR/USD out.</p>
  </div>
  <div>{_pill("Airwallex sandbox", "good")}</div>
</header>

<div class="note">
  The model proposes; deterministic code enforces. Amounts, limits, approvals,
  the reserve floor, idempotency and duplicate protection are all decided in
  code &mdash; never by a language model. Every number below was decided before
  it was rendered.
</div>

{stats}

<div class="grid" style="margin-top:1.25rem">
  <div class="card wide">
    <h2>Treasury plan</h2>
    {_table(["Action", "Payable", "Ccy", "Amount", "State", "Why"], plan_rows,
            "No plan yet — run `make demo`.")}
  </div>

  <div class="card">
    <h2>Approvals waiting on a human</h2>
    {_table(["Payable", "Ccy", "Amount", "State", "Bound to", "Why"],
            appr_rows, "Nothing needs approval.")}
  </div>

  <div class="card">
    <h2>Incidents</h2>
    {_table(["Recovery", "Severity", "Transfer", "What happened", "Next"],
            inc_rows, "No incidents. Every transfer is where we left it.")}
  </div>

  <div class="card wide">
    <h2>Approval queue — a human decides, code still enforces</h2>
    {("".join(appr_queue)) if appr_queue else '<p class="empty">Nothing waiting on a human.</p>'}
    <p class="sub" style="margin-top:.75rem">Approving records a human decision.
      It does <strong>not</strong> move money: execution re-verifies the bound
      hash, amount, counterparty and FX rate through the executor. The endpoint
      is PIN-gated and closed entirely if no PIN is configured.</p>
  </div>

  <div class="card wide">
    <h2>Agent trace — what the model asked, and what code decided</h2>
    {_table(["Tool called", "Verdict", "Model's inputs", "Proposal / reason"],
            trace_rows,
            "No tool calls yet — run `python -m app.seed.live_beats`.")}
    <p class="sub" style="margin-top:.75rem">The agent may only call these five
      tools: <code>list_open_invoices</code>, <code>propose_match</code>,
      <code>explain_decision</code>, <code>request_approval</code>,
      <code>read_plan</code>. There is no payment tool, so it cannot express a
      payment. Every verdict above is decided by code.</p>
  </div>

  <div class="card wide">
    <h2>Receivables</h2>
    {_table(["Invoice", "Customer", "Ccy", "Amount", "Paid", "Open", "State"],
            rec_rows)}
  </div>

  <div class="card wide">
    <h2>Payables</h2>
    {_table(["PO", "Supplier", "Ccy", "Amount", "Rail", "State", ""], pay_rows)}
  </div>

  <div class="card wide">
    <h2>Reconciliation</h2>
    {recon_note}
    <p class="sub">Double-entry ledger, checked per transaction and currency.</p>
    {susp_line}
  </div>
</div>

<footer>
  Live sandbox data. <a href="/state">/state</a> &middot;
  <a href="/incidents">/incidents</a> &middot; <a href="/healthz">/healthz</a>
  &middot; refreshes every 5s
</footer>
</div>
<script>
async function decide(stepId, action) {{
  var el = document.getElementById('pin-' + stepId);
  var pin = el ? el.value : '';
  var body = {{pin: pin}};
  if (action === 'approve') {{ body.approved_by = 'dashboard'; }}
  else {{ body.rejected_by = 'dashboard'; body.reason = 'rejected in dashboard'; }}
  var r = await fetch('/approvals/' + stepId + '/' + action, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body)
  }});
  var out = await r.json();
  if (out.error) {{ alert(out.error); }} else {{ location.reload(); }}
}}
setTimeout(function(){{location.reload();}},5000);
</script>
</body></html>"""
