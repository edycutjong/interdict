"""Orchestrator tests -- the oracle guard, the loop cap, and quarantine.

These run entirely offline against RuleBasedAdjudicator and hand-built verdicts, which
is the point: the guard is a property of the routing boundary, not of the model, so it
must be testable without one.
"""

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import psycopg
import pytest

from interdict.adjudicator import (
    RuleBasedAdjudicator,
    TransientAdjudicationError,
    Verdict,
)
from interdict.db import DSN, connect, relay
from interdict.matcher import T_HI, T_LO, Matcher
from interdict.ofac import Name, SdnEntry
from interdict.orchestrator import (
    GUARD_CLEAR_CEILING,
    MAX_ROUND_TRIPS,
    guard,
    screen_counterparty,
)

PUBLICATION = {"publish_date": "08/07/2026", "record_count": "19199"}


def _db_available() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


def _entry(uid="2674", sdn_type="Individual", name="Abu ABBAS", dobs=("10 Dec 1948",)):
    return SdnEntry(uid=uid, sdn_type=sdn_type, primary_name=name,
                    names=(Name(name, "primary", "primary"),),
                    programs=("SDGT",), dobs=tuple(dobs), nationalities=())


def _verdict(verdict="HOLD", identifier="Abu ABBAS",
             rationale="Primary name matches the SDN record exactly and nothing contradicts it."):
    return Verdict(verdict=verdict, rationale=rationale, matched_identifier=identifier,
                   confidence=0.9, model_id="test", prompt_hash="h", context={})


# ---------------------------------------------------------------------------
# The oracle guard, in isolation
# ---------------------------------------------------------------------------

def _match(score=0.97, **overrides):
    m = Matcher([_entry()]).screen("Abu ABBAS", is_person=True)[0]
    comps = replace(m.components, **overrides) if overrides else m.components
    return replace(m, score=score, components=comps)


def test_guard_accepts_a_supported_hold():
    result, complaint = guard(_match(0.97), _verdict("HOLD"))
    assert result == "AGREE" and complaint is None


def test_guard_rejects_a_clear_on_a_near_identical_name():
    """The hallucination that would matter most: clearing someone who plainly matches."""
    result, complaint = guard(_match(0.99), _verdict("CLEAR"))
    assert result == "DISAGREE"
    assert str(GUARD_CLEAR_CEILING) in complaint


def test_guard_allows_a_clear_when_the_dob_contradicts():
    """Above the ceiling, but with real evidence of a different party -- that is the
    case the model is FOR, so the guard must not block it."""
    result, _ = guard(_match(0.99, dob_signal="disjoint"), _verdict("CLEAR"))
    assert result == "AGREE"


def test_guard_allows_a_clear_on_an_entity_type_mismatch():
    result, _ = guard(_match(0.99, type_signal="mismatch"), _verdict("CLEAR"))
    assert result == "AGREE"


def test_guard_rejects_a_hold_below_the_no_hit_floor():
    result, complaint = guard(_match(0.30), _verdict("HOLD"))
    assert result == "DISAGREE" and "below the no-hit floor" in complaint


def test_guard_rejects_a_fabricated_identifier():
    """A made-up alias in a federal blocking report is the worst possible output."""
    result, complaint = guard(_match(0.97), _verdict("HOLD", identifier="Ali AL-INVENTED"))
    assert result == "DISAGREE" and "does not appear in the SDN record" in complaint


def test_guard_rejects_an_empty_identifier():
    result, complaint = guard(_match(0.97), _verdict("HOLD", identifier="  "))
    assert result == "DISAGREE" and "no matched_identifier" in complaint


def test_guard_accepts_a_substring_identifier():
    """Quoting part of the matched name is honest quoting, not fabrication."""
    result, _ = guard(_match(0.97), _verdict("HOLD", identifier="ABBAS"))
    assert result == "AGREE"


def test_guard_rejects_a_rationale_too_thin_to_file():
    result, complaint = guard(_match(0.97), _verdict("HOLD", rationale="match"))
    assert result == "DISAGREE" and "blocking report" in complaint


