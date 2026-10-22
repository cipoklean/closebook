"""The LLM layer must be USEFUL OFFLINE and SAFE when a model is attached.

The project's rule: the model proposes, code disposes. These tests pin both
halves — that no provider is required, and that a model's answer cannot become
money movement.
"""
import pytest

from app.domain.llm import (
    DEFAULT_MODEL, Explanation, LLMConfig, OpenAICompatibleMatcher,
    candidate_payloads,
)
from app.domain.matching import Candidate, match_inflow


def _cands():
    return [
        Candidate(1, "INV-0041", "Brightline Retail", "USD",
                  amount_open_minor=1_200_000, amount_original_minor=1_200_000),
    ]


# --- offline is a supported mode, not a degraded one ---------------------
def test_no_key_means_no_model_and_no_error():
    """Config now comes from Settings, not the environment. An empty key is the
    offline mode, and it must not error."""
    m = OpenAICompatibleMatcher(LLMConfig(api_key=None))
    assert m.available() is False
    assert m.propose({"amount": 12000, "currency": "USD"}, []) is None


def test_matching_works_with_no_model_at_all():
    """The demo must not depend on a paid key."""
    result = match_inflow(1_200_000, "USD",
                          {"reference": "INV-0041", "payer_name": "Brightline Retail"},
                          _cands(), llm=None)
    assert result.status == "MATCHED"
    assert result.confidence >= 0.9


def test_explanation_falls_back_to_the_deterministic_reason():
    e = Explanation(OpenAICompatibleMatcher(LLMConfig(api_key=None)))
    out = e.explain({"invoice": "INV-0041"}, "held: dependency unmet", {})
    assert out == "held: dependency unmet"


# --- candidates are sent in major units ---------------------------------
def test_candidates_are_sent_in_major_units():
    out = candidate_payloads(_cands())
    assert out[0]["amount_open"] == 12000.0   # not 1200000
    assert out[0]["number"] == "INV-0041"


# --- parsing is defensive ------------------------------------------------
@pytest.mark.parametrize("text", [
    '{"allocations": [], "confidence": 0.0}',
    '```json\n{"allocations": [], "confidence": 0.0}\n```',
    '```\n{"allocations": []}\n```',
    'Here you go: {"allocations": [{"invoice_number": "INV-0041", "amount": 1}]}',
    'no json at all',
    '',
    '[1,2,3]',
])
def test_parser_never_raises(text):
    out = OpenAICompatibleMatcher._parse(text)
    assert out is None or isinstance(out, dict)


def test_model_proposal_cannot_exceed_an_open_balance():
    """Even if a model CONFIDENTLY proposes an over-allocation, code wins."""
    class GreedyModel:
        def propose(self, inflow, candidates):
            return {"allocations": [{"invoice_number": "INV-0041",
                                     "amount": 9_999_999}],
                    "confidence": 1.0,
                    "rationale": "trust me"}

    result = match_inflow(1_200_000, "USD", {"reference": "zzz", "payer_name": "?"},
                          _cands(), llm=GreedyModel())
    # Never a settled auto-match on an impossible proposal.
    assert result.confidence < 0.9 or result.status != "MATCHED"


def test_model_returning_none_escalates_instead_of_crashing():
    """A provider that returns None (no key, or a dead gateway) must not 500.

    `propose()` signals 'nothing usable' by returning None rather than raising,
    and `try_llm` used to call `.get()` on it directly — which crashed mid-beat
    the first time a key was present but the provider was down.
    """
    class Silent:
        def propose(self, inflow, candidates):
            return None

    result = match_inflow(1_200_000, "USD", {"reference": "zzz", "payer_name": "?"},
                          _cands(), llm=Silent())
    # It did NOT crash, the model contributed nothing, and the deterministic
    # reading survived it — silence is not evidence against a plausible match.
    assert result.confidence < 0.90, "nothing here is confident enough to apply"
    assert result.evidence.get("model_consulted") is True
    assert result.evidence["model_attempt"]["confidence"] == 0.0


def test_model_cannot_invent_an_invoice():
    class Fabricator:
        def propose(self, inflow, candidates):
            return {"allocations": [{"invoice_number": "INV-9999", "amount": 12000}],
                    "confidence": 1.0}

    result = match_inflow(1_200_000, "USD", {"reference": "zzz", "payer_name": "?"},
                          _cands(), llm=Fabricator())
    # INV-9999 does not exist, so it cannot appear in any allocation.
    from app.domain.matching import Candidate
    real_ids = {c.invoice_id for c in _cands()}
    assert all(a.invoice_id in real_ids for a in result.allocations)
    assert result.method != "LLM" or result.confidence == 0.0


