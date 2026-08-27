"""The two guards in `interdict.db` that only fire when something has already gone wrong.

Both are unreachable from a healthy database, which is exactly why they need tests of
their own: the failure they prevent is silent, and a live-Postgres suite never produces
the conditions that trigger them.

`one()` is the loud-failure guard. Every call site follows an INSERT ... RETURNING or an
aggregate, so a None means the query changed underneath the code -- and the wrong
behaviour is not "returns None", it is a TypeError raised several frames away at
`[...]["id"]`, pointing at the caller instead of the query.

`run_is_complete()` is the compliance guard. This project briefly shipped the bug where
"no incomplete batches" alone was treated as sufficient. Nothing outstanding is only half
the question; the other half is whether claimed coverage ever reached the end of the
book. The fakes below stand in for `rescreen_batches` so both halves can be failed
independently, which a real run cannot easily be made to do on demand.

No database is touched here -- these tests run whether or not Postgres is up.
"""

import pytest

from interdict.db import emit, one, resume_point, run_is_complete


class FakeCursor:
    """Cursor that returns a canned result, used to force the `one()` guard."""

    def __init__(self, row):
        self._row = row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class BatchTableConn:
    """An in-memory stand-in for the `rescreen_batches` rows of a single run.

    It answers the two aggregates `resume_point` and `run_is_complete` issue, computing
    them the way Postgres would, so the tests exercise the real control flow of those
    functions rather than a monkeypatched shortcut.
    """

    def __init__(self, batches):
        # batches: (batch_start, batch_end, completed)
        self.batches = list(batches)
        self.queries = []
        self._result = None

    # -- cursor protocol ---------------------------------------------------
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.queries.append(" ".join(sql.split()))
        # The WHERE clause is honoured rather than assumed: a query that stopped
        # filtering on completed_at would otherwise still pass against a fake that
        # hard-coded the filter it was written for.
        rows = self.batches
        if "completed_at IS NULL" in sql:
            rows = [b for b in rows if not b[2]]
        if "min(batch_start)" in sql:
            self._result = {"resume": min((b[0] for b in rows), default=None)}
        elif "max(batch_end)" in sql:
            self._result = {"covered": max((b[1] for b in rows), default=0)}
        else:  # pragma: no cover - the two functions under test issue nothing else
            raise AssertionError(f"unexpected query: {sql}")

    def fetchone(self):
        return self._result


# ---------------------------------------------------------------------------
# one()
# ---------------------------------------------------------------------------

def test_a_row_that_exists_is_returned_untouched():
    row = {"id": 7}
    assert one(FakeCursor(row)) is row


def test_a_missing_row_fails_loudly_instead_of_returning_none():
    with pytest.raises(RuntimeError) as exc:
        one(FakeCursor(None))
    assert "exactly one row" in str(exc.value)


def test_the_missing_row_error_is_not_the_typeerror_a_caller_would_otherwise_see():
    """The whole point of the guard: the failure names the query, not the subscript.

    Without it, `one()` returns None and the caller's `[...]["id"]` raises
    `TypeError: 'NoneType' object is not subscriptable` one frame later, which reads as a
    bug in the caller rather than a query that stopped returning a row.
    """
    with pytest.raises(RuntimeError):
        one(FakeCursor(None))
    with pytest.raises(TypeError):
        FakeCursor(None).fetchone()["id"]


def test_an_insert_returning_nothing_surfaces_at_the_emit_call_site():
    cur = FakeCursor(None)
    with pytest.raises(RuntimeError):
        emit(FakeConn(cur), "hold.placed", {"counterparty": 1})
    # The INSERT was still issued -- the guard fires on the result, not before the query.
    assert cur.executed and "INSERT INTO outbox" in cur.executed[0][0]


# ---------------------------------------------------------------------------
# resume_point()
# ---------------------------------------------------------------------------

def test_the_resume_point_is_the_earliest_unfinished_batch_not_the_furthest_finished():
    conn = BatchTableConn([(1, 500, True), (501, 1000, False), (1001, 1500, True)])
    assert resume_point(conn, 1) == 501


def test_a_fully_completed_run_has_no_resume_point():
    conn = BatchTableConn([(1, 500, True), (501, 1000, True)])
    assert resume_point(conn, 1) is None


# ---------------------------------------------------------------------------
# run_is_complete()
# ---------------------------------------------------------------------------

def test_a_run_with_a_batch_abandoned_mid_book_is_not_complete():
    """A worker died inside 501-1000 while later batches finished.

    Claimed coverage reaches the end of the book, so every check based on coverage alone
    says "done" -- but 500 counterparties were never screened against the live
    publication. This is the case the outstanding-batch check exists for.
    """
    conn = BatchTableConn([(1, 500, True), (501, 1000, False), (1001, 1500, True)])
    assert run_is_complete(conn, 1, 1500) is False


def test_an_outstanding_batch_short_circuits_before_the_coverage_query_runs():
    conn = BatchTableConn([(1, 500, False)])
    assert run_is_complete(conn, 1, 500) is False
    assert all("max(batch_end)" not in q for q in conn.queries)


def test_a_run_whose_worker_died_between_batches_is_not_complete():
    """Nothing outstanding, but claimed coverage stops at 1000 of 1500.

    This is the bug the project shipped: no incomplete batches was read as finished,
    while a third of the book had never been claimed at all.
    """
    conn = BatchTableConn([(1, 500, True), (501, 1000, True)])
    assert run_is_complete(conn, 1, 1500) is False


def test_a_run_is_complete_only_when_nothing_is_outstanding_and_coverage_reaches_the_end():
    conn = BatchTableConn([(1, 500, True), (501, 1000, True), (1001, 1500, True)])
    assert run_is_complete(conn, 1, 1500) is True


def test_a_run_that_never_claimed_a_batch_is_not_complete():
    assert run_is_complete(BatchTableConn([]), 1, 1500) is False
