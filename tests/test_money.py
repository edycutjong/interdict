"""Money-plane tests: holds, releases, and the blocking report.

Every assertion here is about a consequence -- money that stopped, money that moved, or
a statutory deadline. Requires the local stack (`make up`).
"""

from datetime import date

import psycopg
import pytest

from interdict.db import DSN, connect, relay, verify_chain
from interdict.money import draft_report, overdue_reports, place_hold, release_hold


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


def _seed(conn, *, name="Ibrahim Al Rashid", amounts=(50_000, 25_000)):
    """A counterparty with queued disbursements and one adjudicated HOLD verdict."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO counterparties (external_ref,name,origin,source) "
                    "VALUES ('cp-1',%s,'sentinel','test') RETURNING id", (name,))
        cp = cur.fetchone()["id"]
        for amount in amounts:
            cur.execute("INSERT INTO disbursements (counterparty_id,amount_cents) "
                        "VALUES (%s,%s)", (cp, amount))
        cur.execute("INSERT INTO list_versions (published_at,source_hash,kind) "
                    "VALUES ('2026-08-07','sdn-hash','SDN') RETURNING id")
        lv = cur.fetchone()["id"]
        cur.execute("INSERT INTO rescreen_runs (list_version_id,trigger) "
                    "VALUES (%s,'DELTA') RETURNING id", (lv,))
        run = cur.fetchone()["id"]
        cur.execute("INSERT INTO matches (run_id,counterparty_id,sdn_uid,det_score,components) "
                    "VALUES (%s,%s,'2674',0.9700,"
                    "'{\"matched_name\":\"Abu ABBAS\",\"matched_category\":\"primary\"}') "
                    "RETURNING id", (run, cp))
        m = cur.fetchone()["id"]
        cur.execute("INSERT INTO adjudications (match_id,verdict,rationale,model_id,"
                    "prompt_hash,context,oracle_guard_result,yente_verdict) "
                    "VALUES (%s,'HOLD','Primary name matches SDN 2674 exactly.',"
                    "'gemini','ph','{}','AGREE','HIT 1.000') RETURNING id", (m,))
        adj = cur.fetchone()["id"]
    return cp, adj


def _states(conn, cp):
    with conn.cursor() as cur:
        cur.execute("SELECT state, count(*) AS n FROM disbursements "
                    "WHERE counterparty_id=%s GROUP BY state", (cp,))
        return {r["state"]: r["n"] for r in cur.fetchall()}


def test_hold_stops_every_queued_disbursement(conn):
    cp, adj = _seed(conn)
    result = place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674")
    assert result.created and result.disbursements_held == 2
    assert _states(conn, cp) == {"HELD": 2}


def test_hold_is_idempotent(conn):
    """A scheduler retry or a redelivered Pub/Sub message must not double-hold."""
    cp, adj = _seed(conn)
    first = place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674")
    second = place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674")
    assert first.created and not second.created
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM holds WHERE counterparty_id=%s", (cp,))
        assert cur.fetchone()["n"] == 1


def test_hold_does_not_touch_paid_money(conn):
    """Money that already left cannot be frozen; the state machine would reject it."""
    cp, adj = _seed(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM disbursements WHERE counterparty_id=%s ORDER BY id", (cp,))
        first = cur.fetchall()[0]["id"]
        cur.execute("UPDATE disbursements SET state='CLEARED' WHERE id=%s", (first,))
        cur.execute("UPDATE disbursements SET state='PAID' WHERE id=%s", (first,))
    result = place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674")
    assert result.disbursements_held == 1
    assert _states(conn, cp) == {"PAID": 1, "HELD": 1}


def test_hold_writes_a_ledger_entry(conn):
    cp, adj = _seed(conn)
    place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674")
    relay(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT event_type, payload FROM ledger ORDER BY seq DESC LIMIT 1")
        row = cur.fetchone()
    assert row["event_type"] == "HOLD_PLACED"
    assert row["payload"]["sdn_uid"] == "2674"
    intact, _ = verify_chain(conn)
    assert intact


def test_report_deadline_is_recorded_on_the_hold(conn):
    cp, adj = _seed(conn)
    place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674",
               blocked_on=date(2026, 8, 17))
    with conn.cursor() as cur:
        cur.execute("SELECT report_due_at FROM holds WHERE counterparty_id=%s", (cp,))
        assert cur.fetchone()["report_due_at"] == date(2026, 8, 31)


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------

def test_release_moves_money_again(conn):
    cp, adj = _seed(conn)
    place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674")
    released = release_hold(conn, counterparty_id=cp, sdn_uid="2674",
                            delta_source_hash="delta-abc")
    assert released == 2
    assert _states(conn, cp) == {"CLEARED": 2}


def test_release_is_a_noop_when_nothing_is_held(conn):
    cp, _ = _seed(conn)
    assert release_hold(conn, counterparty_id=cp, sdn_uid="2674",
                        delta_source_hash="delta-abc") == 0


def test_released_hold_row_is_kept_not_deleted(conn):
    """Nothing is ever deleted -- the freeze must stay visible after the release."""
    cp, adj = _seed(conn)
    place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674")
    release_hold(conn, counterparty_id=cp, sdn_uid="2674", delta_source_hash="d")
    with conn.cursor() as cur:
        cur.execute("SELECT released_at FROM holds WHERE counterparty_id=%s", (cp,))
        rows = cur.fetchall()
    assert len(rows) == 1 and rows[0]["released_at"] is not None


def test_redesignation_after_release_can_hold_again(conn):
    """Delisting followed by re-designation is normal and must not hit the unique index."""
    cp, adj = _seed(conn)
    place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674")
    release_hold(conn, counterparty_id=cp, sdn_uid="2674", delta_source_hash="d1")
    again = place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674")
    assert again.created


def test_release_records_the_authorising_delta(conn):
    """A release must be traceable to the published delta that authorised it."""
    cp, adj = _seed(conn)
    place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674")
    release_hold(conn, counterparty_id=cp, sdn_uid="2674", delta_source_hash="delta-9403f40d")
    relay(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM ledger WHERE event_type='HOLD_RELEASED' "
                    "ORDER BY seq DESC LIMIT 1")
        assert cur.fetchone()["payload"]["authorised_by_delta"] == "delta-9403f40d"


# ---------------------------------------------------------------------------
# The blocking report
# ---------------------------------------------------------------------------

def test_report_is_drafted_from_persisted_facts(conn):
    cp, adj = _seed(conn)
    place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674",
               blocked_on=date(2026, 8, 17))
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM holds WHERE counterparty_id=%s", (cp,))
        hold_id = cur.fetchone()["id"]

    report = draft_report(conn, hold_id, entity="Test NGO")
    assert "Ibrahim Al Rashid" in report.text
    assert "2674" in report.text
    assert "750.00" in report.text            # 50,000 + 25,000 cents
    assert "Primary name matches SDN 2674 exactly." in report.text


def test_report_states_transmission_is_human(conn):
    """The system drafts and files; it never claims to have submitted to OFAC."""
    cp, adj = _seed(conn)
    place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM holds WHERE counterparty_id=%s", (cp,))
        hold_id = cur.fetchone()["id"]
    text = draft_report(conn, hold_id).text
    assert "NOT TRANSMITTED" in text
    assert "TRANSMISSION IS A HUMAN STEP" in text


def test_report_is_filed_to_the_ledger(conn):
    cp, adj = _seed(conn)
    place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM holds WHERE counterparty_id=%s", (cp,))
        hold_id = cur.fetchone()["id"]
    draft_report(conn, hold_id)
    relay(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM ledger WHERE event_type='REPORT_DRAFTED'")
        row = cur.fetchone()
    assert row["payload"]["transmitted"] is False
    assert len(row["payload"]["report_sha256"]) == 64


def test_missing_hold_raises(conn):
    with pytest.raises(LookupError):
        draft_report(conn, 999999)


def test_overdue_reports_surface_past_deadlines(conn):
    cp, adj = _seed(conn)
    place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674",
               blocked_on=date(2026, 1, 5))
    overdue = overdue_reports(conn, as_of=date(2026, 8, 17))
    assert len(overdue) == 1 and overdue[0]["sdn_uid"] == "2674"


def test_released_holds_are_not_overdue(conn):
    cp, adj = _seed(conn)
    place_hold(conn, counterparty_id=cp, adjudication_id=adj, sdn_uid="2674",
               blocked_on=date(2026, 1, 5))
    release_hold(conn, counterparty_id=cp, sdn_uid="2674", delta_source_hash="d")
    assert overdue_reports(conn, as_of=date(2026, 8, 17)) == []
