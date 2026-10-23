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

TWO ANSWERS THAT MUST NEVER BE CONFUSED
---------------------------------------
"the model said nothing useful" and "we never got an answer out of the model"
look identical to a reader, and this module has already shipped that bug once:
a 20s timeout was rendered as "0 calls", i.e. a timeout dressed up as a
considered refusal. So `last_error` is always set when there is no proposal,
and it distinguishes *not configured* from *failed* from *answered*.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
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
    #: Tried when the primary returns a 5xx or times out. A free tier's busy
    #: hour must not read as "the model proposed nothing".
    fallback_model: Optional[str] = None
    timeout_s: int = 45
    #: 429 is a THROTTLE, not a failure: back off and try the same model again.
    max_retries: int = 3
    backoff_s: float = 1.5
    #: Provider-specific request keys, merged into the body — e.g. NVIDIA NIM's
    #: {"chat_template_kwargs": {"enable_thinking": false}}. Keeping this in
    #: config means no provider needs a special case in this file.
    extra_body: dict[str, Any] = field(default_factory=dict)
    #: Ask for a JSON object (`response_format`). Right for the match proposal,
    #: wrong for prose explanations, so it is per-call with this as the default.
    json_mode: bool = True

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
            fallback_model=(getattr(s, "llm_fallback_model", "") or "").strip()
            or None,
            timeout_s=int(getattr(s, "llm_timeout_s", 45) or 45),
            max_retries=int(getattr(s, "llm_max_retries", 3) or 3),
            backoff_s=float(getattr(s, "llm_backoff_s", 1.5) or 1.5),
            extra_body=_parse_extra_body(getattr(s, "llm_extra_body", "")),
            json_mode=bool(getattr(s, "llm_json_mode", True)),
        )

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """Backwards-compatible alias for `from_settings`."""
        return cls.from_settings()

    @property
    def available(self) -> bool:
        return bool(self.api_key)


def _parse_extra_body(raw: Any) -> dict[str, Any]:
    """Parse `LLM_EXTRA_BODY` (a JSON object string) into request-body keys.

    Invalid JSON is IGNORED rather than fatal: a typo in an optional tuning knob
    must not take the matching engine down with it, and the warning says so.
    """
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        log.warning("LLM_EXTRA_BODY is not valid JSON; ignoring it")
        return {}
    if not isinstance(parsed, dict):
        log.warning("LLM_EXTRA_BODY must be a JSON object; ignoring it")
        return {}
    return parsed


def _merge(base: dict, extra: dict) -> dict:
    """Deep-merge provider extras into a request body.

    Dicts merge (so `chat_template_kwargs` can be added inside a nested object
    without clobbering its siblings); anything else is replaced.
    """
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _merge(dict(base[k]), v)
        else:
            base[k] = v
    return base


def _message_text(msg: dict) -> tuple[str, bool]:
    """The answer, and whether it came from `reasoning_content`.

    Some models — and some providers' thinking modes — leave `content` EMPTY and
    put the whole answer in `reasoning_content`. Treating that as "the model
    proposed nothing" is the same confusion between *no answer* and *an answer
    we did not look for* that this module exists to avoid, so the fallback is
    explicit and the fact that it was used is REPORTED, not silent.
    """
    text = (msg.get("content") or "").strip()
    if text:
        return text, False
    reasoning = (msg.get("reasoning_content") or "").strip()
    if reasoning:
        return reasoning, True
    return "", False


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
- An allocation's `amount` is what YOU believe that invoice was paid. It does NOT
  have to equal the invoice's open amount. A PART-PAYMENT is normal, and
  allocating LESS than the open amount is allowed and expected.
- Only the allocations TOGETHER must sum EXACTLY to the payment amount. One
  invoice paid in part is: a single allocation of the full payment amount.
- Do not reject a payment merely because its amount differs from every open
  balance. If the reference or the payer points at one invoice, say so and
  allocate the whole payment to it. Reading a reference is the job.
- Only use invoice numbers listed above.
- `confidence` must be a number between 0 and 1. Use a low number when the only
  evidence is one vague word; use a higher one when the reference names the
  invoice or the payer is clearly that customer.
- If nothing in the payment points at any invoice, return
  {{"allocations": [], "confidence": 0.0}}: an honest "I cannot tell" is worth
  more than a guess.
