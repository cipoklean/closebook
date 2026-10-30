"""Matching engine tests — the Phase 2 acceptance criteria.

closebook.md requires unit tests for: exact, typo, partial, multi-invoice,
overpaid and ambiguous cases. Each test below maps to one demo beat.
"""
from __future__ import annotations

import pytest
from decimal import Decimal

from app.domain.matching import (
    Allocation, Candidate, MatchResult, fold_digits, match_inflow, normalise,
    number_blob, token_set_ratio, try_exact, try_exact_multi, try_llm,
    try_partial, try_rule,
)


def _c(inv_id, number, customer, currency, open_minor, original=None):
    return Candidate(invoice_id=inv_id, number=number, customer_name=customer,
                     currency=currency, amount_open_minor=open_minor,
                     amount_original_minor=original or open_minor)


# beat 2: INV-0041 USD 12,000
BRIGHTLINE = _c(1, "INV-0041", "Brightline Retail", "USD", 1_200_000, 1_200_000)
# beat 3: INV-0042 GBP 8,000
HARROW = _c(2, "INV-0042", "Harrow & Co", "GBP", 800_000, 800_000)
LUMEN = _c(3, "INV-0043", "Lumen Stores", "USD", 450_000, 450_000)


def test_normalise_keeps_words_intact():
    """Digit-lookalike folding must NOT corrupt words: INV-0041 -> inv0041."""
    assert normalise("INV-0041") == "inv0041"
    assert normalise("INV 0041") == "inv0041"
    assert normalise("INV_0041") == "inv0041"
    # "INV" survives — this is the bug the word-safe split fixed.
    assert normalise("INV") == "inv"


def test_fold_digits_handles_lookalike_typos():
    assert fold_digits("0O41") == "0041"
    assert fold_digits("INV-O04l") == "inv-0041"
    assert number_blob("inv 0O41 brightline") == "0041"


def test_token_set_ratio():
    assert token_set_ratio("Brightline Retail", "Brightline Retail") == 1.0
    assert token_set_ratio("Lumen Stores", "Lumen Stores") == 1.0
    assert 0.0 <= token_set_ratio("Harrow and Co", "Harrow & Co") <= 1.0
    assert token_set_ratio("", "anything") == 0.0
    assert token_set_ratio("Acme Ltd", "Globex GmbH") < 0.5


# ---- tier 1: EXACT -------------------------------------------------------
def test_exact_match_clean_reference():
    r = try_exact(1_200_000, "USD", "INV-0041 Brightline", [BRIGHTLINE, LUMEN])
    assert r is not None
    assert r.status == "MATCHED"
    assert r.confidence == 1.0
    assert r.method == "EXACT"
    assert r.allocations[0].invoice_id == 1


def test_a_CLEAN_reference_is_EXACT_at_full_confidence():
    """EXACT means the number is literally there. Nothing was inferred."""
    r = try_exact(1_200_000, "USD", "payment for INV-0041", [BRIGHTLINE, LUMEN])
    assert r is not None
    assert r.method == "EXACT"
    assert r.confidence == 1.0
    assert r.status == "MATCHED"
    assert r.allocations[0].invoice_id == 1


def test_an_O_ZERO_typo_is_RULE_and_below_the_band():
    """Beat 2 literally uses reference "inv 0O41 brightline".

    The old behaviour called this EXACT at 1.00, which is a lie about how much
    was known: nothing in the reference says "0041" until O is folded to 0. It
    is a plausible READING, so it is labelled RULE, scored below the auto band,
    and a human confirms it.
    """
    r = try_exact(1_200_000, "USD", "inv 0O41 brightline", [BRIGHTLINE, LUMEN])
    assert r is not None, "the typo must still be READ, just not trusted"
    assert r.method == "RULE"
    assert r.confidence < 0.90, "folding is a guess and must not auto-apply"
    assert r.status == "PROPOSED"
    assert r.allocations[0].invoice_id == 1
    assert "folded" in r.reason


