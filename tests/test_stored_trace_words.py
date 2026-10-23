"""A STORED failure must read as a sentence too.

The first fix translated errors where they were RAISED, which covers new
failures and misses every row already in the database. Traces are STORED, so an
outage from three days ago was still being printed verbatim on the AI tab:

    HTTPStatusError: Client error '429 Too Many Requests' for url
    'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
    For more information check: https://developer.mozilla.org/...

...inside the visible "The rules" bubble. The translate-on-the-way-out rule is
the one that cannot be bypassed: it reaches rows written before it existed, and
a new caller cannot forget to ask for it.
"""
from __future__ import annotations

import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import reset_default_db, session_scope  # noqa: E402

#: The exact stack the operator saw. Stored, not raised, so only a render-time
#: translation can catch it.
STORED_429 = (
    "HTTPStatusError: Client error '429 Too Many Requests' for url "
    "'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'\n"
    "For more information check: https://developer.mozilla.org/en-US/docs/Web/"
    "HTTP/Status/429\n"
)

#: Vendor strings that must never be readable without opening the fold.
#:
#: `googleapis` on its own is deliberately NOT in this list: the page loads a
#: font from fonts.googleapis.com, which is legitimate infrastructure rather
#: than a stack trace. The ERROR forms are what must be hidden, so the vendor's
#: API host and the status text are listed explicitly.
LEAKY = [r"HTTPStatusError", r"developer\.mozilla\.org", r"generativelanguage",
         r"Too Many Requests"]

TABS = ["home", "money-in", "money-out", "ok", "ai", "setup"]


@pytest.fixture(autouse=True)
def _isolated():
    reset_default_db()
    yield
    reset_default_db()


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def _store_a_failed_trace():
    """A trace row exactly as the 429 left it in the database."""
    from app.models import AgentTrace

    with session_scope() as s:
        s.add(AgentTrace(
            tool="decide_match", verdict="escalated",
            inputs_json='{"inflow_id": 1, "amount_minor": 700000, '
                        '"currency": "USD", "model": "gemini/x"}',
            outputs_json='{"reason": "the model did not answer"}',
            detail=STORED_429, run_id="run-429"))


def _outside_details(html: str) -> str:
    """The page minus every folded block — what a reader sees without tapping."""
    return re.sub(r"<details\b.*?</details>", "", html, flags=re.S)


class TestAStored429ReadsAsASentence:
    def test_the_ai_tab_shows_the_sentence_not_the_stack(self, client):
        _store_a_failed_trace()
        html = client.get("/app?tab=ai").text
        visible = _outside_details(html)
        for pattern in LEAKY:
            assert not re.search(pattern, visible), \
                f"{pattern!r} is readable on the AI tab without opening Detail"

    def test_it_says_what_happened_instead(self, client):
        _store_a_failed_trace()
        visible = _outside_details(client.get("/app?tab=ai").text)
        assert "Model unavailable, so this went to a person instead of guessing." \
            in visible

    def test_the_stack_is_still_there_behind_the_toggle(self, client):
        """Folded away, not deleted: it is what a person needs to report the
        problem, and losing it would make the screen less honest."""
        _store_a_failed_trace()
        html = client.get("/app?tab=ai").text
        folded = "".join(re.findall(r"<details\b.*?</details>", html, flags=re.S))
        assert "HTTPStatusError" in folded
        assert "developer.mozilla.org" in folded

    def test_the_bubble_carries_the_translation(self, client):
        _store_a_failed_trace()
        html = client.get("/app?tab=ai").text
        bubble = re.search(r'data-testid="rules-bubble".*?</span>', html, flags=re.S)
        assert bubble, "the rules bubble must exist"
        assert "HTTPStatusError" not in bubble.group(0)

    def test_no_tab_leaks_it(self, client):
        _store_a_failed_trace()
        for tab in TABS:
            visible = _outside_details(client.get(f"/app?tab={tab}").text)
            for pattern in LEAKY:
                assert not re.search(pattern, visible), f"{tab}: {pattern!r}"

    def test_a_fresh_failure_is_translated_too(self):
        """The render-time rule must not have REPLACED the raise-time one."""
        from app.domain.plain_words import MODEL_UNAVAILABLE, provider_failure
        assert provider_failure(STORED_429)[0] == MODEL_UNAVAILABLE

    def test_ordinary_prose_is_left_alone(self, client):
        """A translator for machine errors, not a filter that eats prose."""
        from app.domain.plain_words import render

        assert render("reference identifies INV-1001 exactly") == \
            ("reference identifies INV-1001 exactly", "")
        assert render("3 held, 1 already done") == ("3 held, 1 already done", "")

    def test_the_landing_is_clean_too(self, client):
        visible = _outside_details(client.get("/").text)
        for pattern in LEAKY:
            assert not re.search(pattern, visible), pattern
