# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are cut by `.github/workflows/release.yml`, which rewrites `__version__` in
`interdict/__init__.py`, prepends the section below, and tags the commit. `tests/test_version.py`
fails the build if the version, this file and the package ever disagree.

## [Unreleased]

<!-- release-workflow inserts new sections directly below this line -->

## [1.0.6] — 2026-08-27

- Take @lhci/cli out of the lockfile: 12 advisories to 0

## [1.0.5] — 2026-08-27

- Ship the judge surface: /judge, 404, 100% coverage, demo video, E2E and perf gates
- Trim docs/ to referenced images, ship the animated hero and a real b-roll

## [1.0.4] — 2026-08-23

- Seven release runs raced and one left a tag pointing nowhere
- Why the oracle's number moves and ours does not
- The dependency group from #3, applied directly, and what it proved
- An unpacked score nobody asserted on
- Bump mypy from 1.13.0 to 2.3.1 (#5)
- Bump pytest-cov from 6.0.0 to 7.1.0 (#4)
- Bump actions/setup-python from 5 to 7 (#2)
- Bump actions/upload-artifact from 4 to 7 (#1)

## [1.0.3] — 2026-08-23

- A world-readable lock file, flagged high by the scanner

## [1.0.2] — 2026-08-23

- A three-minute run that printed nothing until it was over

## [1.0.1] — 2026-08-23

- The arrow said the link leaves the page and it did not

## [1.0.0] — 2026-08-23

First tagged release. Everything below already existed and ran; this is the point at which
the working system got a version number rather than the point at which it was written.

### Added
- **Deterministic matcher** — Jaro-Winkler plus token scoring over the OFAC SDN publication,
  with weak-alias demotion and date-of-birth contradiction handling. No model on this path.
- **Gemini adjudicator** — `gemini-3.5-flash-lite` via the Google GenAI SDK, structured output
  at temperature 0, every verdict carrying a written rationale and `cited_fields[]`.
- **Oracle guard on the return path** — schema check, citation-versus-record check, and
  disagreement detection against OpenSanctions yente, scope-pinned to `us_ofac_sdn`. Capped at
  two round trips in code *and* as a database constraint; anything unresolved is quarantined,
  and quarantined money stays held.
- **Money plane** — idempotent holds under a `UNIQUE` constraint, release on delisting, and the
  10-business-day blocking report drafted against the statutory clock.
- **Hash-chained append-only ledger** in Postgres 16, with an outbox relay and advisory-locked
  chain writes, verifiable from `make verify-ledger`.
- **Cloud Firestore evidence plane** mirroring committed ledger entries with `seq` and
  `entry_hash`, so the chain verifies from the cloud copy alone.
- **Sealed sentinel book** — 400 entries, SHA-256 `66eb151c…`, sealed before Treasury moved.
- **Perturbed challenge set** — transliteration families, reordering, dropped particles,
  transcription confusables and dropped middle names, each derived from the SHA-256 of the name
  so the set is byte-identical on any machine.
- **Evidence console** and 134 tests, plus CI, CodeQL and gitleaks on every push.

### Measured at this release
- Screening top-1 **0.995** against the independent oracle's **0.840** on perturbed names
  (n=400, `data/g1-perturbed.json`).
- Bench p50 **9.34 ms** / p95 **68.82 ms**, deterministic plane only, adjudication excluded
  (`data/bench.json`).

### Known limits
- Nothing runs on Google Cloud compute; the agents, Postgres and yente run locally, and
  Firestore is the one hosted plane.
- The RELEASE leg ships as a labelled replay of the 08/07/2026 delta.
- The 6-hourly poll archives publications; a re-screen is started by hand, so the ledger's
  `trigger` column reads `MANUAL`.

[Unreleased]: https://github.com/edycutjong/interdict/compare/v1.0.6...HEAD
[1.0.0]: https://github.com/edycutjong/interdict/releases/tag/v1.0.0
[1.0.1]: https://github.com/edycutjong/interdict/releases/tag/v1.0.1
[1.0.2]: https://github.com/edycutjong/interdict/releases/tag/v1.0.2
[1.0.3]: https://github.com/edycutjong/interdict/releases/tag/v1.0.3
[1.0.4]: https://github.com/edycutjong/interdict/releases/tag/v1.0.4
[1.0.5]: https://github.com/edycutjong/interdict/releases/tag/v1.0.5
[1.0.6]: https://github.com/edycutjong/interdict/releases/tag/v1.0.6
