#!/usr/bin/env python3
"""`make challenge NAME="..."` -- screen any name, live, against both planes.

This is the dev loop and it is also the demo beat that kills the "you picked a name you
knew would work" objection: a judge can type any name on camera and watch the
deterministic score, the component breakdown, and the independent oracle's opinion
appear side by side.

Usage:
    make challenge NAME="Ibrahim Al Rashid"
    make challenge NAME="Abu Abbas" DOB="3 Mar 1990"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interdict.matcher import T_HI, T_LO, Matcher
from interdict.ofac import parse_sdn
from interdict.oracle import Oracle
from interdict.perturb import perturb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--dob", default=None)
    ap.add_argument("--sdn", type=Path, default=Path("data/SDN.XML"))
    ap.add_argument("--entity", action="store_true",
                    help="screen as an organisation rather than a natural person")
    ap.add_argument("--show-variants", action="store_true",
                    help="also screen deterministic perturbations of this name")
    args = ap.parse_args()

    if not args.name.strip():
        print("give a name: make challenge NAME=\"Ibrahim Al Rashid\"", file=sys.stderr)
        return 2

    entries, publication = parse_sdn(args.sdn)
    matcher = Matcher(entries)
    is_person = not args.entity

    print(f"OFAC publication {publication['publish_date']} "
          f"({publication['record_count']} records, {matcher.name_count} names incl. aliases)")
    print(f"query: {args.name!r}" + (f"  dob={args.dob!r}" if args.dob else ""))
    print("=" * 72)

    results = matcher.screen(args.name, args.dob, is_person=is_person)

    if not results:
        print(f"  NO HIT -- nothing scored at or above T_LO ({T_LO}).")
        print("  This counterparty would be cleared automatically, without adjudication.")
    for r in results[:5]:
        c = r.components
        verdict = "CANDIDATE HIT -> adjudication" if r.score >= T_HI else "below T_HI"
        print(f"\n  {r.score:<7} uid={r.sdn_uid:<8} {verdict}")
        print(f"    matched   {c.matched_name!r} ({c.matched_category}, {c.matched_sdn_type})")
        print(f"    signals   sort={c.sort_ratio} jw={c.jaro_winkler} "
              f"cov={c.token_coverage} set={c.set_ratio}")
        adjustments = []
        if c.weak_alias:
            adjustments.append("weak alias (OFAC-flagged, downweighted)")
        if c.dob_signal != "unavailable":
            adjustments.append(f"DOB {c.dob_signal}")
        if c.type_signal == "mismatch":
            adjustments.append("person/entity type mismatch")
        print(f"    adjusted  {', '.join(adjustments) if adjustments else 'none'}")

    # The independent oracle, every time -- not just when it agrees.
    print("\n" + "-" * 72)
    try:
        with Oracle() as oracle:
            if not oracle.healthy():
                print("  oracle: yente not running (`make up`) -- skipped")
            else:
                hits = oracle.match({"q": {
                    "name": args.name,
                    "schema": "Person" if is_person else "Organization",
                    "dob": args.dob,
                }})["q"]
                if not hits:
                    print("  oracle (yente/us_ofac_sdn): no hit")
                else:
                    top = hits[0]
                    mine = results[0].sdn_uid if results else None
                    agree = "AGREE" if top.sdn_uid == mine else "DISAGREE"
                    print(f"  oracle (yente/us_ofac_sdn): uid={top.sdn_uid} "
                          f"score={top.score:.3f} {top.caption!r}  -> {agree}")
    except Exception as exc:                       # oracle is advisory, never fatal
        print(f"  oracle: unavailable ({exc})")

    if args.show_variants:
        print("\n" + "-" * 72)
        print("  deterministic variants (this is what the challenge set screens):")
        for strategy in ("transliteration", "reorder", "drop_particle", "typo", "drop_middle"):
            p = perturb(args.name, strategy=strategy)
            if not p.changed:
                continue
            res = matcher.screen(p.perturbed, args.dob, is_person=is_person)
            top = res[0] if res else None
            print(f"    {strategy:<16} {p.perturbed[:34]:<34} "
                  f"-> {('uid=' + top.sdn_uid + ' ' + str(top.score)) if top else 'NO HIT'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
