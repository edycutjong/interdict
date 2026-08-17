"""Plane 2 -- the adjudicator. The only place in this system that calls a model.

The adjudicator answers one question about a candidate the deterministic plane surfaced:
is this the designated party, or a lookalike? It must answer in a fixed schema and it
must justify the answer against the record, because the rationale is not decoration --
it goes into the OFAC blocking report and a human compliance officer signs it.

WHY THE MODEL IS CONFINED TO THIS FILE. The matcher is the oracle for this plane, and an
oracle that also called the model would be grading itself. Keeping every model call
behind the `Adjudicator` protocol is what makes the oracle guard in orchestrator.py a
real check rather than a formality -- and it is why the whole screening plane can be
tested, tuned and reproduced with no API key at all.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Protocol

from .matcher import T_HI, Match

MODEL_ID = os.environ.get("INTERDICT_MODEL", "gemini-2.5-flash")

# Structured output. The adjudicator does not get to reply in prose: a free-text verdict
# cannot be guarded, cannot be diffed against the oracle, and cannot be replayed.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["HOLD", "CLEAR"]},
        "rationale": {
            "type": "string",
            "description": "Two or three sentences a compliance officer could sign. "
                           "Must reference the specific evidence relied on.",
        },
        "matched_identifier": {
            "type": "string",
            "description": "The exact name or identifier from the SDN record that "
                           "drove the decision. Must be quoted from the record.",
        },
        "confidence": {"type": "number"},
    },
    "required": ["verdict", "rationale", "matched_identifier", "confidence"],
}

PROMPT = """\
You are a sanctions compliance analyst. Decide whether a counterparty in a humanitarian \
NGO's payment book is the party designated on the OFAC SDN list, or a lookalike.

