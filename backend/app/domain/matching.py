"""Matching — decide which open invoice an incoming payment belongs to.

The tiers run in order and STOP at the first match with confidence >= 0.9
(closebook.md §5.1):

1. EXACT   — reference contains the invoice number (case/space/O-0/I-1/l-1
             normalised) AND amount AND currency all agree.
2. RULE    — same payer (fuzzy token-set >= 0.90) and the amount equals one
             open balance, or there is exactly one open invoice for that
             payer in that currency.
3. PARTIAL — subset-sum over the payer's open invoices within bank-fee
             tolerance (<= 1% or <= 15 units), possibly multi-invoice.
4. LLM     — hand the redacted inflow + candidates to Claude (or the free
             OpenAI-compatible fallback). **Code re-validates the result**:
             allocations must sum to the inflow, invoices must exist and be
             open, currency must match. Anything invalid -> AMBIGUOUS.
5. below 0.75 -> ESCALATE with the evidence shown side by side.

The model never decides money here. It only proposes a reading of
unstructured text; every number it returns is re-checked against the DB.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Iterable, Optional, Protocol

from ..money import minor_for, to_major_str, to_minor
from .policy import (
    MATCH_CONFIDENCE_AUTO, MATCH_CONFIDENCE_ESCALATE, MATERIAL_SHORT_THRESHOLD,
    within_bank_fee_tolerance,
)

# The ceiling for ANY result sourced from the model.
#
# MATCH_CONFIDENCE_AUTO is 0.90 and the comparison is `>=`, so a model claiming
# 0.95 (or a PARTIAL claiming 0.9) would land in the AUTO band and apply money
# with no human — the model having graded its own homework. This must stay
# STRICTLY BELOW the auto threshold, and it is asserted against the threshold in
# `tests/test_matching.py` so a future policy change cannot quietly reopen the
# hole.
_LLM_MAX_CONFIDENCE = 0.89

# ---- text normalisation --------------------------------------------------
# Banks and humans corrupt identifiers: O/0, I/1/l, dashes/spaces/underscores.
_LOOKALIKE = str.maketrans({"O": "0", "o": "0", "I": "1", "i": "1",
                            "l": "1", "|": "1", "S": "5", "s": "5",
                            "B": "8", "Z": "2"})
# Letters whose digit-lookalike mapping would corrupt real words. Without this
# guard normalise("INV") became "1NV" and broke every invoice-number match.
_WORD_SAFE = set("ABCDEFGHJKMNPQRTUVWXY")  # letters that are never 0/1/5/8/2 lookalikes
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise(text: str) -> str:
    """Word-safe normalisation: lowercase, alphanumeric only.

    Deliberately does NOT fold digit lookalikes — folding "INV" to "1NV"
    corrupts real words. Lookalike folding happens in `fold_digits`, and is
    only ever applied to number-like material.
    """
    if not text:
        return ""
    return _NON_ALNUM.sub("", text.lower())


def fold_digits(text: str) -> str:
    """Fold digit lookalikes (O->0, l/I->1, S->5, B->8, Z->2) so a typo'd
    reference like "0O41" reads as "0041".

    Folding is applied PER TOKEN and only to tokens that already contain a
    real digit. That guard is what stops "INV" becoming "1NV" — a pure-alpha
    token is a word and is left alone; a token like "0O41" or "O04l" is
    number-like and gets folded.
    """
    if not text:
        return ""
    out = []
    for tok in re.split(r"(\W+)", text):
        if tok and not tok.isspace() and any(ch.isdigit() for ch in tok):
            out.append(tok.lower().translate(_LOOKALIKE))
        else:
            out.append(tok.lower())
    return "".join(out)


def numbers_in(text: str) -> list[str]:
    """Digit runs inside a string, after folding lookalikes."""
    return re.findall(r"\d+", fold_digits(text or ""))


def number_blob(text: str) -> str:
    """All digit runs of a string joined, for containment checks."""
    return "".join(numbers_in(text))


def normalise_invoice(number: str) -> str:
    return normalise(number)


def token_set_ratio(a: str, b: str) -> float:
    """Jaccard-ish similarity over word tokens (0..1).

    Deliberately simple and dependency-free; the spec asks for a token-set
    ratio >= 90 for the payer-name rule.
    """
    ta = {w for w in re.split(r"\W+", (a or "").lower()) if w}
    tb = {w for w in re.split(r"\W+", (b or "").lower()) if w}
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


# ---- inputs / outputs ---------------------------------------------------
@dataclass
class Candidate:
    """An open receivable the inflow could belong to."""
    invoice_id: int
    number: str
    customer_name: str
    currency: str
    amount_open_minor: int      # amount - already paid
    amount_original_minor: int


@dataclass
class Allocation:
    invoice_id: int
    amount_minor: int


@dataclass
class MatchResult:
    status: str                      # MATCHED | PARTIAL | OVERPAID | AMBIGUOUS
    allocations: list[Allocation] = field(default_factory=list)
    confidence: float = 0.0
    method: str = "RULE"            # EXACT | RULE | LLM
    evidence: dict = field(default_factory=dict)
    shortfall_minor: int = 0        # for PARTIAL
    credit_minor: int = 0           # for OVERPAID
    reason: str = ""

    @property
    def needs_escalation(self) -> bool:
        return self.status == "AMBIGUOUS" or self.confidence < 0.75


class LLMMatcher(Protocol):
    """The only thing the model is allowed to do here."""
    def propose(self, inflow: dict, candidates: list[dict]) -> dict:
        """Return {'invoice_ids': [...], 'allocations': {...},
        'confidence': float, 'rationale': str}."""


# ---- tier 1: EXACT ------------------------------------------------------
def try_exact(inflow_amount_minor: int, inflow_currency: str,
              reference: str, candidates: Iterable[Candidate]) -> Optional[MatchResult]:
    """Invoice number present in the reference AND amount AND currency agree.

    Number comparison folds digit lookalikes so "inv 0O41" matches INV-0041,
    but requires the digits to align to the invoice's own digits (a trailing
    invoice number, not an arbitrary number from the reference).
    """
    ref_norm = normalise(reference)
    ref_nums = number_blob(reference)
    for c in candidates:
        if c.currency != inflow_currency:
            continue
        if c.amount_open_minor != inflow_amount_minor:
            continue
        inv_norm = normalise_invoice(c.number)
        if not inv_norm:
            continue
        # (a) whole normalised reference contains the whole normalised number
        hit = inv_norm in ref_norm
        if not hit and ref_nums:
            # (b) or the invoice's digits appear as a contiguous run in the
            # reference's digits, so a typo'd reference like "0O41" still
            # matches INV-0041. Require at least 3 digits — a 1-2 digit
            # invoice number would otherwise match any reference containing
            # that digit.
            inv_digits = "".join(re.findall(r"\d+", fold_digits(c.number)))
            if len(inv_digits) >= 3 and inv_digits in ref_nums:
                hit = True
        if hit:
            return MatchResult(
                status="MATCHED",
                allocations=[Allocation(c.invoice_id, inflow_amount_minor)],
                confidence=1.0, method="EXACT",
                evidence={"rule": "reference contains invoice number and "
                                  "amount/currency agree",
                          "invoice": c.number,
                          "normalised_reference": ref_norm},
                reason=f"reference identifies {c.number} exactly",
            )
    return None


# ---- tier 2: RULE --------------------------------------------------------
def try_rule(inflow_amount_minor: int, inflow_currency: str,
             payer_name: str, candidates: Iterable[Candidate]) -> Optional[MatchResult]:
    same_ccy = [c for c in candidates if c.currency == inflow_currency]
    if not same_ccy:
        return None
    # exact open-balance for this payer
    for c in same_ccy:
        if c.amount_open_minor == inflow_amount_minor:
            sim = token_set_ratio(payer_name, c.customer_name)
            # A fuzzy-but-strong payer name plus an exact amount is still only
            # good enough to auto-apply when the name is a near-identical match.
            conf = 0.92 if sim >= 0.90 else 0.88
            return MatchResult(
                status="MATCHED",
                allocations=[Allocation(c.invoice_id, inflow_amount_minor)],
                confidence=conf, method="RULE",
                evidence={"rule": "payer amount equals one open balance",
                          "invoice": c.number, "payer": payer_name,
                          "name_similarity": round(sim, 3)},
                reason=f"amount matches the open balance on {c.number}",
            )
    # Exactly one open invoice for this payer in this currency — but ONLY when
    # the payer name genuinely identifies that customer AND the amount is at
    # least consistent with that invoice. Without the first gate a fuzzy name
    # claims a payment that belongs elsewhere; without the second, an
    # identified payer would swallow a genuine part-payment (GB 4,800 against
    # an 8,000 bill), which tier 3 classifies far better than this rule can.
    if len(same_ccy) == 1:
        c = same_ccy[0]
        sim = token_set_ratio(payer_name, c.customer_name) if payer_name else 0.0
        amount_plausible = (
            within_bank_fee_tolerance(c.amount_open_minor, inflow_amount_minor)
            or inflow_amount_minor >= c.amount_open_minor
        )
        if sim >= 0.9 and amount_plausible:
            return MatchResult(
                status="MATCHED",
                allocations=[Allocation(c.invoice_id, inflow_amount_minor)],
                confidence=0.85, method="RULE",
                evidence={"rule": "single open invoice for an identified payer",
                          "invoice": c.number, "name_similarity": round(sim, 3)},
                reason=f"only one open {inflow_currency} invoice for "
                       f"{c.customer_name} ({c.number})",
            )
    return None


def try_exact_multi(inflow_amount_minor: int, inflow_currency: str,
                    candidates: list[Candidate]) -> Optional[MatchResult]:
    """Subset-sum: does the inflow exactly cover two or more open invoices?

    Runs before the single-invoice partial rule so a payment that covers two
    bills is not mistaken for a partial against the larger of the two.
    Bounded to 12 invoices (4095 subsets).
    """
    pool = [c for c in candidates if c.currency == inflow_currency][:12]
    if len(pool) < 2:
        return None
    for mask in range(1, 1 << len(pool)):
        chosen = [c for i, c in enumerate(pool) if mask & (1 << i)]
        if len(chosen) < 2:
            continue
        if sum(c.amount_open_minor for c in chosen) == inflow_amount_minor:
            return MatchResult(
                status="MATCHED",
                allocations=[Allocation(c.invoice_id, c.amount_open_minor) for c in chosen],
                confidence=0.91, method="RULE",
                evidence={"rule": "subset-sum of open invoices equals the inflow",
                          "invoices": [c.number for c in chosen]},
                reason="inflow exactly covers " + ", ".join(c.number for c in chosen),
            )
    return None


# ---- tier 3: PARTIAL / MULTI-INVOICE subset-sum -------------------------
def try_partial(inflow_amount_minor: int, inflow_currency: str,
                payer_name: str, candidates: Iterable[Candidate]) -> Optional[MatchResult]:
    """Subset-sum over the payer's open invoices, allowing a bank-fee shortfall."""
    pool = [c for c in candidates if c.currency == inflow_currency]
    if not pool:
        return None
    pool = pool[:12]

    # --- payer identity gate ---------------------------------------------
    # Amount alone must never auto-apply a payment to an invoice. If we know
    # who sent the money and it does not plausibly match the customer on the
    # open invoices, an amount coincidence is not evidence.
    best_sim = 0.0
    for c in pool:
        if payer_name:
            best_sim = max(best_sim, token_set_ratio(payer_name, c.customer_name))
    payer_plausible = bool(payer_name) and best_sim >= 0.5
    payer_unknown = not payer_name

    # --- best SINGLE-invoice fit, not the first one -----------------------
    # Scoring by relative shortfall stops a small deposit from latching onto a
    # huge invoice just because that invoice happened to be listed first:
    # GBP 4,800 against [12,000, 8,000] fits the 8,000 bill far better.
    best: Optional[tuple[float, Candidate, int]] = None  # (score, invoice, diff)
    for c in pool:
        diff = c.amount_open_minor - inflow_amount_minor
        if diff == 0:
            continue
        score = abs(diff) / max(c.amount_open_minor, 1)
        if best is None or score < best[0]:
            best = (score, c, diff)

    if best is not None:
        _score, c, diff = best
        identity_ok = payer_plausible
        if diff > 0 and within_bank_fee_tolerance(c.amount_open_minor, inflow_amount_minor):
            # Short by a plausible bank fee. Still requires a recognisable
            # payer: an unknown sender with a near-exact amount is a
            # coincidence, not a match.
            conf = 0.93 if identity_ok else 0.70
            return MatchResult(
                status="PARTIAL",
                allocations=[Allocation(c.invoice_id, inflow_amount_minor)],
                confidence=conf, method="RULE",
                shortfall_minor=diff,
                evidence={"rule": "single invoice, shortfall within bank-fee tolerance",
                          "invoice": c.number, "shortfall_minor": diff,
                          "payer_similarity": round(best_sim, 3),
                          "payer_known": not payer_unknown},
                reason=f"partial payment against {c.number}; "
                       f"{diff/100:.2f} short, within bank-fee tolerance",
            )
        if diff > 0:
            short_frac = Decimal(diff) / Decimal(c.amount_open_minor)
            if short_frac > MATERIAL_SHORT_THRESHOLD:
                # A LARGE shortfall is clearly a deliberate part-payment: apply
                # what arrived, keep the invoice PARTIAL, and ask a human
                # whether to chase the rest.
                return MatchResult(
                    status="PARTIAL",
                    allocations=[Allocation(c.invoice_id, inflow_amount_minor)],
                    confidence=0.85 if identity_ok else 0.70, method="RULE",
                    shortfall_minor=diff,
                    evidence={"rule": "material part-payment against one open invoice",
                              "invoice": c.number, "shortfall_minor": diff,
                              "short_fraction": round(float(short_frac), 4),
                              "payer_similarity": round(best_sim, 3)},
                    reason=f"part-payment against {c.number}; "
                           f"{diff/100:.2f} ({float(short_frac)*100:.0f}%) still "
                           f"outstanding, needs approval to chase",
                )
            # Small shortfall but too big to be a bank fee. This is genuinely
            # ambiguous: an unrecognised fee, a wrong invoice, or a typo'd
            # amount all look the same here. Do not guess — escalate.
            return MatchResult(
                status="AMBIGUOUS",
                allocations=[Allocation(c.invoice_id, inflow_amount_minor)],
                confidence=0.70, method="RULE",
                shortfall_minor=diff,
                evidence={"rule": "shortfall beyond bank-fee tolerance but not a "
                                  "material part-payment",
                          "invoice": c.number, "shortfall_minor": diff,
                          "short_fraction": round(float(short_frac), 4),
                          "payer_similarity": round(best_sim, 3)},
                reason=f"amount is {diff/100:.2f} short of {c.number} "
                       f"({float(short_frac)*100:.1f}%) — too large for a bank fee, "
                       f"too small to be a deliberate part-payment; needs a human",
            )
        # diff < 0 -> the inflow EXCEEDS the open balance. Capped BELOW the
        # 0.90 auto threshold on purpose: an overpayment creates a credit or a
        # refund obligation, and a refund always needs human approval (spec
        # §5.2). It must never auto-apply.
        over = -diff
        return MatchResult(
            status="OVERPAID",
            allocations=[Allocation(c.invoice_id, c.amount_open_minor)],
            confidence=0.85 if identity_ok else 0.70,
            method="RULE",
            credit_minor=over,
            evidence={"rule": "inflow exceeds the open balance",
                      "invoice": c.number, "credit_minor": over,
                      "payer_similarity": round(best_sim, 3)},
            reason=f"overpayment of {over/100:.2f} against {c.number}; "
                   f"credit recorded (any refund needs approval)",
        )
    return None


