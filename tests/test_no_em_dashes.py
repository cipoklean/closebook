"""No em dash reaches a screen, a document, or a string that produces one.

WHY THIS EXISTS. The voice on this product is plain English, and an em dash is
the single loudest tell that text was generated rather than written. It also
survives every other check: it renders fine, it passes a smoke test, and it
only shows up when a human reads the page.

The rule is the same for en dashes USED AS PUNCTUATION. Numeric ranges keep
them, because "3-6" with a hyphen reads as arithmetic: `3-6` is minus, `3 to 6`
is prose, `3–6` is a range. So the assertion is not "no en dash anywhere"; it is
"an en dash here means it is joining numbers".

TWO NETS, ON PURPOSE.

  * The RENDER test reads what a person actually sees, on every page, against an
    empty book and a loaded one. It is the only check that cannot be fooled by
    building a string somewhere unexpected.
  * The SOURCE test reads every non-docstring string literal in the app and both
    documents. It catches the sentence that no current page happens to draw yet,
    so a new one fails CI instead of shipping.

Code comments and commit messages are deliberately out of scope. A comment is
written for whoever maintains this; the screen is written for the operator.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import get_default_db, reset_default_db, session_scope  # noqa: E402

EM = "\u2014"
EN = "\u2013"

TABS = ("home", "money-in", "money-out", "ok", "ai", "setup")
PAGES = ["/"] + [f"/app?tab={t}" for t in TABS]

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---- reading a page the way a person reads it -----------------------------
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")


def visible(html: str) -> str:
    """The text a person sees: tags gone, entities decoded, whitespace tidied.

    `<details>` content counts. It is folded, not hidden: the operator opens it,
    and a dash inside it is still a dash on the page.
    """
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    body = _TAG.sub(" ", body)
    body = (body.replace("&mdash;", EM).replace("&ndash;", EN)
                .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return _WS.sub(" ", body).strip()


def punctuation_dashes(text: str) -> list[str]:
    """Every em dash, and every en dash that is NOT joining two digits."""
    out = [f"U+2014 at {text[max(0, i - 40):i + 40]!r}" for i, c in enumerate(text) if c == EM]
    for i, c in enumerate(text):
        if c != EN:
            continue
        before = text[i - 1] if i else ""
        after = text[i + 1] if i + 1 < len(text) else ""
        if before.isdigit() and after.isdigit():
            continue                      # a numeric range: 3-6, 1-12
        out.append(f"U+2013 at {text[max(0, i - 40):i + 40]!r}")
    return out


def client() -> TestClient:
    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Isolated book, and NO provider calls.

    The page schedules a background refresh of live transfer statuses, so a
    workspace with real-looking transfer ids made this suite log in to Airwallex
    and look each one up. A test that reaches a third party is not a test, it is
    a dependency: it fails on a train and it can move nothing but confidence.
    """
    import app.dashboard as dash

    monkeypatch.setattr(dash, "LIVE_STATUS_ENABLED", False, raising=False)
    reset_default_db()
    yield
    reset_default_db()


# ---- a book that draws every state the copy appears in --------------------
def _world() -> None:
    """held, approved, sent, paid, returned, and an AI receipt with a proposal.

    Built with the app's own writers, because a hand-assembled row is how a test
    ends up proving something the product never produces.
    """
    from app.domain.ledger import post
    from app.models import (AgentTrace, Customer, Inflow, InvoiceReceivable,
                            InvoicePayable, MatchStatus, PayStatus, PayoutMethod,
                            PlanStep, RecStatus, StepStatus, StepType, Supplier)

    with session_scope() as s:
        post(s, "w:USD", [("cash:USD", 20_000_00, 0, "USD"),
                          ("opening_balance", 0, 20_000_00, "USD")])

        cust = Customer(name="Brightline Retail sweep")
        s.add(cust)
        s.flush()
        inv = InvoiceReceivable(number="INV-SWEEP-1", customer_id=cust.id,
                                currency="USD", amount_minor=12_000_00,
                                status=RecStatus.PARTIAL)
        s.add(inv)
        s.flush()

        # an AI receipt: a model proposal AND the decision it produced
        s.add(Inflow(currency="USD", amount_minor=12_000_00,
                     payer_name="Brightline Retail sweep",
                     reference_raw="invoce 1OO1 brightline retial",
                     match_status=MatchStatus.MATCHED, match_method="LLM",
                     match_reason="names Brightline",
                     proposed_json=(
                         '{"confidence": 0.89, "method": "LLM", '
                         '"reason": "names Brightline", "evidence": {'
                         '"model_claimed_confidence": 0.9, "allocations": '
                         '[{"invoice_id": %d, "amount_minor": 1200000}]}}' % inv.id)))
        s.flush()
        # AgentTrace holds what the MODEL did. The code column of the receipt is
        # the stored decision on the payment, not a field on the trace.
        s.add(AgentTrace(tool="propose_match", verdict="accepted",
                         inputs_json='{"reference": "invoce 1OO1"}',
                         outputs_json='{"confidence": 0.9}',
                         detail="the model read the reference"))

        sup = Supplier(name="Atlanta Packaging sweep", payout_method=PayoutMethod.LOCAL,
                       currency="USD", airwallex_beneficiary_id="bn_sweep")
        s.add(sup)
        s.flush()
        _payable(s, sup.id, "PO-SWEEP-HELD", 3_500_00, PayStatus.READY)
        _payable(s, sup.id, "PO-SWEEP-PAID", 900_00, PayStatus.PAID)

        # every plan-step state the tracker and the badges can draw
        for key, st, obj, why in (
            ("PO-SWEEP-SENT", StepStatus.SUBMITTED, "tr_sweep_sent", "sent to the bank"),
            ("PO-SWEEP-PAID", StepStatus.PAID, "tr_sweep_paid", "settled"),
            ("PO-SWEEP-BACK", StepStatus.CANCELLED, "tr_sweep_back",
             "BENEFICIARY_BANK_RETURNED: the account name did not match"),
            ("PO-SWEEP-OK", StepStatus.APPROVED, None, "a person approved this"),
            ("PO-SWEEP-HELD", StepStatus.PROPOSED, None, "waiting on INV-SWEEP-1"),
        ):
            s.add(PlanStep(type=StepType.TRANSFER, status=st, step_key=f"PAY:{key}",
                           airwallex_object_id=obj, reason=why,
                           payload_json=('{"payable_number": "%s", "action": "PAY", '
                                         '"amount_minor": 350000, "currency": "USD", '
                                         '"supplier": "Atlanta Packaging sweep"}' % key)))


