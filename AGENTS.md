# AGENTS.md — Closebook

Single source of truth for anyone (agent or human) picking up this repo cold.
If a section below conflicts with reality on disk, **reality wins** — fix this file in the same commit.
Sections 1–4 are *current* (rewrite in place). Sections 5–7 are *history* (append only, newest at the bottom of section 7).

---

## 1. PROJECT SNAPSHOT

Closebook is an agent that **closes the books in real time** for a cross-border SME (demo persona: "Kano Craft Imports Ltd", a Lagos importer paid in USD/GBP, paying suppliers in EUR/USD/CNY). Every time money lands it matches the inflow to open invoices (exact → rule → partial/multi-invoice → LLM-assisted, each re-validated in code), then it makes the money decision that match unlocks: release/hold supplier payments, convert only the FX it needs, pay with spend-locked virtual cards or payouts, chase shortfalls, and escalate low-confidence cases to a human. The core principle is non-negotiable: **the model proposes, deterministic code enforces** — amounts, limits, reserve floors, approvals, idempotency and duplicate protection live in code, never in prompts.

- **Hackathon:** Airwallex "Agentic Banking" on HackerEarth. Build window **Oct 25 – Nov 13 2026**; submission due **Nov 13, 2026, 18:29 UTC**.
- **Deliverables:** a working demo (7-beat scripted scenario, §9 of the spec), a demo video under 5 minutes, and a public open-source repo with copy-paste setup that a fresh clone reaches `make demo` in < 10 min.
- **Full spec:** `~/closebook/closebook.md` (a.k.a. closebook.md) — the authoritative build prompt. Read it before changing scope.
- **Sandbox only.** `https://api.sandbox.airwallex.com`, no real money, no production.

## 2. CURRENT STATUS

- **Phase:** Phase 3 of 6 (Planner), **~55% done**. Phase 1 (Foundation) 100% and verified live; Phase 2 (matching + ledger) 100%; Phase 3 planner done, executor + approvals outstanding.
- **Working now (all verified by actual runs, not just file reads):**
  - `make test` → **120 passed** (money 11, webhook 6, idempotency 4, airwallex-contract 8, matching 28, policy 28, ledger 13, planner 22)
  - **Planner (Phase 3)** → `domain/planner.py`: pure function, cash + payables + budget → ordered CONVERT/PAY/DEFER/CHASE/ESCALATE plan, every step with a reason. Dependency gate, reserve floor, auto-pay limit, FX sizing with drift tolerance, deterministic prioritisation.
  - **`make demo` now runs the REAL planner against the REAL budget** (beat 1 prints the actual plan: DEFER PO-7001, DEFER PO-7002, auto-PAY PO-7003) instead of a hand-rolled preview. Same function the tests assert and the executor will consume.
  - **Rate convention verified LIVE against Airwallex**: `scripts/verify_rate_convention.py` round-trips USD 9,012.85 → EUR 7,914.85 → USD 9,012.84 (error 0.0001%) against the real `client_rate` 1.138726, confirming `rates[CCY] = CCY per 1 USD`.
  - `make seed` → **runs live**: creates USD + GBP global accounts, deposits, snapshots balances. Last verified balances: USD 10,120,000 · GBP 10,018,000
  - **FX quote → conversion verified live**: USD 9,012.85 → EUR 7,914.85 at client_rate 1.138726 (this is exactly the beat-2 SWIFT-fee-inclusive amount)
  - **Matching engine (Phase 2)** → `domain/matching.py`: 5 tiers, all unit-tested, including the code re-validation that stops a bad model proposal
  - **Double-entry ledger (Phase 2)** → `domain/ledger.py`: balanced-per-currency postings, idempotent per (txn, account, currency), receivable status derived from amounts, unapplied cash parked in suspense
  - **Policy engine (Phase 3)** → `domain/policy.py`: reserve floor, auto-pay limit, SWIFT fee, bank-fee tolerance, deterministic prioritisation — all pure functions, all tested
  - **Virtual committed-cash budget (option b, chosen by David)** → `settings/committed_cash.json` + `policy.load_budget()`. Verified live: USD 40,000 committed, USD 5,000 floor, USD 2,500 auto-pay limit. A USD 2,500 payout auto-pays; USD 36,000 breaches the floor; USD 34,000 is fine. The demo now has real tension.
  - `make dev` → FastAPI boots; `/healthz` and `/state` return 200
