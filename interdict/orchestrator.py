"""The orchestrator -- routing, the oracle guard, and quarantine.

This is the thin agent that owns the decision. It calls the matcher, routes the result
to the adjudicator, and **checks the answer at the routing boundary before it is allowed
to move money**. It is the only writer of decisions.

THE ORACLE GUARD. The deterministic plane grades the model plane. Not as a sanity print
in a log -- as a gate the verdict has to pass on its way back through the orchestrator,
with a bounded retry and a terminal quarantine state when the two planes cannot agree.
That is the concrete answer to "how does the system recover if a worker agent loops or
returns a hallucination?": it does not trust the answer, it checks it, it asks once more
with the disagreement stated, and then it stops and escalates to a human rather than
guessing a third time.

WHY THE CAP IS TWO. An unbounded reconsider loop is the classic multi-agent failure:
two agents negotiating forever, burning tokens, with no human ever told. Two round trips
is enough to correct a genuine misread and few enough that quarantine stays a real
outcome rather than a theoretical one. The cap is also a database constraint, so it
holds even if this code is wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import psycopg

from .adjudicator import Adjudicator, build_context
from .db import emit
from .matcher import T_HI, T_LO, Match, Matcher
from .money import place_hold

# A CLEAR above this deterministic score is not credible: the names are effectively
# identical, so clearing one requires evidence the matcher already looked for and did
# not find. Set above T_HI deliberately -- the band between T_HI and here is exactly
# where the model is supposed to have discretion.
GUARD_CLEAR_CEILING = 0.93

MAX_ROUND_TRIPS = 2


@dataclass(frozen=True)
class Decision:
    counterparty_id: int
    sdn_uid: str | None
    verdict: str                  # HOLD | CLEAR | QUARANTINE
    reason: str
    round_trips: int
    det_score: float
    guard: str                    # AGREE | DISAGREE | SKIPPED
    adjudication_id: int | None = None
    hold_id: int | None = None


def guard(match: Match, verdict) -> tuple[str, str | None]:
    """Check a model verdict against the deterministic plane.

    Returns (result, complaint). `complaint` is fed back verbatim on the retry, so the
    adjudicator is told what disagreed rather than merely that something did.
    """
    # A CLEAR on a near-identical name needs evidence the matcher already sought.
    if verdict.verdict == "CLEAR" and match.score >= GUARD_CLEAR_CEILING:
        if match.components.dob_signal != "disjoint" and match.components.type_signal != "mismatch":
            return "DISAGREE", (
                f"deterministic score {match.score} is at or above {GUARD_CLEAR_CEILING} "
                f"and no contradicting date of birth or entity type was found, so a "
                f"CLEAR is not supported by the record")

    # A HOLD below the auto-no-hit floor means the model matched something the
    # deterministic plane could not see at all.
    if verdict.verdict == "HOLD" and match.score < T_LO:
        return "DISAGREE", (
            f"deterministic score {match.score} is below the no-hit floor {T_LO}; "
            f"a HOLD would freeze money on evidence the screening plane cannot see")

    # The identifier must be quoted from the record, not invented. This is the
    # hallucination check that matters: a fabricated alias in a federal blocking report
    # is the worst failure this system could produce.
    quoted = (verdict.matched_identifier or "").strip().upper()
    if not quoted:
        return "DISAGREE", "no matched_identifier was supplied"
    known = {match.components.matched_name.upper()}
    if quoted not in known and not any(quoted in k or k in quoted for k in known):
        return "DISAGREE", (
            f"matched_identifier {verdict.matched_identifier!r} does not appear in the "
            f"SDN record; the record's matched name is "
            f"{match.components.matched_name!r}")

    if len(verdict.rationale.strip()) < 20:
        return "DISAGREE", "rationale is too thin to transcribe into a blocking report"

    return "AGREE", None


def _persist_match(conn: psycopg.Connection, run_id: int, counterparty_id: int,
                   match: Match) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO matches (run_id, counterparty_id, sdn_uid, det_score, components)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (run_id, counterparty_id, sdn_uid) DO UPDATE SET det_score = EXCLUDED.det_score
            RETURNING id
            """,
            (run_id, counterparty_id, match.sdn_uid, match.score,
             json.dumps(match.components.as_dict(), sort_keys=True)),
        )
        return cur.fetchone()["id"]


def _persist_adjudication(conn: psycopg.Connection, match_id: int, verdict,
                          guard_result: str, round_trips: int,
                          yente_verdict: str | None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO adjudications
                (match_id, verdict, rationale, model_id, prompt_hash, context,
                 oracle_guard_result, yente_verdict, round_trips)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (match_id, verdict.verdict, verdict.rationale, verdict.model_id,
             verdict.prompt_hash, json.dumps(verdict.context, sort_keys=True),
             guard_result, yente_verdict, round_trips),
        )
        return cur.fetchone()["id"]


