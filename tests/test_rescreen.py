"""Full-book re-screen: checkpointing, resumability, and delta releases.

The kill-worker test is the one that matters. A crash mid-book must resume at the
unfinished range, because the alternative -- silently skipping counterparties while
reporting success -- is a compliance breach the system would never notice.
"""

from datetime import date
from types import SimpleNamespace

import psycopg
import pytest

from interdict.adjudicator import RuleBasedAdjudicator
from interdict.db import DSN, connect, relay, resume_point
from interdict.matcher import Matcher
from interdict.ofac import DeltaAction, Name, SdnEntry
from interdict.oracle import OracleHit
from interdict.rescreen import apply_delta_removals, open_run, rescreen_book

PUBLICATION = {"publish_date": "08/07/2026", "record_count": "19199"}


def _db_available() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="Postgres not running")


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


def _matcher():
    return Matcher([
        SdnEntry(uid="2674", sdn_type="Individual", primary_name="Abu ABBAS",
                 names=(Name("Abu ABBAS", "primary", "primary"),),
                 programs=("SDGT",), dobs=()),
        SdnEntry(uid="4715", sdn_type="Entity", primary_name="SHINING PATH",
                 names=(Name("SHINING PATH", "primary", "primary"),),
                 programs=("FTO",), dobs=()),
    ])


def _book(conn, n_hits=6, n_clean=6):
    """A book of designated parties and unrelated grantees, with money queued."""
    with conn.cursor() as cur:
        for i in range(n_hits):
            cur.execute(
                "INSERT INTO counterparties (external_ref,name,entity_type,origin,"
                "expected_verdict,source) VALUES (%s,'Abu ABBAS','Individual',"
                "'sentinel','HOLD','test') RETURNING id", (f"hit-{i}",))
            cur.execute("INSERT INTO disbursements (counterparty_id,amount_cents) "
                        "VALUES (%s,10000)", (cur.fetchone()["id"],))
        for i in range(n_clean):
            cur.execute(
                "INSERT INTO counterparties (external_ref,name,entity_type,origin,"
                "expected_verdict,source) VALUES (%s,'Jennifer Marie Thompson',"
                "'Individual','ordinary','CLEAR','test') RETURNING id", (f"ok-{i}",))
            cur.execute("INSERT INTO disbursements (counterparty_id,amount_cents) "
                        "VALUES (%s,10000)", (cur.fetchone()["id"],))


def _run(conn, trigger="SCHEDULER", suffix=""):
    return open_run(conn, published_at=date(2026, 8, 7),
                    source_hash=f"hash{suffix}", kind="SDN", trigger=trigger,
                    record_count=19199)


def _screen(conn, run_id, adjudicator=None, **kwargs):
    return rescreen_book(conn, run_id=run_id, matcher=_matcher(),
                         adjudicator=adjudicator or RuleBasedAdjudicator(),
                         publication=PUBLICATION,
                         blocked_on=date(2026, 8, 17), **kwargs)


def test_full_book_is_screened(conn):
    _book(conn)
    summary = _screen(conn, _run(conn), batch_size=5)
    assert summary.screened == 12
    assert summary.holds == 6 and summary.cleared == 6


def test_run_is_closed_only_when_complete(conn):
    _book(conn)
    run = _run(conn)
    _screen(conn, run, batch_size=5)
    with conn.cursor() as cur:
        cur.execute("SELECT finished_at FROM rescreen_runs WHERE id=%s", (run,))
        assert cur.fetchone()["finished_at"] is not None


def test_killed_worker_leaves_the_run_open(conn):
    _book(conn, n_hits=10, n_clean=10)
    run = _run(conn)
    summary = _screen(conn, run, batch_size=5, stop_after_batches=1)

    assert summary.screened == 5
    with conn.cursor() as cur:
        cur.execute("SELECT finished_at FROM rescreen_runs WHERE id=%s", (run,))
        assert cur.fetchone()["finished_at"] is None, "an interrupted run reported success"


