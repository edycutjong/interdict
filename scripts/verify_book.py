#!/usr/bin/env python3
"""Verify the sealed sentinel book, and FAIL when it does not verify.

WHY THIS EXISTS
---------------
The CI step this replaces was named "Verify the sealed sentinel book still re-derives to its
committed hash" and its body was:

    python scripts/seed_sentinels.py --sdn data/SDN.XML --out /tmp/check.csv
    echo "committed: $(shasum -a 256 data/sentinels.csv | ...)"
    echo "rederived: $(shasum -a 256 /tmp/check.csv | ...)"

It printed two hashes and never compared them. The step was green whichever way they came out,
inside a job called "reproduce the published numbers" -- a check whose only failure signal is
text nobody reads. That is the same shape as the archiver that polled OFAC into an unread log
for five days after it had stopped working, and it is worth naming twice.

WHY IT CANNOT SIMPLY DIFF THE TWO HASHES
----------------------------------------
The book was sealed against ONE publication: the 08/07/2026 Standard Action, snapshot
`ac00228a…`, fetched 2026-08-12. data/PROVENANCE.md says outright that re-running against a
later snapshot will not reproduce the book hash, because entries added or removed since change
the eligible pool -- and OFAC published again on 08/20/2026. CI fetches whatever is live. So a
naive diff would fail every time Treasury acts, which is presumably why it was written as an
echo in the first place: the check was softened until it could not fail, instead of being aimed
at something that is actually invariant.

WHAT IS ACTUALLY INVARIANT
--------------------------
1. The committed book still hashes to the value sealed in PROVENANCE.md. Offline, always true
   unless someone edited the book -- which is precisely the tampering the seal exists to detect.
   This is the check that matters and it was never being made.
2. Given the sealed snapshot itself, the seeder still re-derives the book byte-for-byte. Only
   possible when the live publication IS the sealed one; asserted exactly then.
3. Against any LATER publication, the book must still resolve into it. Sentinels that no longer
   appear have been delisted by Treasury -- which is not an error, it is the entire point of the
   book, and it is the release leg's trigger. So they are reported by uid rather than tolerated
   silently. A book that mostly fails to resolve means the wrong file was passed, not a mass
   delisting, and that does fail.

USAGE
    python3 scripts/verify_book.py                      # check 1 only (offline)
    python3 scripts/verify_book.py --sdn data/SDN.XML   # checks 1 and 2 or 3
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NS = {"s": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML"}

# Below this, the input is the wrong file rather than a publication with delistings in it.
# 400 sentinels; OFAC's heaviest measured removal event in the seven publications surveyed for
# data/PROVENANCE.md was 396 removals across the whole 19,199-record list, so a fifth of this
# book disappearing at once is not a delisting pattern, it is a mismatched snapshot.
MIN_RESOLVED_FRACTION = 0.80


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def provenance_hashes(path: Path) -> tuple[str, str]:
    """Pull the sealed snapshot and book hashes out of PROVENANCE.md.

    Read from the document rather than hardcoded here, so that editing the seal to match a
    tampered book is a visible change to the file whose whole job is to be the record.
    """
    text = path.read_text(encoding="utf-8")
    digests = re.findall(r"`([0-9a-f]{64})`", text)
    if len(digests) < 2:
        sys.exit(f"FAIL: could not read two SHA-256 digests out of {path} -- has the seal record changed shape?")
    return digests[0], digests[1]  # documented order: SDN.XML snapshot, then sentinels.csv


def load_publication(path: Path) -> tuple[set[str], str, str]:
    root = ET.parse(path).getroot()
    # OFAC misspells `publishInformation` as `publshInformation`; parse_sdn pins that typo and
    # so does this, deliberately, rather than searching for either spelling.
    pub = root.find("s:publshInformation", NS)
    publish_date = pub.findtext("s:Publish_Date", default="", namespaces=NS) if pub is not None else ""
    record_count = pub.findtext("s:Record_Count", default="", namespaces=NS) if pub is not None else ""
    if not publish_date or not record_count:
        sys.exit(f"FAIL: {path} carries no publication header -- not an OFAC SDN export?")
    uids = {(e.findtext("s:uid", default="", namespaces=NS) or "").strip() for e in root.findall("s:sdnEntry", NS)}
    return uids - {""}, publish_date, record_count


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sdn", type=Path, default=None, help="OFAC SDN.XML to verify the book against")
    ap.add_argument("--book", type=Path, default=ROOT / "data" / "sentinels.csv")
    ap.add_argument("--provenance", type=Path, default=ROOT / "data" / "PROVENANCE.md")
    args = ap.parse_args()

    sealed_snapshot, sealed_book = provenance_hashes(args.provenance)

    # --- 1. The committed book still hashes to its seal -------------------------------------
    actual = sha256_file(args.book)
    if actual != sealed_book:
        sys.exit(
            f"FAIL: {args.book} does not match the seal in {args.provenance}.\n"
            f"  sealed:    {sealed_book}\n"
            f"  on disk:   {actual}\n"
            "The book is committed BEFORE the delistings it is meant to catch; if it can be "
            "edited afterwards the release leg proves nothing."
        )
    print(f"OK   book matches its seal: {actual}")

    if args.sdn is None:
        print("     (no --sdn given; skipped the publication checks)")
        return 0
    if not args.sdn.exists():
        sys.exit(f"FAIL: {args.sdn} not found -- run `make fetch-sdn` first")

    snapshot = sha256_file(args.sdn)
    uids, publish_date, record_count = load_publication(args.sdn)
    print(f"     publication {publish_date}, {int(record_count):,} records, snapshot {snapshot[:12]}")

    # --- 2. Against the sealed snapshot, the seeder re-derives it byte-for-byte --------------
    if snapshot == sealed_snapshot:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "check.csv"
            proc = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "seed_sentinels.py"), "--sdn", str(args.sdn), "--out", str(out)],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                sys.exit(f"FAIL: seed_sentinels.py exited {proc.returncode}\n{proc.stderr}")
            rederived = sha256_file(out)
        if rederived != sealed_book:
            sys.exit(
                f"FAIL: the sealed snapshot no longer re-derives the sealed book.\n"
                f"  sealed:    {sealed_book}\n"
                f"  rederived: {rederived}\n"
                "Selection is documented as deterministic with no RNG, so this means the seeder changed."
            )
        print(f"OK   re-derived byte-for-byte from the sealed snapshot: {rederived}")
        return 0

    # --- 3. Against a later publication, the book must still resolve into it -----------------
    with args.book.open(encoding="utf-8") as fh:
        book = list(csv.DictReader(fh))
    present = [r for r in book if r["uid"] in uids]
    delisted = [r for r in book if r["uid"] not in uids]
    fraction = len(present) / len(book) if book else 0.0

    print(
        f"     publication has moved past the seal (sealed snapshot {sealed_snapshot[:12]}), so "
        "exact re-derivation is not possible -- see data/PROVENANCE.md"
    )
    print(f"OK   {len(present)}/{len(book)} sentinels still listed")
    if delisted:
        print(f"     {len(delisted)} DELISTED since the seal -- this is the release leg's trigger:")
        for r in delisted:
            print(f"       uid {r['uid']:>7}  {r['stratum']:<9} {r['name']}")
    if fraction < MIN_RESOLVED_FRACTION:
        sys.exit(
            f"FAIL: only {fraction:.0%} of the book resolves into publication {publish_date}. "
            "That is a mismatched or corrupt snapshot, not a delisting pattern."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
