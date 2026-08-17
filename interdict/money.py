"""The money plane -- holds, releases, and the statutory blocking report.

This is where a screening decision becomes a consequence: money stops, or money moves.
Three rules govern everything in this module.

1. **Every state change goes through the outbox in the caller's transaction.** The hold
   and its ledger event commit together or not at all. A hold that exists without a
   ledger entry is an unexplained freeze; a ledger entry without a hold is a lie.

2. **Placing a hold is idempotent.** Re-screening the same book against the same
   publication is normal and expected -- a scheduler retry, a redelivered Pub/Sub
   message, a resumed batch. None of them may double-hold. The database enforces this
   (holds_active_uniq); this module cooperates with it rather than checking first and
   racing.

3. **Releases are never inferred.** Money is only released when OFAC actually delists
   the party -- a `remove` action in a published delta -- and the released hold keeps
   its row forever. Nothing is deleted, ever.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

import psycopg
from psycopg.rows import DictRow

from .businessdays import REPORT_DEADLINE_BUSINESS_DAYS, report_due
from .db import emit, one


@dataclass(frozen=True)
class HoldResult:
    hold_id: int | None
    created: bool
    disbursements_held: int


def place_hold(conn: psycopg.Connection[DictRow], *, counterparty_id: int,
               adjudication_id: int, sdn_uid: str, blocked_on: date | None = None,
               ) -> HoldResult:
    """Freeze every queued disbursement to a counterparty. Idempotent.

    Returns created=False when an active hold already exists, which is a normal outcome
    on a retry and not an error.
    """
    blocked_on = blocked_on or date.today()
    due = report_due(blocked_on)

    with conn.cursor() as cur:
        # Claim the counterparty-level hold first. The partial unique index makes the
        # second caller lose here rather than in a check-then-act race.
        cur.execute(
            """
            INSERT INTO holds (counterparty_id, disbursement_id, adjudication_id, report_due_at)
            VALUES (%s, NULL, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (counterparty_id, adjudication_id, due),
        )
        row = cur.fetchone()
        if row is None:
            return HoldResult(None, created=False, disbursements_held=0)
        hold_id = row["id"]

        # Stop the money. Only QUEUED and CLEARED disbursements can be held; PAID has
        # already left and CANCELLED is terminal -- the state-machine trigger would
        # reject either, so they are excluded here rather than relying on the error.
        cur.execute(
            """
            UPDATE disbursements SET state = 'HELD'
            WHERE counterparty_id = %s AND state IN ('QUEUED','CLEARED')
            RETURNING id
            """,
            (counterparty_id,),
        )
        held = [r["id"] for r in cur.fetchall()]

    emit(conn, "HOLD_PLACED", {
        "hold_id": hold_id,
        "counterparty_id": counterparty_id,
        "adjudication_id": adjudication_id,
        "sdn_uid": sdn_uid,
        "blocked_on": blocked_on.isoformat(),
        "report_due_at": due.isoformat(),
        "report_deadline_business_days": REPORT_DEADLINE_BUSINESS_DAYS,
        "disbursements_held": held,
    })
    return HoldResult(hold_id, created=True, disbursements_held=len(held))


def release_hold(conn: psycopg.Connection[DictRow], *, counterparty_id: int,
                 sdn_uid: str, delta_source_hash: str) -> int:
    """Release funds after OFAC delists a party. Returns disbursements released.

    `delta_source_hash` is required, not optional: a release must be traceable to the
    exact published delta that authorised it. Releasing money on an unattributed signal
    is precisely the failure this system exists to prevent.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE holds SET released_at = now()
            WHERE counterparty_id = %s AND released_at IS NULL
            RETURNING id
            """,
            (counterparty_id,),
        )
        released_holds = [r["id"] for r in cur.fetchall()]
        if not released_holds:
            return 0

        cur.execute(
            """
            UPDATE disbursements SET state = 'CLEARED'
            WHERE counterparty_id = %s AND state = 'HELD'
            RETURNING id
            """,
            (counterparty_id,),
        )
        released = [r["id"] for r in cur.fetchall()]

    emit(conn, "HOLD_RELEASED", {
        "counterparty_id": counterparty_id,
        "sdn_uid": sdn_uid,
        "hold_ids": released_holds,
        "disbursements_released": released,
        "authorised_by_delta": delta_source_hash,
    })
    return len(released)


