# Closebook — Makefile (dev tooling).
#
# PY resolves to an ABSOLUTE venv path so targets that `cd backend/` (to put
# `app` on sys.path) still find the interpreter. A relative `.venv/bin/python`
# breaks the moment a target changes directory (real bug hit 2026-10-06).
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PY := $(ROOT)/.venv/bin/python
PIP := $(ROOT)/.venv/bin/pip

.PHONY: help setup dev seed demo test webhook-url run-ui clean \
        testsprite testsprite-reset testsprite-url

help:
	@echo "make setup      create .venv, install requirements, copy .env.example -> .env (first run only)"
	@echo "make dev        run FastAPI + UI on :8000 (uvicorn, autoreload)"
	@echo "make seed       create/reuse sandbox global accounts, deposit scenario cash, seed ledger, print balances"
	@echo "make demo       run the 7-beat scripted demo (requires seed + .env + sandbox creds)"
	@echo "make test       run pytest (policy, matching, money, webhook, idempotency)"
	@echo "make webhook-url create a cloudflared tunnel and print the public webhook URL"
	@echo ""
	@echo "TestSprite (automated tester) — a SEPARATE instance, own DB, own PIN:"
	@echo "make testsprite       run it on :8020 against data/testsprite.sqlite3"
	@echo "make testsprite-url   open a public tunnel to :8020 and print the URL"
	@echo "make testsprite-reset empty its database (no seeded data) for a clean run"

setup:
	@if [ ! -d .venv ]; then python3 -m venv .venv; fi
	$(PIP) install -r requirements.txt
	@if [ ! -f .env ]; then cp .env.example .env && echo "Created .env from .env.example — fill in the Airwallex credentials (an LLM key is optional)."; fi
	@echo "setup complete."

dev:
	cd backend && $(PY) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

seed:
	cd backend && $(PY) -m app.seed.run_seed

demo:
	cd backend && $(PY) -m app.seed.run_demo

test:
	$(PY) -m pytest -q tests/

# Public URL for the webhook receiver (Settings > Developer > Webhooks in the
# sandbox app).
#
# This uses ngrok pinned to a RESERVED STATIC DOMAIN, so the URL does not change
# on restart. That matters: the Airwallex subscription and PUBLIC_BASE_URL both
# point at it, and a rotating URL silently breaks webhook delivery.
#
# ngrok lives at ~/.config/ngrok/ngrok on this box (not on PATH); override with
#   make webhook-url NGROK=/path/to/ngrok
NGROK ?= $(HOME)/.config/ngrok/ngrok
# Reserved static domain — also mirrored in PUBLIC_BASE_URL in .env.
NGROK_DOMAIN ?= debunk-create-headboard.ngrok-free.dev
# Must match WEBHOOK_PATH in .env; the subscription URL is domain + this path.
WEBHOOK_PATH ?= /webhooks/airwallex

webhook-url:
	@if [ ! -x "$(NGROK)" ]; then echo "ngrok not found at $(NGROK) — set NGROK=/path/to/ngrok"; exit 1; fi
	@if curl -s -m 3 http://127.0.0.1:4040/api/tunnels >/dev/null 2>&1; then \
	  echo "An ngrok agent is already running on :4040."; \
	  curl -s -m 3 http://127.0.0.1:4040/api/tunnels | grep -o '"public_url":"[^"]*"' | head -2; \
	  echo "(stop it first, or it already owns $(NGROK_DOMAIN))"; exit 1; fi
	@mkdir -p logs
	@nohup "$(NGROK)" http --url=$(NGROK_DOMAIN) 8000 --log logs/ngrok.log >/dev/null 2>&1 &
	@for i in $$(seq 1 30); do \
	  code=$$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H 'ngrok-skip-browser-warning: 1' https://$(NGROK_DOMAIN)/healthz || true); \
	  [ "$$code" = "200" ] && break; sleep 1; done; \
	 echo ""; \
	 echo "Webhook notification URL: https://$(NGROK_DOMAIN)$(WEBHOOK_PATH)"; \
	 echo "Verified: HTTP $$code from our app (not an ngrok page)."; \
	 echo "Register that URL in the Airwallex sandbox: Settings > Developer > Webhooks."

