"""The per-decision progress callback.

A full-book run printed nothing between "opened run" and the closing summary. On the
real book that is over three minutes of a blank terminal, in which a working run and a
hung one look exactly alike. `on_decision` closes that gap.

Two properties are worth pinning. It fires once per counterparty, in book order; and it
is handed the very Decision the run recorded, so a progress line cannot drift from what
the ledger stored. The second is identity, not equality, deliberately -- a callback that
received a copy could be reformatted into disagreeing with the ledger later.
"""

from datetime import date

import psycopg
import pytest

from interdict.adjudicator import RuleBasedAdjudicator
from interdict.db import DSN, connect
from interdict.matcher import Matcher
from interdict.ofac import Name, SdnEntry
from interdict.rescreen import open_run, rescreen_book

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
    ])


def _book(conn, names):
    """`origin` is constrained to the four real strata, so the fixture uses them: a
    designated name is a sentinel, anything else an ordinary grantee."""
    with conn.cursor() as cur:
        for i, name in enumerate(names):
            hit = name == "Abu ABBAS"
            cur.execute(
                "INSERT INTO counterparties (external_ref,name,entity_type,origin,"
                "expected_verdict,source) VALUES (%s,%s,'Individual',%s,%s,"
                "'test') RETURNING id",
                (f"cp-{i}", name, "sentinel" if hit else "ordinary",
                 "HOLD" if hit else "CLEAR"))
            cur.execute("INSERT INTO disbursements (counterparty_id,amount_cents) "
                        "VALUES (%s,10000)", (cur.fetchone()["id"],))


def _screen(conn, **kwargs):
    run_id = open_run(conn, published_at=date(2026, 8, 7), source_hash="hash",
                      kind="SDN", trigger="MANUAL", record_count=19199)
    return rescreen_book(conn, run_id=run_id, matcher=_matcher(),
                         adjudicator=RuleBasedAdjudicator(), publication=PUBLICATION,
                         blocked_on=date(2026, 8, 17), **kwargs)


NAMES = ["Abu ABBAS", "Jennifer Marie Thompson", "Abu ABBAS", "Raymond Charles Webb"]


def test_callback_fires_once_per_counterparty_in_book_order(conn):
    _book(conn, NAMES)
    seen = []
    summary = _screen(conn, batch_size=2, on_decision=lambda n, d: seen.append((n, d)))
    assert len(seen) == summary.screened == len(NAMES)
    assert [n for n, _ in seen] == NAMES


def test_callback_receives_the_recorded_decision_object(conn):
    _book(conn, NAMES)
    seen = []
    summary = _screen(conn, batch_size=2, on_decision=lambda n, d: seen.append(d))
    assert [id(d) for d in seen] == [id(d) for d in summary.decisions]


def test_callback_spans_batches(conn):
    """The callback must survive the batch boundary -- batching is where a
    fires-once-per-batch bug would hide."""
    _book(conn, NAMES)
    seen = []
    summary = _screen(conn, batch_size=1, on_decision=lambda n, d: seen.append(d))
    assert summary.batches == len(NAMES)
    assert len(seen) == len(NAMES)


def test_run_is_unchanged_without_a_callback(conn):
    _book(conn, NAMES)
    summary = _screen(conn, batch_size=2)
    assert summary.screened == len(NAMES)
    assert len(summary.decisions) == len(NAMES)
