"""Full-book re-screen -- the unattended loop.

When Treasury publishes, every counterparty in the book is screened again. Not the
delta's entities against the book, but **the whole book against the new list**: a
modify action can change an alias that makes a previously-cleared grantee match, and a
system that only screened the delta's own names would miss it.

Two properties matter more than throughput.

RESUMABILITY. Workers claim batches with FOR UPDATE SKIP LOCKED and finish out of
order, so the resume point is MIN(batch_start) over incomplete batches. A crash mid-book
resumes exactly there. The alternative -- a cursor advanced optimistically -- would skip
whichever ranges were still in flight, leaving counterparties unscreened against a live
sanctions list while the run reported success.

COMPLETENESS IS NOT THE SAME QUESTION. "Nothing incomplete" does not mean "everything
screened", and treating them as equivalent was a real bug here: a worker that dies
*between* batches leaves nothing outstanding, because the remaining ranges were never
claimed in the first place. The run then closed itself as finished with most of the book
untouched. Closing therefore requires both -- nothing outstanding AND claimed coverage
reaching the end of the book.

IDEMPOTENCE. Re-running a completed batch must be harmless, because retries are normal.
Holds are idempotent at the database level, so a redelivered message re-decides the same
way and changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import psycopg
from psycopg.rows import DictRow

from .adjudicator import Adjudicator
from .db import claim_batch, complete_batch, emit, one, run_is_complete
from .matcher import Matcher
from .orchestrator import screen_counterparty

BATCH_SIZE = 500


@dataclass
class RunSummary:
    run_id: int
    screened: int = 0
    holds: int = 0
    cleared: int = 0
    quarantined: int = 0
    batches: int = 0
    decisions: list = field(default_factory=list)

    @property
    def adjudicated(self) -> int:
        return self.holds + self.quarantined


def open_run(conn: psycopg.Connection[DictRow], *, published_at: date, source_hash: str,
             kind: str, trigger: str, record_count: int | None = None) -> int:
    """Register a publication and open a re-screen run against it.

    The publication is keyed by content hash, so re-ingesting the same bytes reuses the
    existing row rather than creating a second version of the same truth.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO list_versions (published_at, source_hash, kind, record_count)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (source_hash) DO UPDATE SET published_at = EXCLUDED.published_at
            RETURNING id
            """,
            (published_at, source_hash, kind, record_count),
        )
        version_id = one(cur)["id"]

        cur.execute(
            "INSERT INTO rescreen_runs (list_version_id, trigger) VALUES (%s,%s) RETURNING id",
            (version_id, trigger),
        )
        run_id = one(cur)["id"]

    emit(conn, "RUN_OPENED", {
        "run_id": run_id, "list_version_id": version_id, "trigger": trigger,
        "published_at": published_at.isoformat(), "source_hash": source_hash,
    })
    return run_id


def _oracle_verdicts(oracle, rows) -> dict[int, str]:
    """Ask yente about a whole batch at once.

    Recorded against every adjudication, not only where the two disagree -- an oracle
    consulted selectively is not an oracle. Batched because grading a 500-row batch one
    HTTP call at a time is slow enough that people stop doing it daily, and a grade you
    stop collecting is worth nothing.

    Never fatal: the oracle is evidence, not a dependency. If yente is down the run
    proceeds and the verdict column is simply absent for those decisions.
    """
    if oracle is None:
        return {}
    try:
        queries = {
            str(r["id"]): {
                "name": r["name"],
                "schema": "Person" if r["entity_type"] == "Individual" else "Organization",
                "dob": r["dob"],
            }
            for r in rows
        }
        hits = oracle.match(queries)
    except Exception:
        return {}

    out: dict[int, str] = {}
    for key, results in hits.items():
        if results:
            top = results[0]
            out[int(key)] = f"HIT {top.sdn_uid} {top.score:.3f}"
        else:
            out[int(key)] = "NO HIT"
    return out


def rescreen_book(conn: psycopg.Connection[DictRow], *, run_id: int, matcher: Matcher,
                  adjudicator: Adjudicator, publication: dict,
                  batch_size: int = BATCH_SIZE, blocked_on: date | None = None,
                  stop_after_batches: int | None = None,
                  oracle=None, on_decision=None, second_opinion=None) -> RunSummary:
    """Screen the entire counterparty book under a run.

    `stop_after_batches` exists for the kill-worker demo beat: it simulates a worker
    dying mid-book so that resuming can be shown to pick up the unfinished range rather
    than skipping it.

    `oracle` is an optional yente client. When supplied, every adjudication is stored
    with the independent oracle's verdict beside it.

    `on_decision(name, decision)` is called as each decision commits. A full-book run
    takes minutes and printed nothing at all until the closing summary, which left an
    operator unable to tell a working run from a hung one -- and made the run
    unwatchable. The callback reports only fields the Decision already carries; it
    never computes or reformats a verdict, so what it shows cannot drift from what the
    ledger stored.
    """
    summary = RunSummary(run_id=run_id)

    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) AS max_id FROM counterparties")
        max_id = one(cur)["max_id"]
    if max_id == 0:
        return summary

    while True:
        if stop_after_batches is not None and summary.batches >= stop_after_batches:
            break

        claimed = claim_batch(conn, run_id, batch_size, max_id)
        if claimed is None:
            break
        start, end = claimed

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, dob, origin, entity_type FROM counterparties "
                "WHERE id BETWEEN %s AND %s ORDER BY id",
                (start, end),
            )
            rows = cur.fetchall()

        oracle_says = _oracle_verdicts(oracle, rows)

        for row in rows:
            decision = screen_counterparty(
                conn, run_id=run_id, counterparty_id=row["id"], name=row["name"],
                dob=row["dob"], is_person=(row["entity_type"] == "Individual"),
                matcher=matcher, adjudicator=adjudicator,
                publication=publication, blocked_on=blocked_on,
                oracle_verdict=oracle_says.get(row["id"]),
                second_opinion=second_opinion,
            )
            summary.screened += 1
            summary.decisions.append(decision)
            if on_decision is not None:
                on_decision(row["name"], decision)
            if decision.verdict == "HOLD":
                summary.holds += 1
            elif decision.verdict == "QUARANTINE":
                summary.quarantined += 1
            else:
                summary.cleared += 1

        complete_batch(conn, run_id, start)
        summary.batches += 1

        # COMMIT PER BATCH. This is what makes the checkpointing in claim_batch real.
        #
        # Without it the entire re-screen is one transaction, and a genuine crash --
        # SIGKILL, OOM, a dead container -- rolls back every rescreen_batches row along
        # with the decisions, leaving nothing to resume from. The resume path would then
        # only ever work after a *graceful* stop, which is the one case that does not
        # need it. Committing here is safe precisely because screening is idempotent:
        # a re-claimed batch collides on the active-hold index and re-decides identically.
        conn.commit()

    # Close the run only when the whole book has actually been screened -- nothing
    # outstanding AND claimed coverage reaching the end. "No incomplete batches" alone
    # would mark a run finished when a worker died between batches, leaving the
    # unclaimed remainder unscreened and unreported.
    if run_is_complete(conn, run_id, max_id):
        with conn.cursor() as cur:
            cur.execute("UPDATE rescreen_runs SET finished_at = now() WHERE id = %s", (run_id,))
        emit(conn, "RUN_COMPLETED", {
            "run_id": run_id, "screened": summary.screened, "holds": summary.holds,
            "cleared": summary.cleared, "quarantined": summary.quarantined,
        })

    return summary


def apply_delta_removals(conn: psycopg.Connection[DictRow], removals, *, delta_source_hash: str,
                         ) -> list[int]:
    """Release funds for every held counterparty that OFAC just delisted.

    Matching is by the SDN uid recorded on the hold's match, not by name -- the party
    whose money is released must be the party Treasury actually removed.
    """
    from .money import release_hold

    released: list[int] = []
    removed_uids = {r.uid for r in removals}
    if not removed_uids:
        return released

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT h.counterparty_id, m.sdn_uid
            FROM holds h
            JOIN adjudications a ON a.id = h.adjudication_id
            JOIN matches m       ON m.id = a.match_id
            WHERE h.released_at IS NULL AND m.sdn_uid = ANY(%s)
            """,
            (list(removed_uids),),
        )
        targets = cur.fetchall()

    for target in targets:
        count = release_hold(conn, counterparty_id=target["counterparty_id"],
                             sdn_uid=target["sdn_uid"],
                             delta_source_hash=delta_source_hash)
        if count:
            released.append(target["counterparty_id"])
    return released
