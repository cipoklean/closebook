"""The LLM layer: interpretation and explanation ONLY.

The rule this module enforces is the whole thesis of the project — **the model
proposes, deterministic code disposes.** Concretely:

  * The model may READ an unstructured bank reference and suggest which invoice
    it belongs to, and it may EXPLAIN a decision to a human.
  * The model may NEVER authorise a payment, set an amount, choose a
    beneficiary, override a reserve floor, or mark money as matched.

`match_inflow()` accepts an `LLMMatcher`; when one is present its proposal is
handed straight back into `try_llm`, which re-derives every number against the
database and escalates anything that does not check out. There is no code path
where a model's word is taken as final.

No provider is required. With no key configured every function degrades to
`None`/offline behaviour and the deterministic tiers carry the demo, which is
why the project has never had a paid LLM dependency.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol

log = logging.getLogger("closebook.llm")

# Any OpenAI-compatible chat-completions endpoint works. Left unset, Closebook
# runs fully deterministically — this is a supported mode, not a degraded one.
# Values come from `config.Settings` (LLM_BASE_URL); these are the fallbacks.
DEFAULT_BASE_URL = "https://api.openai.com/v1"
# Small and cheap on purpose: the job is reading one line of text, and a large
# model would only make the same deterministic decision more slowly.
DEFAULT_MODEL = "gpt-4o-mini"

MAX_REFERENCE_CHARS = 400
"""A bank reference is a short string. Anything longer is not a reference, and
feeding a whole document to a model is how unrelated content gets matched."""


class LLMMatcher(Protocol):
    """What `match_inflow` needs. Any object with this method works — including
    the offline stub, so the signature is the contract, not the class."""

    def propose(self, inflow: dict[str, Any],
                candidates: list[dict[str, Any]]) -> Optional[dict]:
        ...


@dataclass
class LLMConfig:
    api_key: Optional[str] = None
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_s: int = 20

    @classmethod
    def from_settings(cls) -> "LLMConfig":
        """Read config through `config.Settings`, which loads `.env`.

        NOT `os.environ`. Two reasons: `.env` on disk is the documented place
        for these values, and pydantic-settings is already doing the parsing —
        reading the environment separately means the two can disagree, and the
        code that bypasses it silently ignores a configured key.
        """
        from ..config import get_settings
        s = get_settings()
        key = s.llm_api_key.get_secret_value().strip()
        return cls(
            api_key=key or None,
            base_url=s.llm_base_url,
            model=s.llm_model,
        )

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """Backwards-compatible alias for `from_settings`."""
        return cls.from_settings()

    @property
    def available(self) -> bool:
        return bool(self.api_key)


_SYSTEM = (
    "You read bank payment references for a treasury system and suggest which "
    "invoice an incoming payment belongs to. You do not decide anything: a "
    "deterministic system re-checks every number you return and discards your "
    "answer if it does not reconcile exactly. Reply with JSON only."
)

_PROMPT = """An incoming payment arrived. Suggest which open invoice(s) it settles.

Payment:
  amount: {amount} {currency}
  payer: "{payer}"
  reference: "{reference}"

Open invoices:
{candidates}

Reply with JSON only, in exactly this shape:
{{"allocations": [{{"invoice_number": "INV-0041", "amount": 12000}}],
  "confidence": 0.0,
  "rationale": "one sentence"}}