- Never invent an invoice, an amount, or a currency."""


class OpenAICompatibleMatcher:
    """Thin client for any OpenAI-compatible /chat/completions endpoint."""

    def __init__(self, config: Optional[LLMConfig] = None):
        self.cfg = config or LLMConfig.from_env()
        #: Why the last `propose()` returned None. None means it succeeded.
        self.last_error: Optional[str] = None
        #: Which model actually answered the last call (primary or fallback).
        self.last_model: Optional[str] = None
        #: True when the answer had to be read out of `reasoning_content`
        #: because `content` came back empty.
        self.last_used_reasoning: bool = False
        #: How many 429s were waited out on the last call, for the trace.
        self.last_429s: int = 0

    def available(self) -> bool:
        return self.cfg.available

    def propose(self, inflow: dict[str, Any],
                candidates: list[dict[str, Any]]) -> Optional[dict]:
        """The model's reading, or None.

        None means one of TWO different things and they must not be confusable:
        no key is configured, or the provider failed. `last_error` carries the
        distinction so the page can say "no model configured" or "the model was
        down (ReadTimeout)" instead of the old, useless "0 calls".
        """
        self.last_error = None
        self.last_used_reasoning = False
        self.last_model = None
        self.last_429s = 0
        if not self.cfg.available:
            self.last_error = ("no model is configured. LLM_API_KEY is empty, "
                               "so the deterministic tiers decide alone")
            return None
        try:
            out = self._call(inflow, candidates)
        except Exception as exc:  # noqa: BLE001
            # A model failure must never break matching — the deterministic
            # tiers already produced a result, and the LLM is only a fallback.
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("LLM proposal failed (%s); falling back to escalation",
                        type(exc).__name__)
            return None
        if out is None:
            self.last_error = "the model returned nothing usable"
        return out

    # ---- the wire ------------------------------------------------------
    def _call(self, inflow: dict[str, Any],
              candidates: list[dict[str, Any]]) -> Optional[dict]:
        """Build the request, send it, parse the answer.

        Kept as its own method because it is the seam a test overrides to assert
        what actually goes on the wire without a provider — the transport below
        is shared by the proposal and the explanations.
        """
        text = self._post(self._proposal_body(inflow, candidates),
                          json_mode=self.cfg.json_mode)
        return self._parse(text)

    def _proposal_body(self, inflow: dict[str, Any],
                       candidates: list[dict[str, Any]]) -> dict:
        reference = str(inflow.get("reference") or "")[:MAX_REFERENCE_CHARS]
        lines = []
        for c in candidates[:40]:
            lines.append(
                f"  {c['number']}  {c['currency']} {c['amount_open']} "
                f"open  (customer: {c.get('customer', '')})"
            )
        return {
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

    def _post(self, body: dict, *, json_mode: bool) -> str:
        """One logical call: retry/backoff on 429, fall back on 5xx or timeout.

        The two failure shapes need opposite handling and conflating them is
        what breaks a demo on a free tier:

          * **429 is a throttle.** The model works, we are asking too often. Back
            off exponentially and ask the SAME model again, up to `max_retries`.
            Switching models here would just spread the throttling around.
          * **5xx or a timeout is the model being unavailable.** Retrying the
            thing that is down wastes the request budget, so go straight to the
            fallback model — one attempt, no backoff, because the second model is
            a different service.

        Raises on total failure, so `propose()` can turn it into `last_error`.
        """
        import httpx

        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        models = [self.cfg.model]
        if self.cfg.fallback_model and self.cfg.fallback_model != self.cfg.model:
            models.append(self.cfg.fallback_model)

        # EVERY attempt's reason, not just the last one. With two models in
        # play, reporting only the final failure hides the one that explains
        # what happened ("primary: HTTP 503; other: HTTP 500").
        why: list[str] = []
        with httpx.Client(timeout=self.cfg.timeout_s) as client:
            for idx, model in enumerate(models):
                is_fallback = idx > 0
                tries = 1 if is_fallback else max(1, self.cfg.max_retries)
                for attempt in range(tries):
                    use_json = json_mode
                    payload = _merge(dict(body), self.cfg.extra_body)
                    payload["model"] = model
                    if use_json:
                        payload["response_format"] = {"type": "json_object"}
                    try:
                        r = client.post(url, headers=headers, json=payload)
                    except httpx.TimeoutException as exc:
                        why.append(f"{model}: timeout after "
                                   f"{self.cfg.timeout_s}s ({type(exc).__name__})")
                        break                      # -> the other model
                    except httpx.HTTPError as exc:
                        why.append(f"{model}: {type(exc).__name__}: {exc}"[:200])
                        break                      # -> the other model

                    if r.status_code == 400 and use_json:
                        # Not every OpenAI-compatible server implements
                        # `response_format`. Drop it and ask again rather than
                        # lose an answer we could have had.
                        why.append(f"{model}: HTTP 400 with response_format: "
                                   f"{r.text[:120]}")
                        use_json = False
                        payload.pop("response_format", None)
                        try:
                            r = client.post(url, headers=headers, json=payload)
                        except httpx.HTTPError as exc:
                            why.append(f"{model}: {type(exc).__name__}: {exc}"[:200])
                            break
                    if r.status_code == 429:
                        self.last_429s += 1
                        why.append(f"{model}: HTTP 429 (rate limited)")
                        if attempt + 1 < tries:
                            wait = self.cfg.backoff_s * (2 ** attempt)
                            log.info("LLM 429 on %s; retrying in %.1fs", model, wait)
                            time.sleep(wait)
                            continue
                        break                      # -> the other model
                    if r.status_code >= 500:
                        why.append(f"{model}: HTTP {r.status_code}")
                        break                      # -> the other model
                    if r.status_code >= 400:
                        why.append(f"{model}: HTTP {r.status_code}: {r.text[:200]}")
                        break

                    data = r.json()
                    choice = (data.get("choices") or [{}])[0]
                    text, used_reasoning = _message_text(choice.get("message") or {})
                    if not text:
                        why.append(f"{model}: empty content and no reasoning_content")
                        break                      # -> the other model
                    self.last_model = model
                    self.last_used_reasoning = used_reasoning
                    if used_reasoning:
                        log.info("LLM %s returned empty content; read "
                                 "reasoning_content instead", model)
                    return text
        raise RuntimeError("; ".join(why) or "no attempt was made")

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
        body = {
            "model": self.matcher.cfg.model,
            "messages": [
                {"role": "system", "content":
                    "Explain a treasury decision to a business owner in two "
                    "sentences. You may not change any number, amount, or "
                    "outcome: restate the given decision only."},
                {"role": "user", "content":
                    f"Subject: {json.dumps(subject, default=str)}\n"
                    f"Decision: {decision}\n"
                    f"Evidence: {json.dumps(evidence, default=str)[:1500]}"},
            ],
            "temperature": 0,
            "max_tokens": 200,
        }
        # Prose, so NOT json_mode — the same transport, different ask.
        return self.matcher._post(body, json_mode=False).strip()


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
