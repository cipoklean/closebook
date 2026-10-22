"""Make `import app.*` work from tests without installing the package.

The backend package lives at backend/; add it to sys.path so tests and the
Makefile (`python -m pytest ../tests` style) can import cleanly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Point the app at an in-memory / temp DB for tests so we never touch real data.
os.environ.setdefault("CLOSEBOOK_DATABASE_URL", "sqlite:///:memory:")

# Tests must never depend on a developer's local .env, and must never read a
# REAL secret. Approval signing needs CLOSEBOOK_HMAC_SECRET, so set a fixed
# test-only value here. `setdefault` still lets CI override it deliberately.
os.environ.setdefault("CLOSEBOOK_HMAC_SECRET", "test-only-not-a-real-secret")

# The model tier is now WIRED (it never was — `intake` passed llm=None), so a
# test that ingests a deposit would call the configured provider. Tests must
# never touch the network: slow, flaky, and it bills somebody. An empty
# LLM_API_KEY makes the matcher honestly unavailable, which is also the state
# the offline tests assert about.
os.environ["LLM_API_KEY"] = ""
