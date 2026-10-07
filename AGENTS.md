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

- **Phase:** Phase 3 of 6 (Planner + Executor + Approvals), **~80% done**. Phase 1 (Foundation) 100% verified live; Phase 2 (matching + ledger) 100%; Phase 3 planner, approvals and executor all done and tested.
- **Working now (all verified by actual runs, not just file reads):**
  - `make test` → **146 passed** (money 11, webhook 6, idempotency 4, airwallex-contract 8, matching 28, policy 28, ledger 13, planner 25, executor 20, crash-recovery 3)
  - **Executor (Phase 3, `domain/executor.py`)** → the ONLY module permitted to move money. Four hard gates, each with its own tests: (1) `request_id` persisted and COMMITTED before the POST leaves; (2) a step already holding an `airwallex_object_id` is never re-sent; (3) ambiguity is resolved by `find_by_request_id` lookup, never a blind re-send; (4) approvals re-verified on every path including retries. `dry_run=True` by default.
  - **Crash recovery verified (ACCEPTANCE MET)** → `tests/test_crash_recovery.py` simulates dying after the POST left the machine but before the object id was written back. A brand-new `Executor` re-runs the step and **adopts the original transfer via lookup — zero duplicate creates.** A second run after a clean writeback is a pure no-op.
  - **Hash-bound approvals (Phase 3, `domain/approvals.py`)** → every approved term (amount, currency, counterparty, supplier id, invoice ids, FX quote + rate) folds into a SHA-256 `bound_hash`, HMAC-signed with `CLOSEBOOK_HMAC_SECRET`. Re-checked at execution: a different amount, supplier, expiry or void all refuse. `check_fx_drift()` reports drift beyond `fx_tolerance_pct` so a beat-4 re-approval can be driven from real numbers.
  - **Planner (Phase 3)** → `domain/planner.py`: pure function, cash + payables + budget → ordered FX/PAY/DEFER/CHASE/ESCALATE plan, every step with a reason.
  - **`make demo` runs the REAL planner against the REAL budget** (beat 1 prints the actual plan: DEFER PO-7001, DEFER PO-7002, auto-PAY PO-7003).
  - **Rate convention verified LIVE against Airwallex**: `scripts/verify_rate_convention.py` round-trips USD 9,012.85 → EUR 7,914.85 → USD 9,012.84 (error 0.0001%) against the real `client_rate` 1.138726, confirming `rates[CCY] = CCY per 1 USD`.
  - `make seed` → **runs live**: creates USD + GBP global accounts, deposits, snapshots balances. Last verified balances: USD 10,120,000 · GBP 10,018,000
  - **FX quote → conversion verified live**: USD 9,012.85 → EUR 7,914.85 at client_rate 1.138726
  - **Matching engine (Phase 2)** → `domain/matching.py`: 5 tiers, all unit-tested, including the code re-validation that stops a bad model proposal
  - **Double-entry ledger (Phase 2)** → `domain/ledger.py`: balanced-per-currency postings, idempotent per (txn, account, currency), receivable status derived from amounts, unapplied cash parked in suspense
  - **Policy engine (Phase 3)** → `domain/policy.py`: reserve floor, auto-pay limit, SWIFT fee, bank-fee tolerance, deterministic prioritisation — all pure functions, all tested
  - **Virtual committed-cash budget (option b, chosen by David)** → `settings/committed_cash.json` + `policy.load_budget()`. USD 40,000 committed, USD 5,000 floor, USD 2,500 auto-pay limit. A USD 2,500 payout auto-pays, USD 36,000 breaches the floor, USD 34,000 is fine. Sandbox balance is capacity, never policy.
  - `make dev` → FastAPI boots; `/healthz` and `/state` return 200