def test_exact_requires_currency_and_amount_to_agree():
    assert try_exact(1_200_000, "GBP", "INV-0041", [BRIGHTLINE]) is None
    assert try_exact(999, "USD", "INV-0041", [BRIGHTLINE]) is None


# ---- tier 2: RULE --------------------------------------------------------
def test_rule_needs_payer_identity_for_single_invoice():
    """The "only one open invoice" rule must NOT fire on a fuzzy payer name.
    Here "L. Stores" vs "Lumen Stores" is a weak match, and the amount does not
    fit anyway, so tier 2 declines and tier 3 classifies it instead."""
    cands = [_c(9, "INV-0099", "Lumen Stores", "USD", 500_000)]
    assert try_rule(440_000, "USD", "L. Stores", cands) is None


def test_rule_single_invoice_needs_plausible_amount():
    """Even a perfectly identified payer must not have an unrelated-sized
    payment claimed for its invoice — tier 3 handles part-payments."""
    cands = [_c(9, "INV-0099", "Lumen Stores", "USD", 500_000)]
    assert try_rule(120_000, "USD", "Lumen Stores", cands) is None


def test_rule_single_invoice_fires_on_plausible_amount():
    """Identified payer, small shortfall within bank-fee tolerance -> tier 2."""
    cands = [_c(9, "INV-0099", "Lumen Stores", "USD", 500_000)]
    r = try_rule(499_800, "USD", "Lumen Stores", cands)
    assert r is not None
    assert r.confidence == 0.85  # below auto threshold: approval, not auto


def test_single_open_invoice_overpayer_proposes_capped_allocation():
    """F4: a recognized payer overpaying their single open invoice must get a
    proposal capped at the open balance with the rest as credit — not a full-
    amount proposal that approve would refuse as an overpayment."""
    cands = [_c(9, "INV-0099", "Lumen Stores", "USD", 500_000)]
    r = try_rule(600_000, "USD", "Lumen Stores", cands)
    assert r is not None
    assert r.status == "OVERPAID"
    assert [a.amount_minor for a in r.allocations] == [500_000]
    assert r.credit_minor == 100_000
    assert r.confidence < 0.90, "overpayments never auto-apply"


def test_unknown_payer_no_reference_is_not_pinned_to_an_invoice():
    """F1 leftover: an unrecognized payer with no reference must not be pinned
    to one invoice by the overpayment tier. Alpha INV-C1 USD 400, Beta
    INV-C2 USD 600, USD 1,000 from 'Mystery Payer' with an empty reference."""
    alpha = _c(1, "INV-C1", "Alpha", "USD", 400_00)
    beta = _c(2, "INV-C2", "Beta", "USD", 600_00)
    r = try_partial(1_000_00, "USD", "Mystery Payer", [alpha, beta],
                    reference="")
    assert r is not None, "the tier must still answer"
    assert r.status in ("AMBIGUOUS", "UNMATCHED"), r.status
    assert r.allocations == [], "no invoice may be named by an unknown sender"
    text = r.reason.lower()
    assert "suspense" in text, r.reason
    # The amounts do fit across the two customers, so the honest reading names
    # the possibility without choosing it.
    assert "inv-c1" in text and "inv-c2" in text, r.reason


def test_unknown_payer_overpayment_via_orchestrator_is_not_pinned():
    alpha = _c(1, "INV-C1", "Alpha", "USD", 400_00)
    beta = _c(2, "INV-C2", "Beta", "USD", 600_00)
    r = match_inflow(1_000_00, "USD",
                     {"payer_name": "Mystery Payer", "reference_raw": ""},
                     [alpha, beta])
    assert r.status != "OVERPAID", r.status
    assert r.allocations == []