# ---------------------------------------------------------------------------
# End to end through the database
# ---------------------------------------------------------------------------

pytestmark_db = pytest.mark.skipif(not _db_available(), reason="Postgres not running")


@pytest.fixture
def conn():
    with connect() as c:
        with c.cursor() as cur:
            cur.execute("TRUNCATE outbox, quarantine, holds, adjudications, matches, "
                        "rescreen_batches, rescreen_runs, disbursements, counterparties "
                        "RESTART IDENTITY CASCADE")
        c.commit()
        yield c
        c.rollback()


def _setup(conn, name="Abu ABBAS", amount=100_000):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO counterparties (external_ref,name,origin,source) "
                    "VALUES ('cp',%s,'sentinel','test') RETURNING id", (name,))
        cp = cur.fetchone()["id"]
        cur.execute("INSERT INTO disbursements (counterparty_id,amount_cents) "
                    "VALUES (%s,%s)", (cp, amount))
        cur.execute("INSERT INTO list_versions (published_at,source_hash,kind) "
                    "VALUES ('2026-08-07','h','SDN') RETURNING id")
        lv = cur.fetchone()["id"]
        cur.execute("INSERT INTO rescreen_runs (list_version_id,trigger) "
                    "VALUES (%s,'DELTA') RETURNING id", (lv,))
        run = cur.fetchone()["id"]
    return cp, run


def _screen(conn, cp, run, matcher, adjudicator, name="Abu ABBAS", dob=None,
            second_opinion=None):
    return screen_counterparty(
        conn, run_id=run, counterparty_id=cp, name=name, dob=dob, is_person=True,
        matcher=matcher, adjudicator=adjudicator, publication=PUBLICATION,
        blocked_on=date(2026, 8, 17), second_opinion=second_opinion)


@pytestmark_db
def test_hold_verdict_freezes_the_money(conn):
    cp, run = _setup(conn)
    decision = _screen(conn, cp, run, Matcher([_entry()]), RuleBasedAdjudicator("HOLD"))
    assert decision.verdict == "HOLD" and decision.guard == "AGREE"
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM disbursements WHERE counterparty_id=%s", (cp,))
        assert cur.fetchone()["state"] == "HELD"


@pytestmark_db
def test_no_candidate_never_calls_the_model(conn):
    """An auto-no-hit is both the cheap path and the safe one."""
    cp, run = _setup(conn, name="Jennifer Marie Thompson")

    class Exploding:
        model_id = "never"

        def adjudicate(self, context, *, feedback=None):
            raise AssertionError("the model must not be called below T_LO")

    decision = _screen(conn, cp, run, Matcher([_entry()]), Exploding(),
                       name="Jennifer Marie Thompson")
    assert decision.verdict == "CLEAR" and decision.guard == "SKIPPED"


@pytestmark_db
def test_a_near_miss_is_recorded_but_never_adjudicated(conn):
    """Between T_LO and T_HI: worth showing an operator, not worth an adjudication.

    Recorded rather than dropped, so the console can show near misses -- but the model
    is not called and no adjudication row is written.
    """
    cp, run = _setup(conn, name="Abu Habbas")

    class Exploding:
        model_id = "never"

        def adjudicate(self, context, *, feedback=None):
            raise AssertionError("a near miss must not be sent to the model")

    decision = _screen(conn, cp, run, Matcher([_entry()]), Exploding(), name="Abu Habbas")

    assert decision.verdict == "CLEAR" and decision.guard == "SKIPPED"
    assert decision.reason == "below T_HI"
    assert decision.sdn_uid == "2674", "the near miss must be attributed, not anonymous"
    assert T_LO <= decision.det_score < T_HI
    with conn.cursor() as cur:
        cur.execute("SELECT sdn_uid, det_score FROM matches WHERE counterparty_id=%s", (cp,))
        row = cur.fetchone()
        assert row is not None, "a near miss must survive for the console to show it"
        assert row["sdn_uid"] == "2674"
        cur.execute("SELECT count(*) AS n FROM adjudications")
        assert cur.fetchone()["n"] == 0, "a near miss must not spend an adjudication"
        cur.execute("SELECT payload FROM outbox WHERE topic='SCREENED_CLEAR'")
        payload = cur.fetchone()["payload"]
    assert payload["adjudicated"] is False
    assert payload["sdn_uid"] == "2674"
    assert payload["reason"] == "below adjudication threshold"