- **✅ RESOLVED — the sandbox pre-funds 10M problem (option b implemented).** The Airwallex sandbox auto-hosts 10,000,000 in USD/EUR/GBP/CNY, so policy written against the raw balance never binds. David chose **(b): a virtual committed-cash budget**. Closebook tracks its own committed cash in `settings/committed_cash.json` and treats the sandbox balance as *capacity* — the ceiling of what it could move.
- **Bugs found & fixed (all caught by running, before/against live):**
  - **The executor REFUSED to recover a crashed step** — it rejected `SUBMITTED` as non-submittable, which would strand every payment in flight when the process died. `SUBMITTED` is now resolved by `find_by_request_id` lookup instead. Caught ONLY by the crash-recovery test; every unit test passed while this bug was live.
  - **Money could move with no idempotency spine.** The executor silently proceeded with a `None` row; now a PAY/FX step with no persisted `plan_step` is a hard `ExecutionRefused` — there would be nowhere to store the request_id.
  - **`db.session_scope()` was a bare generator** whose docstring promised `with session_scope() as s:` — every caller got `TypeError: 'generator' object does not support the context manager protocol`. It had only "worked" because no shipped path used it; the executor was its first real consumer. Added the missing `@contextmanager`.
  - **Tests depended on a developer's local `.env`** for `CLOSEBOOK_HMAC_SECRET`, so the suite only passed when I injected the variable into my own shell. `tests/conftest.py` now sets a fixed test-only value; `make test` is green with no environment at all.
  - **A SQL `LIKE` on `payload_json` matched plan steps by text** — `%` and `_` are wildcards and the separator spacing had to match the writer exactly. Now parses the JSON and compares the value.
  - **A payable was silently DROPPED from the plan** — the planner rebuilt its list from a dict keyed on (criticality, due_date, defer_cost); two payables sharing that tuple collided and one vanished (`planned={'PO-B'}`). Fixed by sorting indices via `policy.priority_key()`, now the shared source of ordering truth.
  - **`usd_value_minor` had an INVERTED rate convention (the worst bug so far).** `policy` multiplied by the rate while `planner` divided by it, from the same dict — the reserve floor and the auto-pay limit valued the same EUR 9,012.85 payout 30% apart. Every isolated test passed. Standardised on `rates[CCY] = CCY per 1 USD`, documented in the owning function, planner delegates, pinned by a test and proven live.
  - **`Decimal("1.10")` and `Decimal("1.1")` produced different approval hashes**, voiding a valid approval over trailing zeros. `canonical()` now normalises before stringifying.
  - `Decimal.normalize()` → `1E+2` scientific notation; Makefile `Error 127`; `run_demo` read `payout_method` off the wrong table; digit-folding turned `INV` into `1NV`; an unpriced currency valued at 0 USD and auto-paid; a fully-overpaid invoice marked exactly `PAID`, losing the credit; `:memory:` SQLite handing each connection a fresh empty DB
  - **Live-discovered (Phase 1):** login field is `token`; `global_accounts` list is under `items`; create + `fx/*` return bare objects; `balances/current` is a bare list keyed `available_amount`; `fx/rates/current` needs `buy_currency`+`sell_currency`; `fx/quotes/create` requires a `validity` enum
- **Not done:** webhook-to-executor wiring, agent/LLM tools, live beat execution (Phases 4–5). `main.py` serves a minimal index only. The executor is fully tested but has **not yet issued a real live transfer** — every test runs against fakes.
- **Blocker for live work: NONE for Airwallex.** `.env` has `AWX_CLIENT_ID` + `AWX_API_KEY` (auth verified HTTP 201) and now also `CLOSEBOOK_HMAC_SECRET`. Still optional: `AWX_WEBHOOK_SECRET` (Phase 4 only — polling fallback covers the demo) and an LLM provider (Phase 5 only — **no money needed**, use the free OpenAI-compatible route per §5).

