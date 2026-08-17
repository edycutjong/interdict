"""Database access.

Connection details come from one env var so the same code runs against Docker Postgres
locally and Cloud SQL in production -- there is no GCP SDK in this module by design.

The one architectural rule enforced here: **ledger rows are written only by the outbox
relay** (audit F3, option a). Agents append to `outbox` inside their own transaction and
the relay is the single writer that materialises ledger entries. That is what makes the
hash chain linear under concurrency, and it is why `append_ledger` is private to this
module rather than exported for agents to call.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get(
    "INTERDICT_DSN",
    "postgresql://interdict:interdict@localhost:5433/interdict",
)


@contextmanager
def connect(dsn: str | None = None) -> Iterator[psycopg.Connection]:
    with psycopg.connect(dsn or DSN, row_factory=dict_row) as conn:
        yield conn


def emit(conn: psycopg.Connection, topic: str, payload: dict[str, Any]) -> int:
    """Append an event to the transactional outbox.

    Called inside the caller's transaction, so an event is published if and only if the
    decision that produced it committed. This is the only way an agent records anything
    durable about a decision.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO outbox (topic, payload) VALUES (%s, %s) RETURNING id",
            (topic, json.dumps(payload, sort_keys=True)),
        )
        return cur.fetchone()["id"]


def relay(conn: psycopg.Connection, limit: int = 500) -> int:
    """Drain the outbox into the ledger. THE single ledger writer.

    Rows are claimed with FOR UPDATE SKIP LOCKED so multiple relay instances are safe;
    the ledger's own advisory lock is what keeps the resulting chain linear. Returns the
    number of events relayed.
    """
    relayed = 0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, topic, payload FROM outbox
            WHERE published_at IS NULL
            ORDER BY id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (limit,),
        )
        for row in cur.fetchall():
            cur.execute(
                "INSERT INTO ledger (event_type, payload) VALUES (%s, %s)",
                (row["topic"], json.dumps(row["payload"], sort_keys=True)),
            )
            cur.execute(
                "UPDATE outbox SET published_at = now() WHERE id = %s", (row["id"],)
            )
            relayed += 1
    return relayed


def verify_chain(conn: psycopg.Connection) -> tuple[bool, int]:
    """Verify the ledger hash chain end to end.

    Returns (intact, entry_count). This is what `make verify-ledger` runs and what goes
    on camera -- a hash chain nobody checks is decoration.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bool_and(prev_hash = lag_entry) AS intact, count(*) AS n
            FROM (
                SELECT prev_hash, lag(entry_hash) OVER (ORDER BY seq) AS lag_entry
                FROM ledger
            ) t
            WHERE lag_entry IS NOT NULL
            """
        )
        row = cur.fetchone()
        cur.execute("SELECT count(*) AS total FROM ledger")
        total = cur.fetchone()["total"]
    # A chain of 0 or 1 entries is trivially intact.
    return (True if row["intact"] is None else bool(row["intact"])), total


# ---------------------------------------------------------------------------
# Re-screen checkpointing (audit F4)
# ---------------------------------------------------------------------------

def claim_batch(conn: psycopg.Connection, run_id: int, size: int,
                max_id: int) -> tuple[int, int] | None:
    """Claim the next unclaimed batch of counterparty ids for a run.

    Batches are recorded individually rather than tracked by a moving cursor, because
    workers complete out of order and an optimistically-advanced cursor would resume
    past unfinished batches -- silently leaving counterparties unscreened.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(max(batch_end), 0) AS last FROM rescreen_batches WHERE run_id = %s",
            (run_id,),
        )
        start = cur.fetchone()["last"] + 1
        if start > max_id:
            return None
        end = min(start + size - 1, max_id)
        cur.execute(
            "INSERT INTO rescreen_batches (run_id, batch_start, batch_end) VALUES (%s,%s,%s)",
            (run_id, start, end),
        )
        return start, end


def complete_batch(conn: psycopg.Connection, run_id: int, batch_start: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE rescreen_batches SET completed_at = now() "
            "WHERE run_id = %s AND batch_start = %s",
            (run_id, batch_start),
        )


def resume_point(conn: psycopg.Connection, run_id: int) -> int | None:
    """The only safe resume point: MIN(batch_start) over INCOMPLETE batches.

    Never max(completed), never a scalar cursor. If batches 1-500 and 1001-1500 are
    done but 501-1000 died with its worker, resuming at 1501 would leave 500
    counterparties unscreened against a live sanctions publication.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT min(batch_start) AS resume FROM rescreen_batches "
            "WHERE run_id = %s AND completed_at IS NULL",
            (run_id,),
        )
        return cur.fetchone()["resume"]