def test_single_invoice_unknown_payer_empty_reference_is_ambiguous():
    inv = _c(9, "INV-1009", "Lumen Stores", "USD", 1_000_00)
    r = try_partial(1_200_00, "USD", "Mystery Payer", [inv], reference="")
    assert r.status == "AMBIGUOUS", r.status
    assert r.allocations == []
    assert "suspense" in r.reason.lower(), r.reason


def test_reference_naming_the_invoice_gates_the_overpayment_tier():
    inv = _c(9, "INV-1009", "Lumen Stores", "USD", 1_000_00)
    r = try_partial(1_200_00, "USD", "Mystery Payer", [inv],
                    reference="INV-1009")
    assert r.status == "OVERPAID", r.status  # the reference names it
    assert r.confidence < 0.90, "overpayments never auto-apply"


def test_unapplied_proposal_never_says_credit_recorded():
    """A proposal is not an application; 'credit recorded' is a lie until the
    allocation is applied. The reason must say what WOULD happen."""
    inv = _c(9, "INV-1009", "Lumen Stores", "USD", 1_000_00)
    r = try_rule(1_200_00, "USD", "Lumen Stores", [inv])
    assert r.status == "OVERPAID"
    assert "credit recorded" not in r.reason.lower(), r.reason
    assert "would leave" in r.reason.lower(), r.reason
    assert "as credit if approved" in r.reason.lower(), r.reason

    r2 = try_partial(1_200_00, "USD", "Lumen Stores", [inv],
                     reference="INV-1009")
    assert r2.status == "OVERPAID"
    assert "credit recorded" not in r2.reason.lower(), r2.reason
    assert "would leave" in r2.reason.lower(), r2.reason


def test_deferral_reason_calls_it_a_safety_cushion():
    """F7: user-facing copy says safety cushion, never reserve floor."""
    from app.domain.planner import CashPosition, Payable, build_plan
    from app.domain.policy import BudgetSettings

    pos = CashPosition(
        balances={"USD": 6_000_00},
        budget_currency="USD",
        floor_minor=5_000_00,
        rates={"USD": Decimal("1")},
    )
    plan = build_plan(
        [Payable(number="PO-1", supplier="S", currency="USD",
                 amount_minor=2_000_00)],
        pos, BudgetSettings(),
    )
    text = plan.summary()
    assert "safety cushion" in text
    assert "floor" not in text.lower()


def test_waits_for_inflow_reason_calls_it_a_safety_cushion():
    """F7: the waits_for_inflow sentence is user-facing too."""
    from app.domain.planner import CashPosition, Payable, build_plan
    from app.domain.policy import BudgetSettings, Forecast

    pos = CashPosition(
        balances={"USD": 6_000_00},
        budget_currency="USD",
        floor_minor=5_000_00,
        rates={"USD": Decimal("1")},
        forecast=Forecast(
            lines=[{"number": "INV-1", "currency": "USD",
                    "usd_minor": 10_000_00}],
        ),
    )
    plan = build_plan(
        [Payable(number="PO-1", supplier="S", currency="USD",
                 amount_minor=3_000_00)],
        pos, BudgetSettings(),
    )
    text = plan.summary()
    assert "safety cushion" in text
    assert "floor" not in text.lower()


def test_rule_high_confidence_on_fuzzy_payer_and_amount():
    r = try_rule(450_000, "USD", "Lumen Stores", [LUMEN])
    assert r.confidence >= 0.9
    assert r.status == "MATCHED"


# ---- tier 3: PARTIAL / MULTI / OVERPAID ---------------------------------
def test_partial_payment_records_shortfall():
    """Beat 3: GBP 4,800 against GBP 8,000 -> a deliberate 40% part-payment.

    Confidence is 0.85, deliberately BELOW the 0.90 auto threshold: a
    part-payment is applied to the invoice but still needs a human to decide
    whether to chase the remaining GBP 3,200.
    """
    r = try_partial(480_000, "GBP", "Harrow & Co", [HARROW])
    assert r.status == "PARTIAL"
    assert r.shortfall_minor == 320_000   # GBP 3,200
    assert 0.75 <= r.confidence < 0.90     # auto-apply no, escalate yes


