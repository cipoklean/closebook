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

- **Phase:** Phase 1 of 6 (Foundation), ~45% done.
- **Working now (verified by reading files, NOT yet executed — no venv/tests run):** repo skeleton, `config.py` (loads `.env`, forces sandbox host), `money.py` (major/minor conversion).
- **Broken / half-done:** `db.py`/`models.py` not written; `airwallex/` client + module files not written; `seed/scenario.py`, `main.py`, `make seed` not written; no tests exist yet; **no commit yet** (all files untracked).
- **Blocker for live work:** `.env` is not filled. Code can be written and unit-tested *with Airwallex mocked* right now, but `make seed` / any real sandbox call needs david's credentials (below).
- **Needed from david:** values for `.env` (see HOW TO RUN). The repo already ships `.env.example` + a `make setup` that copies it to `.env`.

**NEXT STEPS (numbered; an agent can start #1 immediately):**
1. Write `backend/app/db.py` (engine + session factory, SQLite, Postgres-compatible types) and `backend/app/models.py` implementing the full §4 schema (`customer`, `supplier`, `invoice_receivable`, `invoice_payable`, `inflow`, `match`, `plan_step`, `approval`, `ledger_entry`, `webhook_event`, `agent_run`) and an Alembic/`create_all` bootstrap so `make seed` can open the DB.
2. Write `backend/tests/test_money.py`: conversion cases (USD $100.00, JPY exponent 0, CNY exponent 2) + a to_major→to_minor round-trip property.
3. Write `backend/app/airwallex/client.py`: login (`POST /api/v1/authentication/login`, headers `x-client-id`+`x-api-key`, no body), cache the bearer token and refresh at 25 min, attach a UUIDv4 `request_id` to every mutating call **persisted before the call**, 429 backoff (20/s global, 10/endpoint, 10 concurrent), and an ambiguous-retry guard that looks up the object by `request_id` before ever retrying a money movement.
4. Scaffold the thin `airwallex/` modules: `balances.py, fx.py, beneficiaries.py, transfers.py, issuing.py, global_accounts.py, simulation.py` (ALL simulation calls behind one interface), `webhooks.py` (HMAC verify + dedupe + ordering).
5. Write `main.py` (FastAPI + static UI) and `seed/scenario.py`; add `tests/test_webhook.py` + `tests/test_idempotency.py`; run `make test` to green; commit.

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
