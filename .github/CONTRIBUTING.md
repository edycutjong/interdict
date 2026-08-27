# Contributing

## Getting a working environment

```bash
make install-dev      # venv + runtime deps + ruff/mypy/pytest/pip-audit
make up               # Postgres + Elasticsearch + yente in Docker
make oracle-index     # index us_ofac_sdn into yente (once)
make schema
make fetch-sdn        # 27MB from Treasury; gitignored, re-fetchable
make test
```

`make ci` runs everything CI runs: lint, typecheck, tests with coverage, dependency
audit.

## The rules that are not style preferences

Most of this codebase is ordinary. These few things are load-bearing, and changing them
without understanding why will break a claim the project makes to judges or, worse,
break it silently.

**1. The yente scope pin is not configurable.** The oracle is `/match/us_ofac_sdn` and
nothing else. yente's default scope spans 465 datasets and will match a grantee against
a Wikidata politician or a SAM exclusion — neither of which creates OFAC liability.
Every agreement number in this repo depends on the pin, which is why the manifest
indexes only that one dataset: the wrong endpoint cannot be reached by accident.

**2. Nothing writes to `ledger` except the outbox relay.** Agents call `emit()` inside
their own transaction; the relay is the single writer. This is what keeps the hash chain
linear. If you add a direct insert, the concurrency test will fail, and it is right.

**3. `seq` is assigned inside the chaining trigger, not by a sequence.** A `bigserial`
draws its value at INSERT time, outside the advisory lock, so two writers can chain in
one order and be numbered in another. The chain stays valid but stops reading in order,
which is indistinguishable from corruption to anyone auditing it.

**4. Completeness is not "nothing incomplete".** A re-screen run closes only when there
are no outstanding batches *and* claimed coverage reaches the end of the book. A worker
that dies between batches leaves nothing outstanding while most of the book is
unscreened. This bug shipped once here; the tests that catch it are
`test_killed_worker_leaves_the_run_open` and `test_incomplete_run_reports_short_coverage`.

**5. Never infer identity type from an optional field.** Person-ness was once derived
from "does this row have a date of birth?", which reclassified 27 listed individuals
with no DOB on record as organisations and cleared every one of them. `entity_type` is
an explicit column.

**6. Synthetic data stays labelled.** The counterparty book is ours and is marked as
such in the database, the console, and the demo. The OFAC publication, the delta, alias
categories, dates of birth and delisting actions are Treasury's and are never edited.
If you add a fixture that blurs the line, the project's central claim goes with it.

**7. `--offline` is opt-in and says so.** The adjudication plane must not silently fall
back to the rule-based stand-in. A reproduce command that quietly disables the thing
being judged is worse than one that fails.

## Tests

Name a regression test after the defect it fixes. The test list should read as a
changelog of real bugs found — `test_disjoint_dob_removes_the_hit_entirely` is worth
more than `test_matcher_2`.

Database tests skip without Postgres. That is convenient locally and dangerous in CI,
so CI asserts the skip count is zero.

## Commits

Explain why the change was necessary, not what the diff shows. Several commits here
document a bug the tests caught and what class of failure it belonged to; that is the
standard.