# ---- tier 4: LLM-assisted (code re-validates) --------------------------
def try_llm(inflow_amount_minor: int, inflow_currency: str,
            inflow: dict, candidates: list[Candidate],
            llm: LLMMatcher) -> MatchResult:
    """Ask the model for a reading, then re-validate every number in code."""
    payload = {
        # MAJOR units, and the exact key names OpenAICompatibleMatcher reads:
        # `amount`/`payer`/`reference` and, per candidate, `amount_open`/
        # `customer`. try_llm previously sent `amount_minor`/`payer_name`/
        # `amount_open_minor`, so every lookup raised KeyError, propose()
        # swallowed it and returned None, and the beat recorded "the model was
        # offline" while a key was configured and working. Minor units invite a
        # 100x error that re-validation would then correctly reject anyway.
        "amount": to_major_str(inflow_amount_minor, inflow_currency),
        "currency": inflow_currency,
        "payer": inflow.get("payer_name"),
        "reference": inflow.get("reference_raw"),
    }
    cands = [
        {"invoice_id": c.invoice_id, "number": c.number,
         "customer": c.customer_name, "currency": c.currency,
         "amount_open": to_major_str(c.amount_open_minor, c.currency)}
        for c in candidates
    ]
    try:
        raw = llm.propose(payload, cands)
    except Exception as exc:  # a dead gateway must not become a wrong decision
        return MatchResult(
            status="AMBIGUOUS", confidence=0.0, method="LLM",
            evidence={"rule": "LLM unavailable", "error": str(exc)[:200]},
            reason="model unavailable; escalated rather than guessed",
        )

    # `propose()` returns None (rather than raising) when no key is configured or
    # the gateway fails — that is the offline path, and it is a normal outcome,
    # not an error. Guarding it here is what keeps the demo running with a key
    # present but a provider that is down; without this it crashed mid-beat.
    if raw is None:
        return MatchResult(
            status="AMBIGUOUS", confidence=0.0, method="LLM",
            evidence={"rule": "LLM returned no proposal"},
            reason="model proposed nothing (offline or provider unavailable); "
                   "escalated rather than guessed",
        )

    # The model returns allocations as a LIST of
    # {"invoice_number", "amount"} (that is the shape `_SYSTEM` asks for and
    # what the OpenAI-compatible client parses). try_llm historically expected
    # a DICT keyed by invoice_id, so a perfectly good proposal failed
    # `isinstance(allocations, dict)` and was discarded as "nothing usable" —
    # the agent saw a proposal while the matcher rejected the identical one.
    # Accept both; normalise to the list form used below.
    raw_allocs = raw.get("allocations")
    if isinstance(raw_allocs, dict):
        raw_allocs = [{"invoice_id": k, "amount": v}
                      for k, v in raw_allocs.items()]
    conf = float(raw.get("confidence") or 0.0)
    rationale = raw.get("rationale") or ""

    # --- CODE RE-VALIDATION (the model does not get the last word) ---
    if not isinstance(raw_allocs, list) or not raw_allocs:
        return MatchResult(status="AMBIGUOUS", confidence=0.0, method="LLM",
                           evidence={"rule": "LLM returned no allocations",
                                     "rationale": rationale},
                           reason="model proposed nothing usable; escalated")

    by_number = {c.number: c for c in candidates}
    allocations: dict[int, Any] = {}
    for a in raw_allocs:
        if not isinstance(a, dict):
            continue
        cid = a.get("invoice_id")
        if cid is None:
            cand = by_number.get(a.get("invoice_number"))
            cid = cand.invoice_id if cand else None
        if cid is None:
            continue
        allocations[cid] = a.get("amount")

    if not allocations:
        return MatchResult(status="AMBIGUOUS", confidence=0.0, method="LLM",
                           evidence={"rule": "model named no real invoice",
                                     "rationale": rationale},
                           reason="model referenced an unknown invoice; escalated")

    by_id = {c.invoice_id: c for c in candidates}
    cleaned: list[Allocation] = []
    total = 0
    for k, v in allocations.items():
        try:
            inv_id = int(k)
            # The model answers in MAJOR units (the prompt and `cands` use
            # `to_major_str`). Comparing its 4400 against a 440000 minor inflow
            # made every correct proposal read as a 100x under-allocation, and
            # code rejected it — while re-validation was right to check and
            # wrong about the units.
            amt = to_minor(Decimal(str(v)), inflow_currency)
        except (TypeError, ValueError, ArithmeticError):
            return MatchResult(status="AMBIGUOUS", confidence=0.0, method="LLM",
                               evidence={"rule": "non-integer allocation",
                                         "raw": str(allocations)[:200]},
                               reason="model returned malformed amounts; escalated")
        c = by_id.get(inv_id)
        if c is None:
            return MatchResult(status="AMBIGUOUS", confidence=0.0, method="LLM",
                               evidence={"rule": "unknown invoice in proposal",
                                         "invoice_id": inv_id},
                               reason="model referenced an unknown invoice; escalated")
        if c.currency != inflow_currency:
            return MatchResult(status="AMBIGUOUS", confidence=0.0, method="LLM",
                               evidence={"rule": "currency mismatch",
                                         "invoice": c.number,
                                         "invoice_currency": c.currency,
                                         "inflow_currency": inflow_currency},
                               reason="model crossed currencies; escalated")
        if amt <= 0 or amt > c.amount_open_minor:
            return MatchResult(status="AMBIGUOUS", confidence=0.0, method="LLM",
                               evidence={"rule": "allocation exceeds open balance",
                                         "invoice": c.number, "amount": amt,
                                         "open": c.amount_open_minor},
                               reason="model over-allocated an invoice; escalated")
        cleaned.append(Allocation(inv_id, amt))
        total += amt

    # A MODEL NEVER GRADES ITS OWN WORK.

    # `conf` is the number the model wrote about itself. It is not evidence: a
    # model that says 0.99 about a wrong reading is still wrong, and letting
    # its self-assessment reach MATCH_CONFIDENCE_AUTO (0.90, inclusive) means a
    # fabricated 0.95 pays money with nobody looking.

    # So every LLM-sourced result is pinned into the APPROVAL band: high enough
    # to be applied and recorded, never high enough to move on its own. The
    # model's number is kept as `model_confidence` — the CLAIM — while
    # `confidence` is the band's decision, which code owns.
    #
    # This is the whole thesis in one line: the model proposes, code decides.
    llm_band_conf = min(conf, _LLM_MAX_CONFIDENCE)   # 0.89 < 0.90 by construction

    if total != inflow_amount_minor:
        # Over-allocation becomes a recorded credit; under-allocation is a
        # partial. Neither is silently accepted as exact.
        if total > inflow_amount_minor:
            status = "OVERPAID"
            credit = total - inflow_amount_minor
        else:
            status = "PARTIAL"
            credit = 0
        return MatchResult(
            status=status, allocations=cleaned, confidence=llm_band_conf,
            method="LLM",
            shortfall_minor=(inflow_amount_minor - total) if status == "PARTIAL" else 0,
            credit_minor=credit,
            evidence={"rule": "allocations do not sum to the inflow",
                      "allocated": total, "inflow": inflow_amount_minor,
                      "model_claimed_confidence": conf,
                      "rationale": rationale},
            reason=f"model allocations {'over' if credit else 'under'}-allocate the inflow",
        )

    return MatchResult(
        status="MATCHED", allocations=cleaned, confidence=llm_band_conf,
        method="LLM",
        evidence={"rule": "LLM proposal re-validated in code",
                  "model_claimed_confidence": conf,
                  "rationale": rationale},
        reason=rationale or "model reading confirmed by code re-validation",
    )


