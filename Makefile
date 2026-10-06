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
	@if [ ! -f .env ]; then cp .env.example .env && echo "Created .env from .env.example — fill in Airwallex + Anthropic credentials."; fi
	@echo "setup complete."

dev:
	cd backend && $(PY) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

seed:
	cd backend && $(PY) -m app.seed.run_seed

demo:
	cd backend && $(PY) -m app.seed.run_demo

test:
	$(PY) -m pytest -q tests/

# Print a fresh public URL for the webhook receiver (Settings > Developer > Webhooks in the sandbox app).
webhook-url:
	@if ! command -v cloudflared >/dev/null 2>&1; then echo "cloudflared not found: ~/.local/bin/cloudflared or install from https://developers.cloudflare.com/cloudflare-one/connections/cloudflare-warp/"; exit 1; fi
	@cd /tmp && cloudflared tunnel --url http://localhost:8000 2>&1 | tee closebook-tunnel.log & \
	 pid=$$!; \
	 for i in $$(seq 1 30); do \
	   url=$$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/closebook-tunnel.log | head -n1); \
	   [ -n "$$url" ] && break; sleep 1; done; \
	 echo ""; echo "Webhook notification URL: $$url$(WEBHOOK_PATH)"; \
	 echo "Sandbox source IPs to whitelist: 35.240.211.132 35.187.239.216 34.87.139.23 136.110.35.143 34.142.189.194 35.197.128.86"; \
	 echo "(tunnel runs in background, pid $$pid — kill it when done)"

clean:
	rm -rf .venv .pytest_cache