@pytestmark_db
def test_a_supported_clear_is_honoured_and_its_rationale_recorded(conn):
    """The band between T_HI and the guard ceiling is where the model has discretion.

    A CLEAR there passes the guard, so it must be obeyed -- written down with its
    rationale, with no hold placed and nothing frozen.
    """
    cp, run = _setup(conn, name="Abu Abbas Khalid")
    decision = _screen(conn, cp, run, Matcher([_entry()]),
                       RuleBasedAdjudicator("CLEAR"), name="Abu Abbas Khalid")

    assert decision.verdict == "CLEAR" and decision.guard == "AGREE"
    assert T_HI <= decision.det_score < GUARD_CLEAR_CEILING, "not the discretion band"
    assert decision.round_trips == 1, "an accepted verdict must not be re-asked"
    assert decision.adjudication_id is not None
    assert decision.hold_id is None
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM holds")
        assert cur.fetchone()["n"] == 0, "a cleared counterparty must not be held"
        cur.execute("SELECT verdict, oracle_guard_result FROM adjudications")
        row = cur.fetchone()
        assert (row["verdict"], row["oracle_guard_result"]) == ("CLEAR", "AGREE")
        cur.execute("SELECT state FROM disbursements WHERE counterparty_id=%s", (cp,))
        assert cur.fetchone()["state"] == "QUEUED"
        cur.execute("SELECT payload FROM outbox WHERE topic='SCREENED_CLEAR'")
        payload = cur.fetchone()["payload"]
    assert payload["adjudicated"] is True
    assert payload["adjudication_id"] == decision.adjudication_id
    # The signed reason travels with the event, not just into the adjudications table.
    assert payload["rationale"] == decision.reason


@pytestmark_db
def test_an_unreachable_model_is_quarantined_as_retryable(conn):
    """A rate limit is infrastructure, not a suspected hallucination.

    Money still must not move -- but the queue has to say which failure this was, because
    only this one is fixed by waiting.
    """
    cp, run = _setup(conn)

    class Unreachable:
        model_id = "unreachable"

        def adjudicate(self, context, *, feedback=None):
            raise TransientAdjudicationError(
                "model unreachable after 5 attempts: 429 RESOURCE_EXHAUSTED")

    decision = _screen(conn, cp, run, Matcher([_entry()]), Unreachable())

    assert decision.verdict == "QUARANTINE"
    assert decision.guard == "UNAVAILABLE", "an outage must not be filed as a disagreement"
    assert decision.round_trips == 1, "an outage must not burn the whole retry budget"
    assert "unreachable" in decision.reason
    with conn.cursor() as cur:
        cur.execute("SELECT reason, payload FROM quarantine")
        row = cur.fetchone()
        assert row["reason"] == "ADJUDICATOR_UNAVAILABLE"
        assert row["payload"]["retryable"] is True
        cur.execute("SELECT count(*) AS n FROM adjudications")
        assert cur.fetchone()["n"] == 0, "no decision was made, so none may be recorded"
        cur.execute("SELECT state FROM disbursements WHERE counterparty_id=%s", (cp,))
        assert cur.fetchone()["state"] == "QUEUED"