def test_resume_finishes_the_unscreened_remainder(conn):
    """Demo beat B5: kill a worker mid-book, resume, and lose nothing."""
    _book(conn, n_hits=10, n_clean=10)
    run = _run(conn)
    _screen(conn, run, batch_size=5, stop_after_batches=2)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM matches WHERE run_id=%s", (run,))
        partial = cur.fetchone()["n"]

    resumed = _screen(conn, run, batch_size=5)
    assert resumed.screened == 10          # the remaining two batches

    with conn.cursor() as cur:
        cur.execute("SELECT count(DISTINCT counterparty_id) AS n FROM matches "
                    "WHERE run_id=%s", (run,))
        assert cur.fetchone()["n"] >= partial
        cur.execute("SELECT count(*) AS n FROM rescreen_batches "
                    "WHERE run_id=%s AND completed_at IS NULL", (run,))
        assert cur.fetchone()["n"] == 0
    assert resume_point(conn, run) is None


def test_no_counterparty_is_skipped_across_a_crash(conn):
    """The failure a scalar cursor would cause, asserted head on."""
    _book(conn, n_hits=10, n_clean=10)
    run = _run(conn)
    _screen(conn, run, batch_size=4, stop_after_batches=2)
    _screen(conn, run, batch_size=4)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM counterparties")
        total = cur.fetchone()["n"]
        # Every designated party in the book must have ended up held.
        cur.execute("SELECT count(*) AS n FROM disbursements d "
                    "JOIN counterparties c ON c.id=d.counterparty_id "
                    "WHERE c.expected_verdict='HOLD' AND d.state<>'HELD'")
        assert cur.fetchone()["n"] == 0, "a designated party was skipped by the resume"
    assert total == 20


def test_checkpoints_survive_a_process_death(conn):
    """The checkpoint has to outlive the process, not just the function call.

    `test_no_counterparty_is_skipped_across_a_crash` stops gracefully and resumes on the
    same connection, so it passes whether or not anything was ever committed. That is
    the one case resumability does not need. This asserts the real one: work done before
    the crash is durable, visible to a *different* connection, and still there after
    everything uncommitted is thrown away.

    It fails if rescreen_book batches the whole run into a single transaction -- which
    it did until the per-batch commit was added, meaning a SIGKILL rolled the checkpoints
    back along with the decisions and the resume had nothing to resume from.
    """
    _book(conn, n_hits=10, n_clean=10)
    run = _run(conn)
    _screen(conn, run, batch_size=4, stop_after_batches=2)

    # The crash: discard everything this connection has not committed.
    conn.rollback()

    with connect() as other:
        with other.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM rescreen_batches "
                "WHERE run_id = %s AND completed_at IS NOT NULL", (run,))
            assert cur.fetchone()["n"] == 2, "checkpoints did not survive the crash"

            cur.execute(
                "SELECT count(*) AS n FROM matches WHERE run_id = %s", (run,))
            assert cur.fetchone()["n"] > 0, "decisions did not survive the crash"

        # And the run is resumable from that durable state -- coverage stops at 8 of 20.
        assert resume_point(other, run) is None
        with other.cursor() as cur:
            cur.execute("SELECT max(batch_end) AS covered FROM rescreen_batches "
                        "WHERE run_id = %s", (run,))
            assert cur.fetchone()["covered"] == 8


def test_abandoned_batch_is_reclaimed_not_stepped_over(conn):
    """A worker that dies MID-batch leaves a claimed, uncompleted range.

    Allocating only from max(batch_end) would step over it forever: the range stays
    claimed, never completes, the run can never close, and those counterparties are
    never screened against a live sanctions list. Re-claiming is safe because screening
    is idempotent.
    """
    _book(conn, n_hits=10, n_clean=10)
    run = _run(conn)

    # Simulate the crash directly: claim a batch and never complete it.
    from interdict.db import claim_batch
    first = claim_batch(conn, run, 5, 20)
    assert first == (1, 5)

    # The very next claim must return to the abandoned range, not skip to 6.
    assert claim_batch(conn, run, 5, 20) == (1, 5)

    # And a full re-screen must eventually cover the whole book and close the run.
    _screen(conn, run, batch_size=5)
    assert resume_point(conn, run) is None
    with conn.cursor() as cur:
        cur.execute("SELECT finished_at FROM rescreen_runs WHERE id=%s", (run,))
        assert cur.fetchone()["finished_at"] is not None
        cur.execute("SELECT count(*) AS n FROM disbursements d "
                    "JOIN counterparties c ON c.id=d.counterparty_id "
                    "WHERE c.expected_verdict='HOLD' AND d.state<>'HELD'")
        assert cur.fetchone()["n"] == 0


