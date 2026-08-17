# DEMO — what to run, what you should see

Every command here runs against the real OFAC publication committed to this repo. None
of the numbers below are typed in by hand; each is printed by the command above it.

Setup once:

```bash
make install && make up && make oracle-index && make schema && make fetch-sdn
export GEMINI_API_KEY=...     # free, no billing: https://aistudio.google.com/apikey
```

`run_rescreen.py` **refuses to run without that key** rather than substituting a test
double. Every `--offline` run below is the deterministic plane alone and labels itself
as such on every line of output.

---

## Beat 1 — the feed is real, including Treasury's own typo

```bash
make test
```

133 tests. Two of them are worth watching:

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
python scripts/load_book.py --truncate
python scripts/run_rescreen.py --trigger SCHEDULER
```

536 counterparties screened against 19,199 SDN records; ~459 held, money frozen, every
decision written to the hash-chained ledger. Then grade it against ground truth the
screening path cannot see:

```bash
python scripts/adjudication_quality.py
```

```
  sentinel    HOLD    400/400      lookalike   CLEAR    60/60
  variant     HOLD     59/60       ordinary    CLEAR    16/16
  MISSED HITS  1/460          FROZEN GRANTEES  0/76
```

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

---

## Reproducing on a clean machine

```bash
make reproduce
```

Installs, brings the stack up, indexes the oracle, applies the schema, runs the tests,
and prints the perturbed screening number.