@pytestmark_db
def test_a_zero_round_trip_cap_quarantines_instead_of_deciding(conn, monkeypatch):
    """A misconfigured cap must not write an adjudication with no decision in it.

    Unreachable while MAX_ROUND_TRIPS is 2, which is the point: the branch is what stops
    a config change turning into a hold nobody can inspect.
    """
    monkeypatch.setattr("interdict.orchestrator.MAX_ROUND_TRIPS", 0)
    cp, run = _setup(conn)

    decision = _screen(conn, cp, run, Matcher([_entry()]), RuleBasedAdjudicator("HOLD"))

    assert decision.verdict == "QUARANTINE"
    assert decision.reason == "no adjudication attempted"
    assert decision.round_trips == 0
    with conn.cursor() as cur:
        cur.execute("SELECT reason, payload FROM quarantine")
        row = cur.fetchone()
        assert row["reason"] == "SCHEMA_INVALID"
        assert "MAX_ROUND_TRIPS=0" in row["payload"]["error"]
        cur.execute("SELECT count(*) AS n FROM adjudications")
        assert cur.fetchone()["n"] == 0, "an empty adjudication is the hole this prevents"
        cur.execute("SELECT state FROM disbursements WHERE counterparty_id=%s", (cp,))
        assert cur.fetchone()["state"] == "QUEUED"


@pytestmark_db
def test_the_round_trip_cap_is_also_a_database_constraint(conn):
    """The cap holds even if this module is wrong: a third round trip cannot be stored."""
    cp, run = _setup(conn)
    _screen(conn, cp, run, Matcher([_entry()]), RuleBasedAdjudicator("HOLD"))
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM matches")
        match_id = cur.fetchone()["id"]

    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO adjudications (match_id, verdict, rationale, model_id, "
                "prompt_hash, context, oracle_guard_result, round_trips) "
                "VALUES (%s,'HOLD','r','m','h','{}','AGREE',%s)",
                (match_id, MAX_ROUND_TRIPS + 1),
            )
    conn.rollback()


@pytestmark_db
def test_unsupported_clear_is_quarantined_not_obeyed(conn):
    """The headline failure-tolerance case: the model says CLEAR on an exact match.

    The guard must refuse it, ask once more, and then quarantine -- never release the
    money on an answer the deterministic plane cannot support.
    """
    cp, run = _setup(conn)
    decision = _screen(conn, cp, run, Matcher([_entry()]), RuleBasedAdjudicator("CLEAR"))

    assert decision.verdict == "QUARANTINE"
    assert decision.round_trips == MAX_ROUND_TRIPS
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM disbursements WHERE counterparty_id=%s", (cp,))
        assert cur.fetchone()["state"] == "QUEUED", "money moved on a refused verdict"
        cur.execute("SELECT reason FROM quarantine")
        assert cur.fetchone()["reason"] == "LOOP_CAP"


@pytestmark_db
def test_round_trips_never_exceed_the_cap(conn):
    cp, run = _setup(conn)
    calls = []

    class Stubborn:
        model_id = "stubborn"

        def adjudicate(self, context, *, feedback=None):
            calls.append(feedback)
            return _verdict("CLEAR")

    _screen(conn, cp, run, Matcher([_entry()]), Stubborn())
    assert len(calls) == MAX_ROUND_TRIPS
    # The second call must be told what disagreed, not merely asked again.
    assert calls[0] is None and calls[1] is not None


@pytestmark_db
def test_adjudicator_failure_never_becomes_a_silent_clear(conn):
    cp, run = _setup(conn)

    class Broken:
        model_id = "broken"

        def adjudicate(self, context, *, feedback=None):
            raise RuntimeError("503 model overloaded")

    decision = _screen(conn, cp, run, Matcher([_entry()]), Broken())
    assert decision.verdict == "QUARANTINE"
    with conn.cursor() as cur:
        cur.execute("SELECT reason FROM quarantine")
        assert cur.fetchone()["reason"] == "PARSE_ERROR"
        cur.execute("SELECT state FROM disbursements WHERE counterparty_id=%s", (cp,))
        assert cur.fetchone()["state"] == "QUEUED"


