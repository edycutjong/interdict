#!/usr/bin/env python3
"""Verify the ledger hash chain end to end. Exit 1 if it is broken.

A hash chain nobody checks is decoration, so this is a first-class command rather than
a claim in the README -- and it is a demo beat: run it, then try to tamper and watch the
append-only triggers refuse.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interdict.db import connect, verify_chain


def main() -> int:
    with connect() as conn:
        intact, total = verify_chain(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT event_type, count(*) AS n FROM ledger "
                        "GROUP BY event_type ORDER BY count(*) DESC")
            events = cur.fetchall()
            cur.execute("SELECT min(seq) AS lo, max(seq) AS hi FROM ledger")
            span = cur.fetchone()

    print(f"ledger: {total} entries, chain {'INTACT' if intact else 'FORKED'}")
    if total:
        gapless = (span["hi"] - span["lo"] + 1) == total
        print(f"sequence: {span['lo']}..{span['hi']} "
              f"({'gapless' if gapless else 'HAS GAPS'})")
        for e in events:
            print(f"  {e['event_type']:<18} {e['n']:>6}")
        if not gapless:
            return 1
    return 0 if intact else 1


if __name__ == "__main__":
    raise SystemExit(main())
