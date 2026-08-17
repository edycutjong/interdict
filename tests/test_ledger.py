"""Ledger, state-machine and checkpoint tests.

These are the correctness tests that matter. A lost hold, a forked audit trail, or a
silently-unscreened counterparty are all compliance breaches -- so each is asserted
against the real database rather than a mock.

Requires the local stack: `make up`.
"""

import json
import threading

import psycopg
import pytest

from interdict.db import (DSN, claim_batch, complete_batch, connect, emit, relay,
                          resume_point, verify_chain)


def _db_available() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(), reason="Postgres not running -- `make up` first"
)


@pytest.fixture
def conn():
    with connect() as c:
        yield c
        c.rollback()


@pytest.fixture
def clean():
    """Truncate everything the ledger triggers permit truncating, then reset.

    The ledger itself refuses TRUNCATE by design, so tests that need an empty chain
    run inside a rolled-back transaction rather than by wiping it.
    """
    with connect() as c:
        with c.cursor() as cur:
            cur.execute("TRUNCATE outbox, quarantine, holds, adjudications, matches, "
                        "rescreen_batches, rescreen_runs, disbursements, counterparties "
                        "RESTART IDENTITY CASCADE")
        c.commit()
    yield


# ---------------------------------------------------------------------------
# F3 -- the ledger
# ---------------------------------------------------------------------------

