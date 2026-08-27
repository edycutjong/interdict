"""The cloud evidence plane -- what the mirror must guarantee.

Firestore is a publication plane, not a source of truth, and every property that makes it
safe to run unattended on a timer is a property of *how* it writes: the resume point is
read back from the cloud rather than remembered locally, document ids are derived from the
ledger `seq` so a re-run overwrites instead of duplicating, and writes are batched under
Firestore's own 500-document cap so a large backlog does not blow the batch.

The Firestore SDK and psycopg are faked here. That is deliberate and it is not faking the
judged behaviour: the fake Firestore genuinely stores documents, genuinely sorts and limits
an `order_by` query, and genuinely refuses to reveal a write before its batch commits, so a
mirror that queried ascending, that duplicated on a re-run, that exceeded the batch cap or
that dropped an uncommitted tail would fail these tests. The fake ledger genuinely filters
on `seq > watermark`, so a mirror that ignored the watermark would republish and be caught.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from google.cloud import firestore

from interdict import cloud
from interdict.cloud import (
    BATCH_LIMIT,
    LEDGER_COLLECTION,
    RUNS_COLLECTION,
    FirestoreMirror,
    _row_to_document,
    mirror,
    publish_ledger,
    publish_run_summary,
)

# -- fake Firestore ---------------------------------------------------------


class FakeSnapshot:
    def __init__(self, doc_id: str, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return self._data


class FakeQuery:
    """Sorts and limits for real, so direction and limit are behaviour, not just calls."""

    def __init__(self, db, collection, field, direction):
        self._db = db
        self._collection = collection
        self._field = field
        self._direction = direction
        self._limit = None

    def limit(self, n):
        self._limit = n
        return self

    def stream(self):
        items = list(self._db.store[self._collection].items())
        items.sort(key=lambda kv: (kv[1] or {}).get(self._field, 0),
                   reverse=self._direction == firestore.Query.DESCENDING)
        if self._limit is not None:
            items = items[:self._limit]
        return iter([FakeSnapshot(k, v) for k, v in items])


class FakeDocumentRef:
    def __init__(self, db, collection, doc_id):
        self._db = db
        self.collection = collection
        self.id = doc_id

    def set(self, data, merge=False):
        self._db.direct_sets.append((self.collection, self.id, merge))
        self._db.store[self.collection][self.id] = data


class FakeCollection:
    def __init__(self, db, name):
        self._db = db
        self.name = name
        db.store.setdefault(name, {})

    def document(self, doc_id):
        return FakeDocumentRef(self._db, self.name, doc_id)

    def order_by(self, field, direction=None):
        self._db.order_by_calls.append((self.name, field, direction))
        return FakeQuery(self._db, self.name, field, direction)


class FakeBatch:
    """Nothing is visible until commit -- an uncommitted tail is a lost tail."""

    def __init__(self, db):
        self._db = db
        self._ops: list[tuple[FakeDocumentRef, dict]] = []
        self.committed = False

    def set(self, ref, data):
        if self.committed:
            raise AssertionError("write added to an already-committed batch")
        self._ops.append((ref, data))

    def commit(self):
        if len(self._ops) > BATCH_LIMIT:
            raise AssertionError(f"batch of {len(self._ops)} exceeds Firestore's cap")
        for ref, data in self._ops:
            self._db.store[ref.collection][ref.id] = data
        self._db.batch_commit_sizes.append(len(self._ops))
        self.committed = True


class FakeFirestoreClient:
    def __init__(self, project=None, database=None):
        self.project = project
        self.database = database
        self.store: dict[str, dict[str, dict]] = {
            LEDGER_COLLECTION: {}, RUNS_COLLECTION: {}}
        self.batches: list[FakeBatch] = []
        self.batch_commit_sizes: list[int] = []
        self.direct_sets: list[tuple[str, str, bool]] = []
        self.order_by_calls: list[tuple[str, str, str]] = []

    def collection(self, name):
        return FakeCollection(self, name)

    def batch(self):
        b = FakeBatch(self)
        self.batches.append(b)
        return b


@pytest.fixture
def fake_firestore(monkeypatch):
    """Replace the SDK entry point; every Client() built in this test returns the fake."""
    built: list[FakeFirestoreClient] = []

    def factory(project=None, database=None, **kwargs):
        c = FakeFirestoreClient(project=project, database=database)
        built.append(c)
        return c

    monkeypatch.setattr(firestore, "Client", factory)
    return built


@pytest.fixture
def mir(fake_firestore):
    return FirestoreMirror(project="test-project")


@pytest.fixture
def db(mir):
    return mir._db


# -- fake ledger database ---------------------------------------------------


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._conn.closed_cursors += 1
        return False

    def execute(self, sql, params):
        # psycopg itself rejects a statement whose placeholder count does not match the
        # arguments, so the fake does too -- otherwise a bound value quietly replaced by a
        # literal (`seq > 0`) would still appear to work here.
        assert sql.count("%s") == len(params), (
            f"{sql.count('%s')} placeholders for {len(params)} arguments")
        self._conn.queries.append((" ".join(sql.split()), params))
        if "FROM ledger" in sql:
            assert "seq > %s" in " ".join(sql.split()), "the watermark must be bound"
            watermark, limit = params
            rows = sorted((r for r in self._conn.ledger if r["seq"] > watermark),
                          key=lambda r: r["seq"])
            self._result = rows[:limit]
        elif "rescreen_runs" in sql:
            (run_id,) = params
            self._result = self._conn.runs.get(run_id)
        else:  # pragma: no cover - the module issues no other statement
            raise AssertionError(f"unexpected statement: {sql}")

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result


class FakeConn:
    def __init__(self, ledger=None, runs=None):
        self.ledger = ledger or []
        self.runs = runs or {}
        self.queries: list[tuple[str, tuple]] = []
        self.closed_cursors = 0

    def cursor(self):
        return FakeCursor(self)


def ledger_row(seq: int, **over):
    row = {
        "seq": seq,
        "event_type": "ADJUDICATION_RECORDED",
        "payload": {"match_id": seq, "verdict": "HOLD"},
        "prev_hash": memoryview(bytes([seq - 1] * 4)),
        "entry_hash": memoryview(bytes([seq] * 4)),
        "created_at": datetime(2026, 8, 7, 12, 0, seq % 60, tzinfo=UTC),
    }
    row.update(over)
    return row


RUN_ROW = {
    "id": 42,
    "trigger": "PUBLICATION",
    "started_at": datetime(2026, 8, 7, 9, 0, tzinfo=UTC),
    "finished_at": datetime(2026, 8, 7, 9, 4, tzinfo=UTC),
    "published_at": date(2026, 8, 7),
    "record_count": 19199,
    "source_hash": "a" * 64,
    "matches": 12,
    "adjudications": 12,
    "holds": 3,
    "guard_disagreements": 1,
}


# -- construction -----------------------------------------------------------


def test_a_mirror_without_a_project_refuses_to_construct(fake_firestore, monkeypatch):
    monkeypatch.setattr(cloud, "PROJECT", None)
    with pytest.raises(RuntimeError) as excinfo:
        FirestoreMirror()
    assert "INTERDICT_FIRESTORE_PROJECT" in str(excinfo.value)
    assert fake_firestore == [], "no client may be built without a project"


def test_the_environment_supplies_the_project_and_database_when_no_argument_does(
        fake_firestore, monkeypatch):
    monkeypatch.setattr(cloud, "PROJECT", "env-project")
    monkeypatch.setattr(cloud, "DATABASE", "env-database")
    m = FirestoreMirror()
    assert (m.project, m.database) == ("env-project", "env-database")
    assert (fake_firestore[0].project, fake_firestore[0].database) == (
        "env-project", "env-database")


def test_explicit_arguments_beat_the_environment(fake_firestore, monkeypatch):
    monkeypatch.setattr(cloud, "PROJECT", "env-project")
    monkeypatch.setattr(cloud, "DATABASE", "env-database")
    m = FirestoreMirror(project="arg-project", database="arg-database")
    assert (m.project, m.database) == ("arg-project", "arg-database")
    assert fake_firestore[0].project == "arg-project"


def test_the_cloud_plane_is_off_when_no_project_is_configured(monkeypatch):
    monkeypatch.setattr(cloud, "PROJECT", None)
    assert mirror() is None


def test_the_cloud_plane_is_on_when_a_project_is_configured(fake_firestore, monkeypatch):
    monkeypatch.setattr(cloud, "PROJECT", "live-project")
    m = mirror()
    assert isinstance(m, FirestoreMirror)
    assert m.project == "live-project"


# -- watermark --------------------------------------------------------------


def test_an_empty_collection_resumes_from_zero(mir):
    assert mir.mirrored_through() == 0


def test_the_watermark_is_the_highest_seq_held_not_the_first_written(mir, db):
    """Written out of order on purpose: an ASCENDING query would answer 3."""
    for seq in (9, 3, 17):
        db.store[LEDGER_COLLECTION][f"{seq:012d}"] = {"seq": seq}
    assert mir.mirrored_through() == 17


def test_the_watermark_query_asks_firestore_to_sort_by_seq_descending(mir, db):
    db.store[LEDGER_COLLECTION]["000000000001"] = {"seq": 1}
    mir.mirrored_through()
    assert db.order_by_calls == [
        (LEDGER_COLLECTION, "seq", firestore.Query.DESCENDING)]


def test_a_document_with_no_readable_body_does_not_become_the_watermark(mir, db):
    db.store[LEDGER_COLLECTION]["000000000001"] = None
    assert mir.mirrored_through() == 0


def test_a_document_missing_its_seq_does_not_become_the_watermark(mir, db):
    db.store[LEDGER_COLLECTION]["000000000001"] = {"event_type": "X"}
    assert mir.mirrored_through() == 0


# -- publication of entries -------------------------------------------------


def test_the_document_id_is_the_zero_padded_seq(mir, db):
    mir.publish_entries([{"seq": 7, "event_type": "X"}])
    assert list(db.store[LEDGER_COLLECTION]) == ["000000000007"]


def test_republishing_an_entry_overwrites_rather_than_duplicates(mir, db):
    mir.publish_entries([{"seq": 7, "event_type": "X"}])
    mir.publish_entries([{"seq": 7, "event_type": "X", "payload": {"fixed": True}}])
    assert list(db.store[LEDGER_COLLECTION]) == ["000000000007"]
    assert db.store[LEDGER_COLLECTION]["000000000007"]["payload"] == {"fixed": True}


def test_publishing_reports_how_many_entries_it_wrote(mir):
    assert mir.publish_entries([{"seq": s} for s in range(1, 4)]) == 3


def test_nothing_to_publish_opens_no_batch(mir, db):
    assert mir.publish_entries([]) == 0
    assert db.batches == []


def test_a_backlog_is_split_into_batches_within_firestores_cap(mir, db):
    entries = [{"seq": s} for s in range(1, 2 * BATCH_LIMIT + 2)]
    assert mir.publish_entries(entries) == len(entries)
    assert db.batch_commit_sizes == [BATCH_LIMIT, BATCH_LIMIT, 1]


def test_every_batch_including_the_short_final_one_is_committed(mir, db):
    entries = [{"seq": s} for s in range(1, BATCH_LIMIT + 4)]
    mir.publish_entries(entries)
    assert all(b.committed for b in db.batches)
    assert len(db.store[LEDGER_COLLECTION]) == len(entries)
    assert "000000000503" in db.store[LEDGER_COLLECTION]


def test_ledger_entries_are_only_ever_written_through_a_batch(mir, db):
    mir.publish_entries([{"seq": 1}, {"seq": 2}])
    assert db.direct_sets == []


# -- publication of a run summary -------------------------------------------


def test_the_run_document_id_is_the_zero_padded_run_id(mir, db):
    mir.publish_run({"run_id": 42, "trigger": "PUBLICATION"})
    assert list(db.store[RUNS_COLLECTION]) == ["run-000042"]


def test_a_run_summary_merges_so_a_later_pass_does_not_erase_earlier_fields(mir, db):
    mir.publish_run({"run_id": 42})
    assert db.direct_sets == [(RUNS_COLLECTION, "run-000042", True)]


# -- row -> document --------------------------------------------------------


def test_hashes_are_published_as_hex_so_the_chain_is_readable_in_the_console():
    doc = _row_to_document(ledger_row(3))
    assert doc["prev_hash"] == "02020202"
    assert doc["entry_hash"] == "03030303"
    assert isinstance(doc["prev_hash"], str)


def test_the_document_carries_the_fields_a_verifier_needs():
    row = ledger_row(3)
    doc = _row_to_document(row)
    assert set(doc) == {"seq", "event_type", "payload", "prev_hash", "entry_hash",
                        "created_at"}
    assert doc["seq"] == 3
    assert doc["event_type"] == row["event_type"]
    assert doc["payload"] == row["payload"]
    assert doc["created_at"] == row["created_at"]


# -- publish_ledger ---------------------------------------------------------


def test_publishing_the_ledger_does_not_touch_the_database_when_the_plane_is_off(
        monkeypatch):
    monkeypatch.setattr(cloud, "PROJECT", None)
    conn = FakeConn(ledger=[ledger_row(1)])
    assert publish_ledger(conn) == 0
    assert conn.queries == [], "the database must not be read with nowhere to publish"


def test_publishing_the_ledger_uses_the_configured_mirror_when_none_is_passed(
        fake_firestore, monkeypatch):
    monkeypatch.setattr(cloud, "PROJECT", "live-project")
    conn = FakeConn(ledger=[ledger_row(1), ledger_row(2)])
    assert publish_ledger(conn) == 2
    assert len(fake_firestore[0].store[LEDGER_COLLECTION]) == 2


def test_only_entries_above_the_watermark_are_published(mir, db):
    db.store[LEDGER_COLLECTION]["000000000002"] = {"seq": 2}
    conn = FakeConn(ledger=[ledger_row(s) for s in (1, 2, 3, 4)])
    assert publish_ledger(conn, mir) == 2
    _, params = conn.queries[0]
    assert params[0] == 2, "the watermark must be the seq bound in the query"
    assert sorted(db.store[LEDGER_COLLECTION]) == [
        "000000000002", "000000000003", "000000000004"]


def test_a_ledger_with_nothing_new_publishes_nothing_and_opens_no_batch(mir, db):
    db.store[LEDGER_COLLECTION]["000000000004"] = {"seq": 4}
    conn = FakeConn(ledger=[ledger_row(s) for s in (1, 2, 3, 4)])
    assert publish_ledger(conn, mir) == 0
    assert db.batches == []


def test_the_ledger_read_is_capped_by_the_limit_argument(mir, db):
    conn = FakeConn(ledger=[ledger_row(s) for s in range(1, 11)])
    assert publish_ledger(conn, mir, limit=4) == 4
    assert conn.queries[0][1] == (0, 4)
    assert sorted(db.store[LEDGER_COLLECTION])[-1] == "000000000004"


def test_the_published_documents_are_the_converted_ledger_rows(mir, db):
    conn = FakeConn(ledger=[ledger_row(1)])
    publish_ledger(conn, mir)
    assert db.store[LEDGER_COLLECTION]["000000000001"] == _row_to_document(ledger_row(1))


def test_an_interrupted_mirror_is_repaired_by_running_it_again(mir, db):
    """Resumability with no local state: the second pass reads its own resume point out
    of Firestore, republishes only the tail, and leaves one document per ledger entry."""
    conn = FakeConn(ledger=[ledger_row(s) for s in range(1, 7)])
    assert publish_ledger(conn, mir, limit=4) == 4
    assert publish_ledger(conn, mir, limit=4) == 2
    assert publish_ledger(conn, mir, limit=4) == 0
    assert sorted(db.store[LEDGER_COLLECTION]) == [f"{s:012d}" for s in range(1, 7)]


def test_the_mirror_never_closes_over_an_open_cursor(mir):
    conn = FakeConn(ledger=[ledger_row(1)])
    publish_ledger(conn, mir)
    assert conn.closed_cursors == 1


# -- publish_run_summary ----------------------------------------------------


def test_the_run_summary_does_not_touch_the_database_when_the_plane_is_off(monkeypatch):
    monkeypatch.setattr(cloud, "PROJECT", None)
    conn = FakeConn(runs={42: dict(RUN_ROW)})
    assert publish_run_summary(conn, 42) is False
    assert conn.queries == []


def test_the_run_summary_uses_the_configured_mirror_when_none_is_passed(
        fake_firestore, monkeypatch):
    monkeypatch.setattr(cloud, "PROJECT", "live-project")
    conn = FakeConn(runs={42: dict(RUN_ROW)})
    assert publish_run_summary(conn, 42) is True
    assert list(fake_firestore[0].store[RUNS_COLLECTION]) == ["run-000042"]


def test_an_unknown_run_publishes_nothing(mir, db):
    conn = FakeConn(runs={7: dict(RUN_ROW)})
    assert publish_run_summary(conn, 99, mir) is False
    assert db.store[RUNS_COLLECTION] == {}


def test_the_run_summary_is_read_for_the_run_that_was_asked_for(mir):
    conn = FakeConn(runs={7: dict(RUN_ROW, id=7)})
    assert publish_run_summary(conn, 7, mir) is True
    assert conn.queries[0][1] == (7,)


def test_the_run_summary_carries_the_headline_numbers_a_reviewer_opens_first(mir, db):
    conn = FakeConn(runs={42: dict(RUN_ROW)})
    assert publish_run_summary(conn, 42, mir) is True
    doc = db.store[RUNS_COLLECTION]["run-000042"]
    assert doc == {
        "run_id": 42,
        "trigger": "PUBLICATION",
        "started_at": RUN_ROW["started_at"],
        "finished_at": RUN_ROW["finished_at"],
        "publication": "2026-08-07",
        "sdn_records": 19199,
        "source_sha256": "a" * 64,
        "matches": 12,
        "adjudications": 12,
        "holds": 3,
        "guard_disagreements": 1,
    }


def test_a_run_summary_republished_after_more_screening_reports_the_true_totals(mir, db):
    """Derived from the database, not accumulated in memory: a resumed run's second
    publication must carry the whole run's counts, not the final leg's."""
    conn = FakeConn(runs={42: dict(RUN_ROW, holds=1, finished_at=None)})
    publish_run_summary(conn, 42, mir)
    assert db.store[RUNS_COLLECTION]["run-000042"]["holds"] == 1
    conn.runs[42] = dict(RUN_ROW, holds=3)
    publish_run_summary(conn, 42, mir)
    doc = db.store[RUNS_COLLECTION]["run-000042"]
    assert doc["holds"] == 3
    assert doc["finished_at"] == RUN_ROW["finished_at"]
