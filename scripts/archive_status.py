#!/usr/bin/env python3
"""Fail if the OFAC archiver has stopped working.

WHY THIS EXISTS
---------------
The archiver polls OFAC every six hours on a timer configured outside this repository. On
2026-08-17 a lint pass modernised `datetime.timezone.utc` into `datetime.UTC`, the timer was
invoking a Python 3.9 interpreter, and every poll from then on died with an AttributeError.
Nothing noticed for five days, across a real Treasury publication, because the only evidence
was a traceback in a gitignored log file that nothing reads.

The interpreter is fixed and the script now refuses to start on the wrong Python. Neither of
those is the actual lesson. The actual lesson is that an unattended job with no liveness check
is not unattended, it is unobserved -- and the failure mode that produced nothing at all in the
log (a missing interpreter, a moved checkout, a full disk) would have been even quieter than
the one that did.

So: this reads the heartbeat that `archive_delta.py` writes on every poll, and exits non-zero
when the archiver has gone quiet or a single source has been failing repeatedly. It reads the
heartbeat rather than index.json because index.json only changes when OFAC publishes something
new, and OFAC publishes irregularly -- days of no new entries is normal and says nothing about
whether the poller is alive.

USAGE
    python3 scripts/archive_status.py             # `make archive-status`
    python3 scripts/archive_status.py --max-age 24
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The poll runs every 6h. Twelve hours is two consecutive missed windows -- late enough not to
# fire on one laptop sleep, early enough that a five-day outage is impossible.
DEFAULT_MAX_AGE_H = 12
# Six hours apart, so three in a row is most of a day of one endpoint being unreachable. Below
# that it is Treasury or the network having a moment, which the poller is designed to ride out.
MAX_FAILURE_STREAK = 3


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", type=Path, default=ROOT / "data" / "archive")
    ap.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_H, help="hours")
    args = ap.parse_args()

    hb_path = args.archive / "last-poll.json"
    if not hb_path.exists():
        sys.exit(
            f"FAIL: no heartbeat at {hb_path}. The archiver has not completed a single poll "
            "since this check existed -- check the launchd job:\n"
            "  launchctl list | grep interdict     (second column is the last exit status)\n"
            "  tail data/archive/archiver.log"
        )

    hb = json.loads(hb_path.read_text())
    last = dt.datetime.fromisoformat(hb["utc"])
    age_h = (dt.datetime.now(dt.UTC) - last).total_seconds() / 3600

    if age_h > args.max_age:
        sys.exit(
            f"FAIL: last poll was {age_h:.1f}h ago ({hb['utc']}), limit is {args.max_age:.0f}h. "
            "The archiver is not running. An OFAC publication in this window is lost -- it "
            "cannot be backfilled, because `changes/latest` only ever holds the latest delta."
        )

    streaks = hb.get("consecutive_failures", {})
    stuck = {name: n for name, n in streaks.items() if n >= MAX_FAILURE_STREAK}
    if stuck:
        detail = ", ".join(f"{name} x{n}" for name, n in sorted(stuck.items()))
        sys.exit(
            f"FAIL: source(s) failing on consecutive polls: {detail}. The job still exits 0 "
            "because the other source succeeds, so nothing else would have told you."
        )

    print(f"OK   last poll {age_h:.1f}h ago ({hb['utc']}), new={hb['new']}, failures={hb['failures']}")
    if any(streaks.values()):
        soft = ", ".join(f"{name} x{n}" for name, n in sorted(streaks.items()) if n)
        print(f"     transient failures on the last poll: {soft} (fails at x{MAX_FAILURE_STREAK})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
