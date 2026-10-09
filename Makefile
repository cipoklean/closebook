# Closebook — Makefile (dev tooling).
#
# PY resolves to an ABSOLUTE venv path so targets that `cd backend/` (to put
# `app` on sys.path) still find the interpreter. A relative `.venv/bin/python`
# breaks the moment a target changes directory (real bug hit 2026-10-06).
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PY := $(ROOT)/.venv/bin/python
PIP := $(ROOT)/.venv/bin/pip

.PHONY: help setup dev seed demo test webhook-url run-ui clean

help:
	@echo "make setup      create .venv, install requirements, copy .env.example -> .env (first run only)"
	@echo "make dev        run FastAPI + UI on :8000 (uvicorn, autoreload)"
	@echo "make seed       create/reuse sandbox global accounts, deposit scenario cash, seed ledger, print balances"
	@echo "make demo       run the 7-beat scripted demo (requires seed + .env + sandbox creds)"
	@echo "make test       run pytest (policy, matching, money, webhook, idempotency)"
	@echo "make webhook-url create a cloudflared tunnel and print the public webhook URL"

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
