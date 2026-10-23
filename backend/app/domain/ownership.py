"""Who does this belong to?

THE PROBLEM THIS EXISTS TO SOLVE
    Two Closebook instances — the operator's and an automated tester's — share
    ONE Airwallex sandbox global account. Airwallex has no idea they are
    different: `GET /deposits` returns every deposit in the account to whoever
    asks, and a transfer webhook is delivered to whichever receiver registered
    the URL. So without a rule, a tester's simulated deposit lands in the
    operator's suspense, and the tester's transfer events raise incidents in the
    operator's book. Neither is a test any more.

    Each instance has its OWN database, so a local flag cannot tell one instance
    about the other. The ownership mark therefore has to travel IN the deposit,
    through the shared sandbox, where both instances can see it.

THE RULE, in one line each
    * A deposit is ours if its reference carries OUR instance tag.
    * A deposit carrying ANOTHER instance's tag is never ours. Log, ignore.
    * An UNTAGGED deposit is real money from outside: the primary instance
      takes it, an instance with `CLOSEBOOK_INGEST_UNCLAIMED=0` refuses it,
      because a tester must not sweep up genuine arrivals either.
    * A transfer event is ours only if the `request_id` on it matches a plan
      step in OUR database. Request ids are minted locally, so this cannot be
      forged by a webhook that was aimed at us by mistake.

WHY A TAG AND NOT A LOCAL RECORD
    "Record the deposit id at simulate time" only works for the instance that
    made it — the OTHER instance still sees the deposit in `GET /deposits` and
    has no row to check it against. The mark has to be on the object.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("closebook.ownership")

#: Machine marker appended to a simulated deposit's reference. Deliberately
#: unglamorous and impossible to produce by accident.
TAG_PREFIX = "cbinst:"
#: The charset a name may use. The PRODUCER is constrained to it as well, so
#: the two can never disagree — an instance derived from `sqlite:///:memory:`
#: produced the tag `[cbinst::memory:]`, which this pattern does not match, and
#: the tag then leaked into the reference the operator sees.
_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")
_TAG_RE = re.compile(r"\s*\[" + re.escape(TAG_PREFIX) + r"([A-Za-z0-9._-]{1,32})\]\s*$")

#: The instance an untagged deposit belongs to when nobody claims it.
PRIMARY_INSTANCE = "closebook"


def instance_name() -> str:
    """This instance's name.

    Explicit `CLOSEBOOK_INSTANCE` wins; otherwise it is derived from the
    database filename, so `data/testsprite.sqlite3` is `testsprite` and
    `data/closebook.sqlite3` is `closebook`. Deriving it means a second
    instance is isolated even if whoever starts it forgets to say who it is.
    """
    from ..config import get_settings

    try:
        explicit = (get_settings().instance_name or "").strip()
    except Exception:                                  # settings unavailable
        explicit = ""
    if explicit and _NAME_RE.fullmatch(explicit):
        return explicit

    url = os.environ.get("CLOSEBOOK_DATABASE_URL") or ""
    if "///" in url:
        stem = url.rsplit("/", 1)[-1]
        for suffix in (".sqlite3", ".sqlite", ".db"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        # ONLY IF IT IS USABLE. A name is written into a tag that another
        # process has to parse, so a derived name that the tag grammar cannot
        # represent must fall back rather than produce a tag nobody can read:
        # an in-memory test database derives `:memory:`, and that leaked
        # `[cbinst::memory:]` straight into the reference on screen.
        if stem and _NAME_RE.fullmatch(stem):
            return stem
    return PRIMARY_INSTANCE


def ingest_unclaimed() -> bool:
    """Does this instance take deposits nobody claimed?

    Yes for the primary instance: a customer really paying is an untagged
    deposit, and refusing it would mean the books ignore actual money arriving.
    No for an automated tester: it must not sweep up genuine arrivals either.
    """
    from ..config import get_settings

    try:
        return bool(get_settings().ingest_unclaimed)
    except Exception:
        return True


def tag_reference(reference: str | None, *, me: str | None = None) -> str:
    """Append our instance tag to a reference we are about to create."""
    base = (reference or "").rstrip()
    return f"{base} [{TAG_PREFIX}{me or instance_name()}]" if base \
        else f"[{TAG_PREFIX}{me or instance_name()}]"


def read_tag(*texts: str | None) -> tuple[str | None, str]:
    """Find our tag in any of `texts`, and return (instance, text_without_it).

    Checks `reference`, `statement_ref` and `metadata`-ish fields, because which
    one Airwallex round-trips is its business, not ours — and a tag that only
    travels on one field would silently stop protecting the moment the sandbox
    changed shape.

    Returns `(None, first_non_empty_text)` when nothing is tagged, so callers
    can store the reference the operator actually typed.
    """
    clean = ""
    for text in texts:
        if text is None:
            continue
        s = str(text)
        m = _TAG_RE.search(s)
        if m:
            # Remove the tag and the single separating space we added. The rest
            # is left EXACTLY as it arrived: a messy reference is the whole
            # point of the matcher, and "helpfully" trimming it here would be
            # this module quietly deciding what the operator typed.
            return m.group(1), _TAG_RE.sub("", s)
        if clean == "" and s != "":
            clean = s
    return None, clean


def owns_deposit(claimed_by: str | None, *, me: str | None = None) -> tuple[bool, str]:
    """Is this deposit ours to record? `(yes/no, why)`.

    The `why` is returned rather than logged so the caller records it — an
    ignored event is a decision, and a decision with no reason on it is not
    auditable.
    """
    me = me or instance_name()
    if claimed_by:
        if claimed_by == me:
            return True, f"tagged for {me}"
        return False, f"tagged for '{claimed_by}', not '{me}'"
    if ingest_unclaimed():
        return True, "no tag: treated as a real arrival"
    return False, "no tag and this instance does not take unclaimed deposits"


def owns_transfer(session, request_id: str | None, *, me: str | None = None):
    """Is this transfer event ours? `(step_or_None, why)`.

    Ours means the `request_id` on the event matches a plan step in OUR
    database. Request ids are minted locally before a payment is submitted, so
    an event arriving from another instance's transfer carries a request id we
    have never seen — and acting on one would raise an incident about money we
    did not send.
    """
    from .plan_store import find_step_by_request_id

    me = me or instance_name()
    if not request_id:
        return None, "the event carries no request_id, so it cannot be ours"
    step = find_step_by_request_id(session, request_id)
    if step is None:
        return None, (f"request_id {request_id} is not in {me}'s book: the "
                      f"payment was issued by another instance")
    return step, f"request_id {request_id} is in {me}'s book"


def record_ignored(session, *, kind: str, external_id: str, detail: str) -> None:
    """Persist an ignored event so the decision is visible, not just logged.

    Deliberately NOT the incident table and NOT suspense: the requirement is
    that something foreign is ignored, and an ignored thing that appears in
    either of those is exactly the bug this module removes.
    """
    from ..models import IgnoredEvent

    try:
        ext = str(external_id or "")
        # The deposit webhook inspects two possible payload shapes, so the same
        # foreign deposit can be examined twice in one event. One decision
        # deserves one row.
        already = (session.query(IgnoredEvent)
                   .filter(IgnoredEvent.kind == kind,
                           IgnoredEvent.external_id == ext,
                           IgnoredEvent.instance == instance_name())
                   .first())
        if already is not None:
            return
        session.add(IgnoredEvent(kind=kind, external_id=ext,
                                 instance=instance_name(), detail=detail[:400]))
    except Exception:                                # never break the caller
        log.exception("could not record ignored %s %s", kind, external_id)


__all__ = ["PRIMARY_INSTANCE", "TAG_PREFIX", "ingest_unclaimed", "instance_name",
           "owns_deposit", "owns_transfer", "read_tag", "record_ignored",
           "tag_reference"]