def test_small_shortfall_outside_fee_tolerance_escalates():
    """GBP 7,900 vs 8,000: too big for a bank fee, too small to be a deliberate
    part-payment. Genuinely ambiguous -> must not be guessed."""
    r = try_partial(790_000, "GBP", "Harrow & Co", [HARROW])
    assert r.status == "AMBIGUOUS"
    assert r.needs_escalation


def test_unknown_payer_cannot_auto_apply_on_amount_alone():
    """A deposit with no recognisable payer must not auto-apply to an invoice
    just because the amount nearly fits."""
    r = try_partial(480_000, "GBP", "", [HARROW])
    assert r.confidence < 0.9
    assert r.status in {"PARTIAL", "AMBIGUOUS"}


def test_unrelated_payer_does_not_auto_apply():
    r = try_partial(480_000, "GBP", "Globex GmbH", [HARROW])
    assert r.confidence < 0.9


def test_partial_tolerates_small_bank_fee():
    # 800,000 expected, 799,500 received -> 50 units short, within tolerance
    r = try_partial(799_500, "GBP", "Harrow & Co", [HARROW])
    assert r.status == "PARTIAL"
    assert r.shortfall_minor == 500


def test_multi_invoice_subset_sum():
    a = _c(1, "INV-A", "Acme", "USD", 300_000)
    b = _c(2, "INV-B", "Acme", "USD", 450_000)
    r = try_exact_multi(750_000, "USD", [a, b], payer_name="Acme")
    assert r.status == "MATCHED"
    assert {x.invoice_id for x in r.allocations} == {1, 2}
    assert r.confidence >= 0.9


def test_subset_sum_across_customers_is_ambiguous():
    """F1: an unknown payer's payment must never be auto-applied across two
    different customers' invoices just because their open balances happen to
    sum to the deposit. The subset-sum tier requires a recognized payer."""
    a = _c(1, "INV-A", "Alpha Inc", "USD", 400_000)
    b = _c(2, "INV-B", "Beta LLC", "USD", 600_000)
    r = try_exact_multi(1_000_000, "USD", [a, b],
                        payer_name="Mystery Payer")
    assert r is None, "unknown payer: no auto match across customers"


def test_subset_sum_tie_is_ambiguous():
    """F1: within one recognized payer, if more than one combination of open
    invoices equals the deposit, it is a tie: AMBIGUOUS at 0.50, naming the
    candidates, exactly like the single-invoice tie."""
    a = _c(1, "INV-A", "Acme", "USD", 300_000)
    b = _c(2, "INV-B", "Acme", "USD", 700_000)
    c = _c(3, "INV-C", "Acme", "USD", 500_000)
    d = _c(4, "INV-D", "Acme", "USD", 500_000)
    r = try_exact_multi(1_000_000, "USD", [a, b, c, d],
                        payer_name="Acme")
    assert r is not None
    assert r.status == "AMBIGUOUS"
    assert r.confidence == 0.5
    assert not r.allocations
    assert "INV-A" in r.reason and "INV-B" in r.reason


def test_multi_invoice_needs_two_invoices():
    """A single exact invoice is tier 2's job, not the subset-sum's."""
    a = _c(1, "INV-A", "Acme", "USD", 300_000)
    assert try_exact_multi(300_000, "USD", [a], payer_name="Acme") is None


def test_multi_invoice_beats_single_invoice_partial():
    """The orchestrator must try the subset-sum before the partial rule, so a
    payment covering two bills is not mistaken for a part-payment of one."""
    a = _c(1, "INV-A", "Acme", "USD", 300_000)
    b = _c(2, "INV-B", "Acme", "USD", 450_000)
    r = match_inflow(750_000, "USD",
                     {"payer_name": "Acme", "reference_raw": "batch remittance"},
                     [a, b])
    assert r.status == "MATCHED"
    assert {x.invoice_id for x in r.allocations} == {1, 2}


