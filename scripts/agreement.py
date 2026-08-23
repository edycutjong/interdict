#!/usr/bin/env python3
"""Gate G1 -- screening quality, graded against an oracle we do not control.

The sealed sentinel book gives this harness something most screening demos lack:
**ground truth**. Every sentinel was drawn from the SDN list at seal time and carries
the uid it came from, so "did we find the right person?" is a fact, not an opinion.

Three numbers come out of this, and they mean different things:

  recall        we found the sentinel's true SDN uid in our results.  Ground truth.
  top1          that true uid was our BEST result, not merely present. Ground truth.
  agreement     our top uid equals yente's top uid.  Oracle comparison -- this is the
                number that survives the objection "you graded your own homework",
                because yente is an independent implementation.

Disagreements are printed, not summarised away. They are the interesting part, and the
README publishes the measured gap rather than hiding it.

Usage:
    python scripts/agreement.py --sdn data/SDN.XML --book data/sentinels.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interdict.matcher import T_HI, Matcher
from interdict.ofac import parse_sdn
from interdict.oracle import Oracle
from interdict.perturb import perturb

BATCH = 50


def load_book(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdn", type=Path, default=Path("data/SDN.XML"))
    ap.add_argument("--book", type=Path, default=Path("data/sentinels.csv"))
    ap.add_argument("--limit", type=int, default=0, help="screen only the first N rows")
    ap.add_argument("--json-out", type=Path, help="write the full result set here")
    ap.add_argument(
        "--perturb", action="store_true",
        help="screen DETERMINISTICALLY PERTURBED names instead of verbatim ones. "
             "This is the honest measurement: screening verbatim sentinels against "
             "the list they were copied from is a string-equality test, not a "
             "quality test.")
    ap.add_argument("--strategy", default=None,
                    help="force one perturbation kind (transliteration, reorder, "
                         "drop_particle, typo, drop_middle)")
    args = ap.parse_args()

    entries, publication = parse_sdn(args.sdn)
    matcher = Matcher(entries)
    book = load_book(args.book)
    if args.limit:
        book = book[: args.limit]

    print(f"publication {publication['publish_date']} "
          f"({publication['record_count']} records) | book {len(book)} entries")

    # Screening the book verbatim measures string equality, not screening quality.
    # --perturb replaces each name with a deterministic variant so the question becomes
    # the real one: given a name that is NOT on the list character-for-character, do we
    # still find the right person?
    if args.perturb:
        variants = {row["uid"]: perturb(row["name"], row["uid"], args.strategy)
                    for row in book}
        changed = sum(1 for p in variants.values() if p.changed)
        print(f"PERTURBED mode: {changed}/{len(book)} names altered "
              f"({len(book) - changed} unperturbable and screened verbatim)")
        for row in book:
            row["query_name"] = variants[row["uid"]].perturbed
            row["perturbation"] = variants[row["uid"]].kind
    else:
        print("VERBATIM mode: names are screened exactly as they appear in the "
              "publication. Near-tautological by construction -- use --perturb for "
              "the quality number.")
        for row in book:
            row["query_name"] = row["name"]
            row["perturbation"] = "none"

    with Oracle() as oracle:
        if not oracle.healthy():
            print("ERROR: yente is not ready. Run `make up` and wait for indexing.",
                  file=sys.stderr)
            return 2

        rows, latencies = [], []
        for offset in range(0, len(book), BATCH):
            chunk = book[offset:offset + BATCH]

            queries = {
                str(i): {
                    "name": row["query_name"],
                    "schema": "Person" if row["sdn_type"] == "Individual" else "Organization",
                }
                for i, row in enumerate(chunk)
            }
            oracle_hits = oracle.match(queries)

            for i, row in enumerate(chunk):
                truth = row["uid"]
                is_person = row["sdn_type"] == "Individual"

                t0 = time.perf_counter()
                mine = matcher.screen(row["query_name"], is_person=is_person)
                latencies.append((time.perf_counter() - t0) * 1000)

                my_uids = [m.sdn_uid for m in mine]
                my_top = my_uids[0] if my_uids else None
                theirs = oracle_hits.get(str(i), [])
                their_top = theirs[0].sdn_uid if theirs else None

                rows.append({
                    "uid": truth,
                    "name": row["name"],
                    "query_name": row["query_name"],
                    "perturbation": row["perturbation"],
                    "sdn_type": row["sdn_type"],
                    "stratum": row.get("stratum", ""),
                    "found": truth in my_uids,
                    "top1": my_top == truth,
                    "my_top": my_top,
                    "my_score": mine[0].score if mine else 0.0,
                    "oracle_top": their_top,
                    "agree": my_top == their_top,
                    "oracle_found": truth in [h.sdn_uid for h in theirs],
                })

            print(f"  screened {min(offset + BATCH, len(book))}/{len(book)}", end="\r")

    n = len(rows)
    recall = sum(r["found"] for r in rows) / n
    top1 = sum(r["top1"] for r in rows) / n
    agreement = sum(r["agree"] for r in rows) / n
    oracle_recall = sum(r["oracle_found"] for r in rows) / n
    above_hi = sum(r["my_score"] >= T_HI for r in rows) / n

    print(" " * 40)
    print("=" * 68)
    print(f"  recall (true uid present)      {recall:.3f}")
    print(f"  top-1  (true uid ranked first) {top1:.3f}")
    print(f"  agreement with yente           {agreement:.3f}")
    print(f"  yente's own recall             {oracle_recall:.3f}   <- the bar to clear")
    print(f"  scored >= T_HI ({T_HI})           {above_hi:.3f}")
    print(f"  latency p50 {statistics.median(latencies):.1f}ms  "
          f"p95 {statistics.quantiles(latencies, n=20)[18]:.1f}ms")
    print("=" * 68)

    misses = [r for r in rows if not r["top1"]]
    if misses:
        print(f"\n{len(misses)} rows where the true uid was not ranked first "
              f"(showing up to 15):")
        for r in misses[:15]:
            print(f"  uid={r['uid']:<8} {r['query_name'][:34]:<34} "
                  f"[{r['perturbation'][:12]:<12}] "
                  f"mine={r['my_top']!s:<8} oracle={r['oracle_top']!s:<8} "
                  f"{'AGREE' if r['agree'] else 'DISAGREE'}")

    if args.json_out:
        args.json_out.write_text(json.dumps({
            "publication": publication,
            "metrics": {"recall": recall, "top1": top1, "agreement": agreement,
                        "oracle_recall": oracle_recall, "above_t_hi": above_hi,
                        "n": n},
            "rows": rows,
        }, indent=2))
        print(f"\nwrote {args.json_out}")

    # The quality bar this project set for itself before measuring: top-1 >= 0.85 proceeds,
    # 0.60-0.85 narrows scope and publishes the measured gap, below 0.60 stops. This script
    # reports; a human reads the verdict.
    print()
    if top1 >= 0.85:
        print(f"G1 verdict: PROCEED (top-1 {top1:.3f} >= 0.85)")
    elif top1 >= 0.60:
        print(f"G1 verdict: NARROW SCOPE to Individuals+Entities and publish the "
              f"measured gap (top-1 {top1:.3f})")
    else:
        print(f"G1 verdict: MATCHER IS BROKEN (top-1 {top1:.3f} < 0.60) -- fix and re-gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
