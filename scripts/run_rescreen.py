#!/usr/bin/env python3
"""Run a full-book re-screen -- the loop Cloud Scheduler triggers unattended.

    python scripts/run_rescreen.py                      # screen the whole book
    python scripts/run_rescreen.py --kill-after 1       # die mid-book (demo beat B5)
    python scripts/run_rescreen.py --resume RUN_ID      # pick the unfinished range back up

The adjudicator defaults to Gemini and falls back to the offline rule-based stand-in
when no API key is present, so the pipeline is runnable and demonstrable before the key
lands. The fallback is announced loudly on stdout -- a screening run whose verdicts came
from a test double must never be mistaken for the product path.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interdict.db import (connect, relay, resume_point, run_is_complete,  # noqa: E402
                          verify_chain)
from interdict.matcher import Matcher                                  # noqa: E402
from interdict.ofac import parse_sdn                                   # noqa: E402
from interdict.oracle import Oracle                                    # noqa: E402
from interdict.rescreen import open_run, rescreen_book                 # noqa: E402


def build_adjudicator(force_offline: bool):
    """Resolve the adjudication plane.

    THE DEFAULT PATH IS THE REAL ONE. Running without a key used to fall back to the
    offline stand-in automatically, which meant the command in the README quietly did
    not exercise the product -- the exact shape of failure where a project ships a
    reproduce command that disables the thing being judged. Now the fallback is opt-in:
    no key and no --offline is a hard error, so a stand-in run can only ever happen
    because someone asked for one.
    """
    if force_offline:
        from interdict.adjudicator import RuleBasedAdjudicator
        print("!" * 72)
        print("!! --offline: RULE-BASED STAND-IN, NOT THE PRODUCT PATH.")
        print("!! No model was consulted. These verdicts are deterministic rules and")
        print("!! must not be presented as Gemini adjudications.")
        print("!" * 72)
        return RuleBasedAdjudicator()

    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        raise SystemExit(
            "\nNo GEMINI_API_KEY set.\n\n"
            "The adjudication plane is half of this system and Gemini is required tech\n"
            "for this hackathon, so this command will not silently substitute a test\n"
            "double for it.\n\n"
            "  Free key, no billing account needed: https://aistudio.google.com/apikey\n"
            "  export GEMINI_API_KEY=...\n\n"
            "To run the deterministic plane alone -- useful for CI, and the only honest\n"
            "way to describe such a run -- pass --offline explicitly.\n"
        )

    from interdict.adjudicator import GeminiAdjudicator
    adj = GeminiAdjudicator()
    print(f"adjudicator: Gemini ({adj.model_id})")
    return adj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdn", type=Path, default=Path("data/SDN.XML"))
    ap.add_argument("--trigger", default="MANUAL",
                    choices=["SCHEDULER", "DELTA", "MANUAL", "CHALLENGE"])
    ap.add_argument("--batch-size", type=int, default=100)
    ap.add_argument("--kill-after", type=int, default=None,
                    help="stop after N batches, simulating a worker dying mid-book")
    ap.add_argument("--resume", type=int, default=None, help="resume an existing run id")
    ap.add_argument("--offline", action="store_true", help="force the offline adjudicator")
    ap.add_argument("--no-oracle", action="store_true",
                    help="skip yente grading (faster; the oracle column stays empty)")
    args = ap.parse_args()

    entries, publication = parse_sdn(args.sdn)
    matcher = Matcher(entries)
    adjudicator = build_adjudicator(args.offline)

    with connect() as conn:
        if args.resume:
            run_id = args.resume
            with conn.cursor() as cur:
                cur.execute("SELECT coalesce(max(id),0) AS m FROM counterparties")
                max_id = cur.fetchone()["m"]
            if run_is_complete(conn, run_id, max_id):
                print(f"run {run_id} is complete -- nothing to resume")
                return 0
            point = resume_point(conn, run_id)
            if point is not None:
                print(f"resuming run {run_id} at counterparty id {point} "
                      f"(MIN(batch_start) over ABANDONED batches)")
            else:
                print(f"resuming run {run_id} -- no abandoned batch, but claimed "
                      f"coverage stops short of the book; continuing from there")
        else:
            run_id = open_run(
                conn,
                published_at=date(2026, 8, 7),
                source_hash="ac00228a68345e5c0d7174713cf97e5d5a8efe7cec5c2f540ed87106f49f7474",
                kind="SDN", trigger=args.trigger,
                record_count=int(publication["record_count"]),
            )
            print(f"opened run {run_id} against publication {publication['publish_date']}")

        oracle = None
        if not args.no_oracle:
            candidate = Oracle()
            if candidate.healthy():
                oracle = candidate
                print("oracle: yente /match/us_ofac_sdn (every decision graded)")
            else:
                candidate.close()
                print("oracle: yente unreachable -- decisions will be ungraded")

        try:
            summary = rescreen_book(
                conn, run_id=run_id, matcher=matcher, adjudicator=adjudicator,
                publication=publication, batch_size=args.batch_size,
                blocked_on=date(2026, 8, 17), stop_after_batches=args.kill_after,
                oracle=oracle,
            )
        finally:
            if oracle is not None:
                oracle.close()
        relay(conn)
        conn.commit()

        print()
        print(f"  screened     {summary.screened}")
        print(f"  HELD         {summary.holds}")
        print(f"  cleared      {summary.cleared}")
        print(f"  quarantined  {summary.quarantined}")
        print(f"  batches      {summary.batches}")

        with conn.cursor() as cur:
            cur.execute("SELECT coalesce(max(id),0) AS m FROM counterparties")
            book_max = cur.fetchone()["m"]
        if not run_is_complete(conn, run_id, book_max):
            with conn.cursor() as cur:
                cur.execute("SELECT coalesce(max(batch_end),0) AS covered "
                            "FROM rescreen_batches WHERE run_id=%s", (run_id,))
                covered = cur.fetchone()["covered"]
            print(f"\n  RUN INCOMPLETE -- the run did NOT mark itself finished.")
            print(f"  claimed coverage reaches id {covered} of {book_max}; "
                  f"abandoned batch at {resume_point(conn, run_id)}")
            print(f"  python scripts/run_rescreen.py --resume {run_id}")

        with conn.cursor() as cur:
            cur.execute("SELECT state, count(*) AS n, sum(amount_cents) AS total "
                        "FROM disbursements GROUP BY state ORDER BY state")
            print("\n  money:")
            for row in cur.fetchall():
                print(f"    {row['state']:<10} {row['n']:>4}  ${row['total'] / 100:,.2f}")

        intact, total = verify_chain(conn)
        print(f"\n  ledger: {total} entries, chain {'INTACT' if intact else 'FORKED'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
