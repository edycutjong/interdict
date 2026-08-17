#!/usr/bin/env python3
"""Benchmark the screening plane. Reproducible, and honest about what is measured.

WHAT IS TIMED. Only the deterministic plane: blocking, scoring, and threshold decisions
against the real 19,199-record publication. That is the part that runs 24,576 name
comparisons per counterparty and the part whose cost scales with the book.

WHAT IS NOT TIMED, and why saying so matters. Adjudication latency is dominated by a
network round trip to Gemini, so folding it in would produce a number that measures
Google's serving latency rather than this system's work -- and it would move every time
anyone re-ran it. It is reported separately when a key is present.

    python scripts/bench.py
    python scripts/bench.py --runs 5 --json-out data/bench.json
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interdict.matcher import Matcher                    # noqa: E402
from interdict.ofac import parse_sdn                     # noqa: E402
from interdict.perturb import perturb                    # noqa: E402


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    k = (len(ordered) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdn", type=Path, default=Path("data/SDN.XML"))
    ap.add_argument("--book", type=Path, default=Path("data/sentinels.csv"))
    ap.add_argument("--runs", type=int, default=3, help="full-book passes to time")
    ap.add_argument("--json-out", type=Path, default=Path("data/bench.json"))
    args = ap.parse_args()

    print(f"python {platform.python_version()} on {platform.platform()}")
    print(f"{platform.processor() or 'unknown cpu'}\n")

    t0 = time.perf_counter()
    entries, publication = parse_sdn(args.sdn)
    parse_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    matcher = Matcher(entries)
    index_s = time.perf_counter() - t0

    with args.book.open(newline="", encoding="utf-8") as fh:
        book = list(csv.DictReader(fh))

    # Perturbed queries, because verbatim names hit the exact-token block immediately
    # and would time the easy path rather than the real one.
    queries = [(perturb(r["name"], r["uid"]).perturbed,
                r["sdn_type"] == "Individual") for r in book]

    print(f"  parse       {parse_s * 1000:8.0f} ms   "
          f"{len(entries):,} entries (publication {publication['publish_date']})")
    print(f"  index       {index_s * 1000:8.0f} ms   "
          f"{matcher.name_count:,} names incl. aliases")
    print(f"  book        {len(queries):,} counterparties, perturbed queries\n")

    per_query: list[float] = []
    per_pass: list[float] = []

    for run in range(args.runs):
        pass_start = time.perf_counter()
        for name, is_person in queries:
            t0 = time.perf_counter()
            matcher.screen(name, is_person=is_person)
            per_query.append((time.perf_counter() - t0) * 1000)
        elapsed = time.perf_counter() - pass_start
        per_pass.append(elapsed)
        print(f"  pass {run + 1}: full book in {elapsed:.2f}s "
              f"({len(queries) / elapsed:,.0f} counterparties/s)")

    stats = {
        "p50_ms": round(percentile(per_query, 0.50), 2),
        "p95_ms": round(percentile(per_query, 0.95), 2),
        "p99_ms": round(percentile(per_query, 0.99), 2),
        "mean_ms": round(statistics.mean(per_query), 2),
        "max_ms": round(max(per_query), 2),
        "full_book_s": round(statistics.median(per_pass), 3),
        "throughput_per_s": round(len(queries) / statistics.median(per_pass)),
        "parse_ms": round(parse_s * 1000),
        "index_ms": round(index_s * 1000),
        "entries": len(entries),
        "names_indexed": matcher.name_count,
        "book_size": len(queries),
        "runs": args.runs,
    }

    print(f"\n  per-counterparty screen (n={len(per_query):,})")
    print(f"    p50   {stats['p50_ms']:>8.2f} ms")
    print(f"    p95   {stats['p95_ms']:>8.2f} ms")
    print(f"    p99   {stats['p99_ms']:>8.2f} ms")
    print(f"    max   {stats['max_ms']:>8.2f} ms")
    print(f"\n  full book: {stats['full_book_s']:.2f}s median "
          f"({stats['throughput_per_s']:,} counterparties/s)")

    # The comparison that gives the number meaning: an OFAC publication lands roughly
    # weekly, and re-screening the entire book against it takes seconds.
    print(f"\n  A full re-screen of {stats['book_size']} counterparties against all "
          f"{stats['entries']:,} SDN\n  records completes in "
          f"{stats['full_book_s']:.1f}s. OFAC publishes roughly weekly.")

    args.json_out.write_text(json.dumps({
        "publication": publication,
        "platform": {"python": platform.python_version(), "system": platform.platform()},
        "measured": "deterministic screening plane only; adjudication excluded "
                    "(network-bound on Gemini)",
        "stats": stats,
    }, indent=2))
    print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
