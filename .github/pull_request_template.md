## What this changes

<!-- Why it was necessary, not what the diff shows. -->

## Invariants

Tick what applies. If you touched any of these, say how you know it still holds —
CONTRIBUTING.md explains why each one is load-bearing.

- [ ] I did not add a direct write to `ledger` (the outbox relay is the single writer)
- [ ] I did not change the yente scope pin (`/match/us_ofac_sdn` only)
- [ ] I did not infer identity type, or any other fact, from an optional field being present
- [ ] Synthetic data is still labelled as synthetic everywhere it surfaces
- [ ] `--offline` is still opt-in and still announces itself
- [ ] N/A — this change touches none of the above

## Verification

```
make ci
```

- [ ] `make test` passes with **zero skipped database tests**
- [ ] If this fixes a defect, there is a regression test named after it
- [ ] If this changes a number quoted in `README.md` or `DEMO.md`, both are updated

## Screening impact

<!-- Delete if not applicable. If scoring, thresholds or blocking changed, paste the
     before/after from `make challenge-set` — a change that moves recall or the
     false-hit rate needs the measurement, not an assurance. -->