Rules:
- `amount` values must sum EXACTLY to the payment amount.
- Only use invoice numbers listed above.
- If you are not confident, return {{"allocations": [], "confidence": 0.0}}.
- Never invent an invoice, an amount, or a currency."""


class OpenAICompatibleMatcher:
    """Thin client for any OpenAI-compatible /chat/completions endpoint."""

    def __init__(self, config: Optional[LLMConfig] = None):
        self.cfg = config or LLMConfig.from_env()

    def available(self) -> bool:
        return self.cfg.available

    def propose(self, inflow: dict[str, Any],
                candidates: list[dict[str, Any]]) -> Optional[dict]:
        if not self.cfg.available:
            return None
        try:
            return self._call(inflow, candidates)
        except Exception as exc:  # noqa: BLE001
            # A model failure must never break matching — the deterministic
            # tiers already produced a result, and the LLM is only a fallback.
            log.warning("LLM proposal failed (%s); falling back to escalation",
                        type(exc).__name__)
            return None

    def _call(self, inflow: dict[str, Any],
              candidates: list[dict[str, Any]]) -> Optional[dict]:
        import httpx

        reference = str(inflow.get("reference") or "")[:MAX_REFERENCE_CHARS]
        lines = []
        for c in candidates[:40]:
            lines.append(
                f"  {c['number']}  {c['currency']} {c['amount_open']} "
                f"open  (customer: {c.get('customer', '')})"
            )
        body = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _PROMPT.format(
                    amount=inflow.get("amount"), currency=inflow.get("currency"),
                    payer=str(inflow.get("payer") or "")[:120],
                    reference=reference,
                    candidates="\n".join(lines) or "  (none)",
                )},
            ],
            "temperature": 0,
        }
        with httpx.Client(timeout=self.cfg.timeout_s) as client:
            r = client.post(
                f"{self.cfg.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.cfg.api_key}"},
                json=body,
            )
            r.raise_for_status()
            data = r.json()
        text = data["choices"][0]["message"]["content"]
        return self._parse(text)

    @staticmethod
    def _parse(text: str) -> Optional[dict]:
        """Extract the JSON object. Tolerates code fences, which models add
        even when told not to."""
        raw = (text or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1] if "```" in raw[3:] else raw[3:]
            if raw.lstrip().lower().startswith("json"):
                raw = raw.lstrip()[4:]
        raw = raw.strip().strip("`")
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(raw[start:end + 1])
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None


class Explanation:
    """Plain-language narration of a decision the deterministic code ALREADY
    made. There is no path from here back into a number.

    Without a key this returns the deterministic reason verbatim, which is the
    honest answer: the explanation is a rendering, not the source of truth.
    """

    def __init__(self, matcher: Optional[OpenAICompatibleMatcher] = None):
        self.matcher = matcher or OpenAICompatibleMatcher()

    def explain(self, subject: dict[str, Any],
                decision: str, evidence: dict[str, Any]) -> str:
        if not self.matcher.available():
            return decision
        try:
            return self._ask(subject, decision, evidence)
        except Exception as exc:  # noqa: BLE001
            log.warning("explanation failed (%s); using the deterministic reason",
                        type(exc).__name__)
            return decision

    def _ask(self, subject: dict[str, Any], decision: str,
             evidence: dict[str, Any]) -> str:
        import httpx

        body = {
            "model": self.matcher.cfg.model,
            "messages": [
                {"role": "system", "content":
                    "Explain a treasury decision to a business owner in two "
                    "sentences. You may not change any number, amount, or "
                    "outcome — restate the given decision only."},
                {"role": "user", "content":
                    f"Subject: {json.dumps(subject, default=str)}\n"
                    f"Decision: {decision}\n"
                    f"Evidence: {json.dumps(evidence, default=str)[:1500]}"},
            ],
            "temperature": 0,
            "max_tokens": 200,
        }
        with httpx.Client(timeout=self.matcher.cfg.timeout_s) as client:
            r = client.post(
                f"{self.matcher.cfg.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization":
                         f"Bearer {self.matcher.cfg.api_key}"},
                json=body,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()


def candidate_payloads(candidates) -> list[dict[str, Any]]:
    """Model-facing view of a candidate: numbers in MAJOR units.

    Sending minor units invites a 100x error (1200000 vs 12000) that the
    re-validation would then correctly reject, wasting the proposal. The code
    still re-checks; this just makes a correct answer possible.
    """
    out = []
    for c in candidates:
        out.append({
            "number": c.number,
            "customer": c.customer_name,
            "currency": c.currency,
            "amount_open": c.amount_open_minor / 100,
        })
    return out


__all__ = [
    "Explanation", "LLMConfig", "LLMMatcher", "OpenAICompatibleMatcher",
    "candidate_payloads",
]
