# Closebook

Continuous treasury and payments for a cross-border SME, built on the Airwallex
sandbox.

The demo business is **Kano Craft Imports Ltd**, a Lagos importer paid by
customers in USD and GBP who pays suppliers in EUR and USD.

> **The model proposes; deterministic code enforces.**
> Amounts, limits, approvals, the reserve floor, idempotency and duplicate
> protection are decided in code — never by a language model. A model may read a
> messy bank reference and explain a decision. It cannot authorise a payment,
> choose an amount, or mark money as matched.

---

## What it actually does

| Beat | What happens |
|---|---|
| 1 | Plan the day. Real planner, real budget. Small USD payouts auto-pay; the big EUR payout needs a human. |
| 2 | A USD 12,000 deposit arrives with a typo'd reference (`inv 0O41 brightline`) and matches `INV-0041` anyway. That unlocks `PO-7001`. |
| 3 | A GBP 4,800 part-payment matches partially — and the dependent payable **stays held**. A part payment is not a settlement. |
| 4 | A card authorisation of 7,000.01 against a 7,000 limit declines, and the decline matches our policy. |
| 5 | A payout fails. Closebook looks the transfer up on Airwallex before deciding anything, and proposes a **replacement under a duplicate lock with a new request id**. It never auto-pays twice. |
| 6 | An ambiguous USD 4,400 from `L. Stores` scores 0.70 and escalates to a human with the evidence side by side. |
| 7 | Reconciled double-entry ledger, unapplied cash shown, reserve floor respected. |

### The parts that matter most

**Money movement is idempotent by construction.** Every payout has a durable
`plan_step` row whose `request_id` is written to the database *before* the API
call. A step that already carries an `airwallex_object_id` is never sent again.
A step left `SUBMITTED` by a crash is resolved by **looking the request up on
Airwallex**, not by re-sending it.

**Approvals are bound to exact terms.** The SHA-256 hash covers amount,
currency, counterparty, supplier, invoice ids, quote id and the normalised FX
rate, and is HMAC-signed so we can prove *this* Closebook issued it. Change any
bound term by a single cent and the approval is void.

**A failure webhook is a claim, not proof.** `payout.transfer.failed` triggers
a lookup. If the transfer is still `PROCESSING`, Closebook watches it and
creates **nothing** — because a replacement could double-pay a transfer that has
not settled yet.

**The treasury runs on its own committed-cash budget, not the sandbox balance.**
Airwallex prefunding is *capacity*. Policy is measured against
`settings/committed_cash.json`. This is why a payout can be correctly refused
for breaching the reserve floor while the sandbox still shows millions.

**No paid LLM is required.** `backend/app/domain/llm.py` is optional. With no
API key set, matching runs fully deterministically and explanations fall back to
the deterministic reason. Offline is a supported mode, not a degraded one.

---

## Setup

Requires Python 3.11+.

```bash
git clone <your-fork> closebook && cd closebook
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill it in — see below
```

### `.env`

All values are **yours**; none are committed.

```
AWX_CLIENT_ID=...            # Airwallex sandbox client id
AWX_API_KEY=...              # Airwallex sandbox API key
AWX_WEBHOOK_SECRET=...       # optional; see "Webhooks" below
CLOSEBOOK_HMAC_SECRET=...    # generate: python -c "import secrets;print(secrets.token_hex(32))"
OPENAI_API_KEY=...           # OPTIONAL. No LLM key is needed to run the demo.
```

`CLOSEBOOK_HMAC_SECRET` signs approvals. **If it is empty, approval signing
raises at the first live use** while unit tests still pass — fill it in.

Nothing in this repository ever contacts a live Airwallex environment; the
client refuses any non-sandbox host.

---

## Run it

```bash
make setup     # venv + deps
make seed      # seed the scenario; creates sandbox accounts and deposits funds
make dev       # dashboard on http://127.0.0.1:8000
make demo      # the seven beats, deterministic decisions only (no network)
make test      # the full suite
```

### Going live against the sandbox

```bash
cd backend
../.venv/bin/python -m app.seed.live_beats   # beats 2-7 execute for real
```

Beats are **idempotent** — running twice gives identical balances. That is
checked on purpose; a demo you can only run once cannot be verified.

### The probes

Each of these proved a real API assumption rather than assuming one:

```bash
.venv/bin/python scripts/verify_rate_convention.py      # FX round trip
.venv/bin/python scripts/probe_beneficiary_schema.py    # live schema
.venv/bin/python scripts/probe_webhook_recovery.py      # signed webhook end to end
```

---

## Webhooks

Webhooks are **optional**. Polling is the supported fallback.

With `AWX_WEBHOOK_SECRET` unset, the receiver still works: it honours
Airwallex's sandbox `client-secret-key` test-event header, which carries the
secret per request.

Delivery is at-least-once, so events are deduped on `event_id` and processed
oldest-first. The endpoint acknowledges immediately and processes in the
background.

---

## Layout

```
backend/app/
  domain/         pure logic — no I/O, fully unit-tested
    matching.py     5-tier invoice matcher
    ledger.py       double-entry, idempotent postings
    policy.py       reserve floor, limits, committed cash, priority
    planner.py      deterministic treasury planner
    executor.py     THE ONLY MODULE THAT MOVES MONEY
    approvals.py    hash-bound, HMAC-signed approvals
    plan_store.py   plan-step identity (payable, action)
    incidents.py    failure classification and recovery decisions
    llm.py          optional, offline-safe model layer
  airwallex/      one module per API surface, each verified live
  seed/           the demo scenario and its two drivers
  ui.py           the dashboard (hand-written, no build step)
```

`tests/` is 240 tests. `AGENTS.md` is the engineering log: every decision,
every live-verified correction, and every bug found by running the real thing.

---

## Troubleshooting

**"The dashboard could not read state."** Check `make test` still passes, then
check the DB. The page falls back deliberately rather than showing wrong numbers.

**A payout says it is already done.** That is the duplicate guard working. Look
at `/state` and the plan table.

**Everything is refused.** Check `settings/committed_cash.json` — with
USD 40,000 committed and a USD 5,000 floor, a USD 36,000 payout is correctly
refused. That is the point.

---

## Notes

- Airwallex **sandbox only**. No real funds can move.
- Internal money is integer **minor units**; Airwallex receives major-unit strings.
- Raw sandbox balances are capacity. Policy runs on the committed-cash budget.
