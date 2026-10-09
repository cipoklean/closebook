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
    assert result.status == "AMBIGUOUS"
    assert result.confidence == 0.0


def test_model_cannot_invent_an_invoice():
    class Fabricator:
        def propose(self, inflow, candidates):
            return {"allocations": [{"invoice_number": "INV-9999", "amount": 12000}],
                    "confidence": 1.0}

    result = match_inflow(1_200_000, "USD", {"reference": "zzz", "payer_name": "?"},
                          _cands(), llm=Fabricator())
    assert result.status == "AMBIGUOUS"


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
