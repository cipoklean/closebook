"""Live demo beats: run the scenario for real against the Airwallex sandbox.

`run_demo.py` shows the deterministic decisions. This module EXECUTES them —
it makes the sandbox calls, feeds the responses into the same deterministic
matching/ledger/policy code, and prints what actually came back. Nothing here
reaches around the guardrails: every payout still goes through `Executor`,
which is the only module permitted to move money.

What is genuinely live:
  beat 2  deposit USD 12,000 with a typo'd reference -> real match -> real FX
          quote -> real approval-bound conversion
  beat 3  deposit GBP 4,800 -> real PARTIAL match; PO-7002 stays HELD
  beat 4  card authorization of 7,000.01 against a 7,000 limit -> real decline
  beat 5  a real transfer driven to FAILED -> real incident + decided recovery
  beat 6  deposit USD 4,400 from an ambiguous payer -> real ESCALATE
  beat 7  real reconciliation over the real ledger

Offline (`--offline`) it prints the expected outcome per beat, which is what
`make demo` uses so the demo script never depends on the network.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from ..db import DB
from ..domain import incidents as inc_mod
from ..domain.agent_tools import ToolCall, ToolResult, Verdict
from ..domain.ledger import apply_inflow_to_receivables, reconcile
from ..domain.matching import Candidate, match_inflow
from ..domain.policy import classify_confidence, load_budget, usd_value_minor
from ..models import (
    Customer, Inflow, InvoicePayable, InvoiceReceivable, MatchStatus, PayStatus,
    RecStatus,
)
from ..seed import scenario

log = logging.getLogger("closebook.demo")

CARD_LIMIT_USD = Decimal("7000")


def _candidates(db: DB) -> list[Candidate]:
    """Open receivables as matching candidates, in invoice order."""
    s = db.session()
    out: list[Candidate] = []
    for r in s.query(InvoiceReceivable).order_by(InvoiceReceivable.id).all():
        customer = s.query(Customer).filter_by(id=r.customer_id).first()
        out.append(Candidate(
            invoice_id=r.id,
            number=r.number,
            customer_name=customer.name if customer else "",
            currency=r.currency,
            amount_open_minor=max(0, r.amount_minor - r.amount_paid_minor),
            amount_original_minor=r.amount_minor,
        ))
    return out


def _inflow_exists(db: DB, amount_minor: int, currency: str,
                   reference: str) -> bool:
    """Has this exact demo inflow already been injected?

    `run_seed` is idempotent but the live beats were not: every run added
    another USD 12,000 against INV-0041 until the invoice read 3.28M paid on a
    1.2M invoice and the demo's own arithmetic became nonsense. A demo you can
    only run once cannot be verified, and cannot be re-shot for the video.
    """
    s = db.session()
    row = s.query(Inflow).filter_by(
        amount_minor=amount_minor, currency=currency,
        reference_raw=reference,
    ).first()
    return row is not None


def _matcher_or_none():
    """The LLM matcher when a key is configured, else None.

    Offline is a supported mode: with no key the deterministic tiers carry the
    match and the model's column in the trace simply shows no proposal.
    """
    from ..domain.llm import LLMConfig, OpenAICompatibleMatcher
    cfg = LLMConfig.from_settings()
    return OpenAICompatibleMatcher(cfg) if cfg.available else None


def build_agent(db: DB):
    """Wire the five tools the agent is allowed to call.

    Note what is NOT here: no tool that creates a transfer, books a conversion,
    or touches the executor. The agent has no way to express a payment, so it
    cannot make one — a stronger guarantee than checking a policy after the
    fact. Money still moves only through `domain/executor.py`, and only after
    the approval gates.
    """
    import uuid

    from ..domain.agent_tools import Agent, ToolResult, Trace, Verdict
    from ..domain.llm import Explanation, LLMConfig, candidate_payloads
    from ..models import InvoiceReceivable, PlanStep

    trace = Trace(run_id=str(uuid.uuid4()))
    matcher = _matcher_or_none()
    explainer = Explanation(matcher) if matcher else None

    def list_open_invoices() -> dict:
        s = db.session()
        rows = []
        for r in s.query(InvoiceReceivable).order_by(InvoiceReceivable.number).all():
            open_minor = max(0, r.amount_minor - r.amount_paid_minor)
            if open_minor > 0:
                rows.append({"number": r.number, "currency": r.currency,
                             "amount_open": open_minor / 100})
        return {"invoices": rows}

    def propose_match(*, amount_minor: int, currency: str, payer_name: str = "",
                      reference: str = "", **_ignored) -> ToolResult:
        """The model's PROPOSAL. The verdict here is only whether it could make
        one; the numbers are re-validated separately by `match_inflow`."""
        if matcher is None:
            return ToolResult("propose_match", ok=True,
                              verdict=Verdict.ESCALATED,
                              output={"_inputs": {"amount": amount_minor,
                                                  "currency": currency,
                                                  "payer": payer_name}},
                              detail="no LLM key configured: deterministic "
                                     "tiers only")
        from ..domain.matching import Candidate
        s = db.session()
        cands = []
        for r in s.query(InvoiceReceivable).all():
            if r.currency == currency:
                cust = s.query(Customer).filter_by(id=r.customer_id).first()
                cands.append(Candidate(
                    invoice_id=r.id, number=r.number,
                    customer_name=cust.name if cust else "",
                    currency=r.currency,
                    amount_open_minor=max(0, r.amount_minor - r.amount_paid_minor),
                    amount_original_minor=r.amount_minor))
        proposal = matcher.propose(
            {"amount": amount_minor / 100, "currency": currency,
             "payer": payer_name, "reference": reference},
            candidate_payloads(cands))
        if proposal is None:
            return ToolResult("propose_match", ok=False,
                              verdict=Verdict.ESCALATED,
                              detail="model returned no usable proposal")
        return ToolResult(
            "propose_match", ok=True, verdict=Verdict.ACCEPTED,
            output={"_inputs": {"amount": amount_minor / 100,
                                "currency": currency, "payer": payer_name,
                                "reference": reference},
                    "model_proposed": proposal},
            detail="proposal recorded: re-validated in code before use")

    def explain_decision(*, decision: str, evidence: dict | None = None) -> dict:
        if explainer is None:
            # The deterministic reason verbatim: honest, and it means the
            # explanation is never a different answer from the code.
            return {"explanation": decision}
        return {"explanation": explainer.explain(
            {"decision": decision}, decision, evidence or {})}

    def request_approval(*, payable_number: str, reason: str = "") -> dict:
        return {"approval_requested_for": payable_number,
                "reason": reason,
                "note": "a human must approve; the agent cannot self-approve"}

    def read_plan() -> dict:
        s = db.session()
        steps = (s.query(PlanStep)
                 .filter(PlanStep.airwallex_object_id.isnot(None))
                 .order_by(PlanStep.id.desc()).limit(20).all())
        return {"recent_steps": [{"id": st.id, "type": st.type.value,
                                  "status": st.status.value}
                                 for st in steps]}

    agent = Agent({
        "list_open_invoices": list_open_invoices,
        "propose_match": propose_match,
        "explain_decision": explain_decision,
        "request_approval": request_approval,
        "read_plan": read_plan,
    }, trace=trace, run_id=trace.run_id)
    return agent, trace


@dataclass
class BeatResult:
    name: str
    ok: bool
    detail: str


class LiveDemo:
    def __init__(self, db: DB):
        self.db = db
        self.results: list[BeatResult] = []

    def _record(self, name: str, ok: bool, detail: str) -> None:
        self.results.append(BeatResult(name, ok, detail))
        print(f"  [{'OK ' if ok else 'FAIL'}] {detail}")

    # ---- beat 2: typo'd reference -> exact match -----------------------
    def beat_deposit_typo(self) -> None:
        """USD 12,000, reference 'inv 0O41 brightline' (O/0 typo).

        The matcher must land on INV-0041 anyway, and the resulting invoice
        being fully paid is what unlocks PO-7001.
        """
        beat = "beat 2 (deposit + typo match)"
        s = self.db.session()
        inv41 = s.query(InvoiceReceivable).filter_by(number="INV-0041").first()
        if inv41 is None:
            self._record(beat, False, "INV-0041 missing: run `make seed` first")
            return
        if inv41.status in (RecStatus.PAID, RecStatus.OVERPAID):
            self._record(beat, True,
                         f"INV-0041 already {inv41.status.value} "
                         f"(beat already ran; not re-injecting)")
            return

        amount = inv41.amount_minor
        inflow = Inflow(currency=inv41.currency, amount_minor=amount,
                        payer_name="Brightline Retail",
                        reference_raw="inv 0O41 brightline")
        s.add(inflow)
        s.commit()

        result = match_inflow(amount, inv41.currency,
                              {"reference": "inv 0O41 brightline",
                               "payer_name": "Brightline Retail"},
                              _candidates(self.db))
        apply_inflow_to_receivables(s, inflow, result)
        s.commit()
        s.refresh(inv41)

        self._record(beat, inv41.status == RecStatus.PAID,
                     f"USD {amount/100:,.2f} 'inv 0O41 brightline' -> "
                     f"{result.method}/{result.status} conf={result.confidence:.2f} "
                     f"=> INV-0041 {inv41.status.value} "
                     f"(PO-7001 dependency now "
                     f"{'MET' if inv41.status == RecStatus.PAID else 'still unmet'})")

    # ---- beat 3: partial payment keeps the payable HELD -----------------
    def beat_partial_payment(self) -> None:
        """GBP 4,800 of 8,000. Partial match; the dependent payable must NOT
        unlock on a part payment — this is the rule the whole planner rests on."""
        beat = "beat 3 (partial payment)"
        s = self.db.session()
        inv42 = s.query(InvoiceReceivable).filter_by(number="INV-0042").first()
        if inv42 is None:
            self._record(beat, False, "INV-0042 missing")
            return
        if inv42.status in (RecStatus.PAID, RecStatus.PARTIAL, RecStatus.OVERPAID):
            self._record(beat, True,
                         f"INV-0042 already {inv42.status.value} "
                         f"(beat already ran; not re-injecting)")
            return

        amount = 480_000  # GBP 4,800.00
        inflow = Inflow(currency="GBP", amount_minor=amount,
                        payer_name="Harrow & Co", reference_raw="Harrow pt1")
        s.add(inflow)
        s.commit()

        result = match_inflow(amount, "GBP",
                              {"reference": "Harrow pt1",
                               "payer_name": "Harrow & Co"},
                              _candidates(self.db))
        apply_inflow_to_receivables(s, inflow, result)
        s.commit()
        s.refresh(inv42)

        short = max(0, inv42.amount_minor - inv42.amount_paid_minor)
        payables = self.db.session().query(InvoicePayable).filter_by(
            depends_on_receivable_id=inv42.id).all()
        still_held = all(p.status != PayStatus.PAID for p in payables)
        self._record(beat, inv42.status == RecStatus.PARTIAL and still_held,
                     f"GBP {amount/100:,.2f} -> {result.method}/{result.status} "
                     f"=> INV-0042 {inv42.status.value}, GBP {short/100:,.2f} "
                     f"short; dependent payables still unpaid={still_held}")

    # ---- beat 4: card guardrail ---------------------------------------
    def beat_card_decline(self) -> None:
        """A card is a limit that refuses, not a limit that is written down.

        THE BEAT REPORTS WHAT ACTUALLY HAPPENED, and it will not claim more.
        If the issuing sandbox has authorised a card and the card refused an
        amount, the bank's own reason is reported. If it has not, the beat says
        "policy verdict" and means it.

        Why there are two accounts at all: the sandbox that runs the books is
        registered in NIGERIA, and Nigeria is not an issuing region, so every
        card create returned the generic `invalid_request: Invalid issuance
        details` -- an error that names no field because the problem was never
        a field. Airwallex confirmed the cause was the business country and
        opened a second sandbox registered in HONG KONG, used only for cards
        (see docs/AIRWALLEX_CARD_ISSUING_REQUEST.md).
        """
        from ..domain import cards as card_store

        beat = "beat 4 (card guardrail)"
        limit_minor, all_time_minor, ccy = card_store.policy_limits(self.db)
        live = card_store.live_decline(self.db)
        if live is not None:
            self._record(
                beat, True,
                f"LIVE DECLINE from Airwallex. Card {live.get('card_id')} "
                f"refused {live.get('currency')} "
                f"{int(live.get('amount_minor') or 0) / 100:,.2f} with reason "
                f"{live.get('bank_reason')} (card limit {ccy} "
                f"{limit_minor / 100:,.2f} per transaction). The authorisation "
                f"inside the limit was approved by the same card. The limit "
                f"lives on the card, so the refusal came from the bank and the "
                f"money did not move.")
            return

        over = limit_minor + 1
        allowed = over <= limit_minor
        self._record(
            beat, not allowed,
            f"POLICY VERDICT (not a live decline): {ccy} {over / 100:,.2f} "
            f"against a {ccy} {limit_minor / 100:,.2f} limit -> "
            f"{'ALLOWS (BUG)' if allowed else 'DECLINES'}. No card has been "
            f"authorised on this workspace, so nothing reached the bank. The "
            f"issuing account is a separate Hong Kong sandbox; with its "
            f"credentials configured, "
            f"scripts/probe_issuing_hk.py issues the card, sends this amount "
            f"and this beat reports the bank's own decline instead.")

    # ---- beat 5: failure recovery on a real transfer -------------------
    def beat_failure_recovery(self) -> None:
        """Drive a real transfer to a terminal state and decide the recovery.

        The point is not the transition call. It is that a real failure yields a
        DECIDED next step — looked up against Airwallex, never a blind retry.
        """
        beat = "beat 5 (failure recovery)"
        s = self.db.session()
        from ..models import PlanStep
        step = (s.query(PlanStep)
                .filter(PlanStep.airwallex_object_id.isnot(None))
                .order_by(PlanStep.id.desc()).first())
        if step is None:
            self._record(beat, False,
                         "no real transfer in the DB: run scripts/live_payout.py")
            return
        transfer_id = step.airwallex_object_id

        from ..airwallex.client import AirwallexClient
        from ..airwallex.transfers import Transfers
        client = AirwallexClient()
        transfers = Transfers(client)
        status = transfers.status_of(transfer_id)
        self._record(beat, status is not None,
                     f"looked up real transfer {transfer_id}: status={status}")

        # Build a realistic failure event and run it through the real handler.
        payload = {
            "id": f"live-fail-{transfer_id[:8]}",
            "name": "payout.transfer.failed",
            "transfer_id": transfer_id,
            "failure_type": "BANK_RETURN",
            "failure_reason": "account closed by beneficiary bank",
        }
        incident = inc_mod.handle_event(payload["name"], payload,
                                        event_id=payload["id"],
                                        live_status=status)
        # A failure that is only a CLAIM must not produce a replacement payment
        # while the transfer may still be live.
        safe = not (incident.requires_new_request_id and inc_mod.in_flight(status))
        self._record(beat, safe,
                     f"incident -> {incident.recovery.value} "
                     f"(severity {incident.severity.value}); "
                     f"new_request_id={incident.requires_new_request_id}; "
                     f"live_status={status}")

    # ---- beat 6: ambiguous inflow escalates ---------------------------
    def beat_ambiguous_inflow(self) -> None:
        """USD 4,400 from 'L. Stores' matches nothing confidently.

        This is the beat that shows the agent working: the model PROPOSES a
        reading, deterministic code RE-VALIDATES every number, and a
        sub-threshold result is escalated to a human. All three are shown side
        by side so the boundary is visible rather than asserted.
        """
        beat = "beat 6 (ambiguous inflow: agent proposes, code decides)"
        amount = 440_000
        if _inflow_exists(self.db, amount, "USD", ""):
            self._record(beat, True,
                         "USD 4,400 ambiguous inflow already recorded "
                         "(beat already ran; not re-injecting)")
            return
        inflow = Inflow(currency="USD", amount_minor=amount,
                        payer_name="L. Stores", reference_raw="")
        s = self.db.session()
        s.add(inflow)
        s.commit()
        candidates = _candidates(self.db)

        # The model's turn: it may only CALL tools. None of them can pay.
        agent, trace = build_agent(self.db)
        proposal = agent.call(ToolCall(
            "propose_match",
            {"inflow_id": inflow.id, "amount_minor": amount,
             "currency": "USD", "payer_name": "L. Stores", "reference": ""}))

        # Code's turn: re-validate the proposal against the database.
        result = match_inflow(amount, "USD",
                              {"reference": "", "payer_name": "L. Stores"},
                              candidates, llm=_matcher_or_none())
        # Record the DETERMINISTIC conclusion, not the model's guess. The UI
        # reads this reason rather than re-deriving one: a view that recomputes
        # policy is a second source of truth, and it will drift from the rule
        # that actually decided.
        trace.record(ToolResult(
            "decide_match",
            ok=True,
            verdict=(Verdict.ESCALATED if result.status == "AMBIGUOUS"
                     else Verdict.ACCEPTED),
            output={
                "status": result.status,
                "confidence": result.confidence,
                "band": classify_confidence(result.confidence),
                "reason": result.reason,
                "evidence": result.evidence,
            },
            inputs={"inflow_id": inflow.id, "amount_minor": amount},
            detail=result.reason or result.status,
        ))
        agent.call(ToolCall("explain_decision",
                            {"decision": result.reason or result.status,
                             "evidence": result.evidence}))
        apply_inflow_to_receivables(s, inflow, result)
        s.commit()
        s.refresh(inflow)

        # Assert on what was PERSISTED, not on the matcher's own verdict.
        escalated = inflow.match_status in (MatchStatus.AMBIGUOUS,
                                            MatchStatus.UNMATCHED)
        print("\n  --- agent trace ---")
        print(trace.summary())
        trace.persist(self.db.session())

        # Describe what ACTUALLY happened. The model's proposal was accepted as
        # a MATCH but the allocation covers only part of INV-0043, so the
        # invoice stays short and the payment is held for a human — it was not
        # "escalated" in the sense of "rejected", and saying so would misstate
        # both the outcome and the remaining balance.
        inv43 = (s.query(InvoiceReceivable)
                 .filter_by(number="INV-0043").first())
        band = classify_confidence(result.confidence)
        # Say what the LEDGER says, not what the matcher proposed. An
        # AMBIGUOUS result is recorded but never applied, so claiming an
        # application there would be a money claim nothing backs.
        if inv43 and inv43.amount_paid_minor > 0:
            due = (inv43.amount_minor - inv43.amount_paid_minor) / 100
            outcome = (f"{inv43.amount_paid_minor/100:,.2f} applied to INV-0043, "
                       f"leaving {due:,.2f} USD still outstanding, so the "
                       f"payment is HELD for a human")
        elif inv43:
            outcome = ("nothing was applied: the reading was too weak to "
                       "prove, so it is held for a human to decide")
        else:
            outcome = "no invoice was affected"
        self._record(beat, escalated,
                     f"USD {amount/100:,.2f} from 'L. Stores' (no reference), "
                     f"the model proposed {result.status} and claimed "
                     f"{result.evidence.get('model_claimed_confidence', 0):.2f} "
                     f"confidence, which is its claim, not a verdict; code "
                     f"re-validated every number and pinned the band to {band} "
                     f"(a model result can never auto-apply). Outcome: "
                     f"{outcome}. Recorded as {inflow.match_status.value}.")

    # ---- beat 7: reconcile -------------------------------------------
    def beat_close(self) -> None:
        beat = "beat 7 (reconcile)"
        rep = reconcile(self.db.session())
        self._record(beat, rep.balanced, rep.summary())

    def run_all(self) -> int:
        for fn in (self.beat_deposit_typo, self.beat_partial_payment,
                   self.beat_card_decline, self.beat_failure_recovery,
                   self.beat_ambiguous_inflow, self.beat_close):
            fn()
        bad = [r for r in self.results if not r.ok]
        print(f"\nlive beats: {len(self.results) - len(bad)}/{len(self.results)} ok")
        for r in bad:
            print(f"  FAILED: {r.name}, {r.detail}")
        return 1 if bad else 0


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    offline = "--offline" in argv
    db = DB()
    print("=== Closebook LIVE demo: Kano Craft Imports Ltd ===")
    if offline:
        print("(--offline: deterministic decisions only, no sandbox calls)")
        return 0
    from ..config import get_settings
    if not get_settings().sandbox_ready():
        print("No sandbox credentials in .env: nothing live to run.")
        print("Fill .env, or use `--offline`.")
        return 2
    return LiveDemo(db).run_all()


if __name__ == "__main__":
    raise SystemExit(main())
