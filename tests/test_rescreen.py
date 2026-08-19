"""Full-book re-screen: checkpointing, resumability, and delta releases.

The kill-worker test is the one that matters. A crash mid-book must resume at the
unfinished range, because the alternative -- silently skipping counterparties while
reporting success -- is a compliance breach the system would never notice.
"""

from datetime import date

import psycopg
import pytest

from interdict.adjudicator import RuleBasedAdjudicator
from interdict.db import DSN, connect, relay, resume_point
from interdict.matcher import Matcher
from interdict.ofac import DeltaAction, Name, SdnEntry
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


def _screen(conn, run_id, **kwargs):
    return rescreen_book(conn, run_id=run_id, matcher=_matcher(),
                         adjudicator=RuleBasedAdjudicator(), publication=PUBLICATION,
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
