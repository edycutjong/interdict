#!/usr/bin/env python3
"""Grade the adjudication plane against the book's ground truth.

The screening path never reads `counterparties.expected_verdict` -- the orchestrator
does not know the column exists. That is the only reason this number means anything:
the system is not told the answer and is then scored on it.

Two error classes, and they are not equally bad:

  MISSED HIT     a party who should have been held was cleared. In production this is a
                 payment to a designated party under a strict-liability statute.
  FROZEN GRANTEE a party who should have been cleared was held. Real harm -- an aid
                 disbursement stops -- but recoverable, and visible to a human.

Reported separately for that reason. A single "accuracy" figure would let one hide
inside the other.

    python scripts/adjudication_quality.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interdict.db import connect

QUERY = """
SELECT c.origin,
       c.expected_verdict,
       CASE WHEN d.state = 'HELD' THEN 'HOLD' ELSE 'CLEAR' END AS actual,
       count(*) AS n
FROM counterparties c
JOIN disbursements d ON d.counterparty_id = c.id
WHERE c.expected_verdict IS NOT NULL
GROUP BY c.origin, c.expected_verdict, actual
ORDER BY c.origin
"""

DETAIL = """
SELECT c.origin, c.name, c.dob, c.expected_verdict,
       CASE WHEN d.state = 'HELD' THEN 'HOLD' ELSE 'CLEAR' END AS actual,
       m.det_score, a.rationale
FROM counterparties c
JOIN disbursements d ON d.counterparty_id = c.id
LEFT JOIN matches m ON m.counterparty_id = c.id
LEFT JOIN adjudications a ON a.match_id = m.id
WHERE c.expected_verdict IS NOT NULL
  AND c.expected_verdict <> (CASE WHEN d.state = 'HELD' THEN 'HOLD' ELSE 'CLEAR' END)
ORDER BY c.expected_verdict, c.origin
LIMIT %s
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", type=Path, default=Path("data/adjudication-quality.json"))
    ap.add_argument("--show", type=int, default=12, help="how many errors to print")
    args = ap.parse_args()

    with connect() as conn, conn.cursor() as cur:
        cur.execute(QUERY)
        rows = cur.fetchall()
        cur.execute(DETAIL, (args.show,))
        errors = cur.fetchall()

    if not rows:
        print("no graded counterparties -- run scripts/load_book.py then "
              "scripts/run_rescreen.py first")
        return 1

    by_origin: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_origin.setdefault(row["origin"], {"correct": 0, "wrong": 0,
                                                      "expected": row["expected_verdict"]})
        key = "correct" if row["actual"] == row["expected_verdict"] else "wrong"
        bucket[key] += row["n"]

    print(f"  {'population':<12}{'expect':<8}{'correct':>9}{'wrong':>8}{'rate':>9}")
    print("  " + "-" * 46)
    for origin in ("sentinel", "variant", "lookalike", "ordinary"):
        b = by_origin.get(origin)
        if not b:
            continue
        total = b["correct"] + b["wrong"]
        print(f"  {origin:<12}{b['expected']:<8}{b['correct']:>9}{b['wrong']:>8}"
              f"{b['correct'] / total:>9.3f}")

    missed = sum(b["wrong"] for o, b in by_origin.items() if b["expected"] == "HOLD")
    frozen = sum(b["wrong"] for o, b in by_origin.items() if b["expected"] == "CLEAR")
    should_hold = sum(b["correct"] + b["wrong"] for b in by_origin.values()
                      if b["expected"] == "HOLD")
    should_clear = sum(b["correct"] + b["wrong"] for b in by_origin.values()
                       if b["expected"] == "CLEAR")

    print()
    print(f"  MISSED HITS      {missed:>4} / {should_hold:<5} "
          f"({missed / should_hold:.3f}) -- payments to a designated party")
    print(f"  FROZEN GRANTEES  {frozen:>4} / {should_clear:<5} "
          f"({frozen / should_clear:.3f}) -- aid stopped in error")

    if errors:
        print(f"\n  errors (up to {args.show}):")
        for e in errors:
            print(f"    [{e['origin']:<9}] expected {e['expected_verdict']:<5} "
                  f"got {e['actual']:<5} score={e['det_score']} {e['name'][:34]!r}")
            if e["rationale"]:
                print(f"                {e['rationale'][:96]}")

    args.json_out.write_text(json.dumps({
        "by_origin": by_origin,
        "missed_hits": missed, "should_hold": should_hold,
        "frozen_grantees": frozen, "should_clear": should_clear,
    }, indent=2))
    print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
