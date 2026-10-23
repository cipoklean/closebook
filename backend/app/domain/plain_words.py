"""Say it in words a person can act on.

WHY THIS EXISTS
    Two screens printed a machine's sentence at a person:

      * settling a transfer failed with Airwallex's own JSON in the URL —
        `?err={"code":"operation_failed",...}` — so the operator saw braces and
        a status code where they needed to know whether their books were safe;
      * AI activity showed
        `HTTPStatusError: Client error '429 Too Many Requests' ... googleapis ...
        developer.mozilla.org` — a stack of vendor infrastructure on a page
        about the operator's money.

    A raw error is not a message. It says what broke in the language of the
    thing that broke, and the reader is left to work out the part they actually
    care about: did anything change, and what do I do now.

THE RULE
    Every failure answers two questions in this order: what happened to my
    money, and what do I do next. The machine's own words are still kept — they
    are genuinely useful when someone reports a problem — but behind a toggle,
    where they help instead of shouting.
"""
from __future__ import annotations

import re

#: What a person reads first, per kind of failure.
SETTLE_REFUSED = ("Airwallex's sandbox refused this step. Nothing changed in "
                  "your books. Try again in a minute.")
MODEL_UNAVAILABLE = ("Model unavailable, so this went to a person instead of "
                     "guessing.")
NOT_REACHED = ("Could not reach Airwallex. Nothing changed in your books.")


def _looks_like_provider_error(text: str) -> bool:
    t = (text or "").lower()
    return any(s in t for s in (
        "httperror", "httpstatuserror", "client error", "server error",
        "too many requests", "429", "502", "503", "504", "timeout",
        "timed out", "connection", "unavailable", "rate limit",
    ))


def settle_failure(error: object) -> tuple[str, str]:
    """(the sentence, the detail) for a failed sandbox transition.

    The distinction that matters to the reader is not which HTTP status came
    back, it is that NOTHING MOVED — so that is the sentence, and the status is
    the footnote.
    """
    detail = str(error or "").strip()
    if not detail:
        return SETTLE_REFUSED, ""
    return SETTLE_REFUSED, detail


def provider_failure(error: object) -> tuple[str, str]:
    """(the sentence, the detail) for a model call that did not answer.

    A 429 or a 503 is OUR problem to retry, never something the operator did,
    and the sentence says what the system did instead of guessing: it handed the
    payment to a person. That is the honest and reassuring half of the news.
    """
    detail = str(error or "").strip()
    if not detail:
        return MODEL_UNAVAILABLE, ""
    if _looks_like_provider_error(detail):
        return MODEL_UNAVAILABLE, detail
    return MODEL_UNAVAILABLE, detail


def one_line(text: object, limit: int = 140) -> str:
    """Collapse a blob to a single readable line for a pill or a table cell."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


__all__ = ["MODEL_UNAVAILABLE", "NOT_REACHED", "SETTLE_REFUSED", "one_line",
           "provider_failure", "settle_failure"]