def test_incomplete_run_reports_short_coverage(conn):
    """Stopping between batches leaves nothing 'incomplete' but the book unscreened."""
    from interdict.db import run_is_complete
    _book(conn, n_hits=10, n_clean=10)
    run = _run(conn)
    _screen(conn, run, batch_size=5, stop_after_batches=1)

    # Nothing outstanding...
    assert resume_point(conn, run) is None
    # ...but the run is emphatically not complete.
    assert not run_is_complete(conn, run, 20)


def test_rescreen_is_idempotent_across_runs(conn):
    """A second publication re-screens the book without double-holding."""
    _book(conn)
    _screen(conn, _run(conn, suffix="-a"), batch_size=5)
    _screen(conn, _run(conn, suffix="-b"), batch_size=5)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM holds WHERE released_at IS NULL")
        assert cur.fetchone()["n"] == 6


def test_empty_book_is_not_an_error(conn):
    assert _screen(conn, _run(conn), batch_size=5).screened == 0


# ---------------------------------------------------------------------------
# The independent oracle -- graded, batched, and never fatal
# ---------------------------------------------------------------------------

class _StubOracle:
    """Stands in for yente: records what it was asked, answers what it was told to.

    Keyed by counterparty id so a verdict landing on the wrong row is visible.
    """

    def __init__(self, answers=None):
        self._answers = answers or {}
        self.asked: list[dict] = []

    def match(self, queries):
        self.asked.append(queries)
        return {key: self._answers.get(int(key), []) for key in queries}


def _hit(uid="2674", score=0.95):
    return OracleHit(sdn_uid=uid, score=score, caption="ABBAS, Abu", canonical_id="Q1")


def _add(conn, ref, name, entity_type="Individual", dob=None, origin="sentinel"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO counterparties (external_ref,name,entity_type,dob,origin,source) "
            "VALUES (%s,%s,%s,%s,%s,'test') RETURNING id",
            (ref, name, entity_type, dob, origin))
        return cur.fetchone()["id"]


def _yente_verdicts(conn):
    """Every adjudication's recorded oracle verdict, in counterparty order."""
    with conn.cursor() as cur:
        cur.execute("SELECT m.counterparty_id, a.yente_verdict FROM adjudications a "
                    "JOIN matches m ON m.id = a.match_id ORDER BY m.counterparty_id")
        return [(r["counterparty_id"], r["yente_verdict"]) for r in cur.fetchall()]


def test_the_oracle_verdict_is_recorded_beside_the_right_decision(conn):
    """An oracle consulted selectively -- or misfiled -- is not an oracle.

    The verdict is keyed by counterparty id, so a wrong mapping would grade one
    grantee's decision with another's evidence.
    """
    _book(conn, n_hits=3, n_clean=0)
    oracle = _StubOracle({1: [_hit(score=0.95)], 2: [_hit(uid="4715", score=0.9412)]})

    _screen(conn, _run(conn), batch_size=5, oracle=oracle)

    # id 3 was screened too -- the oracle simply found nothing, which is itself a
    # recorded grade and must not be confused with "not asked".
    assert _yente_verdicts(conn) == [
        (1, "HIT 2674 0.950"), (2, "HIT 4715 0.941"), (3, "NO HIT")]