- **✅ RESOLVED — the sandbox pre-funds 10M problem (option b implemented).** The Airwallex sandbox auto-hosts 10,000,000 in USD/EUR/GBP/CNY, so policy written against the raw balance never binds. David chose **(b): a virtual committed-cash budget**. Closebook tracks its own committed cash in `settings/committed_cash.json` and treats the sandbox balance as *capacity* — the ceiling of what it could move. Tests `test_repo_settings_make_the_floor_bind` and `test_floor_never_seen_against_raw_sandbox_balance` pin both halves.
- **Bugs found & fixed (all caught by running, before/against live):**
  - **`usd_value_minor` had an INVERTED rate convention (the worst bug so far).** `policy` multiplied by the rate (`1 EUR = X USD`) while `planner` divided by it (`1 USD = X CCY`), from the same dict — so the reserve floor and the auto-pay limit were reading the same number two opposite ways. A EUR 9,012.85 payout was valued at "USD 7,891.65" (wrong by 30%). Standardised on **`rates[CCY] = CCY per 1 USD`**, documented it in `usd_value_minor`'s docstring as the single source of truth, made `CashPosition.to_usd_minor` delegate to `policy.usd_value_minor` so the two can never drift again, added `test_rate_convention_is_ccy_per_usd`, and proved it live with `scripts/verify_rate_convention.py`.
  - `Decimal.normalize()` → `1E+2` scientific notation — Airwallex's JSON parser would reject it
  - Makefile `Error 127` (relative venv path after `cd backend`) → absolute `ROOT`-resolved `PY`
  - `run_demo` read `payout_method` off `InvoicePayable`; it lives on `Supplier`
  - **Planner:** the dependency-shortfall message hardcoded `'USD'`, so it reported a GBP shortfall in the wrong currency → added `Payable.dependency_currency`; the "only one open invoice" style rules from Phase 2 kept biting and were gated on payer identity + plausible amount
  - **Matching engine (Phase 2):** digit-lookalike folding turned `INV` into `1NV`, breaking every invoice-number match → split into word-safe `normalise()` and token-scoped `fold_digits()`; tier 1–3 returned before the LLM ran, so a hostile model proposal was never re-validated → orchestrator now only stops at ≥0.90 confidence and tier 4 always re-validates; a fuzzy payer + mismatched amount could auto-match a single open invoice → gated on payer identity ≥0.90 **and** a plausible amount; an unpriced currency valued at 0 USD read as "small" and auto-paid → `requires_approval` now rejects unpriceable currencies outright; a 1–2 digit invoice number matched any reference containing that digit → digit matching now requires ≥3 digits
  - **Ledger (Phase 2):** a fully-overpaid invoice was marked exactly `PAID`, losing the credit → credit is now attached to the invoice and the cash parked in `unapplied_cash`; in-memory SQLite gave each connection a fresh empty DB → `StaticPool` for `:memory:`
  - **Live-discovered (Phase 1):** login field is `token` (not `access_token`); `global_accounts` list is under `items`; `global_accounts/create` + `fx/*` return **bare objects**; `balances/current` is a **bare list** keyed `available_amount` spanning ~46 currencies; `fx/rates/current` needs `buy_currency`+`sell_currency`; `fx/quotes/create` requires a `validity` **enum** (`MIN_1`…`HR_24`) and returns `awx_rate`/`client_rate`; balance snapshot must skip currencies absent from our exponent table
- **Not done:** executor, approvals, agent/LLM tools, webhook-to-executor wiring, React dashboard (Phases 3–5). `main.py` serves a minimal index only.
- **Blocker for live work: NONE for Airwallex.** `.env` has `AWX_CLIENT_ID` + `AWX_API_KEY` and they authenticate live (HTTP 201). Still optional: `AWX_WEBHOOK_SECRET` (Phase 4 only — polling fallback covers the demo) and `ANTHROPIC_API_KEY` (Phase 5 only — **no money needed**: wire the agent to the free NaraRouter OpenAI-compatible provider instead, per §5).

