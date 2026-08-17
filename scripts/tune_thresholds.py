#!/usr/bin/env python3
"""Threshold tuning -- pick T_HI and T_LO from measurement, not taste.

The two thresholds decide who gets adjudicated and whose money stops, so "0.85 felt
right" is not a defensible answer to a judge asking how the system was tuned.

The sentinel book supplies both classes at once, which is what makes this measurable
without inventing a synthetic negative population:

  POSITIVE   the score our matcher gives the sentinel's TRUE uid.
  NEGATIVE   the best score it gives any OTHER uid for the same query. This is the
             real false-positive risk -- not "some unrelated name scores low", but
             "for this exact query, how close did the wrong person get?"

Both are measured on PERTURBED names, because thresholds tuned on verbatim names are
tuned on a distribution that does not occur in production.

    recall@T       fraction of true hits at or above T  -- misses here are sanctions
                   evasion that slipped through
    false_hit@T    fraction of queries where a WRONG uid reaches T -- these are the
                   holds that freeze an innocent grantee's money
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interdict.matcher import T_HI, T_LO, Matcher
from interdict.ofac import parse_sdn
from interdict.perturb import perturb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdn", type=Path, default=Path("data/SDN.XML"))
    ap.add_argument("--book", type=Path, default=Path("data/sentinels.csv"))
    ap.add_argument("--json-out", type=Path, default=Path("data/thresholds.json"))
    ap.add_argument("--verbatim", action="store_true",
                    help="tune on unperturbed names (not recommended -- see docstring)")
    args = ap.parse_args()

    entries, publication = parse_sdn(args.sdn)
    matcher = Matcher(entries)
    with args.book.open(newline="", encoding="utf-8") as fh:
        book = list(csv.DictReader(fh))

    positives: list[float] = []
    negatives: list[float] = []

    for row in book:
        query = row["name"] if args.verbatim else perturb(row["name"], row["uid"]).perturbed
        is_person = row["sdn_type"] == "Individual"
        results = matcher.screen(query, is_person=is_person, limit=25)

        true_score = next((r.score for r in results if r.sdn_uid == row["uid"]), 0.0)
        best_wrong = max((r.score for r in results if r.sdn_uid != row["uid"]), default=0.0)
        positives.append(true_score)
        negatives.append(best_wrong)

    n = len(positives)
    mode = "VERBATIM" if args.verbatim else "PERTURBED"
    print(f"publication {publication['publish_date']} | {n} queries | {mode}")
    print()
    print(f"  {'T':<8}{'recall':<10}{'false_hit':<12}{'margin':<10}")
    print("  " + "-" * 38)

    sweep = []
    for i in range(40, 101, 2):
        t = i / 100
        recall = sum(p >= t for p in positives) / n
        false_hit = sum(x >= t for x in negatives) / n
        sweep.append({"t": t, "recall": recall, "false_hit": false_hit})
        marker = ""
        if abs(t - T_HI) < 1e-9:
            marker = "  <- current T_HI"
        elif abs(t - T_LO) < 1e-9:
            marker = "  <- current T_LO"
        if t >= 0.60:
            print(f"  {t:<8.2f}{recall:<10.3f}{false_hit:<12.3f}"
                  f"{recall - false_hit:<10.3f}{marker}")

    # Pick T_HI at the widest separation between catching real hits and freezing the
    # wrong grantee's money. Reported, never auto-applied -- a threshold that changes
    # itself is not a threshold anyone can audit.
    best = max(sweep, key=lambda s: s["recall"] - s["false_hit"])
    print()
    print(f"  max-margin T = {best['t']:.2f} "
          f"(recall {best['recall']:.3f}, false_hit {best['false_hit']:.3f})")

    cur_recall = sum(p >= T_HI for p in positives) / n
    cur_false = sum(x >= T_HI for x in negatives) / n
    print(f"  current T_HI = {T_HI:.2f} "
          f"(recall {cur_recall:.3f}, false_hit {cur_false:.3f})")

    # A miss is a sanctions breach; a false hit is a frozen grantee and a wasted
    # adjudication. They are not symmetric, so the recommendation is the lowest T that
    # still holds false_hit under 5%.
    safe = [s for s in sweep if s["false_hit"] <= 0.05]
    if safe:
        rec = max(safe, key=lambda s: s["recall"])
        print(f"  recommended  = {rec['t']:.2f} "
              f"(recall {rec['recall']:.3f}, false_hit {rec['false_hit']:.3f}) "
              f"-- lowest T holding false_hit <= 5%")

    args.json_out.write_text(json.dumps({
        "publication": publication, "mode": mode, "n": n, "sweep": sweep,
        "current": {"T_HI": T_HI, "T_LO": T_LO,
                    "recall": cur_recall, "false_hit": cur_false},
    }, indent=2))
    print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