clean:
	rm -rf .venv .pytest_cache

# ---------------------------------------------------------------------------
# TestSprite — a SEPARATE instance for automated testers.
#
# An automated tester presses every button it can reach and submits invented
# data. Pointed at the operator's workspace that is not a test, it is an
# incident. This instance has its own database, its own port, its own public
# URL and its own PIN, and it labels every row it writes "testsprite".
#
# It shares the Airwallex SANDBOX keys and the NVIDIA LLM config on purpose:
# the point is the same code against the same sandbox APIs. Nothing in this
# project touches real money.
#
# Requires TESTSPRITE_PIN in .env (see .env.example). The PIN is never printed.
# ---------------------------------------------------------------------------
TESTSPRITE_PORT ?= 8020

testsprite:
	@"$(ROOT)/scripts/testsprite.sh"

# One command to start from nothing. Deletes and rebuilds the file, seeds
# NOTHING, so the run builds its own world through the Setup screens.
testsprite-reset:
	@"$(PY)" "$(ROOT)/scripts/testsprite_reset.py"

# Its own tunnel.
#
# WHY CLOUDFLARED AND NOT NGROK: ngrok's free plan puts the account's ONE
# reserved static domain on every `ngrok http` endpoint, so a second tunnel
# fails with ERR_NGROK_334 ("already online"). Its only suggested alternative is
# `--pooling-enabled`, which load-balances both endpoints on the SAME hostname —
# that would route an automated tester into the operator's real workspace at
# random, which is the exact thing this instance exists to prevent.
#
# cloudflared also removes the free-tier warning page entirely, so a bot browser
# needs no special header at all. Its URL is random and changes on restart,
# because a quick tunnel cannot claim a fixed hostname without a Cloudflare
# account and a DNS record.
TESTSPRITE_TUNNEL ?= $(shell which cloudflared 2>/dev/null || echo /usr/local/bin/cloudflared)

testsprite-url:
	@if [ ! -x "$(TESTSPRITE_TUNNEL)" ]; then echo "cloudflared not found — set TESTSPRITE_TUNNEL=/path/to/cloudflared"; exit 1; fi
	@if curl -s -o /dev/null -m 3 http://127.0.0.1:$(TESTSPRITE_PORT)/healthz; then \
	  echo "the TestSprite instance is up on :$(TESTSPRITE_PORT)"; \
	else echo "warning: nothing is answering on :$(TESTSPRITE_PORT) yet — run 'make testsprite' first"; fi
	@mkdir -p logs
	@rm -f logs/cloudflared-testsprite.log
	@nohup "$(TESTSPRITE_TUNNEL)" tunnel --url http://127.0.0.1:$(TESTSPRITE_PORT) \
	  --no-autoupdate > logs/cloudflared-testsprite.log 2>&1 &
	@for i in $$(seq 1 40); do \
	  url=$$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' logs/cloudflared-testsprite.log 2>/dev/null | head -1); \
	  [ -n "$$url" ] && break; sleep 1; done; \
	  if [ -z "$$url" ]; then echo "no URL in logs/cloudflared-testsprite.log yet — check that file"; exit 1; fi; \
	  echo ""; \
	  echo "TestSprite URL: $$url"; \
	  echo ""; \
	  echo "Checking with a PLAIN curl — no headers, no user-agent tricks:"; \
	  for path in / /app; do \
	    code=$$(curl -s -o /dev/null -w '%{http_code}' -m 20 "$$url$$path" || true); \
	    title=$$(curl -s -m 20 "$$url$$path" | grep -oE '<title>[^<]*</title>' | head -1); \
	    echo "  $$path -> HTTP $$code  $$title"; \
	  done; \
	  echo ""; \
	  echo "Add this to .env so the instance's own links point at itself:"; \
	  echo "    TESTSPRITE_BASE_URL=$$url"
