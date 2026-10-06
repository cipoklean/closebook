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

- **Phase:** Phase 1 of 6 (Foundation), **100% done** — verified by execution.
- **Working now (all verified by actual runs, not just file reads):**
  - `make test` → **22 passed** (money: 11, webhook: 6, idempotency: 4, +1)
  - `make seed` → seeds the local ledger and prints the plan-of-record (offline path works; sandbox funding is gated on `.env`)
  - `make demo` → drives all 7 beats offline (beat-1 preview prints which payables auto-pay vs stay HELD)
  - `make dev` → FastAPI boots; `/healthz` and `/state` return 200
  - All 13 Phase-1 modules import cleanly (config, money, db, models + 9 airwallex modules)
- **Bugs found & fixed this phase (all caught by tests, before any live call):**
  - `Decimal.normalize()` produced `1E+2` (scientific notation) — Airwallex's JSON parser would reject it; fixed with manual trailing-zero strip
  - Makefile targets broke (`Error 127`) because they `cd backend` but called the venv python by a *relative* path — fixed with an absolute `ROOT`-resolved `PY`
  - `run_demo` beat-1 read `payout_method` off `InvoicePayable`, but that column lives on `Supplier` (AttributeError on first run) — fixed to join through `p.supplier`
- **Not done:** matching engine, policy engine, planner, executor, agent/LLM tools, React dashboard (Phases 2–5). `main.py` serves a minimal index only.
- **Blocker for live work:** `.env` is still empty (created 0600 from `.env.example`). Everything above runs *without* creds; only `make seed`'s sandbox-funding path and the live demo beats need them.
- **Needed from david:** paste `AWX_CLIENT_ID`, `AWX_API_KEY`, `AWX_WEBHOOK_SECRET`, `ANTHROPIC_API_KEY` into `~/closebook/.env` (via `vim ~/closebook/.env` or the file — never in chat).

**NEXT STEPS (numbered; an agent can start #1 immediately):**
1. **Phase 2 — `domain/matching.py`**: the 5-tier matcher (EXACT → RULE → partial/multi-invoice subset-sum → LLM-assisted with code re-validation → <0.75 ESCALATE). Unit tests for exact, typo (O/0), partial, multi-invoice, overpaid, ambiguous — per closebook.md §5.1/Phase 2 acceptance.
2. **Phase 2 — `domain/ledger.py`**: double-entry postings + reconciliation report; receivable status transitions (OPEN→PARTIAL→PAID/OVERPAID) on each match.
3. **Phase 3 — `domain/policy.py`**: reserve floor, auto-pay limit, match-confidence auto threshold, SWIFT EUR 12.85 fee, priority ordering (criticality → due date → defer cost) as pure functions with unit tests.
4. **Phase 3 — `domain/planner.py` + `executor.py`**: rebuild-on-trigger, diff against previous plan, never re-issue a step with an `airwallex_object_id`, idempotent execution with the request_id guard. Acceptance: beats 1–2 end-to-end; kill mid-transfer and restart → no duplicate.
5. After Phase 3: wire `main.py`'s webhook path to the executor (currently marks processed only), and build the React dashboard for Phase 5.

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
  backend/
    app/
      __init__.py           # (to add)
      config.py             # Settings (pydantic-settings); loads .env; forces sandbox host  [DONE]
      money.py             # major<->minor conversion, CURRENCY_EXPONENT table               [DONE]
      db.py                # engine + session (TO DO)
      models.py            # §4 ORM tables (TO DO)
      main.py              # FastAPI app + routes + static UI (TO DO)
      airwallex/
        client.py          # auth refresh, request_id, 429 backoff, retry guard (TO DO)
        balances.py fx.py beneficiaries.py transfers.py issuing.py
        global_accounts.py simulation.py webhooks.py   (TO DO)
      domain/
        matching.py policy.py planner.py executor.py ledger.py   (TO DO, Phase 2–3)
      agent/
        tools.py prompts.py runner.py   (TO DO, Phase 5)
      seed/
        scenario.py         # Kano Craft seed (TO DO, Phase 1)
      tests/               # pytest (TO DO)
  frontend/                # one-page dashboard, served by FastAPI (TO DO)
```

**Key flows and where they live:**
- **Matching** — `domain/matching.py`: exact → rule → partial/multi-invoice → LLM-assisted (code re-validates sums/ids/currency); below 0.75 → ESCALATE.
- **Policy** — `domain/policy.py`: reserve floor (5k USD), auto-pay limit (2.5k USD), match-confidence auto threshold (0.9), refunds/overpayments always need approval, SWIFT adds a flat EUR 12.85. Pure functions, unit-tested.
- **Planner** — `domain/planner.py`: rebuilds the plan on every trigger; recomputes only affected steps; never re-issues a step that has an `airwallex_object_id`.
- **Executor** — `domain/executor.py`: persist step+request_id → call → store object id → poll/await terminal state. Transfers terminal = PAID|CANCELLED; cards accept on `transaction_type: CLEARING`; FX quotes are single-use, booked over REST.
- **Approvals** — `approval` table: every approval binds a hash of amount+currency+counterparty+invoice ids+evidence; any change voids it.
- **Webhooks** — `airwallex/webhooks.py`: verify `x-signature` = HMAC-SHA256(`x-timestamp` + raw payload, `AWX_WEBHOOK_SECRET`), dedupe on event id, order by `created_at`, 200 fast. Polling fallback for deposits/transfers.
- **Data model** — §4 of the spec (ORM in `models.py`); `plan_step.request_id` is UNIQUE (idempotency); `webhook_event.event_id` is UNIQUE.

## 5. DECISIONS LOG (append only)

- **2026-10-06 — Force sandbox host in `config.py`.** Added a `field_validator` on `AWX_BASE_URL` that rejects any host other than `api.sandbox.airwallex.com`/`sandbox.airwallex.com`. Why: defense-in-depth so no dev points the build at production. Alternatives: none (a hard rule from the spec §1.1).
- **2026-10-06 — Integer minor units internally, major at the boundary.** All money is stored/computed in integer minor units and converted only in `money.py`. Why: spec §1.3. CNY uses exponent 2 (100 fen), not 1.
- **2026-10-06 — ALL simulation behind one interface (`airwallex/simulation.py`).** Demo seeds via simulation endpoints; putting them behind one seam means decision logic never depends on sandbox mechanics and prod/real money can swap in without rewrite.
- **2026-10-06 — Python 3.14.7 on this VM.** Spec said 3.11+/3.12. Deviation noted; verify httpx/pydantic/anthropic install cleanly on 3.14 during `make setup` (potential wheels gap).

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
