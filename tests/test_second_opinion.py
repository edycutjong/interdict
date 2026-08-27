"""Preflight budgeting and the second model.

Both additions are evidence-side, never decision-side. The tests below are written to
fail if that ever stops being true: nothing here may hold money, clear a counterparty
or reach quarantine.

The Google GenAI SDK is faked at `genai.Client` -- no API key, no network, no cost.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from google import genai

import interdict.adjudicator as adj
from interdict.adjudicator import (
    GeminiAdjudicator,
    GemmaSecondOpinion,
    SecondOpinion,
    TransientAdjudicationError,
    budget_run,
)

CONTEXT = {
    "query_name": "Abu Abbas Khalid", "query_dob": "not provided", "query_type": "Individual",
    "publication": "08/07/2026", "sdn_uid": 2674, "primary_name": "ABU ABBAS",
    "matched_name": "Abu ABBAS", "matched_category": "strong", "sdn_type": "Individual",
    "programs": "SDGT", "record_dobs": "10 Dec 1948", "nationalities": "none on record",
    "det_score": 0.93, "sort_ratio": 0.95, "jaro_winkler": 0.97,
    "token_coverage": 1.0, "weak_alias": False, "dob_signal": "unavailable",
    "type_signal": "match",
}


# ---------------------------------------------------------------------------
# A fake SDK that also serves count_tokens and list
# ---------------------------------------------------------------------------

class _Models:
    def __init__(self, *, tokens=None, served=(), gen=None):
        self.token_calls, self.gen_calls, self.list_calls = [], [], 0
        self._tokens = tokens
        self._served = served
        self._gen = gen

    def count_tokens(self, **kw):
        self.token_calls.append(kw)
        if isinstance(self._tokens, BaseException):
            raise self._tokens
        return SimpleNamespace(total_tokens=self._tokens)

    def list(self):
        self.list_calls += 1
        return [SimpleNamespace(name=n) for n in self._served]

    def generate_content(self, **kw):
        self.gen_calls.append(kw)
        if isinstance(self._gen, BaseException):
            raise self._gen
        return SimpleNamespace(text=self._gen)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    holder = SimpleNamespace(models=None)

    def build(**kw):
        holder.models = _Models(**kw)
        monkeypatch.setattr(
            genai, "Client",
            lambda api_key=None, **_: SimpleNamespace(models=holder.models),
        )
        return GeminiAdjudicator(api_key="test-key")

    holder.build = build
    return holder


# ---------------------------------------------------------------------------
# count_tokens -- the number the quota decision rests on
# ---------------------------------------------------------------------------

def test_the_token_count_comes_from_the_api_not_a_guess(client):
    a = client.build(tokens=412)
    assert a.count_prompt_tokens(CONTEXT) == 412
    call = client.models.token_calls[0]
    assert call["model"] == adj.MODEL_ID
    # It must count the REAL prompt. Counting a placeholder would produce a budget for
    # a run that is not the one about to be executed.
    assert "Abu Abbas Khalid" in call["contents"]


def test_the_reconsider_turn_is_counted_when_present(client):
    a = client.build(tokens=500)
    a.count_prompt_tokens(CONTEXT, feedback="deterministic check says HOLD")
    assert "RECONSIDER" in client.models.token_calls[0]["contents"]


def test_a_missing_total_is_an_error_not_a_zero(client):
    # A silent zero would under-count the run and report a quota estimate that reads as
    # comfortable. This is the whole reason the None branch exists.
    a = client.build(tokens=None)
    with pytest.raises(TransientAdjudicationError, match="no total"):
        a.count_prompt_tokens(CONTEXT)


# ---------------------------------------------------------------------------
# model_available -- fail before the run opens, not during it
# ---------------------------------------------------------------------------

def test_the_pinned_model_is_recognised_through_the_models_prefix(client):
    a = client.build(served=[f"models/{adj.MODEL_ID}", "models/gemma-4-31b-it"])
    assert a.model_available() is True


def test_a_typo_in_the_model_id_is_caught_before_any_run_opens(client):
    a = client.build(served=["models/gemini-3.5-flash", "models/gemma-4-31b-it"])
    assert a.model_available() is False


def test_a_nameless_entry_in_the_served_list_does_not_crash_the_check(client):
    a = client.build(served=[None, f"models/{adj.MODEL_ID}"])
    assert a.model_available() is True


# ---------------------------------------------------------------------------
# budget_run
# ---------------------------------------------------------------------------

def test_an_empty_book_costs_nothing_and_asks_the_api_nothing(client):
    a = client.build(tokens=400)
    assert budget_run(a, []) == {"contexts": 0, "tokens_total": 0, "minutes": 0.0, "sampled": 0}
    assert client.models.token_calls == []


def test_the_budget_samples_rather_than_counting_every_context(client):
    a = client.build(tokens=400)
    out = budget_run(a, [CONTEXT] * 536, sample=8)
    # 536 count_tokens calls to learn the cost of 536 adjudications would be its own joke.
    assert len(client.models.token_calls) == 8
    assert out["sampled"] == 8
    assert out["contexts"] == 536
    assert out["tokens_total"] == 400 * 536


def test_wall_clock_is_set_by_the_request_ceiling_not_by_tokens(client):
    a = client.build(tokens=400)
    # Free tier serves 5 requests a minute; 536 adjudications is ~107 minutes whatever
    # the prompts weigh. This is the honest source of the "~90 minutes" figure.
    assert budget_run(a, [CONTEXT] * 536)["minutes"] == pytest.approx(107.2)
    assert budget_run(a, [CONTEXT] * 100, rpm=10)["minutes"] == pytest.approx(10.0)


def test_a_book_smaller_than_the_sample_is_counted_whole(client):
    a = client.build(tokens=300)
    out = budget_run(a, [CONTEXT] * 3, sample=8)
    assert out["sampled"] == 3 and out["tokens_total"] == 900


# ---------------------------------------------------------------------------
# The second model
# ---------------------------------------------------------------------------

def _gemma(gen):
    return GemmaSecondOpinion(SimpleNamespace(models=_Models(gen=gen)))


def test_the_second_model_answers_the_same_question_under_the_same_framing():
    models = _Models(gen=json.dumps({"verdict": "HOLD", "rationale": "Name and programme align."}))
    op = GemmaSecondOpinion(SimpleNamespace(models=models)).opine(CONTEXT)
    assert isinstance(op, SecondOpinion)
    assert op.verdict == "HOLD"
    assert op.model_id == adj.SECOND_MODEL_ID

    cfg = models.gen_calls[0]["config"]
    # Same system instruction as the product path: two verdicts are only comparable if
    # they are answers to the same question.
    assert cfg["system_instruction"] == adj.SYSTEM_INSTRUCTION
    assert cfg["temperature"] == 0.0
    assert cfg["response_schema"] is adj.SECOND_OPINION_SCHEMA
    # The untrusted record text rides in contents, never in the framing.
    assert "Abu Abbas Khalid" in models.gen_calls[0]["contents"]


def test_an_unreachable_second_model_records_nothing_rather_than_agreement():
    # NULL must read as "not asked", never as "agreed". An outage recorded as agreement
    # is the same bug the yente path is written to avoid.
    assert _gemma(RuntimeError("503 UNAVAILABLE")).opine(CONTEXT) is None


def test_an_empty_body_from_the_second_model_records_nothing():
    assert _gemma("").opine(CONTEXT) is None


def test_a_malformed_body_from_the_second_model_records_nothing():
    assert _gemma("{not json").opine(CONTEXT) is None


def test_a_second_model_failure_never_raises_into_the_screening_run():
    # The first verdict is already guarded and persisted by the time this is asked.
    # A transient failure here must not take down a run that has produced a decision.
    for boom in (RuntimeError("boom"), KeyError("verdict"), ValueError("bad")):
        assert _gemma(boom).opine(CONTEXT) is None
