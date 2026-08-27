# DEMO — what to run, what you should see

Every command here runs against the real OFAC publication. None of the numbers below are
typed in by hand; each is printed by the command above it.

**Which publication.** The figures here are from **08/07/2026 — 19,199 records, 4,393 weak
aliases**, the snapshot the sentinel book is sealed to (`data/PROVENANCE.md`). `make
fetch-sdn` fetches whatever is *current*, and Treasury has published since: 08/20/2026,
19,249 records, 4,401 weak aliases. So running these commands today prints the newer
figures, which is correct and expected — `make verify-book` tells you which publication you
have and confirms all 400 sentinels are still listed in it. Nothing below is invalidated by
that; the numbers are labelled rather than frozen.

Setup once:

```bash
make install && make up && make oracle-index && make schema && make fetch-sdn
export GEMINI_API_KEY=...     # free, no billing: https://aistudio.google.com/apikey

# The cloud evidence plane (Beat 9). Optional -- every other beat runs without it.
export INTERDICT_FIRESTORE_PROJECT=your-project
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json   # roles/datastore.user
```

**On pace.** Free-tier Gemini allows **five requests a minute**, so a full-book run takes
roughly 90 minutes wall clock and spends almost all of it waiting. That is the honest
number and it is not a problem worth hiding: this is a batch job against a list Treasury
republishes weekly. The adjudicator honours the server's own `retry in Ns` hint rather
than hammering it.

`run_rescreen.py` **refuses to run without that key** rather than substituting a test
double. Every `--offline` run below is the deterministic plane alone and labels itself
as such on every line of output.

---

## Beat 1 — the feed is real, including Treasury's own typo

```bash
make test
```

343 tests, 100% statement coverage. Two of them are worth watching:

- `test_ofac_schema_typo_is_pinned` — OFAC's schema misspells `publishInformation` as
  **`publshInformation`**. We match OFAC's spelling and pin it, so if Treasury ever
  fixes the typo we get a loud failure rather than a silently empty publication date.
- `test_weak_alias_count_is_as_published` — **4,393** aliases in the 08/07/2026
  publication carry OFAC's own `weak` flag. Not a number we chose.

## Beat 2 — screen any name, live

```bash
make challenge NAME="Muhammad Zaydan"
```

Resolves to **uid 2674** at score 1.000 via a *strong alias*, not the primary name, with
the full signal breakdown and yente's independent opinion beside it.

Then contradict the date of birth:

```bash
make challenge NAME="Abu Abbas" DOB="3 Mar 1990"
```

The hit disappears. uid 2674's real DOB is 10 Dec 1948, and a disjoint interval cuts the
score below the adjudication bar. **Type this with a name we did not choose** — that is
the point of the command.

## Beat 3 — the number that is not a string-equality test

```bash
make challenge-set
```

```
  recall (true uid present)      1.000
  top-1  (true uid ranked first) 0.995
  yente's own recall             0.840   <- the bar to clear
```

400 counterparties, every name deterministically perturbed so none of them appear on the
list character-for-character. Transliterations from OFAC's own alias lists, reordering,
dropped particles, transcription confusables. Seeded from SHA-256, so this set is
byte-identical on your machine.

For contrast, `make agreement` screens the names verbatim and scores 1.000. That number
is near-tautological and we do not report it.

## Beat 4 — the full unattended re-screen

```bash
python scripts/load_book.py --truncate --sentinels 30 --variants 30 --lookalikes 30
python scripts/run_rescreen.py --trigger SCHEDULER --batch-size 20
```

A **stratified sample of 101** counterparties screened against the 19,199 SDN records of
the 08/07/2026 publication; 59 held, money frozen, every decision written to the
hash-chained ledger and adjudicated by
`gemini-3.5-flash-lite`. Sampled rather than run whole because free-tier Gemini caps
requests per model per project per day — drop `--sentinels` on a billed project.

Then grade it against ground truth the screening path cannot see:

```bash
python scripts/adjudication_quality.py
```

```
  sentinel    HOLD     30/30       lookalike   CLEAR    25/25
  variant     HOLD     29/30       ordinary    CLEAR    16/16
  MISSED HITS  1/60           FROZEN GRANTEES  0/41
```