def test_overpaid_records_credit():
    r = try_partial(500_000, "USD", "Lumen Stores", [_c(3, "INV-0043", "Lumen Stores", "USD", 450_000)])
    assert r.status == "OVERPAID"
    assert r.credit_minor == 50_000


# ---- tier 4: LLM with code re-validation --------------------------------
class StubLLM:
    def __init__(self, response):
        self.response = response

    def propose(self, inflow, candidates):
        return self.response


def _needs_llm_candidates():
    """Two open invoices, unknown payer: the deterministic tiers cannot reach
    0.90 (amount-only match is capped at 0.88 without a recognisable payer),
    so the orchestrator MUST fall through to tier 4. That is what makes these
    tests actually exercise the LLM path and its code re-validation."""
    return [_c(1, "INV-Z1", "Zephyr Trading", "USD", 700_000),
            _c(2, "INV-Z2", "Globex GmbH", "USD", 500_000)]


def test_llm_proposal_is_revalidated_in_code():
    r = match_inflow(
        700_000, "USD",
        {"payer_name": "", "reference_raw": "wire ref 88123"},
        _needs_llm_candidates(),
        llm=StubLLM({"allocations": {"1": 7_000}, "confidence": 0.96,
                     "rationale": "single open invoice matches the reference"}),
    )
    assert r.method == "LLM"
    assert r.status == "MATCHED"
    assert r.allocations[0].invoice_id == 1


def test_llm_overallocation_is_caught_by_code():
    """Model claims 900,000 against a 700,000 open balance -> its numbers are
    discarded. The deterministic reading (0.88, amount-only, no payer) is kept
    and recorded as hearing the model out — a model that over-allocates does not
    also get to erase a plausible reading by shrugging."""
    # MAJOR units, as the real OpenAI-compatible client returns them.
    r = match_inflow(
        700_000, "USD", {"payer_name": "", "reference_raw": "wire ref 88123"},
        _needs_llm_candidates(),
        llm=StubLLM({"allocations": {"1": 9_000}, "confidence": 0.99,
                     "rationale": "looks right to me"}),
    )
    assert r.method == "RULE", "the model's numbers were NOT used"
    assert r.confidence < 0.90, "and it still needs a human"
    assert r.evidence.get("model_consulted") is True
    assert r.evidence["model_attempt"]["confidence"] == 0.0, \
        "the model's rejected attempt is recorded"


def test_llm_allocations_must_sum_to_inflow():
    """F3: allocations that sum to MORE than the inflow are rejected outright —
    approving them would apply money that never arrived. The orchestrator keeps
    the best DETERMINISTIC reading (below the auto band) and records the model
    attempt as escalated; the model's numbers are never applied."""
    r = match_inflow(
        700_000, "USD", {"payer_name": "", "reference_raw": "batch"},
        _needs_llm_candidates(),
        llm=StubLLM({"allocations": {"1": 7_000, "2": 5_000},
                     "confidence": 0.99, "rationale": "both"}),
    )
    assert r.method != "LLM", "the over-sum model numbers were not used"
    assert r.confidence < 0.90, "nothing over-sum may auto-apply"
    attempt = r.evidence["model_attempt"]
    assert attempt["status"] == "AMBIGUOUS"
    assert attempt["confidence"] == 0.0
    assert "over" in attempt["reason"].lower() or \
        "never arrived" in attempt["reason"].lower()


def test_try_llm_rejects_over_sum_proposal_itself():
    """F3: at the tier itself, an over-sum proposal is AMBIGUOUS with no
    allocations — there is no OVERPAID-over-sum shape to approve any more."""
    r = try_llm(
        700_000, "USD", {"payer_name": "", "reference_raw": "batch"},
        _needs_llm_candidates(),
        StubLLM({"allocations": {"1": 7_000, "2": 5_000},
                 "confidence": 0.99, "rationale": "both"}),
    )
    assert r.status == "AMBIGUOUS"
    assert r.confidence == 0.0
    assert not r.allocations
    assert r.evidence.get("rule") == "model allocations exceed the inflow"