def test_model_cannot_cross_currencies():
    class CurrencyMixer:
        def propose(self, inflow, candidates):
            return {"allocations": [{"invoice_number": "INV-0041", "amount": 12000}],
                    "confidence": 1.0}

    result = match_inflow(1_200_000, "EUR", {"reference": "zzz", "payer_name": "?"},
                          _cands(), llm=CurrencyMixer())
    assert result.status == "AMBIGUOUS"


def test_a_raising_model_does_not_break_the_deterministic_result(monkeypatch):
    class Exploding:
        def propose(self, inflow, candidates):
            raise RuntimeError("provider is down")

    result = match_inflow(1_200_000, "USD",
                          {"reference": "INV-0041", "payer_name": "Brightline Retail"},
                          _cands(), llm=Exploding())
    # The exact tier already matched; a broken model changes nothing.
    assert result.status == "MATCHED"


def test_default_model_is_small_and_cheap():
    assert DEFAULT_MODEL == "gpt-4o-mini"


def test_config_is_read_through_settings_not_os_environ():
    """Audit 5: `llm.py` must not read `os.environ` directly.

    Note pydantic-settings legitimately reads the environment as a source (env
    vars override .env), so the fix is NOT "ignore the environment" — it is
    "go through Settings like everything else", so there is one parsing path.
    This asserts the module-level invariant: no direct environ access, and
    from_settings() mirrors Settings.
    """
    import ast
    import inspect

    from app.domain import llm as llm_module
    from app.config import get_settings

    # Walk the AST for real `os.environ` access rather than grepping the source,
    # which also matches the docstrings explaining why we do NOT do this.
    tree = ast.parse(inspect.getsource(llm_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            raise AssertionError("llm.py reads the environment directly, "
                                 "bypassing Settings")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "os", "llm.py imports os"
    assert "get_settings" in inspect.getsource(llm_module)

    # from_settings must agree with the Settings object, field for field.
    s = get_settings()
    cfg = LLMConfig.from_settings()
    assert cfg.base_url == s.llm_base_url
    assert cfg.model == s.llm_model
    assert cfg.available == bool(s.llm_api_key.get_secret_value().strip())


# --- the transport: throttle, fallback, thinking-off, reasoning -----------
# Every test here uses a FAKE httpx client. The suite must never reach a
# provider: it would be slow, flaky, and it would bill somebody.
class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else __import__("json").dumps(body)

    def json(self):
        return self._body if isinstance(self._body, dict) else __import__("json").loads(self._body)


class _FakeClient:
    """Replays a scripted list of responses and records every request body."""

    def __init__(self, *responses):
        self.scripted = list(responses)
        self.sent: list[dict] = []
        self.urls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        import copy
        self.urls.append(url)
        # Snapshot: the transport reuses one payload dict when it drops
        # `response_format`, so recording by reference would rewrite history.
        self.sent.append(copy.deepcopy(json))
        if not self.scripted:
            raise AssertionError("more calls than scripted responses")
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok(content="", reasoning=None, model="m"):
    msg = {"role": "assistant", "content": content}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return _Resp(200, {"choices": [{"message": msg}], "model": model})


def _install(monkeypatch, fake, sleeps=None):
    import httpx
    monkeypatch.setattr(httpx, "Client", lambda **kw: fake)
    if sleeps is not None:
        monkeypatch.setattr("app.domain.llm.time.sleep", lambda s: sleeps.append(s))
    return fake


def _cfg(**kw):
    kw.setdefault("api_key", "test-key")
    kw.setdefault("max_retries", 3)
    kw.setdefault("backoff_s", 1.5)
    return LLMConfig(**kw)


def test_a_429_is_a_throttle_and_the_SAME_model_is_retried(monkeypatch):
    """"Too many requests" is not "the model is down". Switching model here
    would only spread the throttling around, so we back off and ask again."""
    waits = []
    fake = _install(monkeypatch, _FakeClient(
        _Resp(429, {"error": "slow down"}), _Resp(429, {"error": "slow down"}),
        _ok('{"allocations": [], "confidence": 0.0}')), sleeps=waits)

    m = OpenAICompatibleMatcher(_cfg(model="primary", fallback_model="other"))
    out = m.propose({"amount": 1, "currency": "USD", "reference": "x"}, [])
    assert out == {"allocations": [], "confidence": 0.0}
    assert waits == [1.5, 3.0], "exponential backoff, base 1.5"
    assert [b["model"] for b in fake.sent] == ["primary"] * 3, \
        "a 429 must not switch models"
    assert m.last_429s == 2


def test_a_429_that_never_clears_still_falls_back(monkeypatch):
    fake = _install(monkeypatch, _FakeClient(
        _Resp(429, {}), _Resp(429, {}),
        _ok('{"allocations": [], "confidence": 0.5}')), sleeps=[])
    m = OpenAICompatibleMatcher(_cfg(model="primary", fallback_model="other",
                                     max_retries=2))
    assert m.propose({"amount": 1, "currency": "USD"}, []) is not None
    assert [b["model"] for b in fake.sent] == ["primary", "primary", "other"]


def test_a_5xx_goes_STRAIGHT_to_the_fallback_model(monkeypatch):
    """No point backing off from a service that is unwell. One attempt each."""
    fake = _install(monkeypatch, _FakeClient(
        _Resp(503, {"error": "high demand"}),
        _ok('{"allocations": [], "confidence": 0.0}')), sleeps=[])
    m = OpenAICompatibleMatcher(_cfg(model="primary", fallback_model="other"))
    m.propose({"amount": 1, "currency": "USD"}, [])
    assert [b["model"] for b in fake.sent] == ["primary", "other"]
    assert m.last_model == "other"


def test_a_timeout_goes_straight_to_the_fallback_model(monkeypatch):
    import httpx

    fake = _install(monkeypatch, _FakeClient(
        httpx.ReadTimeout("too slow"),
        _ok('{"allocations": [], "confidence": 0.0}')), sleeps=[])
    m = OpenAICompatibleMatcher(_cfg(model="primary", fallback_model="other"))
    m.propose({"amount": 1, "currency": "USD"}, [])
    assert [b["model"] for b in fake.sent] == ["primary", "other"]


def test_both_models_down_is_reported_not_swallowed(monkeypatch):
    _install(monkeypatch, _FakeClient(_Resp(503, {}), _Resp(500, {})), sleeps=[])
    m = OpenAICompatibleMatcher(_cfg(model="primary", fallback_model="other"))
    assert m.propose({"amount": 1, "currency": "USD"}, []) is None
    assert "primary" in m.last_error and "503" in m.last_error, \
        "the primary's failure must survive into the report"
    assert "other" in m.last_error and "500" in m.last_error, \
        "...and so must the fallback's"


def test_empty_content_falls_back_to_reasoning_content_and_says_so(monkeypatch):
    """Thinking-mode models can return an empty `content` with the answer in
    `reasoning_content`. Reading that as "the model proposed nothing" is the
    confusing-a-timeout-with-a-refusal bug all over again."""
    _install(monkeypatch, _FakeClient(_ok(
        content="", reasoning='{"allocations": [], "confidence": 0.4}')))
    m = OpenAICompatibleMatcher(_cfg())
    out = m.propose({"amount": 1, "currency": "USD"}, [])
    assert out == {"allocations": [], "confidence": 0.4}
    assert m.last_used_reasoning is True


def test_content_is_preferred_over_reasoning_when_both_exist(monkeypatch):
    _install(monkeypatch, _FakeClient(_ok(
        content='{"allocations": [], "confidence": 0.9}', reasoning="thinking...")))
    m = OpenAICompatibleMatcher(_cfg())
    m.propose({"amount": 1, "currency": "USD"}, [])
    assert m.last_used_reasoning is False


def test_a_truly_empty_answer_falls_back_to_the_other_model(monkeypatch):
    fake = _install(monkeypatch, _FakeClient(
        _ok(content="   "), _ok(content='{"allocations": [], "confidence": 0.1}')))
    m = OpenAICompatibleMatcher(_cfg(model="primary", fallback_model="other"))
    assert m.propose({"amount": 1, "currency": "USD"}, []) is not None
    assert [b["model"] for b in fake.sent] == ["primary", "other"]


def test_extra_body_is_merged_so_thinking_can_be_switched_off(monkeypatch):
    """NVIDIA NIM wants {"chat_template_kwargs": {"enable_thinking": false}} to
    skip the reasoning pass. It arrives as config, so no provider is special-cased
    in the client."""
    fake = _install(monkeypatch, _FakeClient(
        _ok('{"allocations": [], "confidence": 0.2}')), sleeps=[])
    m = OpenAICompatibleMatcher(_cfg(
        extra_body={"chat_template_kwargs": {"enable_thinking": False}}))
    m.propose({"amount": 1, "currency": "USD"}, [])
    sent = fake.sent[0]
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["messages"] and sent["temperature"] == 0, \
        "merging must not clobber the real request"


def test_json_mode_is_requested_and_dropped_if_the_server_refuses_it(monkeypatch):
    fake = _install(monkeypatch, _FakeClient(
        _Resp(400, {"error": "response_format is not supported"}),
        _ok('{"allocations": [], "confidence": 0.3}')), sleeps=[])
    m = OpenAICompatibleMatcher(_cfg(json_mode=True))
    out = m.propose({"amount": 1, "currency": "USD"}, [])
    assert out == {"allocations": [], "confidence": 0.3}
    assert fake.sent[0].get("response_format") == {"type": "json_object"}
    assert "response_format" not in fake.sent[1], \
        "the retry must not keep the key the server rejected"


def test_explanations_are_prose_and_so_not_json_mode(monkeypatch):
    fake = _install(monkeypatch, _FakeClient(_ok("Because the floor would break.")),
                    sleeps=[])
    m = OpenAICompatibleMatcher(_cfg(json_mode=True))
    assert Explanation(m).explain({"a": 1}, "DEFER", {}) == \
        "Because the floor would break."
    assert "response_format" not in fake.sent[0]


def test_extra_body_config_is_parsed_but_bad_json_is_ignored(caplog):
    from app.domain.llm import _parse_extra_body
    assert _parse_extra_body('{"chat_template_kwargs": {"enable_thinking": false}}') \
        == {"chat_template_kwargs": {"enable_thinking": False}}
    assert _parse_extra_body("") == {}
    assert _parse_extra_body("{not json") == {}, "a typo must not break matching"
    assert _parse_extra_body("[1,2]") == {}, "must be an object"


def test_no_key_makes_no_request_at_all(monkeypatch):
    """Offline is a supported mode: it must not even try, and must not error."""
    fake = _install(monkeypatch, _FakeClient(), sleeps=[])
    m = OpenAICompatibleMatcher(LLMConfig(api_key=None))
    assert m.propose({"amount": 1, "currency": "USD"}, []) is None
    assert fake.sent == []
    assert "no model is configured" in m.last_error


def test_the_new_knobs_are_read_through_Settings(monkeypatch):
    """Fallback model, retries, backoff, JSON mode and extra_body all come from
    `.env` via Settings — never from os.environ, so one place owns them."""
    from app.config import get_settings
    from app.domain.llm import LLMConfig

    s = get_settings()
    monkeypatch.setattr(s, "llm_model", "nvidia/nemotron-3.5-lightning-30b-a3b")
    monkeypatch.setattr(s, "llm_fallback_model", "deepseek-ai/deepseek-v4.1-flash")
    monkeypatch.setattr(s, "llm_max_retries", 5)
    monkeypatch.setattr(s, "llm_backoff_s", 2.0)
    monkeypatch.setattr(s, "llm_json_mode", True)
    monkeypatch.setattr(s, "llm_extra_body",
                        '{"chat_template_kwargs": {"enable_thinking": false}}')
    monkeypatch.setattr("app.config.get_settings", lambda: s)

    cfg = LLMConfig.from_settings()
    assert cfg.model == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert cfg.fallback_model == "deepseek-ai/deepseek-v4.1-flash"
    assert cfg.max_retries == 5
    assert cfg.backoff_s == 2.0
    assert cfg.json_mode is True
    assert cfg.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_an_empty_fallback_model_is_none_not_a_string(monkeypatch):
    """An empty env var must mean "no fallback", not a model named "" that the
    transport would then try to call."""
    from app.config import get_settings
    from app.domain.llm import LLMConfig

    s = get_settings()
    monkeypatch.setattr(s, "llm_fallback_model", "   ")
    monkeypatch.setattr("app.config.get_settings", lambda: s)
    assert LLMConfig.from_settings().fallback_model is None
