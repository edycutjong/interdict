#!/usr/bin/env python3
"""Seed the sentinel stratum of Interdict's counterparty book.

WHY THIS EXISTS
---------------
Interdict claims it autonomously RELEASES blocked funds when OFAC delists someone. The cheap way to
show that is to pre-seed a counterparty you already know was removed and "replay" the delta — but
that is hindsight, and an adversarial judge is right to discount it.

Instead we seed "sentinels": counterparties whose names are drawn from entries that are CURRENTLY on
the SDN list, commit the book and its SHA-256 to git BEFORE any future delta exists, and wait. If
OFAC later removes someone who happens to be in the book, the release leg fires on an entry nobody
could have known would be delisted -- and the git timestamp proves it.

SAMPLING IS STRATIFIED, NOT UNIFORM
-----------------------------------
Measured base rate (see specs/day1-data-verification.md section 7): 396 removals across 7 OFAC
publications between 2026-06-26 and 2026-08-07, but BURSTY -- 96% came from two events and 3 of 7
publications had zero removals. Uniform sampling of 400 names (~2% of the list) gives only ~28%
probability that any sentinel fires in a quiet window.

Removals concentrate in specific programs that are a small share of the list:

    SDNTK  32.3% of removals but  7.1% of entries  -> 4.5x enriched
    IRAQ2  10.6% of removals but  0.8% of entries  -> 13x  enriched

Stratifying the same 400 names into those programs lifts effective coverage from ~2% to ~13%,
taking quiet-window probability from ~28% to ~88%.

WHY THIS IS NOT CHERRY-PICKING
------------------------------
Selection is on PROGRAM MEMBERSHIP, which is public and fixed at seed time. We still cannot know
which entries OFAC will remove. Within every stratum, selection is by SHA-256 of the entry's uid --
deterministic, reproducible from this script, and impossible to steer toward a known outcome. The
weighting is disclosed in the README and each row is labelled `sentinel=true`.

USAGE
    python3 scripts/seed_sentinels.py --sdn data/SDN.XML --out data/sentinels.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

NS = {"s": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML"}

# (program tag, target count). Order is significant: earlier strata claim entries first, so an
# entry carrying both SDNTK and IRAQ2 lands in SDNTK and is not double-counted.
STRATA: list[tuple[str, int]] = [
    ("SDNTK", 250),
    ("IRAQ2", 100),
]
REMAINDER_TARGET = 50
ELIGIBLE_TYPES = {"Individual", "Entity"}  # vessels/aircraft are an explicit non-goal


@dataclass(frozen=True)
class Sentinel:
    uid: str
    name: str
    sdn_type: str
    programs: str
    stratum: str
    selection_hash: str
    sentinel: str
    source_dataset: str
    source_record_id: str


def canonical(text: str) -> str:
    """NFC-normalise and collapse whitespace so the CSV hash is stable across machines."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def entry_name(entry: ET.Element) -> str:
    last = entry.findtext("s:lastName", default="", namespaces=NS) or ""
    first = entry.findtext("s:firstName", default="", namespaces=NS) or ""
    return canonical(f"{last}, {first}" if first.strip() else last)


def selection_key(uid: str) -> str:
    """Deterministic, non-steerable ordering key. Documented so anyone can reproduce the choice."""
    return hashlib.sha256(f"interdict-sentinel-v1:{uid}".encode()).hexdigest()


def load_entries(sdn_path: Path) -> tuple[list[dict], str, str]:
    root = ET.parse(sdn_path).getroot()

    # NOTE: OFAC's own schema misspells this element as "publshInformation". That typo is real and
    # load-bearing -- a parser assuming the correct spelling silently gets nothing.
    pub = root.find("s:publshInformation", NS)
    publish_date = (pub.findtext("s:Publish_Date", default="?", namespaces=NS) if pub is not None else "?")
    record_count = (pub.findtext("s:Record_Count", default="?", namespaces=NS) if pub is not None else "?")

    entries = []
    for e in root.findall("s:sdnEntry", NS):
        sdn_type = (e.findtext("s:sdnType", default="", namespaces=NS) or "").strip()
        if sdn_type not in ELIGIBLE_TYPES:
            continue
        uid = (e.findtext("s:uid", default="", namespaces=NS) or "").strip()
        name = entry_name(e)
        if not uid or not name:
            continue
        programs = sorted({(p.text or "").strip() for p in e.findall(".//s:program", NS) if p.text})
        entries.append({"uid": uid, "name": name, "sdn_type": sdn_type, "programs": programs})
    return entries, publish_date, record_count


def select(entries: list[dict]) -> list[Sentinel]:
    claimed: set[str] = set()
    picked: list[Sentinel] = []

    for program, target in STRATA:
        pool = [e for e in entries if program in e["programs"] and e["uid"] not in claimed]
        pool.sort(key=lambda e: selection_key(e["uid"]))
        chosen = pool[:target]
        if len(chosen) < target:
            print(
                f"  ! stratum {program}: wanted {target}, pool only has {len(pool)}",
                file=sys.stderr,
            )
        for e in chosen:
            claimed.add(e["uid"])
            picked.append(_to_sentinel(e, program))
        print(f"  {program:8} selected {len(chosen):4} of {len(pool)} eligible")

    pool = [e for e in entries if e["uid"] not in claimed]
    pool.sort(key=lambda e: selection_key(e["uid"]))
    for e in pool[:REMAINDER_TARGET]:
        claimed.add(e["uid"])
        picked.append(_to_sentinel(e, "REMAINDER"))
    print(f"  {'REMAINDER':8} selected {min(REMAINDER_TARGET, len(pool)):4} of {len(pool)} eligible")

    # Stable output order, independent of stratum processing order.
    picked.sort(key=lambda s: s.uid.zfill(12))
    return picked


def _to_sentinel(e: dict, stratum: str) -> Sentinel:
    return Sentinel(
        uid=e["uid"],
        name=e["name"],
        sdn_type=e["sdn_type"],
        programs="|".join(e["programs"]),
        stratum=stratum,
        selection_hash=selection_key(e["uid"])[:16],
        sentinel="true",
        source_dataset="us_ofac_sdn",
        source_record_id=f"sdn:{e['uid']}",
    )


def write_csv(rows: list[Sentinel], out_path: Path) -> str:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(rows[0]).keys()) if rows else []
    # newline="" + \n lineterminator so the hash matches on Windows checkouts too.
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))
    return hashlib.sha256(out_path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sdn", type=Path, required=True, help="path to OFAC SDN.XML (follow redirects when downloading)")
    ap.add_argument("--out", type=Path, default=Path("data/sentinels.csv"))
    args = ap.parse_args()

    if not args.sdn.exists():
        print(f"error: {args.sdn} not found", file=sys.stderr)
        return 2

    sdn_hash = hashlib.sha256(args.sdn.read_bytes()).hexdigest()
    entries, publish_date, record_count = load_entries(args.sdn)

    print(f"SDN.XML          sha256 {sdn_hash}")
    print(f"publish date     {publish_date}   records {record_count}")
    print(f"eligible pool    {len(entries)} (Individual + Entity only)")
    print("selecting sentinels:")

    rows = select(entries)
    book_hash = write_csv(rows, args.out)

    print()
    print(f"wrote {len(rows)} sentinels -> {args.out}")
    print(f"BOOK SHA-256     {book_hash}")
    print()
    print("Commit this file AND the hash now. The sentinel proof only covers OFAC deltas")
    print("published AFTER this commit -- every day of delay is a day of lost coverage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