# ---------------------------------------------------------------------------
# The blocking report
# ---------------------------------------------------------------------------

REPORT_TEMPLATE = """\
OFAC BLOCKING REPORT (DRAFT — NOT TRANSMITTED)
Generated by Interdict on {generated_on}.

  Reporting entity      {entity}
  Blocked party         {party_name}
  OFAC SDN UID          {sdn_uid}
  Programs              {programs}
  Date blocked          {blocked_on}
  Report due            {due} ({days} business days from the blocking)
  Property blocked      {amount} {currency} across {count} queued disbursement(s)

  Basis for blocking
  {rationale}

  Deterministic screening score: {det_score} (matched {matched_name!r}, {matched_category})
  Independent oracle (OpenSanctions yente, scope us_ofac_sdn): {oracle}

TRANSMISSION IS A HUMAN STEP. Interdict drafts this report and files it to the
append-only ledger; it does not submit it to OFAC. Filing is done by the compliance
officer through OFAC Reporting System at https://ofac.treasury.gov.
"""


@dataclass(frozen=True)
class BlockingReport:
    hold_id: int
    text: str
    due: date


def draft_report(conn: psycopg.Connection[DictRow], hold_id: int, *,
                 entity: str = "Reporting entity (configure INTERDICT_ENTITY)",
                 ) -> BlockingReport:
    """Draft the OFAC blocking report for a hold and file it to the ledger.

    The draft is generated from persisted facts only -- the adjudication rationale, the
    deterministic score, the oracle's verdict -- so every line of it is traceable to a
    row rather than to a model's recollection at drafting time.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT h.id, h.report_due_at, h.placed_at,
                   c.name AS party_name, c.id AS counterparty_id,
                   a.rationale, a.verdict, a.yente_verdict,
                   m.sdn_uid, m.det_score, m.components
            FROM holds h
            JOIN counterparties c ON c.id = h.counterparty_id
            JOIN adjudications a ON a.id = h.adjudication_id
            JOIN matches m       ON m.id = a.match_id
            WHERE h.id = %s
            """,
            (hold_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"no hold {hold_id}")

        cur.execute(
            """
            SELECT coalesce(sum(amount_cents),0) AS total, count(*) AS n,
                   coalesce(min(currency),'USD') AS currency
            FROM disbursements WHERE counterparty_id = %s AND state = 'HELD'
            """,
            (row["counterparty_id"],),
        )
        money = one(cur)

    components = row["components"] or {}
    text = REPORT_TEMPLATE.format(
        generated_on=date.today().isoformat(),
        entity=entity,
        party_name=row["party_name"],
        sdn_uid=row["sdn_uid"],
        programs=components.get("programs", "see SDN record"),
        blocked_on=row["placed_at"].date().isoformat(),
        due=row["report_due_at"].isoformat(),
        days=REPORT_DEADLINE_BUSINESS_DAYS,
        amount=f"{money['total'] / 100:,.2f}",
        currency=money["currency"],
        count=money["n"],
        rationale=row["rationale"],
        det_score=row["det_score"],
        matched_name=components.get("matched_name", "?"),
        matched_category=components.get("matched_category", "?"),
        oracle=row["yente_verdict"] or "not recorded",
    )

    emit(conn, "REPORT_DRAFTED", {
        "hold_id": hold_id,
        "sdn_uid": row["sdn_uid"],
        "due": row["report_due_at"].isoformat(),
        "report_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "transmitted": False,
    })
    return BlockingReport(hold_id, text, row["report_due_at"])


def overdue_reports(conn: psycopg.Connection[DictRow], as_of: date | None = None) -> list[dict]:
    """Holds whose statutory report deadline has passed without a filing.

    Surfaced on the console because a missed deadline is its own violation, separate
    from the blocking itself.
    """
    as_of = as_of or date.today()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT h.id, h.report_due_at, c.name, m.sdn_uid
            FROM holds h
            JOIN counterparties c ON c.id = h.counterparty_id
            JOIN adjudications a ON a.id = h.adjudication_id
            JOIN matches m       ON m.id = a.match_id
            WHERE h.released_at IS NULL
              AND h.report_filed_at IS NULL
              AND h.report_due_at < %s
            ORDER BY h.report_due_at
            """,
            (as_of,),
        )
        return cur.fetchall()