**NEXT STEPS (numbered; an agent can start #1 immediately):**
1. **Phase 4 — first LIVE money movement through the executor**: seed a real beneficiary for a supplier, persist a `plan_step`, execute one small auto-pay (PO-7003, USD 1,800 — under the 2,500 limit so no approval needed). Verify the transfer object id lands in `airwallex_object_id` and that re-running is a no-op. Everything so far is unit-tested against fakes; this proves the real path.
2. **Phase 4 — wire the webhook receiver to the executor** so a `payout.transfer.failed` triggers the incident/recovery flow instead of a blind retry (beat 5).
3. **Phase 4b — beats 3–6 execute live** instead of printing expected outcomes, including the beat-4 re-approval driven by `check_fx_drift`.
4. **Phase 5 — agent/LLM tools + one-page dashboard.** The UI must surface the plan, the approvals and the reconciliation report — anything not linked from the main screen does not exist for a judge.
5. **Phase 5 — README + demo video.** Submission deadline 2026-11-13 18:29 UTC.

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
        approvals.py        # SHA-256 bound_hash over the exact approved terms + HMAC        [DONE]
        executor.py         # the ONLY module allowed to move money; request_id spine        [DONE]
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
    test_planner.py test_planner_priority.py test_executor.py test_crash_recovery.py
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
- **2026-10-07 — Approvals bind TERMS, not amounts-in-prose.** Every term an approver agrees to (amount, currency, counterparty, supplier id, invoice ids, FX quote + rate) is folded into a SHA-256 `bound_hash` and HMAC-signed; `assert_fresh()` re-derives that hash at execution time and refuses on any difference. Rationale: a human approved *€9,000 at that rate to that supplier* — a €9,000 approval that quietly becomes €11,400 after a 30% FX move is no longer the thing they agreed to. Re-checked on every execution path *including retries*, because a retry is exactly when nobody re-reads the numbers. Rejected: a PENDING/APPROVED column (survives no drift) and a hash computed only at signing time (protects nothing at the moment it matters).
- **2026-10-07 — The hash is unkeyed; a separate HMAC is keyed.** `bound_hash` is a content address so a human can read and diff which terms were approved; `sign()` is the keyed half that proves this instance issued it. Rationale: a leaked hash alone cannot forge an approval, so there is no reason to entangle the two. Rejected: signing the hash itself.
- **2026-10-07 — Money cannot move without a durable idempotency spine.** A PAY or FX step with no persisted `plan_step` row is a hard `ExecutionRefused`, not a silent no-op. Rationale: the request_id lives on that row and it is the only thing standing between a crash and a duplicate payment; if there is nowhere to persist it, the step cannot be executed *safely*, and skipping a payment is cheap while a double payout is not. Rejected: returning `None` and proceeding.
- **2026-10-07 — A step left `SUBMITTED` is unresolved, not done.** `SUBMITTED` means "we sent it and may never have found out" — which is precisely the state a crash mid-POST leaves behind. The executor resolves it by `request_id` lookup rather than refusing it. Rationale: refusing would strand every payment that was in flight when the process died, turning a recoverable crash into stuck money. This shipped as a bug and only the crash-recovery acceptance test caught it while every unit test stayed green.
- **2026-10-07 — The executor is `dry_run=True` by default.** Real submission is opt-in. Rationale: a money-moving module should have to be told to move money; the safe state should be the state you get by accident. Rejected: defaulting to live because the tests inject fakes anyway.
- **2026-10-07 — `PlanStep` rows are found by PARSING `payload_json`, never by SQL `LIKE`.** Rationale: `%` and `_` are LIKE wildcards so a payable number containing either matches the wrong row, and separator spacing must match the writer byte-for-byte. Text-matching a JSON column to identify a payment is how a step gets bound to someone else's transfer. Rejected: the LIKE query for being one line shorter.
- **2026-10-07 — Tests set their own `CLOSEBOOK_HMAC_SECRET`.** `tests/conftest.py` provides a fixed test-only value so the suite never reads a developer's real `.env` and never needs an injected variable. Rationale: I had a green 146-test run that only passed because I had exported the secret in my own shell — a suite that depends on undocumented local state is not green, it is lucky. Rejected: relying on `.env` being present.
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

### Beneficiary/transfer traps (verified live 2026-10-07)
- **`transfers/create` needs BOTH `source_currency` and `transfer_currency`, plus a `reason`.** Missing either returns `validation_failed` naming the field. Cross-currency payouts debit `source_currency` and pay `transfer_currency`.
- **Surface `payload.details.errors[].source`, not `str(exc)`.** Airwallex's top-level message for a validation failure is generic; the offending field is only in the details array. `_error_detail()` in the executor extracts `field=… code=…` and it immediately found `source_currency` on the next call. A money path that logs only the exception string gives an operator nothing to act on.
- **A per-run `plan_step` is not a durable duplicate guard.** The executor's `ALREADY_DONE` check works within one run, but seeding a FRESH `plan_step` per invocation creates a second real payment — which happened, twice, in the sandbox. Binding one plan_step to a payable per plan run is the actual fix; see §7. Do not report "no duplicate protection" from a single-run test alone.
- **IBAN checksums must be computed.** `NL08INGB0000001234` passes MOD-97; a plausible-looking IBAN typed from memory does not (code 083, at creation time).
- **`request_id or _reserve_request_id(...)` evaluates the fallback EAGERLY.** Passing an explicit `request_id` with no `plan_step` still called `_reserve_request_id(None, None)` and crashed on `None.request_id`. This sat in `beneficiaries`, `transfers`, `fx` and `issuing` — including the exact two methods the executor calls for every payment — and only appeared when I finally invoked `create()` for real. Use `if request_id is None:` everywhere. The tell: one module (`global_accounts.py`) already had the correct guard.
- **A docstring is not evidence that a helper works.** `db.session_scope()` promised `with` usage and raised TypeError for every caller. Same class of problem: write the call, then run it.
- **`validate()` and `create()` take DIFFERENT shapes.** `validate(inner_beneficiary)` wraps it for you; `create()` needs `{"beneficiary": {...}}` already in the body plus `transfer_methods: [...]`. Passing the full body to `validate` double-nests it and returns error 001 on fields that are present.
- **Airwallex error 001 names the field you did NOT send.** That is the whole debugging strategy for beneficiaries: send, read which field it complains about, fix that one, repeat. Do not reason about the shape — probe it.
- **`account_routing_value1`, not `account_routing_number`.** `entity_type` is `PERSONAL`/`COMPANY`, never `INDIVIDUAL`. EUR corridors need an explicit `iban` with a real MOD-97 checksum or you get code 083 at CREATION (which is the good time to find out).
- **"optional" in the schema can still be required at create time.** Fields flagged `rules_known: false / DEPENDENCY_NOT_PROVIDED` (routing number, account number, category) are only revealed once you supply the field they depend on. Trust the create-time error, not the schema's `required` flag.
- **An IBAN checksum must be computed, not invented.** `NL08INGB0000001234` passes MOD-97; the plausible-looking `NL91INGB0000001234` I typed from memory does not.

### Executor/approval traps (Phase 3)
- **A crashed step is `SUBMITTED`, and that state must be RESOLVED not REFUSED.** My first executor rejected `SUBMITTED` as "not submittable" — which meant every payment in flight when the process died would be stuck forever. Never treat an in-flight status as either proof of success or proof of failure; it is proof of *ignorance*, and the only cure is a `request_id` lookup. This passed every unit test and was caught only by the crash-recovery acceptance test.
- **`db.session_scope()` was a bare generator with a docstring promising `with` usage.** It raised `TypeError: ... does not support the context manager protocol` for every caller. It had only "worked" because no shipped code path used it — the executor was its first real consumer. A helper that is only exercised by its first caller today is a helper that is broken.
- **A money path that tolerates a `None` row is a money path with no idempotency spine.** Do not "handle" the missing plan_step gracefully; refuse the transfer. Graceful handling here means a payment nobody can de-duplicate after a crash.
- **Never identify a plan step with SQL `LIKE` on a JSON column.** `%` and `_` are wildcards and the spacing must match the writer byte-for-byte. Parse the JSON and compare values.
- **`Decimal("1.10") != Decimal("1.1")` as strings.** `str()` of each differs, so an approval hash built on `str(fx_rate)` gets voided by trailing zeros alone. Normalise before hashing.
- **A green suite that depends on your own shell is not green.** I ran 146 passing tests with `CLOSEBOOK_HMAC_SECRET` exported manually; `make test` then failed 5. Put the fixture in `conftest.py` and always run the REAL command (`make test`), never a hand-modified invocation.
- **An FX step's identity is the payable it funds.** Real `Step`s of action FX carry `payable_number` (see `planner.py`); a test that builds one without it is not testing a realistic step, it is testing an impossible one.

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

### 2026-10-07 03:35 WAT - Phase 3 complete: executor (only module that moves money) + hash-bound approvals + crash recovery ACCEPTED
- Did: wrote `domain/executor.py` (the sole money-moving path: request_id persisted AND committed before the POST, never re-issue a step holding an `airwallex_object_id`, resolve ambiguity by `find_by_request_id` lookup, `dry_run=True` by default) and `domain/approvals.py` (SHA-256 `bound_hash` over amount/currency/counterparty/supplier/invoice ids/FX quote+rate, HMAC-signed, re-verified via `assert_fresh()` on every execution path including retries; `check_fx_drift()` for beat-4 re-approval). Added `tests/test_executor.py` (20) and `tests/test_crash_recovery.py` (3).
- Files: backend/app/domain/executor.py (new), backend/app/domain/approvals.py (new), tests/test_executor.py (new), tests/test_crash_recovery.py (new), backend/app/db.py (`@contextmanager` fix), tests/conftest.py (test-only HMAC secret), AGENTS.md.
- Commands: `make test` → **146 passed** (was 123). `make demo` unchanged and still printing the real planner's plan for beat 1.
- Result: works, and the crash-recovery ACCEPTANCE is met — killing the process after the POST leaves but before the object id is written back, then restarting with a brand-new Executor, **adopts the original transfer via request_id lookup with zero duplicate creates**; a second run after a clean writeback is a pure no-op. Four real bugs found and fixed: (1) the executor REFUSED crashed `SUBMITTED` steps, which would strand every payment in flight at the moment of a crash — the single most important bug in this phase, and it passed every unit test until the acceptance test existed; (2) `db.session_scope()` was a bare generator whose docstring promised `with` usage and which raised TypeError for every caller — it had only escaped notice because nothing shipped used it, and the executor was its first real consumer; (3) money could move with no idempotency spine (None rows silently tolerated) — now a hard `ExecutionRefused`; (4) a SQL `LIKE` on `payload_json` matched plan steps by text, where `%` and `_` are wildcards. Also fixed `Decimal("1.10")` vs `Decimal("1.1")` hashing differently and voiding valid approvals, and caught that my own 146-test run only passed because I had exported `CLOSEBOOK_HMAC_SECRET` in my shell — `make test` failed 5 until conftest set a test-only value. Verified with the real `make test` command, not a hand-modified invocation.
- Result (limits): every executor test runs against fakes. **No live transfer has been issued through this executor yet.** Live beneficiary creation + one small real payout is the next step, since that is the only thing that can prove the real path rather than the modelled one.
- Next: Phase 4 — create a real Airwallex beneficiary, persist a plan_step, and execute the PO-7003 USD 1,800 auto-pay live (under the 2,500 limit so no approval needed), then re-run to prove the no-op. Then wire the webhook receiver to the executor for beat 5.

### 2026-10-07 04:20 WAT - Live beneficiary corridor probe: 9 more API contract corrections found
- Did: wrote `scripts/probe_beneficiary_schema.py` and `scripts/probe_beneficiary_create.py`, and derived the real corridor payloads by asking Airwallex and reading its error codes instead of guessing. Created the two demo beneficiaries LIVE.
- Files: scripts/probe_beneficiary_schema.py (new), scripts/probe_beneficiary_create.py (new), backend/app/airwallex/beneficiaries.py (contract + bug fixes), backend/app/airwallex/{client,fx,transfers,issuing,global_accounts}.py (eager-`or` fix), AGENTS.md.
- Commands: `make test` → 146 passed. `python scripts/probe_beneficiary_schema.py` → flat dotted schema map. `python scripts/probe_beneficiary_create.py` → **both corridors VALIDATE OK and CREATE OK**: US/USD LOCAL beneficiary `9f187b28-9973-4535-8001-68cacc8a3cd8`, NL/EUR SWIFT beneficiary `6647f065-5075-412f-ba90-199e54200fe5`.
- Result: works. Nine contract corrections, every one found by a real response rather than by reasoning: (1) `/beneficiaries/schema` returns a **flat dotted map** keyed `create_beneficiary_request.beneficiary.<field>`, not a nested `properties` tree — my first walker assumed the tree shape and silently printed nothing at all; (2) allowed values live in `validation_functions[].parameters.values`, e.g. `entity_type` is exactly `PERSONAL` or `COMPANY` (I sent `INDIVIDUAL` → code 011); (3) `payment_methods` sits at the top level, sibling of `beneficiary`; (4) the US routing field is **`account_routing_value1`**, NOT `account_routing_number`; (5) `account_name` is required in bank_details; (6) EUR corridors require an explicit **`iban`**, and a bad checksum fails with code 083 **at creation**, not at pay time; (7) `validate()` takes the inner beneficiary and wraps it itself, but `create()` requires the body to already contain a `beneficiary` key — an asymmetry my first draft got backwards; (8) `create()` additionally needs `transfer_methods` as a list; (9) fields marked `rules_known: false / DEPENDENCY_NOT_PROVIDED` in the schema are still required at create time, so "optional in the schema" is not "optional in practice".
  Also a genuine latent bug in five modules: `rid = request_id or _reserve_request_id(db_session, plan_step)` evaluates the RIGHT side eagerly, so passing an explicit `request_id` with no `plan_step` still crashed on `None.request_id`. Present in `beneficiaries`, `transfers`, `fx`, `issuing` — including the two the executor calls on every payment. `global_accounts.py` already had the correct guard, which is how the inconsistency became visible; all six now use the same explicit `if request_id is None` form. `_reserve_request_id` now also mints a fresh id when `plan_step is None` instead of crashing.
- Next: execute the first LIVE money movement through the executor — PO-7003 USD 1,800 to the US beneficiary, under the auto-pay limit so no approval is needed, then re-run to prove the no-op. Then a second live payout that DOES require approval, to exercise `assert_fresh` against a real quote.

### 2026-10-07 05:05 WAT - FIRST LIVE MONEY MOVEMENT: USD 1,800 paid through the executor, duplicate guard proven
- Did: wrote `scripts/live_payout.py`, which builds the real plan from the real budget, persists a real `plan_step`, executes it through the real `Executor` against the live sandbox, then re-runs it. Along the way improved the executor's error reporting and fixed two transfer-body contract bugs.
- Files: scripts/live_payout.py (new), backend/app/airwallex/transfers.py (source_currency + reason), backend/app/domain/executor.py (`_error_detail`), tests/test_airwallex_contract.py (+4 contract tests), AGENTS.md.
- Commands: `make test` → **150 passed** (was 146). `python scripts/live_payout.py` → transfer `df48c693-6126-499c-8039-71849f250d16` created for USD 1,800.00, status PROCESSING on Airwallex; re-run returned **ALREADY_DONE, no duplicate**.
- Result: **it works, against the real sandbox.** The chain that had only ever been tested against fakes now holds live: real plan step → persisted request_id `2812fc8f-…` committed before the POST → live transfer object id written back → second execution short-circuited.
  Two more live contract bugs, both found by reading real errors: `transfers/create` requires **`source_currency`** (the debited currency) in addition to `transfer_currency`, and requires a **`reason`** string — neither was in my Phase 1 wrapper. The second only surfaced because I first fixed the executor's error reporting: `str(ApiError)` was "[code] generic message" and the offending field lives in `payload.details.errors[].source`. `_error_detail()` now extracts `field=… code=…`, which turned a dead end into a one-line fix — it paid for itself on the very next call.
- **Honest caveat — a real duplicate exists, and it is NOT the executor's fault.** Running `live_payout.py` a second time produced a SECOND live transfer (`06283797-…`, same USD 1,800) because the script mints a fresh `plan_step` on every invocation, so there is nothing for the executor to recognise. Within one run the guard is proven correct; across separate runs of the seeding script it is not. The fix belongs in the plan layer (a payable must resolve to ONE plan_step per plan run, and a payable already carrying a transfer must not be re-planned) — that is the next piece of work, and it is called out as its own step rather than papered over. Two sandbox transfers for USD 1,800 now exist; this is a sandbox and no real money moved.
- Next: make the plan→step binding unique so a payable cannot be executed twice from separate runs (the real fix for the caveat above), then the live approval-gated payout (PO-7001, EUR 9,000 SWIFT) to exercise `assert_fresh` against a real quote, then webhook→executor wiring for beat 5.
