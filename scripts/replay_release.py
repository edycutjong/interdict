#!/usr/bin/env python3
"""RELEASE leg -- labelled REPLAY of the real 2026-08-07 delisting.

WHAT IS REAL AND WHAT IS NOT. Everything that drives a decision here is Treasury's:
the eight parties, their uids, their names, their sanctions programs, and the fact that
OFAC removed them are all read out of the archived `/changes/latest` publication
(sha256 9403f40d9496..., fetched 2026-08-12 and committed). What is ours is the payment
book -- the disbursements those parties are receiving -- which is synthetic and is
labelled synthetic everywhere it appears.

WHY IT IS A REPLAY AND SAYS SO. Those eight are already gone from the 08/07 publication,
so a live release cannot be staged against today's list without pretending. Instead the
pre-removal list state is reconstructed from the delta's own records, the parties are
held against it, and then the real removal actions are applied. Every step is Treasury
data; none of it is a live event, and nothing on screen implies otherwise.

The sealed sentinel book remains the path to a genuinely live release: if OFAC delists
anyone in it, git log proves the book predates the removal. That is upside, not the plan.

    python scripts/replay_release.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interdict.adjudicator import RuleBasedAdjudicator
from interdict.db import connect, relay, verify_chain
from interdict.matcher import Matcher
from interdict.money import draft_report
from interdict.ofac import Name, SdnEntry, parse_delta, parse_sdn
from interdict.orchestrator import screen_counterparty
from interdict.rescreen import apply_delta_removals, open_run

DELTA = Path("data/archive/delta-20260812T221237+0000-9403f40d9496.xml")
DELTA_SHA = "9403f40d949617405bdcddd25bb1f3784ce6ba25a7137101d5b27742ddeb05bc"


def reconstruct(action) -> SdnEntry:
    """Rebuild a removed party's SDN entry from the delta's own record of it."""
    names = tuple(Name(n, "primary" if n == action.name else "strong",
                       "primary" if n == action.name else "a.k.a.")
                  for n in dict.fromkeys(action.names or [action.name]))
    return SdnEntry(
        uid=action.uid,
        sdn_type=action.entity_type or "Individual",
        primary_name=action.name,
        names=names or (Name(action.name, "primary", "primary"),),
        programs=action.programs,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdn", type=Path, default=Path("data/SDN.XML"))
    ap.add_argument("--delta", type=Path, default=DELTA)
    args = ap.parse_args()

    actions = parse_delta(args.delta)
    removals = [a for a in actions if a.action == "remove"]
    entries, publication = parse_sdn(args.sdn)

    print("=" * 74)
    print("  REPLAY -- real OFAC delisting of 2026-08-07, replayed against a")
    print("  SYNTHETIC payment book. Not a live event.")
    print(f"  delta sha256 {DELTA_SHA[:24]}...  ({len(removals)} removals)")
    print("=" * 74)

    # Pre-removal list state: the publication plus the eight parties as they stood
    # before Treasury removed them.
    reconstructed = [reconstruct(a) for a in removals]
    pre_removal = Matcher(entries + reconstructed)
    print(f"\n  reconstructed pre-removal list: {len(entries)} + {len(reconstructed)} "
          f"delisted parties")

    with connect() as conn:
        # Seed the delisted parties into the book with money queued to them.
        with conn.cursor() as cur:
            for a in removals:
                cur.execute(
                    # expected_verdict is deliberately NULL: these eight are held and
                    # then RELEASED on purpose, so grading them on screening quality
                    # would score correct behaviour as eight errors. They exercise the
                    # release leg, not the screening decision.
                    """
                    INSERT INTO counterparties
                        (external_ref,name,dob,entity_type,origin,expected_verdict,source)
                    VALUES (%s,%s,NULL,%s,'sentinel',NULL,%s)
                    ON CONFLICT (external_ref) DO NOTHING
                    RETURNING id
                    """,
                    (f"replay:{a.uid}", a.name, a.entity_type or "Individual",
                     f"ofac-delta-2026-08-07:{a.uid}"),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "INSERT INTO disbursements (counterparty_id,amount_cents,memo) "
                        "VALUES (%s,%s,'Humanitarian disbursement (SYNTHETIC)')",
                        (row["id"], 250_000 + int(a.uid) % 750_000),
                    )

            cur.execute("SELECT id, name, entity_type FROM counterparties "
                        "WHERE external_ref LIKE 'replay:%' ORDER BY id")
            targets = cur.fetchall()

        # ---- Step 1: screen against the PRE-REMOVAL list -> these must HOLD ----
        run_id = open_run(conn, published_at=date(2026, 8, 7),
                          source_hash=DELTA_SHA + "-prereplay", kind="DELTA",
                          trigger="CHALLENGE")
        print(f"\n  [1] screening against the pre-removal list (run {run_id})")
        held = 0
        for t in targets:
            decision = screen_counterparty(
                conn, run_id=run_id, counterparty_id=t["id"], name=t["name"], dob=None,
                is_person=(t["entity_type"] == "Individual"),
                matcher=pre_removal, adjudicator=RuleBasedAdjudicator(),
                publication=publication, blocked_on=date(2026, 8, 17))
            flag = "HELD" if decision.verdict == "HOLD" else decision.verdict
            held += decision.verdict == "HOLD"
            print(f"      {flag:<11} {decision.det_score:<8} {t['name'][:46]}")

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n, coalesce(sum(amount_cents),0) AS total "
                        "FROM disbursements d JOIN counterparties c ON c.id=d.counterparty_id "
                        "WHERE c.external_ref LIKE 'replay:%' AND d.state='HELD'")
            frozen = cur.fetchone()
        print(f"      -> {held}/{len(targets)} held, "
              f"${frozen['total'] / 100:,.2f} frozen")

        # ---- Step 2: draft the statutory reports on the 10-business-day clock ----
        with conn.cursor() as cur:
            cur.execute("SELECT h.id FROM holds h JOIN counterparties c "
                        "ON c.id=h.counterparty_id WHERE c.external_ref LIKE 'replay:%' "
                        "AND h.released_at IS NULL ORDER BY h.id LIMIT 1")
            first = cur.fetchone()
        if first:
            report = draft_report(conn, first["id"], entity="Synthetic NGO (DEMO)")
            print(f"\n  [2] blocking report drafted, due {report.due} "
                  f"(10 business days)")

        # ---- Step 3: apply Treasury's real removals -> RELEASE ----
        print("\n  [3] applying the real 2026-08-07 removals")
        released = apply_delta_removals(conn, removals, delta_source_hash=DELTA_SHA)
        relay(conn)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT d.state, count(*) AS n, sum(d.amount_cents) AS total "
                        "FROM disbursements d JOIN counterparties c "
                        "ON c.id=d.counterparty_id WHERE c.external_ref LIKE 'replay:%' "
                        "GROUP BY d.state ORDER BY d.state")
            after = cur.fetchall()
        print(f"      -> released {len(released)} counterparties")
        for row in after:
            print(f"         {row['state']:<10} {row['n']:>3}  ${row['total'] / 100:,.2f}")

        with conn.cursor() as cur:
            cur.execute("SELECT event_type, count(*) AS n FROM ledger "
                        "WHERE event_type IN ('HOLD_PLACED','HOLD_RELEASED','REPORT_DRAFTED') "
                        "GROUP BY event_type ORDER BY event_type")
            events = cur.fetchall()
        intact, total = verify_chain(conn)
        print(f"\n  ledger: {total} entries, chain {'INTACT' if intact else 'FORKED'}")
        for e in events:
            print(f"    {e['event_type']:<16} {e['n']}")

    print("\n  Every party, uid, programme and removal above is Treasury's.")
    print("  The payment book is synthetic and labelled synthetic.")

    # A forked chain is the one failure this system exists to make impossible, and until now
    # this script printed the word FORKED and exited 0 -- so the demo would have shown a green
    # prompt under a broken audit trail, and any CI step running it would have passed.
    if not intact:
        print("\n  FAIL: the ledger chain is FORKED. Nothing else on this run is trustworthy.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