def test_llm_currency_crossing_is_rejected():
    r = match_inflow(
        700_000, "USD", {"payer_name": "", "reference_raw": ""},
        [_c(1, "INV-Z1", "X", "GBP", 700_000),
         _c(2, "INV-Z2", "X", "GBP", 500_000)],
        llm=StubLLM({"allocations": {"1": 7_000}, "confidence": 0.99,
                     "rationale": "same amount"}),
    )
    assert r.status == "AMBIGUOUS"


def test_llm_unknown_invoice_is_rejected():
    """An invoice the model invented must not appear in the result."""
    r = match_inflow(
        700_000, "USD", {"payer_name": "", "reference_raw": "wire"},
        _needs_llm_candidates(),
        llm=StubLLM({"allocations": {"999": 700_000}, "confidence": 0.99,
                     "rationale": "?"}),
    )
    assert all(a.invoice_id in (1, 2) for a in r.allocations), \
        "invoice 999 does not exist and must not be allocated"
    assert r.method != "LLM"


def test_a_dead_llm_neither_crashes_nor_invents_anything():
    """A gateway that 503s must not become a wrong decision — and must not
    destroy the deterministic reading either."""
    class Boom:
        def propose(self, inflow, candidates):
            raise RuntimeError("gateway 503")

    r = match_inflow(700_000, "USD", {"payer_name": "", "reference_raw": "wire"},
                     _needs_llm_candidates(), llm=Boom())
    assert r.method == "RULE", "decided without the model"
    assert r.confidence < 0.90
    assert r.evidence.get("model_consulted") is True
    assert "503" in str(r.evidence["model_attempt"].get("error")), \
        "and the outage is on the record"


# ---- orchestrator --------------------------------------------------------
def test_beat2_end_to_end_typo_deposit():
    """The typo is READ, and it is NOT what decided the match.

    With a recognised payer and an exact amount, the RULE tier identifies the
    invoice on its own evidence (payer identity + open balance) and scores 0.92.
    That is honest: 0.92 is what the payer and the amount justify, NOT the 1.00
    the folded reference used to claim.
    """
    r = match_inflow(1_200_000, "USD",
                     {"payer_name": "Brightline Retail",
                      "reference_raw": "inv 0O41 brightline"},
                     [BRIGHTLINE, LUMEN])
    assert r.method == "RULE", "never EXACT — the number was folded, not found"
    assert 0.90 <= r.confidence < 1.0, "confident, but not certain"
    assert r.status == "MATCHED"


def test_a_folded_reference_with_NO_other_evidence_is_PROPOSED():
    """When folding is the ONLY thing pointing at an invoice, it is a proposal.

    An unknown payer and a typo'd number is exactly the case that must reach a
    human rather than move an invoice on its own.
    """
    r = match_inflow(1_200_000, "USD",
                     {"payer_name": "Somebody Else",
                      "reference_raw": "chq 0O41"},
                     [BRIGHTLINE])
    assert r.status == "PROPOSED"
    assert r.method == "RULE"
    assert r.confidence < 0.90


def test_beat3_end_to_end_partial_deposit():
    r = match_inflow(480_000, "GBP",
                     {"payer_name": "Harrow & Co", "reference_raw": "Harrow pt1"},
                     [HARROW])
    assert r.status == "PARTIAL"
    assert r.shortfall_minor == 320_000


def test_beat6_ambiguous_escalates():
    """USD 4,400 from "L. Stores" must NOT auto-match INV-0043 (USD 4,500)."""
    r = match_inflow(440_000, "USD",
                     {"payer_name": "L. Stores", "reference_raw": ""},
                     [BRIGHTLINE, LUMEN])
    assert r.status == "AMBIGUOUS"
    assert r.needs_escalation