def test_the_oracle_is_asked_about_the_whole_batch_at_once(conn):
    """Batched deliberately: a per-row HTTP call is slow enough that people stop
    grading daily, and a grade you stop collecting is worth nothing."""
    _add(conn, "hit-0", "Abu ABBAS", dob="10 Dec 1948")
    _add(conn, "org-0", "SHINING PATH", entity_type="Entity")
    _add(conn, "ok-0", "Jennifer Marie Thompson", origin="ordinary")
    oracle = _StubOracle()

    _screen(conn, _run(conn), batch_size=10, oracle=oracle)

    assert len(oracle.asked) == 1, "the oracle was called per row, not per batch"
    asked = oracle.asked[0]
    assert set(asked) == {"1", "2", "3"}
    assert asked["1"] == {"name": "Abu ABBAS", "schema": "Person", "dob": "10 Dec 1948"}
    # A designated organisation asked about as a Person grades against the wrong index.
    assert asked["2"]["schema"] == "Organization"
    assert asked["3"]["schema"] == "Person"


def test_the_oracle_is_asked_once_per_batch(conn):
    _book(conn, n_hits=2, n_clean=2)
    oracle = _StubOracle()

    _screen(conn, _run(conn), batch_size=2, oracle=oracle)

    assert [sorted(q) for q in oracle.asked] == [["1", "2"], ["3", "4"]]


def test_an_unreachable_oracle_never_stops_the_run(conn):
    """The oracle is evidence, not a dependency. yente being down must not stop
    money being held against a live sanctions list."""
    class Down:
        def match(self, queries):
            raise RuntimeError("connection refused")

    _book(conn, n_hits=3, n_clean=3)
    summary = _screen(conn, _run(conn), batch_size=2, oracle=Down())

    assert summary.screened == 6 and summary.holds == 3 and summary.cleared == 3
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM disbursements WHERE state='HELD'")
        assert cur.fetchone()["n"] == 3
    # ...and the column is simply absent. A failed lookup must never be recorded as
    # a grade, because "NO HIT" would read as the oracle actively disagreeing.
    assert _yente_verdicts(conn) == [(1, None), (2, None), (3, None)]


def test_without_an_oracle_no_verdict_is_invented(conn):
    _book(conn, n_hits=2, n_clean=0)
    _screen(conn, _run(conn), batch_size=5)
    assert _yente_verdicts(conn) == [(1, None), (2, None)]


# ---------------------------------------------------------------------------
# Progress reporting and the summary counts
# ---------------------------------------------------------------------------

def test_each_decision_is_reported_as_it_is_made(conn):
    """A full-book run printed nothing until the closing summary, which left an
    operator unable to tell a working run from a hung one."""
    _book(conn, n_hits=2, n_clean=2)
    seen = []

    _screen(conn, _run(conn), batch_size=2,
            on_decision=lambda name, d: seen.append((name, d.counterparty_id, d.verdict)))

    assert [(n, i) for n, i, _ in seen] == [
        ("Abu ABBAS", 1), ("Abu ABBAS", 2),
        ("Jennifer Marie Thompson", 3), ("Jennifer Marie Thompson", 4)]
    # What was reported must be what the ledger stored -- the callback reports the
    # Decision's own fields precisely so the two cannot drift.
    with conn.cursor() as cur:
        cur.execute("SELECT counterparty_id FROM holds ORDER BY counterparty_id")
        held = [r["counterparty_id"] for r in cur.fetchall()]
    assert [i for _, i, v in seen if v == "HOLD"] == held


def test_a_refused_verdict_is_counted_as_quarantined_not_cleared(conn):
    """The model says CLEAR on an exact match; the guard refuses it.

    Summarising that as a clear would report an unresolved escalation as a
    successful screening -- the run would look clean while three grantees sit in
    quarantine with their money untouched.
    """
    _book(conn, n_hits=3, n_clean=2)

    summary = _screen(conn, _run(conn), batch_size=5,
                      adjudicator=RuleBasedAdjudicator("CLEAR"))

    assert summary.screened == 5
    assert summary.quarantined == 3
    assert summary.cleared == 2, "a refused verdict was summarised as a clear"
    assert summary.holds == 0
    assert summary.adjudicated == 3
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM quarantine")
        assert cur.fetchone()["n"] == 3
        cur.execute("SELECT count(*) AS n FROM disbursements WHERE state<>'QUEUED'")
        assert cur.fetchone()["n"] == 0, "money moved on a refused verdict"