**NEXT STEPS (numbered; an agent can start #1 immediately):**
1. **Phase 3 — `domain/executor.py`**: run plan steps against Airwallex with the request_id guard — persist the `request_id` BEFORE sending, never re-issue a step that already has an `airwallex_object_id`, and on an ambiguous timeout look the object up by request_id before any retry. Acceptance: kill mid-transfer and restart → no duplicate payment.
2. **Phase 3 — approvals**: build the approval bundle (exact amount, currency, counterparty, invoice IDs, evidence hash), hash-bind it with `CLOSEBOOK_HMAC_SECRET`, and void the approval if any bound term changes (e.g. FX drift beyond `fx_tolerance_pct`) — the demo's beat-4 re-approval. The planner already sets `needs_approval` + `approval_reason` per step, so this consumes existing output.
3. **Phase 4 — webhook path**: wire `main.py`'s receiver to the executor (currently marks processed only) so a `payout.transfer.failed` triggers the incident/recovery flow instead of a blind retry — beat 5.
4. **Phase 4 — beats 3–6**: make `deposit_typo_match`, `partial_payment`, `guardrail`, `failure_recovery`, `ambiguous_inflow` actually execute live instead of printing the expected outcome.
5. **Phase 5 — dashboard + README**: the one-page UI must surface the plan, the approvals, and the reconciliation report — anything not linked from the main screen does not exist for a judge.

## 3. HOW TO RUN

All from the repo root `~/closebook`. Requires Python 3.11+ (this VM has 3.14.7).

```bash
make setup          # create .venv, install requirements.txt, copy .env.example -> .env (first run only)
make test           # run the pytest suite (backend/tests, ../tests wiring in Makefile)
make dev            # FastAPI + UI on :8000 (uvicorn, autoreload)
make seed           # create/reuse sandbox global accounts, deposit scenario cash, seed ledger, print balances
make demo           # run the 7-beat scripted demo (needs seed + .env + sandbox creds)
make webhook-url    # open a cloudflared tunnel and print the public webhook notification URL
```

**Required env vars** (by name only — values come from `.env.example`, never committed here):
- `AWX_CLIENT_ID`, `AWX_API_KEY` — Airwallex sandbox API keys (sandbox.airwallex.com → Account > Developer > API keys). Use a business named **Sandbox Business** / **New Business Sandbox** so onboarding auto-approves.
- `AWX_WEBHOOK_SECRET` — per-URL secret from the sandbox web app (Settings > Developer > Webhooks).
- `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` — Claude model id + key for the agent phase.
- `CLOSEBOOK_HMAC_SECRET` — signs the approval-bundle hashes.
- `PUBLIC_BASE_URL`, `WEBHOOK_PATH` — where the webhook receiver is reachable.
- Optional: `LOG_LEVEL`.

## 4. ARCHITECTURE MAP

```
closebook/
  closebook.md              # authoritative spec (full build prompt)
  AGENTS.md                 # this file
  .env.example              # secrets template (copy to .env)
  .gitignore                # .env, *.db, .venv, __pycache__ ...
  requirements.txt          # backend deps
  Makefile                  # setup/dev/seed/demo/test/webhook-url
  settings/
    committed_cash.json     # VIRTUAL COMMITTED-CASH BUDGET + limits policy runs against
  backend/
    app/
      config.py             # Settings (pydantic-settings); loads .env; forces sandbox host  [DONE]
      money.py              # major<->minor conversion, CURRENCY_EXPONENT table               [DONE]
      db.py                 # engine + DB/session_scope helpers; StaticPool for :memory:       [DONE]
      models.py             # §4 ORM tables, integer minor units, unique request_id/event_id  [DONE]
      main.py               # FastAPI app: /healthz, /state, minimal index, webhook receiver   [PARTIAL]
      airwallex/
        client.py           # auth refresh, request_id, 429 backoff, retry guard               [DONE]
        balances.py fx.py beneficiaries.py transfers.py issuing.py
        global_accounts.py simulation.py webhooks.py                                         [DONE]
      domain/
        matching.py         # 5-tier matcher + code re-validation of model proposals          [DONE]
        ledger.py           # double-entry postings, invoice status, reconciliation            [DONE]
        policy.py           # committed-cash budget, floor/limits, fees, prioritisation       [DONE]
        planner.py          # pure fn: cash+payables+budget -> ordered justified plan          [DONE]
        executor.py                                                                 (TO DO, Phase 3)
      agent/
        tools.py prompts.py runner.py                                                        (TO DO, Phase 5)
      seed/
        scenario.py         # Kano Craft seed data + 7 demo beats                              [DONE]
        run_seed.py         # live: accounts, deposits, balance snapshots                     [DONE]
        run_demo.py         # drives the 7 beats; beat 1 runs the REAL planner                 [DONE]
  scripts/
    verify_rate_convention.py   # live proof our CCY-per-USD convention matches Airwallex    [DONE]
  tests/
    conftest.py test_money.py test_webhook.py test_idempotency.py
    test_airwallex_contract.py test_matching.py test_policy.py test_ledger.py
    test_planner.py
  frontend/                # one-page dashboard, served by FastAPI (TO DO, Phase 5)
```

**Key flows and where they live:**
- **Matching** — `domain/matching.py`: `try_exact` (word-safe invoice-number containment, ≥3 digits, O/0 folding) → `try_rule` (payer identity ≥0.90 + plausible amount) → `try_exact_multi` (subset-sum over open invoices) → `try_partial` (best fit by relative shortfall; material part-payment vs ambiguous small shortfall) → `try_llm` (code re-validates every number). Orchestrator stops only at ≥0.90 confidence. All matching runs on integer minor units.
- **Policy** — `domain/policy.py`: `load_budget()` reads `settings/committed_cash.json`; `CommittedCash` is the virtual budget the floor and limits are measured against; reserve floor USD 5,000, auto-pay limit USD 2,500, match-confidence auto threshold 0.90, refunds/overpayments always need approval, SWIFT adds a flat EUR 12.85 *inside* the converted amount. Unpriceable currencies are never auto-payable. Pure functions, unit-tested.
- **Ledger** — `domain/ledger.py`: `post()` enforces debits == credits per (txn, currency) and is idempotent per (txn, account, currency); `apply_inflow_to_receivables()` is the only place a receivable's paid amount/status changes; unapplied cash parks in `unapplied_cash`; `reconcile()` is a pure read.
- **Planner** — `domain/planner.py` (Phase 3): a PURE function — cash position + payables + budget in, an ordered `Plan` out, no DB/API/model. Order comes from `policy.prioritise()`, never from the query order. Rules in order: dependency gate (a part-payment does NOT release a payable) → currency supported/pricable → rail fee added → CONVERT if we don't hold the currency (sized with `fx_tolerance_pct` drift headroom, rounded UP) → reserve floor (defer, never trim) → auto-pay limit (flag `needs_approval`, never drop). Capacity shortfalls and policy breaches get *different* reasons because the fix differs.
- **Executor** — `domain/executor.py`: persist step+request_id → call → store object id → poll/await terminal state. Transfers terminal = PAID|CANCELLED; cards accept on `transaction_type: CLEARING`; FX quotes are single-use, booked over REST.
- **Approvals** — `approval` table: every approval binds a hash of amount+currency+counterparty+invoice ids+evidence; any change voids it.
- **Webhooks** — `airwallex/webhooks.py`: verify `x-signature` = HMAC-SHA256(`x-timestamp` + raw payload, `AWX_WEBHOOK_SECRET`), dedupe on event id, order by `created_at`, 200 fast. Polling fallback for deposits/transfers.
- **Data model** — §4 of the spec (ORM in `models.py`); `plan_step.request_id` is UNIQUE (idempotency); `webhook_event.event_id` is UNIQUE. Money is integer minor units everywhere except at the Airwallex boundary.

## 5. DECISIONS LOG (append only)

- **2026-10-06 — Force sandbox host in `config.py`.** Added a `field_validator` on `AWX_BASE_URL` that rejects any host other than `api.sandbox.airwallex.com`/`sandbox.airwallex.com`. Why: defense-in-depth so no dev points the build at production. Alternatives: none (a hard rule from the spec §1.1).
- **2026-10-06 — Integer minor units internally, major at the boundary.** All money is stored/computed in integer minor units and converted only in `money.py`. Why: spec §1.3. CNY uses exponent 2 (100 fen), not 1.
- **2026-10-06 — ALL simulation behind one interface (`airwallex/simulation.py`).** Demo seeds via simulation endpoints; putting them behind one seam means decision logic never depends on sandbox mechanics and prod/real money can swap in without rewrite.
- **2026-10-06 — Python 3.14.7 on this VM.** Spec said 3.11+/3.12. Deviation noted; verify httpx/pydantic/anthropic install cleanly on 3.14 during `make setup` (potential wheels gap).
- **2026-10-06 — No `ANTHROPIC_API_KEY`: wire the agent to the free NaraRouter provider.** david has no Anthropic key and no budget for one. Phase 5's agent will use a provider-agnostic client reading `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` from `.env`, defaulting to the Anthropic SDK but falling back to the OpenAI-compatible route (NaraRouter, base `https://router.bynara.id/v1`, keys prefixed `sk-nry-`). Rationale: matching/policy/planner/executor never call a model, so this blocks only the explanation layer, and that layer must degrade gracefully anyway (a dead gateway during the live demo must not stop the money logic). Rejected: requiring a paid key.
- **2026-10-06 — Skip `AWX_WEBHOOK_SECRET`; poll instead.** Webhooks need a reachable public URL and a per-URL secret, and david found the scope form heavy. The builder guide itself recommends a polling fallback, and the demo's failure beat is self-inflicted (we call the simulation endpoint), so webhooks are realism-bonus, not load-bearing. Rejected: blocking Phase 2–3 on a webhook subscription.
- **2026-10-06 — Contract tests from captured live responses.** `tests/test_airwallex_contract.py` pins every envelope confirmed live. Rationale: 8 documented-vs-actual mismatches on the first probe; a regression guard is cheaper than rediscovering them mid-demo. Rejected: trusting docs/guides for envelopes.
- **2026-10-06 — Virtual committed-cash budget (option b), chosen by david over (a) reframe-as-scale and (c) request a fresh unfunded sandbox business.** The sandbox pre-funds 10M per major currency, so the USD 5,000 reserve floor and USD 2,500 auto-pay limit never bind against the raw balance and the demo would show no policy tension. Closebook now tracks committed cash in `settings/committed_cash.json` and treats the sandbox balance as *capacity*. Rationale: honest, judge-legible in one sentence ("your bank balance is capacity, your cash position is what's committed"), needs no platform access, and makes both the floor and the auto-pay limit demonstrably fire. Rejected: (a) because it dodges the interesting problem, (c) because it depends on Airwallex's cooperation and a deadline.
- **2026-10-06 — Matching confidence bands are policy, not taste.** ≥0.90 auto-apply; 0.75–0.90 apply but require approval; <0.75 escalate. Overpayments and part-payments are deliberately capped **below** 0.90 so they can never auto-apply — an overpayment creates a refund obligation, and a part-payment needs a human to decide whether to chase. Rationale: the spec says the model proposes and code enforces; the bands are where "enforce" actually lives. Rejected: a single flat threshold.
- **2026-10-06 — Payer identity gates every amount-based match.** A payment is only auto-applied on amount alone if the payer is unrecognised *and* the amount is within bank-fee tolerance; a known payer must match the customer name at ≥0.90. Rationale: amount coincidence is not evidence, and a small deposit latching onto the wrong invoice is the exact failure a treasury agent must not have. Rejected: amount-only matching.
- **2026-10-06 — Small shortfall outside bank-fee tolerance ESCALATES; a large one is a PARTIAL.** Above 10% of the invoice still outstanding = deliberate part-payment (apply, chase later). Below that but outside the ≤1%/≤15-unit fee tolerance = genuinely ambiguous (an odd fee, a wrong invoice, a typo all look identical) so it escalates rather than guessing. Rationale: this boundary is what beat 3 exercises. Rejected: treating every shortfall as a partial.
- **2026-10-06 — Model output is always re-validated arithmetically, even on the success path.** `try_llm` re-checks that allocations sum to the inflow, reference only real open invoices, never cross currencies, and never exceed an open balance; any violation returns AMBIGUOUS at confidence 0. Rejected: trusting a confidence number from the model.
- **2026-10-06 — Unpriceable currencies are never auto-payable.** A currency with no FX rate values at 0 USD, which would read as "small" and sail under the limit. `requires_approval` now rejects unsupported/unpriced currencies outright. Rationale: "unpriced" must never be silently read as "cheap".
- **2026-10-06 — ONE rate convention for the whole codebase: `rates[CCY] = CCY per 1 USD`, so USD value = amount / rate.** Chosen because it matches how a conversion actually reads (sell USD, buy CCY) and the live `client_rate` 1.138726 is the *inverse* (USD per EUR). `policy.usd_value_minor` is the single source of truth and `CashPosition.to_usd_minor` delegates to it rather than re-deriving. Rationale: this bug shipped inside my own Phase 3 draft — policy multiplied while planner divided, from the same dict, so the reserve floor and the auto-pay limit valued the same payout two different ways (30% apart). Pinned by `test_rate_convention_is_ccy_per_usd` and proven live by `scripts/verify_rate_convention.py` (USD→EUR→USD round-trip error 0.0001%). Rejected: allowing each module its own convention.
- **2026-10-06 — The planner is a pure function, and `make demo` runs the real one.** No DB, no API, no model: cash + payables + budget in, an ordered justified plan out. Rationale: the entire money decision is then unit-testable and byte-identical across runs, which is the first thing a judge will try to check; and the demo cannot drift from the tested logic. Rejected: a hand-rolled preview in `run_demo.py` (it disagreed with policy and was a second source of truth).
- **2026-10-06 — A part-payment does NOT release a dependent payable.** PO-7002 stays HELD while INV-0042 is GBP 3,200 short, even though GBP 4,800 has arrived. Rationale: paying a supplier with a customer's money they still owe is precisely how a treasury gets into trouble; the dependency is met only by a FULLY PAID receivable. This is beat 3's whole lesson.
- **2026-10-06 — Capacity and policy are different limits with different messages.** Running out of a currency is `insufficient_balance` (fix: wait for funds); breaching the committed-cash floor is `reserve_floor` (fix: wait for inflows or get approval). Collapsing them into one generic "can't pay" would leave a human unable to tell a timing problem from a policy problem.
- **2026-10-06 — A step that breaches the reserve floor is DEFERRED with its FULL amount, never trimmed to fit.** Shaving a payout down to whatever the floor allows would quietly pay a supplier less than invoiced. Rationale: deferral is visible and reversible; a silent trim is neither.

## 6. GOTCHAS AND LEARNINGS (append only)

- **Terminal cwd is stale in this session.** A persisted cwd pointed at a non-existent `/home/ubuntu/car-landing`; any `cd` command failed with exit 126. Fix: always pass `workdir` explicitly (project runs from `/home/ubuntu/closebook`).
- **Webhook signature must be computed on the RAW body, before JSON parsing.** Airwallex's own doc warns: re-serialized/normalized JSON changes whitespace and breaks the HMAC. Verify on `raw_body`, constant-time compare, then parse. (Source: airwallex.com docs "Listen for webhook events".)
- **Sandbox webhooks are unreliable in the demo.** Keep the polling fallback (`GET /transfers`, re-read balances) so beats 5–7 don't depend on a push arriving.
- **Transfers end CANCELLED, never FAILED.** A failed transfer auto-cancels within seconds; read `failure_type`/`failure_reason`. Budget two flat EUR 12.85 SWIFT fees if both original+replacement are SWIFT.
- **Developer MCP cannot create global accounts** — use REST `POST /global_accounts/create`.
- **EUR global account `country_code` must be NL/DE/FR** (BE is rejected); it is the account country, not the currency.
- **`Decimal.normalize()` returns scientific notation** (`100` → `1E+2`). Airwallex's JSON parser would reject that; never use `.normalize()` for API amount strings — strip trailing zeros manually (`money.to_major_str`).
- **A Makefile target that `cd`s elsewhere breaks on relative paths.** `cd backend && .venv/bin/python` fails with `Error 127`; resolve the interpreter to an absolute path first (`ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))`).
- **Schema columns that "should" be on a model may live on a related one.** `payout_method` is on `Supplier`, not `InvoicePayable` — a first `make demo` run died on `AttributeError`. When a field is conceptually per-supplier, read it through the relationship, don't denormalize it onto the payable.
- **Tests catch wiring bugs that file reads don't.** All three Phase-1 bugs above were found by *running* `make test`/`seed`/`demo`/TestClient, never by reading the code. Run the targets, don't just write them.

### Live-sandbox response shapes (verified 2026-10-06, against api.sandbox.airwallex.com)
Do not trust the builder guide or the docs for envelopes — these were all confirmed by real calls:
- `POST /authentication/login` → **201** with `{"expires_at": ..., "token": ...}`. The field is **`token`**, not `access_token`.
- `GET /global_accounts` → 200 with **`{"items": [...]}`**, not `{"data": [...]}`.
- `POST /global_accounts/create` → 201 with a **bare object** (`id`, `status`, `required_features`, `supported_features`) — no `data` wrapper. Created accounts start in `status: PROCESSING`.
- `GET /balances/current` → 200 with a **bare JSON list** of `{"currency", "available_amount", "pending_amount", ...}` covering **~46 world currencies**, mostly 0. There is no `available_balance` and no `data` wrapper.
- `GET /fx/rates/current` → params are **`buy_currency` + `sell_currency`**. Passing `base`/`quote` returns `400 invalid_argument "'buy_currency' is not specified"`.
- `POST /fx/quotes/create` → `validity` is **required and is an ENUM**: `MIN_1`, `MIN_15`, `MIN_30`, `HR_1`, `HR_4`, `HR_8`, `HR_24`. A duration string like `"1h"` fails with `invalid_argument: 1h is not a valid type`. Response is a bare object with `id`, `awx_rate`, **and** `client_rate`, plus `usage: SINGLE_USE` and `valid_to_at`.
- `POST /fx/conversions/create` → bare object: `awx_rate`, `client_rate`, `buy_amount`, `sell_amount`, `conversion_id`, `quote_id`, `rate_details[]`. Verified: USD 9,012.85 → EUR 7,914.85.
- **Rule of thumb:** assume a bare object/list unless proven otherwise, and write a contract test from the captured response. `tests/test_airwallex_contract.py` now pins all of the above.

### Other live findings
- **The sandbox pre-funds 10,000,000 in USD, EUR, GBP and CNY.** Cash-scarcity policy (reserve floor, auto-pay limit) has no bite against a 10M balance unless the policy runs off a Closebook-side virtual committed-cash budget. See §2 "SCOPE DECISION NEEDED".
- **Your sandbox business may already have accounts in unexpected countries** — the first live run found a pre-existing `HK`/HKD global account alongside the ones we created (US/USD, GB/GBP).
- **The Airwallex Docs MCP needs no auth and is the fastest way to check a schema**: `POST https://mcp.sandbox.airwallex.com/docs` with `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_public_docs","arguments":{"question":"..."}}}` (response arrives as SSE `data:` lines). Use this instead of guessing endpoint fields.
- **A test fake that replays one payload for every call will break auth.** `AirwallexClient._ensure_token` runs before every request, so the fake must special-case the login path (see `tests/test_airwallex_contract.py`).

### Matching-engine traps (Phase 2 — each one cost real time)
- **Never fold digit lookalikes across a whole string.** `str.translate` mapping `I→1, O→0, S→5, B→8, Z→2` turns `INV-0041` into `1NV-0041`, so `"inv0041" in ref` fails on *every* real invoice. Split it: `normalise()` (lowercase + alphanumeric only, never folds) for whole-string comparison, and `fold_digits()` for number-like material. Fold **per token, and only into tokens that already contain a real digit** — that guard is what stops `INV` becoming `1NV` while still folding `0O41` → `0041`.
- **A short digit run matches anything.** With digit-run containment, a 1–2 digit invoice number matches any reference containing that digit. Require ≥3 digits.
- **"Return the first candidate that fits" is a bug generator.** `try_partial` originally returned on the first candidate, so GBP 4,800 latched onto a £12,000 invoice listed first instead of the £8,000 one it actually belonged to. Score every candidate by *relative* shortfall and take the best fit.
- **Subset-sum must run BEFORE the single-invoice partial rule**, or a payment covering two bills reads as a part-payment of the larger one.
- **Returning from the orchestrator at the first non-None tier silently skips model re-validation.** The tiers now only short-circuit at ≥0.90 confidence; anything weaker falls through to the LLM and back.
- **A rule that fires "only one open invoice exists" will happily eat a part-payment.** It needs *both* payer identity ≥0.90 *and* a plausible amount before it claims the payment.
- **Amount-only matching is unsafe.** Cap an amount-only match below the auto threshold and gate it on a recognisable payer, or an unrelated party's deposit gets applied to your invoice.
- **Derive status from amounts in fixtures and code alike.** A test helper that hardcodes `status=OPEN` on a fully-paid invoice produces a failure that looks like a bug in `reconcile()` but is really a lying fixture.
- **`InvoiceReceivable` has no `customer_name` column** — it links to `Customer` via `customer_id`. Read `inv.customer.name`; don't assume a denormalised field.

### Ledger/DB traps (Phase 2)
- **`:memory:` SQLite hands every connection a brand-new empty database.** `create_all` and the test session would see different schemas. Use `StaticPool` for in-memory URLs or the tests are silently meaningless.
- **An overpaid invoice must not land as exactly `PAID`.** Allocating only the invoice amount marks it PAID and loses the credit. Attach the credit to the invoice (so it reads OVERPAID) and park the cash in `unapplied_cash` for refund/carry-forward.
- **Double-entry balances PER CURRENCY.** Debiting USD and crediting EUR must raise `LedgerImbalance`, not slip through on a summed total.

### Planner traps (Phase 3)
- **THE rate-convention trap — read this before touching any FX math.** Two modules can each be internally consistent and still be inverses of each other. `policy` multiplied by the rate while `planner` divided by it, from the *same* dict, and every test in isolation still passed. Only a real cross-check caught it: an approval reason that read "USD 7,891.65" for a EUR 9,012.85 payment. Fix: one function owns the conversion (`policy.usd_value_minor`), everything else delegates, and the convention is asserted in a test *and* proven against a live Airwallex quote (`scripts/verify_rate_convention.py`). When a number crosses a module boundary, write the round-trip test.
- **A deferral message that hardcodes a currency will lie.** The dependency shortfall for INV-0042 is GBP while PO-7002 is USD; printing `'USD'` reported the wrong unit. Carry `dependency_currency` on the payable.
- **`prioritise()` returns objects, not an index** — and rebuilding your payable list from a dict keyed on (criticality, due_date, defer_cost) **silently DROPS a payable** when two share that tuple. This shipped in my first planner draft and a regression test caught it with `planned={'PO-B'}` — one supplier simply absent from the plan. Sort INDICES by `policy.priority_key()` (now the shared single source of truth for the ordering) instead. A supplier unpaid because of a dict collision is not an acceptable failure mode.
- **Rounding FX DOWN leaves the payout short.** `_fx_sell_needed` must divide by the rate *reduced by* the drift tolerance, then round the sell amount UP — otherwise a 0.5% adverse move leaves the far end a few cents short and the transfer fails.
- **Distinguish "we cannot price this" from "this is too expensive".** No FX rate → ESCALATE with `no_rate`; unsupported currency → ESCALATE with `unsupported_currency`; over the auto-pay limit → PAY with `needs_approval`. Three different human actions.

## 7. ACTIVITY LOG (append only, newest at the bottom)

### 2026-10-06 20:00 WAT - Recon + repo skeleton
- Did: confirmed Python 3.14.7 / pip / make / git; created the `closebook/` tree and `git init`; wrote `.env.example`, `.gitignore`, `requirements.txt`, `Makefile`.
- Files: .env.example, .gitignore, requirements.txt, Makefile, plus empty dirs `backend/app/{airwallex,domain,agent,seed}`, `backend/tests`, `frontend`.
- Commands: `find`/`git status` (baseline captured). No tests (none written yet).
- Result: partial (skeleton only; nothing executable yet).
- Next: config + money + db/models.

### 2026-10-06 20:30 WAT - config.py + money.py
- Did: wrote `config.py` (pydantic-settings, loads `.env`, sandbox-host validator, `sandbox_ready()` helper) and `money.py` (major/minor conversion + `CURRENCY_EXPONENT`). Researched the Airwallex webhook-signature doc and captured it into Gotchas.
- Files: backend/app/config.py, backend/app/money.py.
- Commands: none run (venv not created; not executed).
- Result: partial (files present, unverified — no test/import run yet).
- Next: db/models, then airwallex/client, then seed+main, then first test run.

### 2026-10-06 22:14 WAT - AGENTS.md created (new standing rule)
- Did: created this file per david's new standing rule; backfilled status/decisions/gotchas/activity from disk ground truth; committed it with the current code so git history and the log agree.
- Files: AGENTS.md (new).
- Commands: `find`/`git status`/`date` (baseline re-captured just before writing). No tests yet.
- Result: works (log reflects verified on-disk state; all 6 non-AGENTS.md files confirmed present; no commits existed prior).
- Next: START #1 in NEXT STEPS (db.py + models.py + migration bootstrap).

### 2026-10-06 22:40 WAT - Phase 1 code written (venv + 13 modules)
- Did: created `.venv` (background install, exit 0); wrote `db.py`, `models.py` (full §4 schema, 12 tables incl. balance_snapshot), `airwallex/{client,global_accounts,balances,fx,beneficiaries,transfers,issuing,simulation,webhooks}.py`, `seed/{scenario,run_seed,run_demo}.py`, `main.py` (FastAPI + webhook receiver + `/healthz`/`/state`), and tests `test_money.py`, `test_webhook.py`, `test_idempotency.py` (with `conftest.py` putting `backend/` on sys.path).
- Files: see tree in §4; all under backend/ and tests/.
- Commands: `.venv/bin/python -m pytest -q tests/` → 22 passed.
- Result: works — but three real bugs surfaced during verification (see next entry).
- Next: fix the three bugs, then commit Phase 1.

### 2026-10-06 23:00 WAT - Three real bugs caught by running, not writing
- Did: hit and fixed (1) `Decimal.normalize()` → `1E+2` scientific notation (money.py `to_major_str` rewrote to strip trailing zeros manually); (2) Makefile `Error 127` — targets `cd backend` but called `.venv/bin/python` by a relative path, fixed with `ROOT := $(abspath ...)` absolute `PY`; (3) `make demo` beat-1 `AttributeError: 'InvoicePayable' object has no attribute 'payout_method'` — that column lives on `Supplier`, fixed to join through `p.supplier`.
- Files: backend/app/money.py, Makefile, backend/app/seed/run_demo.py.
- Commands: `make test` → 22 passed; `make seed` → seeds local ledger, prints plan-of-record; `make demo` → all 7 beats offline; `make dev`-equivalent (`TestClient`) → `/healthz` 200, `/state` 200.
- Result: works. Phase 1 verified end-to-end offline.
- Next: update AGENTS.md + commit Phase 1 in one commit (per standing rule).

### 2026-10-06 23:20 WAT - LIVE sandbox probe: credentials work, 8 API assumptions corrected
- Did: probed `api.sandbox.airwallex.com` with the real credentials david added to `.env`. Login → **HTTP 201**, credentials valid. Then fixed every module whose assumed envelope was wrong. Discovered the docs MCP (`mcp.sandbox.airwallex.com/docs`, no auth) and used it to settle the `validity` schema instead of guessing again. Verified a full FX quote→conversion live (USD 9,012.85 → EUR 7,914.85). Added `tests/test_airwallex_contract.py` pinning all captured shapes (8 tests).
- Files: backend/app/airwallex/{client,global_accounts,balances,fx,transfers,simulation}.py, backend/app/seed/run_seed.py, tests/test_airwallex_contract.py, AGENTS.md.
- Commands: `make test` → 30 passed; `make seed` (live) → created US/USD + GB/GBP global accounts, deposited, snapshot OK; FX quote+conversion live OK.
- Result: works. Corrected 8 live-discovered assumptions: token field name, `items` envelope, bare-object create/fx responses, bare-list balances keyed `available_amount` (~46 currencies), fx rate param names, `validity` enum, awx_rate vs client_rate, snapshot must skip unsupported currencies. Also hit `TypeError: datetime >= int` in a display helper.
- Next: Phase 2 matching engine. **Open decision for david:** sandbox pre-funds 10M per currency, which neutralises the reserve floor / auto-pay limit as demo tension — recommend policy running off a virtual committed-cash budget.

### 2026-10-07 00:05 WAT - Correction to the 10M funding finding
- Did: re-read the live balances after seed. Sandbox pre-funds 10,000,000 in USD/EUR/GBP/CNY *before* any deposit, so `make seed` added on top (USD 10,120,000 = 10M pre-funded + 40k + prior 80k deposits). This means the demo's cash-scarcity premise does not hold against real sandbox balances. Recorded as an open scope decision in §2/§6 rather than silently proceeding.
- Files: AGENTS.md (§2 SCOPE DECISION, §6).
- Commands: live `GET /balances/current` via the client.
- Result: partial — the code is correct, the demo *narrative* needs a decision. Recommend a Closebook-side virtual committed-cash budget so the reserve floor still binds without needing platform access.
- Next: get david's pick among (a) reframe as scale, (b) virtual committed-cash budget, (c) fresh non-prefunded sandbox business; then start Phase 2 matching.

### 2026-10-07 01:20 WAT - Phase 2: matching engine, ledger, policy, and the committed-cash budget (option b)
- Did: implemented the three core domain modules and wired option (b) for real. **`domain/matching.py`** — 5 tiers (`try_exact` → `try_rule` → `try_exact_multi` subset-sum → `try_partial` best-fit → `try_llm` with full code re-validation); word-safe `normalise()` separated from token-scoped `fold_digits()`; payer-identity gate on every amount-based match; material-short vs ambiguous-short boundary. **`domain/ledger.py`** — `post()` balancing per currency and idempotent per (txn, account, currency), `apply_inflow_to_receivables()` as the only writer of invoice status, `reconcile()` as a pure read. **`domain/policy.py`** — `CommittedCash`, `BudgetSettings`, `load_budget()` reading `settings/committed_cash.json`, reserve floor, auto-pay limit, SWIFT fee inside the converted amount, deterministic `prioritise()`. **`settings/committed_cash.json`** — the virtual budget (USD 40,000 committed, USD 5,000 floor, USD 2,500 auto-pay limit).
- Files: backend/app/domain/{matching,ledger,policy}.py (new), settings/committed_cash.json (new), tests/{test_matching,test_policy,test_ledger}.py (new), backend/app/db.py (StaticPool + module-level DB helpers), AGENTS.md (§1–4 rewritten, §5–7 appended).
- Commands: `make test` → **97 passed** (was 30). Live check against the real settings file: USD 2,500 payout auto-pays, USD 34,000 does not breach the floor, USD 36,000 does — the demo tension option (b) was meant to create now demonstrably exists.
- Result: works, and the test-writing found **six genuine overreach bugs in my own first draft**, all now fixed: (1) `fold_digits` corrupted `INV`→`1NV`, breaking every invoice match; (2) tiers 1–3 returned before tier 4, so a hostile LLM proposal was never re-validated; (3) a fuzzy payer + mismatched amount auto-matched a single open invoice; (4) the "only one open invoice" rule ate a genuine part-payment; (5) an unpriced currency valued at 0 USD read as "small" and auto-paid; (6) a fully-overpaid invoice was marked exactly `PAID`, losing the credit. Also fixed: `:memory:` SQLite handing each connection a fresh empty DB (StaticPool), and a test fixture that hardcoded `status=OPEN` on a paid invoice.
- Next: Phase 3 — `domain/planner.py` (plan CONVERT/PAY/DEFER/ESCALATE against the committed-cash budget, deferring anything that breaches the floor), then `executor.py` with the request_id guard.

### 2026-10-07 02:40 WAT - Phase 3: planner (pure function), wired into `make demo`, rate convention unified
- Did: built `domain/planner.py` — a pure function (cash + payables + budget → ordered justified plan, no DB/API/model). Rules in order: dependency gate (a part-payment does NOT release a dependent payable), currency supported/pricable, rail fee added inside the amount, CONVERT sized with drift tolerance rounded UP, reserve floor (defer, never trim), auto-pay limit (flag approval, never drop). Rewrote `run_demo.py` beat 1 to run the REAL planner over the REAL `settings/committed_cash.json` instead of a hand-rolled preview. Added `scripts/verify_rate_convention.py` and `tests/test_planner*.py`.
- Files: backend/app/domain/planner.py (new), backend/app/seed/run_demo.py (rewritten), backend/app/domain/policy.py (rate convention + `priority_key`), scripts/verify_rate_convention.py (new), tests/test_planner.py + tests/test_planner_priority.py (new), AGENTS.md.
- Commands: `make test` → **123 passed** (was 97). `make demo` → beat 1 prints the real plan: DEFER PO-7001 (waiting on INV-0041), DEFER PO-7002 (waiting on INV-0042), auto-PAY PO-7003. Live: `scripts/verify_rate_convention.py` → USD 9,012.85 → EUR 7,914.85 → USD 9,012.84, round-trip error 0.0001%, `client_rate` 1.138726, inverse 0.878174 EUR/USD.
- Result: works, and two serious bugs were caught and fixed. **(1) Inverted rate convention** — `policy.usd_value_minor` multiplied by the rate while `planner.CashPosition.to_usd_minor` divided by it, from the SAME dict, so the reserve floor and the auto-pay limit valued the same EUR 9,012.85 payout 30% apart ("USD 7,891.65"). Every isolated unit test passed. Standardised on `rates[CCY] = CCY per 1 USD`, documented it in the function that owns it, made the planner delegate instead of re-deriving, pinned it with `test_rate_convention_is_ccy_per_usd`, and proved it live. **(2) A payable was silently DROPPED from the plan** — the planner rebuilt its payable list from a dict keyed on (criticality, due_date, defer_cost); two payables sharing that tuple collided and one vanished (`planned={'PO-B'}`). Fixed by sorting indices via `policy.priority_key()`, now the shared source of truth for ordering. Both were found only because I cross-checked module boundaries against each other and against live data rather than trusting green tests.
- Next: Phase 3 — `domain/executor.py` with the request_id guard (persist before sending, never re-issue a step holding an `airwallex_object_id`, resolve ambiguity by lookup before any retry). The planner already emits `needs_approval` + `approval_reason`, so approvals can consume it directly.