@pytestmark_db
def test_decisions_are_recorded_with_their_guard_result(conn):
    cp, run = _setup(conn)
    _screen(conn, cp, run, Matcher([_entry()]), RuleBasedAdjudicator("HOLD"))
    with conn.cursor() as cur:
        cur.execute("SELECT oracle_guard_result, round_trips, context FROM adjudications")
        row = cur.fetchone()
    assert row["oracle_guard_result"] == "AGREE"
    assert row["round_trips"] == 1
    # The full model input is persisted so `make replay` is bit-for-bit.
    assert row["context"]["sdn_uid"] == "2674"


@pytestmark_db
def test_hold_reaches_the_ledger(conn):
    cp, run = _setup(conn)
    _screen(conn, cp, run, Matcher([_entry()]), RuleBasedAdjudicator("HOLD"))
    relay(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT event_type FROM ledger ORDER BY seq")
        assert "HOLD_PLACED" in [r["event_type"] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# The second model: recorded evidence, never a vote
#
# Everything below asserts the same boundary from a different angle -- Gemma's answer
# lands in a column and changes nothing else. If any of these start passing while the
# second opinion influences a verdict, the separation has been lost.
# ---------------------------------------------------------------------------

class _Opinion:
    """Stands in for GemmaSecondOpinion. Returns whatever it was handed."""

    def __init__(self, verdict=None, boom=None):
        self._verdict, self._boom, self.asked = verdict, boom, []

    def opine(self, context):
        self.asked.append(context)
        if self._boom is not None:
            raise self._boom
        if self._verdict is None:
            return None
        return SimpleNamespace(verdict=self._verdict, rationale="because",
                               model_id="gemma-4-31b-it")


def _gemma_col(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT gemma_verdict FROM adjudications ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        return row["gemma_verdict"] if row else None


@pytestmark_db
def test_an_agreeing_second_model_is_recorded_as_agreeing(conn):
    cp, run = _setup(conn)
    matcher = Matcher([_entry()])
    op = _Opinion("HOLD")
    d = _screen(conn, cp, run, matcher, RuleBasedAdjudicator("HOLD"), second_opinion=op)
    assert d.verdict == "HOLD"
    col = _gemma_col(conn)
    assert col.startswith("HOLD AGREE") and "gemma-4-31b-it" in col
    assert len(op.asked) == 1, "the second model is asked exactly once per adjudication"


@pytestmark_db
def test_a_disagreeing_second_model_is_recorded_and_changes_nothing(conn):
    # The point of the whole feature. Gemma says CLEAR, Gemini says HOLD, the money
    # still stops -- divergence is a signal for a human, not a vote that can free funds.
    cp, run = _setup(conn)
    matcher = Matcher([_entry()])
    d = _screen(conn, cp, run, matcher, RuleBasedAdjudicator("HOLD"),
                second_opinion=_Opinion("CLEAR"))
    assert d.verdict == "HOLD", "a dissenting second model must not release money"
    assert _gemma_col(conn).startswith("CLEAR DISAGREE")
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM disbursements WHERE counterparty_id=%s", (cp,))
        assert cur.fetchone()["state"] == "HELD"
        cur.execute("SELECT count(*) AS n FROM quarantine")
        assert cur.fetchone()["n"] == 0, "disagreement is evidence, not a quarantine route"


@pytestmark_db
def test_an_unreachable_second_model_leaves_the_column_null(conn):
    cp, run = _setup(conn)
    matcher = Matcher([_entry()])
    d = _screen(conn, cp, run, matcher, RuleBasedAdjudicator("HOLD"),
                second_opinion=_Opinion(None))
    assert d.verdict == "HOLD"
    # NULL reads as "not asked or unreachable". Recording an outage as agreement is the
    # exact bug the yente path is written to avoid.
    assert _gemma_col(conn) is None


@pytestmark_db
def test_no_second_model_is_the_default_and_leaves_the_column_null(conn):
    cp, run = _setup(conn)
    matcher = Matcher([_entry()])
    assert _screen(conn, cp, run, matcher, RuleBasedAdjudicator("HOLD")).verdict == "HOLD"
    assert _gemma_col(conn) is None