def _quarantine(conn: psycopg.Connection, match_id: int | None, reason: str,
                payload: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO quarantine (reason, match_id, payload) VALUES (%s,%s,%s)",
            (reason, match_id, json.dumps(payload, sort_keys=True)),
        )
    emit(conn, "QUARANTINED", {"reason": reason, "match_id": match_id, **payload})


def screen_counterparty(conn: psycopg.Connection, *, run_id: int, counterparty_id: int,
                        name: str, dob: str | None, is_person: bool,
                        matcher: Matcher, adjudicator: Adjudicator,
                        publication: dict, oracle_verdict: str | None = None,
                        blocked_on: date | None = None) -> Decision:
    """Screen one counterparty end to end and act on the result.

    The full path: match -> adjudicate -> guard -> (retry once) -> hold, clear, or
    quarantine. Every branch writes to the outbox in this transaction.
    """
    results = matcher.screen(name, dob, is_person=is_person)

    # Nothing reached the floor: an auto-no-hit. The model is never called, which is
    # both the cheap path and the safe one -- there is nothing for it to hallucinate on.
    if not results:
        emit(conn, "SCREENED_CLEAR", {
            "counterparty_id": counterparty_id, "run_id": run_id,
            "reason": "no candidate at or above T_LO", "adjudicated": False,
        })
        return Decision(counterparty_id, None, "CLEAR", "no candidate above T_LO",
                        0, 0.0, "SKIPPED")

    match = results[0]

    # Between T_LO and T_HI: surfaced and recorded, but not strong enough to spend an
    # adjudication on. Recorded rather than dropped so the console can show near misses.
    if match.score < T_HI:
        _persist_match(conn, run_id, counterparty_id, match)
        emit(conn, "SCREENED_CLEAR", {
            "counterparty_id": counterparty_id, "run_id": run_id,
            "sdn_uid": match.sdn_uid, "det_score": float(match.score),
            "reason": "below adjudication threshold", "adjudicated": False,
        })
        return Decision(counterparty_id, match.sdn_uid, "CLEAR",
                        "below T_HI", 0, match.score, "SKIPPED")

    entry = matcher.entries[match.sdn_uid]
    context = build_context(name, dob, "Individual" if is_person else "Entity",
                            match, entry, publication)
    match_id = _persist_match(conn, run_id, counterparty_id, match)

    feedback: str | None = None
    verdict = None
    guard_result = "DISAGREE"

    for attempt in range(1, MAX_ROUND_TRIPS + 1):
        try:
            verdict = adjudicator.adjudicate(context, feedback=feedback)
        except Exception as exc:
            # A model failure must never become a silent CLEAR.
            _quarantine(conn, match_id, "PARSE_ERROR", {
                "counterparty_id": counterparty_id, "error": str(exc)[:500],
                "attempt": attempt,
            })
            return Decision(counterparty_id, match.sdn_uid, "QUARANTINE",
                            f"adjudicator failed: {exc}"[:200], attempt,
                            match.score, "DISAGREE")

        guard_result, complaint = guard(match, verdict)
        if guard_result == "AGREE":
            break
        feedback = complaint

    adjudication_id = _persist_adjudication(
        conn, match_id, verdict, guard_result, attempt, oracle_verdict)

    # Still disagreeing after the cap: stop, do not guess a third time, escalate.
    if guard_result != "AGREE":
        _quarantine(conn, match_id, "ORACLE_DISAGREE" if attempt < MAX_ROUND_TRIPS
                    else "LOOP_CAP", {
            "counterparty_id": counterparty_id,
            "adjudication_id": adjudication_id,
            "det_score": float(match.score),
            "model_verdict": verdict.verdict,
            "complaint": feedback,
            "round_trips": attempt,
        })
        return Decision(counterparty_id, match.sdn_uid, "QUARANTINE",
                        feedback or "oracle guard disagreed", attempt,
                        match.score, guard_result, adjudication_id)

    if verdict.verdict == "HOLD":
        result = place_hold(conn, counterparty_id=counterparty_id,
                            adjudication_id=adjudication_id, sdn_uid=match.sdn_uid,
                            blocked_on=blocked_on)
        return Decision(counterparty_id, match.sdn_uid, "HOLD", verdict.rationale,
                        attempt, match.score, guard_result, adjudication_id,
                        result.hold_id)

    emit(conn, "SCREENED_CLEAR", {
        "counterparty_id": counterparty_id, "run_id": run_id,
        "sdn_uid": match.sdn_uid, "det_score": float(match.score),
        "adjudication_id": adjudication_id, "adjudicated": True,
        "rationale": verdict.rationale,
    })
    return Decision(counterparty_id, match.sdn_uid, "CLEAR", verdict.rationale,
                    attempt, match.score, guard_result, adjudication_id)
