"""The cloud evidence plane -- Cloud Firestore.

Postgres is the *correctness* core: the append-only triggers, the illegal-transition
checks, the hash chain built under an advisory lock. None of that moves. It is the part
of this system that has to be right, and it is right because a relational engine is
enforcing it rather than application code asking nicely.

Firestore is the *publication* plane: the durable, externally readable record of what
Interdict decided. It exists because a compliance audit trail that only lives on the
machine that produced it is not an audit trail -- the examiner has to be able to read it
without the operator's laptop, and a reviewer has to be able to watch decisions land in
the Cloud Console while a re-screen is running.

WHY IT MIRRORS THE LEDGER RATHER THAN THE DECISION PATH. Agents never write here. The
mirror reads committed ledger rows and republishes them, so a document exists in
Firestore if and only if the ledger entry that produced it committed -- the same
guarantee the transactional outbox gives inside Postgres, extended one hop further out.
Each document carries its own `seq`, `prev_hash` and `entry_hash`, so the chain is
verifiable from the cloud copy alone, against a local database the verifier does not
have and does not have to trust.

WHY THE WATERMARK LIVES IN FIRESTORE. The mirror is resumable without a local table: it
asks Firestore for the highest `seq` it already holds and republishes everything above
it. Document ids are the zero-padded sequence number, so a re-run overwrites rather than
duplicates and an interrupted mirror is repaired by running it again. That makes this
safe to call on a timer, which is what an unattended system needs.

Firestore is a mirror, never a source of truth. A publish failure is loud and retried on
the next pass; it can never roll back a decision Postgres has already committed.
"""

from __future__ import annotations

import os
from typing import Any

import psycopg
from psycopg.rows import DictRow

PROJECT = os.environ.get("INTERDICT_FIRESTORE_PROJECT")
DATABASE = os.environ.get("INTERDICT_FIRESTORE_DATABASE", "(default)")

LEDGER_COLLECTION = "interdict_ledger"
RUNS_COLLECTION = "interdict_runs"

# Firestore's own cap on a single batched write.
BATCH_LIMIT = 500


class FirestoreMirror:
    """Publishes committed ledger entries to Cloud Firestore.

    Constructed only when INTERDICT_FIRESTORE_PROJECT is set. Everything below degrades
    to a no-op without it, so the whole screening plane still runs, tests and all, with
    no cloud credentials -- the same rule the adjudicator follows.
    """

    def __init__(self, project: str | None = None, database: str | None = None):
        from google.cloud import firestore  # imported lazily: no project, no import

        self.project = project or PROJECT
        self.database = database or DATABASE
        if not self.project:
            raise RuntimeError(
                "No Firestore project. Set INTERDICT_FIRESTORE_PROJECT (and point "
                "GOOGLE_APPLICATION_CREDENTIALS at a service-account key with "
                "roles/datastore.user)."
            )
        self._db = firestore.Client(project=self.project, database=self.database)

    # -- watermark ---------------------------------------------------------

    def mirrored_through(self) -> int:
        """Highest ledger seq already in Firestore, or 0.

        This is the resume point. Querying it rather than storing it locally is what
        lets the mirror recover from an interruption with no extra state to corrupt.
        """
        from google.cloud import firestore

        docs = list(
            self._db.collection(LEDGER_COLLECTION)
            .order_by("seq", direction=firestore.Query.DESCENDING)
            .limit(1)
            .stream()
        )
        if not docs:
            return 0
        top = docs[0].to_dict() or {}
        return int(top.get("seq", 0))

    # -- publication -------------------------------------------------------

    def publish_entries(self, entries: list[dict[str, Any]]) -> int:
        """Write ledger entries. Document id is the zero-padded seq, so this is idempotent."""
        written = 0
        for start in range(0, len(entries), BATCH_LIMIT):
            batch = self._db.batch()
            for e in entries[start:start + BATCH_LIMIT]:
                ref = self._db.collection(LEDGER_COLLECTION).document(f"{e['seq']:012d}")
                batch.set(ref, e)
                written += 1
            batch.commit()
        return written

    def publish_run(self, run: dict[str, Any]) -> None:
        """Write the run summary -- what a reviewer opens first."""
        ref = self._db.collection(RUNS_COLLECTION).document(f"run-{run['run_id']:06d}")
        ref.set(run, merge=True)


def mirror() -> FirestoreMirror | None:
    """The configured mirror, or None when the cloud plane is switched off."""
    if not PROJECT:
        return None
    return FirestoreMirror()


def _row_to_document(row: DictRow) -> dict[str, Any]:
    """Ledger row -> Firestore document.

    Hashes are hex rather than bytes so the document is readable in the Console and
    diffable against `make verify-ledger` output by eye.
    """
    return {
        "seq": int(row["seq"]),
        "event_type": row["event_type"],
        "payload": row["payload"],
        "prev_hash": bytes(row["prev_hash"]).hex(),
        "entry_hash": bytes(row["entry_hash"]).hex(),
        "created_at": row["created_at"],
    }


def publish_ledger(conn: psycopg.Connection[DictRow], mir: FirestoreMirror | None = None,
                   limit: int = 5000) -> int:
    """Mirror every committed ledger entry Firestore does not yet hold.

    Returns the number of entries published. Safe to call repeatedly and safe to call
    concurrently with screening: it only ever reads rows the ledger has already
    committed, and re-publishing an entry overwrites an identical document.
    """
    mir = mir or mirror()
    if mir is None:
        return 0

    watermark = mir.mirrored_through()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT seq, event_type, payload, prev_hash, entry_hash, created_at
            FROM ledger WHERE seq > %s ORDER BY seq LIMIT %s
            """,
            (watermark, limit),
        )
        rows = cur.fetchall()

    if not rows:
        return 0
    return mir.publish_entries([_row_to_document(r) for r in rows])


def publish_run_summary(conn: psycopg.Connection[DictRow], run_id: int,
                        mir: FirestoreMirror | None = None) -> bool:
    """Mirror one run's headline numbers: what was screened, what was held, what stopped.

    Derived from the database rather than accumulated in memory, so a resumed run
    publishes the true totals and not just the totals of its final leg.
    """
    mir = mir or mirror()
    if mir is None:
        return False

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.trigger, r.started_at, r.finished_at,
                   lv.published_at, lv.record_count, lv.source_hash,
                   (SELECT count(*) FROM matches m WHERE m.run_id = r.id) AS matches,
                   (SELECT count(*) FROM adjudications a
                      JOIN matches m ON m.id = a.match_id
                     WHERE m.run_id = r.id) AS adjudications,
                   (SELECT count(*) FROM adjudications a
                      JOIN matches m ON m.id = a.match_id
                     WHERE m.run_id = r.id AND a.verdict = 'HOLD') AS holds,
                   (SELECT count(*) FROM adjudications a
                      JOIN matches m ON m.id = a.match_id
                     WHERE m.run_id = r.id AND a.oracle_guard_result = 'DISAGREE')
                       AS guard_disagreements
            FROM rescreen_runs r
            JOIN list_versions lv ON lv.id = r.list_version_id
            WHERE r.id = %s
            """,
            (run_id,),
        )
        row = cur.fetchone()

    if row is None:
        return False

    mir.publish_run({
        "run_id": int(row["id"]),
        "trigger": row["trigger"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "publication": str(row["published_at"]),
        "sdn_records": row["record_count"],
        "source_sha256": row["source_hash"],
        "matches": row["matches"],
        "adjudications": row["adjudications"],
        "holds": row["holds"],
        "guard_disagreements": row["guard_disagreements"],
    })
    return True