def test_the_summary_counts_only_the_decisions_a_model_actually_made(conn):
    """`adjudicated` is the count of decisions that cost a model call. The
    auto-no-hit path never reaches the adjudicator and must not inflate it."""
    _book(conn, n_hits=3, n_clean=3)

    summary = _screen(conn, _run(conn), batch_size=5)

    assert summary.screened == 6
    assert summary.adjudicated == 3
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM adjudications")
        assert cur.fetchone()["n"] == summary.adjudicated


# ---------------------------------------------------------------------------
# Delta removals -> release
# ---------------------------------------------------------------------------

def _removal(uid="2674"):
    return DeltaAction(uid=uid, action="remove", name="Abu ABBAS",
                       entity_type="Individual", programs=("SDGT",))


def test_delisting_releases_the_held_money(conn):
    _book(conn, n_hits=3, n_clean=0)
    _screen(conn, _run(conn), batch_size=5)

    released = apply_delta_removals(conn, [_removal()], delta_source_hash="delta-abc")
    assert len(released) == 3
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM disbursements WHERE state='CLEARED'")
        assert cur.fetchone()["n"] == 3


def test_release_matches_on_uid_not_name(conn):
    """The party released must be the party Treasury actually removed."""
    _book(conn, n_hits=3, n_clean=0)
    _screen(conn, _run(conn), batch_size=5)

    released = apply_delta_removals(conn, [_removal(uid="9999")],
                                    delta_source_hash="delta-abc")
    assert released == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM disbursements WHERE state='HELD'")
        assert cur.fetchone()["n"] == 3


def test_release_is_recorded_in_the_ledger(conn):
    _book(conn, n_hits=2, n_clean=0)
    _screen(conn, _run(conn), batch_size=5)
    apply_delta_removals(conn, [_removal()], delta_source_hash="delta-9403f40d")
    relay(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM ledger WHERE event_type='HOLD_RELEASED' "
                    "ORDER BY seq DESC LIMIT 1")
        assert cur.fetchone()["payload"]["authorised_by_delta"] == "delta-9403f40d"


def test_no_removals_is_a_noop(conn):
    _book(conn, n_hits=2, n_clean=0)
    _screen(conn, _run(conn), batch_size=5)
    assert apply_delta_removals(conn, [], delta_source_hash="d") == []


def test_the_second_model_reaches_the_book_and_not_just_the_unit_test(conn):
    """The regression test for a wiring bug the unit tests could not see.

    `GemmaSecondOpinion` was correct, `screen_counterparty` accepted the provider, and
    every unit test passed -- while `run_rescreen.py` built the provider and never handed
    it to `rescreen_book`. End to end the column stayed NULL for a whole 59-adjudication
    run, and because the failure path was silent it read as "Gemma declined 59 times".

    This asserts the seam those tests skipped: a provider given to rescreen_book must
    arrive at every adjudication.
    """
    class _Stub:
        def __init__(self):
            self.calls = 0

        def opine(self, context):
            self.calls += 1
            return SimpleNamespace(verdict="HOLD", rationale="r", model_id="gemma-test")

    _book(conn)
    stub = _Stub()
    summary = _screen(conn, _run(conn), batch_size=5, second_opinion=stub)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n, count(gemma_verdict) AS g FROM adjudications")
        row = cur.fetchone()

    assert row["n"] > 0, "no adjudication happened; this test would pass vacuously"
    assert row["g"] == row["n"], "an adjudication was written with no second opinion"
    assert stub.calls == row["n"], "the provider was not consulted once per adjudication"
    assert summary.holds > 0
