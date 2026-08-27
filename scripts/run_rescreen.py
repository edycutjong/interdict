#!/usr/bin/env python3
"""Run a full-book re-screen -- what the 6-hourly poll starts on its own.

scripts/archive_delta.py spawns this with --trigger SCHEDULER, under a lock, when Treasury
publishes a content hash the archive has not seen. The launchd timer in ops/ arms that. It
is not Cloud Scheduler and there is no Google Cloud compute in this build; see README.md,
"On what is not here". Run it by hand any time -- the trigger column records which it was.

    python scripts/run_rescreen.py                      # screen the whole book
    python scripts/run_rescreen.py --kill-after 1       # die mid-book (demo beat B5)
    python scripts/run_rescreen.py --resume RUN_ID      # pick the unfinished range back up

The adjudicator is Gemini. Running without a key is a hard error rather than a silent
downgrade -- the stand-in is opt-in via --offline and announces itself on every line, so
a run whose verdicts came from a test double can never be mistaken for the product path.

When INTERDICT_FIRESTORE_PROJECT is set the committed ledger is mirrored to Cloud
Firestore at the end of the run, which is where the audit trail becomes readable without
this machine.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interdict.adjudicator import FREE_TIER_RPM
from interdict.cloud import mirror, publish_ledger, publish_run_summary
from interdict.db import (
    connect,
    relay,
    resume_point,
    run_is_complete,
    verify_chain,
)
from interdict.matcher import Matcher
from interdict.ofac import parse_sdn
from interdict.oracle import Oracle
from interdict.rescreen import open_run, rescreen_book


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

    # Fail before the run opens rather than on the first adjudication. A typo in
    # INTERDICT_MODEL used to surface after a run had claimed batches and taken a lock.
    if not adj.model_available():
        raise SystemExit(
            f"\nModel {adj.model_id!r} is not served to this key.\n"
            "Check INTERDICT_MODEL, or list what is available with\n"
            "  python -c \"from google import genai,os; "
            "print([m.name for m in genai.Client(api_key=os.environ['GEMINI_API_KEY'])"
            ".models.list()])\"\n"
        )

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
    ap.add_argument("--progress", action="store_true",
                    help="print each decision as it commits, instead of only the summary")
    ap.add_argument("--second-model", action="store_true",
                    help="also record an independent Gemma verdict on every adjudication "
                         "(evidence only -- it cannot change a decision)")
    ap.add_argument("--budget-only", action="store_true",
                    help="count tokens and print what the run would cost, then exit "
                         "without spending any quota")
    ap.add_argument("--no-oracle", action="store_true",
                    help="skip yente grading (faster; the oracle column stays empty)")
    args = ap.parse_args()

    entries, publication = parse_sdn(args.sdn)
    matcher = Matcher(entries)
    adjudicator = build_adjudicator(args.offline)

    # --budget-only: what would this run cost, before any of it is spent.
    #
    # The book is screened deterministically -- no model is called -- and a context is
    # built for exactly the counterparties that would reach adjudication. That set is
    # the real denominator: screening 536 rows does not mean 536 model calls, because
    # anything below T_HI is decided by the deterministic plane for free.
    if args.budget_only:
        if args.offline:
            raise SystemExit("--budget-only needs the real adjudicator; drop --offline.")
        from interdict.adjudicator import budget_run, build_context
        from interdict.matcher import T_HI

        contexts = []
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name, dob, entity_type FROM counterparties ORDER BY id")
            rows = cur.fetchall()
        for row in rows:
            is_person = row["entity_type"] == "Individual"
            results = matcher.screen(row["name"], row["dob"], is_person=is_person)
            if not results:
                continue
            match = results[0]
            if match.score < T_HI:
                continue
            contexts.append(build_context(
                row["name"], row["dob"], "Individual" if is_person else "Entity",
                match, matcher.entries[match.sdn_uid], publication))

        b = budget_run(adjudicator, contexts)
        print()
        print(f"  book                 {len(rows)} counterparties")
        print(f"  reach adjudication   {b['contexts']}  (the rest are decided for free)")
        print(f"  tokens/call (mean)   {b.get('tokens_per_call_mean', 0)}  "
              f"sampled {b['sampled']} via count_tokens")
        print(f"  tokens total         {b['tokens_total']:,}")
        print(f"  wall clock           ~{b['minutes']} min at {FREE_TIER_RPM} req/min "
              f"(free tier paces this, not the tokens)")
        print()
        print("  Nothing was adjudicated and no quota was spent.")
        return 0

    second_opinion = None
    if args.second_model and not args.offline:
        from interdict.adjudicator import GemmaSecondOpinion
        second_opinion = GemmaSecondOpinion(adjudicator._client)
        print(f"second opinion: Gemma ({second_opinion.model_id}) -- recorded, never a vote")

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

        def report(name: str, d) -> None:
            """One line per decision, printed as it commits.

            Every field is read straight off the Decision -- nothing here recomputes a
            verdict or rounds a score into a different number than the ledger holds. The
            guard column is printed even when it says SKIPPED, because a run where the
            oracle went unreachable must look different from one where it agreed.
            """
            uid = d.sdn_uid or "--"
            print(f"  [{d.verdict:<10}] {name[:38]:<38} "
                  f"score {d.det_score:.4f}  uid {uid:<6} "
                  f"guard {d.guard:<8} rt {d.round_trips}  {d.reason[:52]}",
                  flush=True)

        try:
            summary = rescreen_book(
                conn, run_id=run_id, matcher=matcher, adjudicator=adjudicator,
                publication=publication, batch_size=args.batch_size,
                blocked_on=date(2026, 8, 17), stop_after_batches=args.kill_after,
                oracle=oracle, on_decision=report if args.progress else None,
                second_opinion=second_opinion,
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
            print("\n  RUN INCOMPLETE -- the run did NOT mark itself finished.")
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

        # The cloud evidence plane. Deliberately after the chain check and after the
        # commit above: Firestore mirrors decisions that are already durable, and a
        # publish failure must never be able to unmake one.
        mir = mirror()
        if mir is None:
            print("  firestore: off (set INTERDICT_FIRESTORE_PROJECT to mirror)")
        else:
            published = publish_ledger(conn, mir)
            publish_run_summary(conn, run_id, mir)
            print(f"  firestore: +{published} ledger entries, run summary published "
                  f"-> {mir.project}/{mir.database}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