**Read the strata carefully.** All 59 adjudications came back HOLD; not one CLEAR-expected
row reached the model, because a contradicting date of birth cuts a lookalike below the
adjudication bar first. The clears are the deterministic plane's work. What the model is
graded on here is confirming holds with a citation — and it got 59 of 59 past the oracle
guard, with nothing quarantined.

Every one of those 59 decisions also carries yente's independent verdict, because an
oracle consulted only where it agrees is not an oracle:

| | count |
|---|---|
| both flagged a hit | **58** |
| we held, yente missed | 1 |
| **we cleared, yente flagged** | **0** |

The zero is the point. Under strict liability the dangerous direction is being *more
permissive* than the independent oracle.

## Beat 5 — kill a worker mid-book

The one that matters for an unattended system.

```bash
python scripts/run_rescreen.py --trigger SCHEDULER --kill-after 2   # worker dies
python scripts/run_rescreen.py --resume <RUN_ID>                    # pick it back up
```

The interrupted run **refuses to mark itself finished**, and the resume starts at
`MIN(batch_start)` over incomplete batches — not after the last completed one. A scalar
cursor would resume past the ranges still in flight and leave those counterparties
unscreened against a live sanctions list while reporting success.

Two failure modes are asserted directly in `tests/test_rescreen.py`:
`test_no_counterparty_is_skipped_across_a_crash` and
`test_killed_worker_leaves_the_run_open`.

## Beat 6 — money moves back on a real delisting

```bash
python scripts/replay_release.py
```

```
  [1] 8/8 held against the reconstructed pre-removal list
  [2] blocking report drafted, due 2026-08-31 (10 business days)
  [3] applying the real 2026-08-07 removals -> released 8
      ledger: chain INTACT
```

**This is a labelled REPLAY and says so on screen.** Those eight parties are already gone
from the 08/07 publication, so a live release cannot be staged against today's list
without pretending. Their uids, names, programmes and removals all come from Treasury's
archived delta (sha256 `9403f40d9496…`). The payment book is synthetic.

## Beat 7 — the ledger cannot be rewritten

```bash
make verify-ledger
```

Then try to tamper:

```sql
UPDATE ledger SET payload = '{}' WHERE seq = 1;
-- ERROR: ledger is append-only; UPDATE is not permitted
```

And the fork test, which is the reason `seq` is assigned inside the chaining trigger
rather than by a sequence:

```bash
.venv/bin/python -m pytest tests/test_ledger.py -k concurrent -v
```

## Beat 8 — performance

```bash
make bench
```

| p50 | p95 | p99 | full book (400) |
|---|---|---|---|
| 9.3 ms | 68.8 ms | 106.7 ms | 7.6 s |

OFAC publishes roughly weekly. A full re-screen takes seconds.

## Beat 9 — the audit trail leaves the machine

A hash chain only the operator can read is not much of an audit trail. Every committed
ledger entry is mirrored to Cloud Firestore with its `seq`, `prev_hash` and `entry_hash`
intact, so the chain verifies from the cloud copy alone — against a local database the
verifier does not have and does not have to trust.

The run prints what it published:

```
  ledger: 3765 entries, chain INTACT
  firestore: +3765 ledger entries, run summary published -> <project>/(default)
```

Open the **Firestore console** on `interdict_ledger` while a re-screen is running and
documents land live. `interdict_runs` holds one summary document per run: trigger,
publication date, SDN record count, source SHA-256, and the hold / adjudication /
guard-disagreement counts.

The mirror is resumable with no local state — it asks Firestore for the highest `seq` it
already holds and republishes everything above it, and document ids are the padded
sequence number, so re-running it overwrites rather than duplicates:

```bash
python -c "from interdict.db import connect; from interdict.cloud import publish_ledger; \
           conn=connect().__enter__(); print(publish_ledger(conn), 'entries mirrored')"
```

Firestore is a mirror and never a source of truth. A publish failure is loud and retried
on the next pass; it cannot unmake a decision Postgres has already committed.

---

## Reproducing on a clean machine

```bash
make reproduce
```

Installs, brings the stack up, indexes the oracle, applies the schema, runs the tests,
and prints the perturbed screening number.