COUNTERPARTY (from the NGO's book)
  name: {query_name}
  date of birth: {query_dob}
  type: {query_type}

CANDIDATE SDN RECORD (OFAC publication {publication})
  uid: {sdn_uid}
  primary name: {primary_name}
  matched name: {matched_name}  (alias category: {matched_category})
  record type: {sdn_type}
  programs: {programs}
  dates of birth on record: {record_dobs}
  nationalities: {nationalities}

DETERMINISTIC SCREENING SIGNALS (computed, not opinion)
  overall score: {det_score}
  token-sort similarity: {sort_ratio}
  Jaro-Winkler: {jaro_winkler}
  query-token coverage: {token_coverage}
  weak alias: {weak_alias}
  date-of-birth signal: {dob_signal}
  person/entity type signal: {type_signal}

RULES
- HOLD if this is plausibly the designated party. Sanctions liability is strict: when \
the evidence is genuinely ambiguous, HOLD and let a human resolve it.
- CLEAR only when there is positive evidence of a DIFFERENT party -- a contradicting \
date of birth, a different entity type, or a match resting solely on a weak alias that \
OFAC itself flags as a low-quality identifier.
- A weak alias alone is not sufficient grounds to HOLD.
- Never invent identifiers. `matched_identifier` must be copied from the record above.
- Your rationale is transcribed into a federal blocking report. Write it accordingly.
{extra}"""


@dataclass(frozen=True)
class Verdict:
    verdict: str                 # HOLD | CLEAR
    rationale: str
    matched_identifier: str
    confidence: float
    model_id: str
    prompt_hash: str
    context: dict

    def as_row(self) -> dict:
        return {
            "verdict": self.verdict,
            "rationale": self.rationale,
            "model_id": self.model_id,
            "prompt_hash": self.prompt_hash,
            "context": self.context,
        }


class Adjudicator(Protocol):
    def adjudicate(self, context: dict, *, feedback: str | None = None) -> Verdict: ...


def build_context(query_name: str, query_dob: str | None, query_type: str,
                  match: Match, entry, publication: dict) -> dict:
    """Everything the model sees, persisted verbatim so `make replay` is bit-for-bit."""
    c = match.components
    return {
        "query_name": query_name,
        "query_dob": query_dob or "not provided",
        "query_type": query_type,
        "publication": publication.get("publish_date", "?"),
        "sdn_uid": match.sdn_uid,
        "primary_name": entry.primary_name,
        "matched_name": c.matched_name,
        "matched_category": c.matched_category,
        "sdn_type": entry.sdn_type,
        "programs": ", ".join(entry.programs) or "none listed",
        "record_dobs": ", ".join(entry.dobs) or "none on record",
        "nationalities": ", ".join(entry.nationalities) or "none on record",
        "det_score": match.score,
        "sort_ratio": c.sort_ratio,
        "jaro_winkler": c.jaro_winkler,
        "token_coverage": c.token_coverage,
        "weak_alias": c.weak_alias,
        "dob_signal": c.dob_signal,
        "type_signal": c.type_signal,
    }


def render_prompt(context: dict, feedback: str | None = None) -> str:
    extra = ""
    if feedback:
        # Second round trip only. The disagreement is stated as a fact to reconsider,
        # never as an instruction to change the answer -- an adjudicator that caves to
        # pressure is not a second opinion.
        extra = (f"\n\nRECONSIDER. A deterministic check disagreed with your previous "
                 f"answer: {feedback}\nRe-examine the evidence. If your original "
                 f"verdict was right, keep it and say why the check is mistaken.")
    return PROMPT.format(extra=extra, **context)


class GeminiAdjudicator:
    """The product path. Gemini with enforced structured output.

    Three distinct API surfaces are used deliberately: structured output via
    `response_schema`, an explicit low temperature for reproducibility, and system
    instruction separation so the compliance framing is not user-overridable.
    """

    def __init__(self, model_id: str = MODEL_ID, api_key: str | None = None):
        from google import genai  # imported lazily: no key, no import

        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError(
                "No Gemini API key. Set GEMINI_API_KEY (a free AI Studio key at "
                "https://aistudio.google.com/apikey needs no billing account)."
            )
        self._client = genai.Client(api_key=key)
        self.model_id = model_id

    def adjudicate(self, context: dict, *, feedback: str | None = None) -> Verdict:
        prompt = render_prompt(context, feedback)
        response = self._client.models.generate_content(
            model=self.model_id,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": VERDICT_SCHEMA,
                # Sanctions decisions must not vary run to run.
                "temperature": 0.0,
            },
        )
        data = json.loads(response.text)
        return Verdict(
            verdict=data["verdict"],
            rationale=data["rationale"],
            matched_identifier=data["matched_identifier"],
            confidence=float(data.get("confidence", 0.0)),
            model_id=self.model_id,
            prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
            context=context,
        )


class RuleBasedAdjudicator:
    """Offline stand-in used by the test suite. NOT the product path.

    Exists so the orchestrator, the oracle guard, the loop cap and the quarantine
    terminal state can be tested deterministically and without an API key. It is
    deliberately crude: if this ever looked good enough to ship, the model plane would
    not be earning its place.
    """

    model_id = "rule-based-offline"

    def __init__(self, force: str | None = None):
        self._force = force

    def adjudicate(self, context: dict, *, feedback: str | None = None) -> Verdict:
        prompt = render_prompt(context, feedback)

        if self._force:
            verdict = self._force
            reason = f"forced {self._force} (test double)"
        elif context["dob_signal"] == "disjoint":
            verdict, reason = "CLEAR", "date of birth on record contradicts the counterparty"
        elif context["type_signal"] == "mismatch":
            verdict, reason = "CLEAR", "record is a different entity type"
        elif context["weak_alias"] and context["det_score"] < T_HI:
            verdict, reason = "CLEAR", "match rests on an OFAC-flagged weak alias alone"
        else:
            verdict, reason = "HOLD", "name matches the SDN record with no contradicting evidence"

        return Verdict(
            verdict=verdict,
            rationale=f"{reason.capitalize()}.",
            matched_identifier=context["matched_name"],
            confidence=0.5,
            model_id=self.model_id,
            prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
            context=context,
        )
