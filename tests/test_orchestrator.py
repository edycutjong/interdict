"""Orchestrator tests -- the oracle guard, the loop cap, and quarantine.

These run entirely offline against RuleBasedAdjudicator and hand-built verdicts, which
is the point: the guard is a property of the routing boundary, not of the model, so it
must be testable without one.
"""

from dataclasses import replace
from datetime import date

import psycopg
import pytest

from interdict.adjudicator import RuleBasedAdjudicator, Verdict
from interdict.db import DSN, connect, relay
from interdict.matcher import Matcher
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


def _screen(conn, cp, run, matcher, adjudicator, name="Abu ABBAS", dob=None):
    return screen_counterparty(
        conn, run_id=run, counterparty_id=cp, name=name, dob=dob, is_person=True,
        matcher=matcher, adjudicator=adjudicator, publication=PUBLICATION,
        blocked_on=date(2026, 8, 17))


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