def _payable(s, supplier_id, number, minor, status):
    from app.models import InvoicePayable

    s.add(InvoicePayable(number=number, supplier_id=supplier_id, currency="USD",
                         amount_minor=minor, status=status, criticality=3))


class TestWhatAPersonSees:
    """The render net: every page, empty and loaded."""

    @pytest.mark.parametrize("path", PAGES)
    def test_an_empty_book_says_nothing_with_a_dash(self, path):
        html = client().get(path).text
        assert html, f"{path} rendered nothing at all"
        found = punctuation_dashes(visible(html))
        assert not found, f"{path} (empty book):\n  " + "\n  ".join(found)

    @pytest.mark.parametrize("path", PAGES)
    def test_a_loaded_book_says_nothing_with_a_dash(self, path):
        _world()
        html = client().get(path).text
        found = punctuation_dashes(visible(html))
        assert not found, f"{path} (loaded book):\n  " + "\n  ".join(found)

    def test_the_loaded_world_actually_draws_those_states(self):
        """Otherwise the render test above passes by rendering nothing.

        A page that shows no receipt cannot contain a bad receipt, so proving
        the states are ON the page is what makes the assertion mean anything.
        """
        _world()
        c = client()
        money_out = visible(c.get("/app?tab=money-out").text)
        # Money out lists PAYABLES, not plan-step keys: assert the bills that are
        # actually on that screen, or this passes by asserting something the page
        # was never going to draw.
        assert "PO-SWEEP-HELD" in money_out, money_out[:600]
        assert "PO-SWEEP-PAID" in money_out, money_out[:600]
        ai = visible(c.get("/app?tab=ai").text)
        # the AI screen renders the inflow and the "Model said / Code decided"
        # receipt; assert THOSE, not a confidence number that can legitimately
        # read n/a depending on the tier that decided.
        assert "Brightline Retail sweep" in ai, ai[:400]
        assert "MATCHED" in ai
        assert "What the model" in ai, ai[:600]
        # and the states the badges and the tracker draw on the page
        assert "3,500.00" in money_out


class TestWhatTheCodeCanSay:
    """The source net: every string literal, so an undrawn sentence still fails."""

    def _strings(self, path: pathlib.Path):
        tree = ast.parse(path.read_text())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                yield node.lineno, node.value

    def test_no_string_literal_in_the_app_holds_a_dash(self):
        bad: list[str] = []
        for path in sorted((ROOT / "backend" / "app").rglob("*.py")):
            for lineno, value in self._strings(path):
                hits = punctuation_dashes(value)
                if hits:
                    bad.append(f"{path.relative_to(ROOT)}:{lineno} {value[:90]!r}")
        assert not bad, (
            f"{len(bad)} string literal(s) still carry a dash:\n  " + "\n  ".join(bad))

    @pytest.mark.parametrize("doc", ["README.md", "testsprite/closebook-PRD.md"])
    def test_the_documents_hold_no_dash(self, doc):
        path = ROOT / doc
        assert path.exists(), f"{doc} is missing"
        hits = punctuation_dashes(path.read_text())
        assert not hits, f"{doc}:\n  " + "\n  ".join(hits[:20])

    def test_a_numeric_range_is_still_allowed(self):
        """The exemption has to work, or the rule will be blamed for a range."""
        assert punctuation_dashes("beats 3\u20136 run live") == []
        assert punctuation_dashes("3-6") == []

    def test_punctuation_is_still_caught(self):
        """And the exemption must not become a hole."""
        assert punctuation_dashes("a payment \u2014 held")
        assert punctuation_dashes("held \u2013 not paid")
