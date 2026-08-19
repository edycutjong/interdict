#!/usr/bin/env python3
"""Load the counterparty book into Postgres.

THE BOOK IS SYNTHETIC AND IS LABELLED AS SUCH -- in the database (`counterparties.origin`),
in the console, and on screen in the demo. A real NGO's grantee ledger is not ours to
publish, and pretending otherwise would be the one dishonesty that could sink an
otherwise verifiable submission. What is NOT synthetic is everything the decisions turn
on: the OFAC publication, the delta, the alias categories, the dates of birth, and the
delisting actions are all Treasury's.

Four populations, because a book of nothing but guaranteed hits proves nothing -- and
because "clears lookalikes" is only a real claim if some of the book is genuinely NOT
the designated party. Each carries the verdict a correct system must reach, which is
what makes adjudication quality measurable rather than asserted.

  sentinel   400 names drawn from the SDN list at seal time and committed with their
             SHA-256 BEFORE any later publication existed. If OFAC delists one, git log
             proves the book predates the removal.                    expect HOLD

  variant    a sentinel's name in a different transliteration -- the SAME person, so
             catching it is the entire job. These are where a screening system that
             only does string equality quietly fails.                 expect HOLD

  lookalike  a DIFFERENT person who shares a designated party's surname and whose date
             of birth contradicts the record. In production these are the false
             positives that freeze an innocent grantee's money, and clearing them on
             documented evidence is what the adjudication plane is for. expect CLEAR

  ordinary   unrelated to anyone on the list, so the auto-no-hit path is exercised
             rather than assumed.                                     expect CLEAR

An earlier version of this file called the perturbed names "lookalikes". That was wrong
and worth naming: a transliteration is the same human being, so holding it is correct,
and scoring it as a false positive would have penalised the system for working.

Usage:
    python scripts/load_book.py --book data/sentinels.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interdict.db import connect
from interdict.ofac import parse_sdn
from interdict.perturb import perturb

# Ordinary grantee names. Generic by construction -- they exist to exercise the
# auto-no-hit path, not to represent anyone.
ORDINARY = [
    "Riverside Community Health Trust", "Northgate Water Initiative",
    "Sarah Elizabeth Whitfield", "Daniel Okonkwo Adeyemi",
    "Highland Refugee Support Network", "Marta Lucia Fernandez",
    "Eastbrook Medical Supplies Cooperative", "Priya Raghunathan Menon",
    "Clearwater Sanitation Project", "Thomas Andrew Blackwood",
    "Lakeshore Nutrition Alliance", "Amina Ndiaye Sow",
    "Fairview Education Fund", "Gregory Paul Hastings",
    "Summit Valley Relief Services", "Yuki Tanaka Morimoto",
]


# Given names used to build true lookalikes. Drawn from the same naming traditions as
# the designated parties, because a lookalike that is obviously foreign to the record
# is not a hard case and would flatter the adjudicator's score.
LOOKALIKE_GIVEN = [
    "Yusuf", "Layla", "Karim", "Fatima", "Tariq", "Noor", "Samir", "Hana",
    "Bilal", "Rania", "Idris", "Salma", "Nabil", "Dalia", "Faris", "Zahra",
]

# Contradicting dates of birth: decades away from any plausible record date, so a CLEAR
# rests on documented evidence rather than on the model's intuition about names.
LOOKALIKE_DOB = ["14 Mar 1991", "2 Sep 1988", "27 Jun 1995", "9 Nov 1993",
                 "18 Jan 1997", "5 May 1990", "23 Aug 1994", "11 Dec 1992"]


def stable_amount(seed: str, lo: int = 25_000, hi: int = 4_000_000) -> int:
    """A deterministic disbursement amount in cents. No RNG -- reproducible books."""
    digest = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:6], "big")
    return lo + digest % (hi - lo)


def surname_of(name: str) -> str:
    """The surname as OFAC formats it.

    Sentinel names arrive as "SURNAME, Given Middle" for individuals, so the surname is
    everything before the comma; otherwise fall back to the last token.
    """
    if "," in name:
        return name.split(",", 1)[0].strip()
    parts = name.split()
    return parts[-1] if parts else name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", type=Path, default=Path("data/sentinels.csv"))
    ap.add_argument("--variants", type=int, default=60,
                    help="how many sentinel names to perturb into same-person variants")
    ap.add_argument("--lookalikes", type=int, default=60,
                    help="how many different-person lookalikes to synthesise")
    ap.add_argument("--sdn", type=Path, default=Path("data/SDN.XML"))
    ap.add_argument("--truncate", action="store_true",
                    help="clear the book first (leaves the ledger untouched -- it is "
                         "append-only and refuses TRUNCATE by design)")
    ap.add_argument("--sentinels", type=int, default=None,
                    help="cap the number of sentinels loaded (default: all 400)")
    args = ap.parse_args()

    with args.book.open(newline="", encoding="utf-8") as fh:
        sentinels = list(csv.DictReader(fh))

    # STRATIFIED SAMPLING, for grading the model plane under a request budget.
    #
    # Free-tier Gemini caps requests per day per model per project, so grading every
    # counterparty against the real adjudicator is not a thing that can happen in one
    # sitting. Grading only the first N by id would be worse than useless: the book is
    # laid out one stratum at a time, so the first 100 rows are all sentinels -- names
    # copied verbatim out of the list being searched, the single easiest case, and the
    # one whose score means the least.
    #
    # Capping sentinels while keeping every variant, lookalike and ordinary row gives a
    # sample where each stratum is represented, and keeps the two strata that actually
    # test judgement -- variants (same person, must HOLD) and lookalikes (different
    # person with a contradicting DOB, must CLEAR) -- at full strength.
    #
    # The full-book screening numbers are unaffected: the deterministic plane calls no
    # model and is still measured across all 400 by `make challenge-set`.
    if args.sentinels is not None:
        sentinels = sentinels[:args.sentinels]

    # Sentinels ARE the listed parties, so they carry the record's own date of birth.
    # An invented placeholder date would contradict the real one, fire the disjoint-DOB
    # signal, and clear the very parties the book exists to catch -- which is exactly
    # what a first pass with a hardcoded "1 Jan 1970" did.
    entries, _ = parse_sdn(args.sdn)
    dob_by_uid = {e.uid: (e.dobs[0] if e.dobs else None) for e in entries}

    with connect() as conn:
        if args.truncate:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE holds, adjudications, matches, quarantine, "
                            "rescreen_batches, rescreen_runs, disbursements, "
                            "counterparties RESTART IDENTITY CASCADE")

        rows: list[tuple] = []

        for s in sentinels:
            rows.append((
                f"sdn:{s['uid']}", s["name"], dob_by_uid.get(s["uid"]),
                s["sdn_type"], "sentinel", "HOLD",
                f"sealed-book:{s['selection_hash']}",
            ))

        # VARIANTS -- the same designated party under a different transliteration, with
        # no date of birth (a payment instruction rarely carries one). Correct verdict
        # is HOLD: catching these is the entire job.
        for s in sentinels[: args.variants]:
            variant = perturb(s["name"], s["uid"])
            if not variant.changed:
                continue
            rows.append((
                f"variant:{s['uid']}", variant.perturbed, None,
                s["sdn_type"], "variant", "HOLD", f"perturbed:{variant.kind}",
            ))

        # LOOKALIKES -- a DIFFERENT person sharing a designated party's surname, with a
        # date of birth that contradicts the record. Correct verdict is CLEAR, and the
        # clearing has to rest on the DOB evidence rather than on a hunch about names.
        # Individuals only: an organisation has no date of birth to contradict, so the
        # same construction would produce an unanswerable case rather than a hard one.
        individuals = [s for s in sentinels if s["sdn_type"] == "Individual"]
        for i, s in enumerate(individuals[: args.lookalikes]):
            surname = surname_of(s["name"])
            given = LOOKALIKE_GIVEN[i % len(LOOKALIKE_GIVEN)]
            rows.append((
                f"lookalike:{s['uid']}", f"{surname}, {given}",
                LOOKALIKE_DOB[i % len(LOOKALIKE_DOB)],
                "Individual", "lookalike", "CLEAR",
                f"synthetic-lookalike-of:{s['uid']}",
            ))

        for i, name in enumerate(ORDINARY):
            # Two-or-three-word personal names vs organisations, decided once here
            # rather than re-derived downstream.
            is_org = any(tok in name for tok in
                         ("Trust", "Initiative", "Network", "Cooperative", "Project",
                          "Alliance", "Fund", "Services"))
            rows.append((f"ordinary:{i}", name, None,
                         "Entity" if is_org else "Individual",
                         "ordinary", "CLEAR", "synthetic-grantee"))

        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO counterparties "
                "(external_ref,name,dob,entity_type,origin,expected_verdict,source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (external_ref) DO NOTHING",
                rows,
            )

            # One queued disbursement each, so a hold has money to stop.
            cur.execute("SELECT id, external_ref FROM counterparties "
                        "WHERE id NOT IN (SELECT counterparty_id FROM disbursements)")
            pending = cur.fetchall()
            cur.executemany(
                "INSERT INTO disbursements (counterparty_id,amount_cents,memo) "
                "VALUES (%s,%s,%s)",
                [(p["id"], stable_amount(p["external_ref"]),
                  "Q3 humanitarian disbursement (SYNTHETIC)") for p in pending],
            )

            cur.execute("SELECT origin, expected_verdict, count(*) AS n "
                        "FROM counterparties GROUP BY origin, expected_verdict "
                        "ORDER BY origin")
            counts = cur.fetchall()
            cur.execute("SELECT count(*) AS n, sum(amount_cents) AS total FROM disbursements")
            money = cur.fetchone()

    print("counterparty book loaded (ALL COUNTERPARTIES ARE SYNTHETIC AND LABELLED):")
    for row in counts:
        print(f"  {row['origin']:<12} {row['n']:>5}   expect {row['expected_verdict']}")
    print(f"  {'disbursements':<12} {money['n']:>5}  "
          f"${money['total'] / 100:,.2f} queued")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