def test_ledger_is_append_only_update(conn):
    emit(conn, "TEST", {"a": 1})
    relay(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute("UPDATE ledger SET payload = '{}' WHERE seq = (SELECT max(seq) FROM ledger)")


def test_ledger_is_append_only_delete(conn):
    emit(conn, "TEST", {"a": 1})
    relay(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ledger WHERE seq = (SELECT max(seq) FROM ledger)")


def test_ledger_chain_is_intact_after_writes(conn):
    for i in range(5):
        emit(conn, "TEST_CHAIN", {"i": i})
    relay(conn)
    intact, total = verify_chain(conn)
    assert intact and total >= 5


def test_ledger_chain_does_not_fork_under_concurrent_writers():
    """Two writers racing must still produce ONE linear chain.

    This is the test the advisory lock in ledger_chain() exists for. Without it both
    transactions read the same tail hash and emit two rows claiming the same parent --
    a forked audit trail, which is worse than no audit trail because it looks valid.
    """
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def writer(tag: str):
        try:
            with connect() as c:
                barrier.wait(timeout=10)   # maximise the overlap
                for i in range(10):
                    emit(c, "CONCURRENT", {"tag": tag, "i": i})
                    relay(c)
                    c.commit()
        except Exception as exc:            # pragma: no cover - surfaced via assert
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(t,)) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"concurrent writers raised: {errors}"

    with connect() as c:
        intact, total = verify_chain(c)
        assert intact, "ledger chain FORKED under concurrent writers"
        assert total >= 20

        # And every entry_hash must be unique -- a fork can also show up as two rows
        # sharing a parent without breaking the lag() check at the seam.
        with c.cursor() as cur:
            cur.execute("SELECT count(*) AS n, count(DISTINCT prev_hash) AS d FROM ledger")
            row = cur.fetchone()
        assert row["n"] == row["d"], "two ledger rows share a parent -- chain forked"


def test_sequence_order_is_chain_order():
    """Regression: seq must be assigned under the chaining lock.

    With a bigserial, the sequence is drawn at INSERT time -- outside the advisory lock
    that serialises hashing -- so two concurrent writers can chain in one order and be
    numbered in the other. The chain stays linear but no longer reads in order, which
    is indistinguishable from corruption to anyone auditing it. A contiguous, gapless
    sequence is the observable form of that guarantee.
    """
    with connect() as c:
        with c.cursor() as cur:
            cur.execute("SELECT min(seq) AS lo, max(seq) AS hi, count(*) AS n FROM ledger")
            row = cur.fetchone()
    if row["n"] == 0:
        pytest.skip("empty ledger")
    assert row["hi"] - row["lo"] + 1 == row["n"], "ledger sequence has gaps"


def test_ledger_writes_only_via_outbox(conn, clean):
    """The relay is the single writer; emit() alone must not touch the ledger."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM ledger")
        before = cur.fetchone()["n"]
    emit(conn, "NOT_YET", {"x": 1})
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM ledger")
        assert cur.fetchone()["n"] == before, "emit() wrote to the ledger directly"
    assert relay(conn) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM ledger")
        assert cur.fetchone()["n"] == before + 1


def test_relay_is_idempotent(conn, clean):
    emit(conn, "ONCE", {"x": 1})
    assert relay(conn) == 1
    assert relay(conn) == 0, "a published outbox row was relayed twice"


# ---------------------------------------------------------------------------
# Disbursement state machine
# ---------------------------------------------------------------------------

def _party(conn, ref="p1"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO counterparties (external_ref,name,origin,source) "
            "VALUES (%s,'Test Party','ordinary','test') RETURNING id", (ref,))
        return cur.fetchone()["id"]


def _disb(conn, cp_id, amount=1000):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO disbursements (counterparty_id,amount_cents) "
                    "VALUES (%s,%s) RETURNING id", (cp_id, amount))
        return cur.fetchone()["id"]


def _set_state(conn, disb_id, state):
    with conn.cursor() as cur:
        cur.execute("UPDATE disbursements SET state=%s WHERE id=%s", (state, disb_id))


@pytest.mark.parametrize("path", [
    ["HELD", "CLEARED", "PAID"],          # held on a delta, released on delisting, paid
    ["CLEARED", "HELD", "CLEARED"],       # cleared, re-screened into a hit, released
    ["CANCELLED"],
])
def test_legal_transition_paths(conn, clean, path):
    d = _disb(conn, _party(conn))
    for state in path:
        _set_state(conn, d, state)


@pytest.mark.parametrize("setup,illegal", [
    ([],                        "PAID"),       # money may not move before it is screened
    (["CANCELLED"],             "PAID"),       # cancelled is terminal
    (["CLEARED", "PAID"],       "HELD"),       # paid is terminal -- money already left
    (["CLEARED", "PAID"],       "CLEARED"),
    (["HELD"],                  "PAID"),       # a held disbursement may not be paid
])
def test_illegal_transitions_are_rejected(conn, clean, setup, illegal):
    d = _disb(conn, _party(conn))
    for state in setup:          # setup steps must themselves be legal
        _set_state(conn, d, state)
    with pytest.raises(psycopg.errors.CheckViolation):
        _set_state(conn, d, illegal)


# ---------------------------------------------------------------------------
# Hold idempotency
# ---------------------------------------------------------------------------

def _adjudication(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO list_versions (published_at,source_hash,kind) "
                    "VALUES ('2026-08-07','h1','SDN') RETURNING id")
        lv = cur.fetchone()["id"]
        cur.execute("INSERT INTO rescreen_runs (list_version_id,trigger) "
                    "VALUES (%s,'MANUAL') RETURNING id", (lv,))
        run = cur.fetchone()["id"]
        cp = _party(conn, "adj-party")
        cur.execute("INSERT INTO matches (run_id,counterparty_id,sdn_uid,det_score,components) "
                    "VALUES (%s,%s,'2674',0.99,'{}') RETURNING id", (run, cp))
        m = cur.fetchone()["id"]
        cur.execute("INSERT INTO adjudications "
                    "(match_id,verdict,rationale,model_id,prompt_hash,context,oracle_guard_result) "
                    "VALUES (%s,'HOLD','r','m','h','{}','AGREE') RETURNING id", (m,))
        return cur.fetchone()["id"], cp


def test_double_hold_on_same_pair_is_rejected(conn, clean):
    """Re-screening the same book against the same publication must not double-hold."""
    adj, cp = _adjudication(conn)
    d = _disb(conn, cp)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO holds (counterparty_id,disbursement_id,adjudication_id,"
                    "report_due_at) VALUES (%s,%s,%s,'2026-08-21')", (cp, d, adj))
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO holds (counterparty_id,disbursement_id,adjudication_id,"
                        "report_due_at) VALUES (%s,%s,%s,'2026-08-21')", (cp, d, adj))


def test_hold_can_be_replaced_after_release(conn, clean):
    """Once released, the same pair may be held again by a later delta.

    This is why the unique index keys on released_at with NULLS NOT DISTINCT rather
    than on the pair alone -- a delisting followed by a re-designation is normal.
    """
    adj, cp = _adjudication(conn)
    d = _disb(conn, cp)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO holds (counterparty_id,disbursement_id,adjudication_id,"
                    "report_due_at) VALUES (%s,%s,%s,'2026-08-21') RETURNING id", (cp, d, adj))
        h = cur.fetchone()["id"]
        cur.execute("UPDATE holds SET released_at = now() WHERE id = %s", (h,))
        cur.execute("INSERT INTO holds (counterparty_id,disbursement_id,adjudication_id,"
                    "report_due_at) VALUES (%s,%s,%s,'2026-09-01')", (cp, d, adj))


# ---------------------------------------------------------------------------
# F4 -- batch checkpointing
# ---------------------------------------------------------------------------

def test_resume_point_is_min_incomplete_not_max_completed(conn, clean):
    """The kill-worker-mid-book scenario, asserted directly.

    Batches complete out of order. If batch 2 dies while 1 and 3 succeed, resuming
    must return to batch 2 -- a scalar cursor would resume after 3 and silently leave
    those counterparties unscreened against a live sanctions list.
    """
    with conn.cursor() as cur:
        cur.execute("INSERT INTO list_versions (published_at,source_hash,kind) "
                    "VALUES ('2026-08-07','h-resume','SDN') RETURNING id")
        lv = cur.fetchone()["id"]
        cur.execute("INSERT INTO rescreen_runs (list_version_id,trigger) "
                    "VALUES (%s,'SCHEDULER') RETURNING id", (lv,))
        run = cur.fetchone()["id"]

    # Each batch is completed as it is claimed, because claim_batch now re-claims an
    # abandoned range before allocating a new one -- claiming three in a row without
    # completing them would simply hand back the first range three times.
    b1 = claim_batch(conn, run, 500, 1500)
    complete_batch(conn, run, b1[0])
    b2 = claim_batch(conn, run, 500, 1500)
    complete_batch(conn, run, b2[0])
    b3 = claim_batch(conn, run, 500, 1500)
    complete_batch(conn, run, b3[0])
    assert (b1, b2, b3) == ((1, 500), (501, 1000), (1001, 1500))

    # Now worker 2's range is abandoned: reopen it by clearing its completion.
    with conn.cursor() as cur:
        cur.execute("UPDATE rescreen_batches SET completed_at = NULL "
                    "WHERE run_id = %s AND batch_start = 501", (run,))

    assert resume_point(conn, run) == 501


def test_resume_point_is_none_when_run_is_complete(conn, clean):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO list_versions (published_at,source_hash,kind) "
                    "VALUES ('2026-08-07','h-done','SDN') RETURNING id")
        lv = cur.fetchone()["id"]
        cur.execute("INSERT INTO rescreen_runs (list_version_id,trigger) "
                    "VALUES (%s,'SCHEDULER') RETURNING id", (lv,))
        run = cur.fetchone()["id"]
    start, _ = claim_batch(conn, run, 500, 400)
    complete_batch(conn, run, start)
    assert resume_point(conn, run) is None


def test_claim_batch_returns_none_past_the_end(conn, clean):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO list_versions (published_at,source_hash,kind) "
                    "VALUES ('2026-08-07','h-end','SDN') RETURNING id")
        lv = cur.fetchone()["id"]
        cur.execute("INSERT INTO rescreen_runs (list_version_id,trigger) "
                    "VALUES (%s,'SCHEDULER') RETURNING id", (lv,))
        run = cur.fetchone()["id"]
    start, _ = claim_batch(conn, run, 500, 100)
    assert (start, _) == (1, 100)
    # Only once the range is completed does the allocator move past the end of the book;
    # an uncompleted range is re-handed out rather than abandoned.
    assert claim_batch(conn, run, 500, 100) == (1, 100)
    complete_batch(conn, run, start)
    assert claim_batch(conn, run, 500, 100) is None


def test_round_trip_cap_is_enforced_by_the_database(conn, clean):
    """The <=2 matcher/adjudicator round-trip cap is a constraint, not a convention."""
    adj_id, _ = _adjudication(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT match_id FROM adjudications WHERE id=%s", (adj_id,))
        m = cur.fetchone()["match_id"]
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO adjudications (match_id,verdict,rationale,model_id,"
                "prompt_hash,context,oracle_guard_result,round_trips) "
                "VALUES (%s,'HOLD','r','m','h','{}','AGREE',3)", (m,))
