"""The AI tab counts what is STORED, however old it is.

Run 4 reported "0 calls" while 26 traces sat in `agent_trace` from the same run.
The tab reads a context list, so the count is only as honest as that list: any
window, join or cap in between turns a recorded call into an invisible one.

These tests seed rows directly, including rows from days ago and more rows than
the list renders, because "it works for the three I just made" is exactly how a
count goes wrong.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import get_default_db, reset_default_db, session_scope  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    reset_default_db()
    yield
    reset_default_db()


def _trace(n: int, *, days_ago: float = 0.0, tool: str = "propose_match",
           verdict: str = "accepted") -> None:
    from app.models import AgentTrace

    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with session_scope() as s:
        s.add(AgentTrace(
            tool=tool, verdict=verdict,
            inputs_json='{"reference": "inv %d", "amount_minor": 120000}' % n,
            outputs_json='{"confidence": 0.88}',
            detail=f"call {n}",
            created_at=when))


def _ai_page() -> str:
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app).get("/app?tab=ai").text


def _count(html: str) -> int:
    """The number the pill shows."""
    m = re.search(r'data-testid="ai-count"[^>]*>\s*(\d+)\s*call', html)
    assert m, "the AI tab rendered no count pill at all"
    return int(m.group(1))


def _stored() -> int:
    from app.models import AgentTrace

    return get_default_db().session().query(AgentTrace).count()


class TestTheCountMatchesTheStore:
    def test_it_says_the_number_that_is_stored(self):
        for i in range(7):
            _trace(i)
        assert _stored() == 7
        assert _count(_ai_page()) == 7

    def test_old_rows_count_too(self):
        """A trace from last week is still a call that happened."""
        for i in range(3):
            _trace(i, days_ago=9)
        assert _count(_ai_page()) == 3, "traces older than today were not counted"

    def test_a_run_of_twenty_six_is_not_quietly_capped_at_two_dozen(self):
        """The pill must count the store, not the length of the visible list."""
        for i in range(26):
            _trace(i)
        assert _stored() == 26
        assert _count(_ai_page()) == 26, (
            "the count is the length of the rendered list, so a busy run reads as "
            "a smaller one")

    def test_code_only_decisions_are_counted_as_rows(self):
        """`decide_match` is a row in the same table and a call in the same run."""
        _trace(1, tool="propose_match")
        _trace(2, tool="decide_match", verdict="escalated")
        assert _count(_ai_page()) == 2

    def test_an_empty_book_says_zero(self):
        assert _count(_ai_page()) == 0