# ---- orchestrator -------------------------------------------------------
def match_inflow(inflow_amount_minor: int, inflow_currency: str,
                 inflow_meta: dict, candidates: list[Candidate],
                 llm: Optional[LLMMatcher] = None) -> MatchResult:
    """Run the tiers in order and return the first confident match.

    Tiers stop at confidence >= 0.9. If nothing reaches that bar and an LLM
    is available, tier 4 runs; anything still short of 0.75 escalates.
    """
    reference = inflow_meta.get("reference_raw") or ""
    payer = inflow_meta.get("payer_name") or ""

    # Deterministic tiers first. Note we do NOT stop at the first tier that
    # returns anything — only at the first result confident enough to act on
    # (>= MATCH_CONFIDENCE_AUTO). A weaker deterministic result still leaves
    # room for the model to read the reference better.
    best: Optional[MatchResult] = None
    for tier in (lambda: try_exact(inflow_amount_minor, inflow_currency, reference, candidates),
                 lambda: try_rule(inflow_amount_minor, inflow_currency, payer, candidates),
                 lambda: try_exact_multi(inflow_amount_minor, inflow_currency, candidates),
                 lambda: try_partial(inflow_amount_minor, inflow_currency, payer, candidates)):
        res = tier()
        if res is None:
            continue
        if res.confidence >= MATCH_CONFIDENCE_AUTO:
            return res
        if best is None or res.confidence > best.confidence:
            best = res

    # Tier 4: the model reads the unstructured reference. It is only consulted
    # when the deterministic tiers did not reach the auto threshold — and its
    # answer is ALWAYS re-validated in code (try_llm cannot return a clean
    # auto-match on an arithmetically impossible proposal).
    if llm is not None and (best is None or best.confidence < 0.9):
        return try_llm(inflow_amount_minor, inflow_currency, inflow_meta, candidates, llm)

    if best is not None:
        return best

    return MatchResult(
        status="AMBIGUOUS", confidence=0.0, method="RULE",
        evidence={"rule": "no tier reached the auto threshold",
                  "reference": reference, "payer": payer,
                  "candidate_count": len(candidates)},
        reason="no confident match; a human decides",
    )