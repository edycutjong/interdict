# Sentinel book — provenance and integrity record

**Sealed 2026-08-13.** This file exists so a judge can verify that Interdict's release-leg demo was
not staged. Everything here is reproducible from this repository.

## The claim being proved

Interdict autonomously **releases** blocked funds when OFAC delists a party. The easy way to show
that is to pre-seed a counterparty you already know was removed and replay the delta — hindsight,
and rightly discounted.

Instead the counterparty book contains 400 **sentinels** drawn from entries that were on the SDN
list *at seal time*. This file and its hashes are committed **before** any later OFAC publication
exists. If OFAC subsequently removes someone in this book, the release leg fires on an entry nobody
could have known would be delisted, and `git log` proves the book predates the removal.

## Hashes

| Artifact | SHA-256 |
|---|---|
| `SDN.XML` (source snapshot, not committed — 27 MB) | `ac00228a68345e5c0d7174713cf97e5d5a8efe7cec5c2f540ed87106f49f7474` |
| `sentinels.csv` (the book) | `66eb151cc7473024ed3f9fb4f43700edde828a5ab53d7122e6145c58e72b9b1b` |

Source snapshot: OFAC SDN list, **published 08/07/2026, 19,199 records**, fetched
2026-08-12 from `https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML`
(302 → presigned S3; the fetch must follow redirects).

## Reproduce

```bash
curl -sSL -o data/SDN.XML \
  "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
shasum -a 256 data/SDN.XML          # must match the snapshot hash above
python3 scripts/seed_sentinels.py --sdn data/SDN.XML --out data/sentinels.csv
shasum -a 256 data/sentinels.csv    # must match the book hash above
```

Selection is byte-for-byte deterministic — verified by re-running against the same snapshot and
comparing output. There is no RNG.

> Re-running against a *later* SDN snapshot will NOT reproduce this hash, by design: entries added
> or removed since 08/07/2026 change the eligible pool. That is why the snapshot hash is recorded
> alongside the book hash.

## Composition

| Stratum | Rows | Selection |
|---|---|---|
| `SDNTK` (counter-narcotics) | 250 | lowest 250 by `sha256("interdict-sentinel-v1:" + uid)` among SDNTK entries |
| `IRAQ2` | 100 | same rule within IRAQ2 |
| `REMAINDER` | 50 | same rule across everything not already claimed |
| **Total** | **400** | 233 Individual · 167 Entity |

Vessels and aircraft are excluded — an explicit non-goal.

## Why the strata are weighted, and why that is not cherry-picking

Measured from every OFAC Recent Actions publication between 2026-06-26 and 2026-08-07: **396
removals across 7 publications**, but bursty — 96% came from two events and 3 of 7 publications had
zero removals. Removals concentrate in programs that are a small share of the list:

| Program | Share of removals | Share of list | Enrichment |
|---|---|---|---|
| SDNTK | 32.3% | 7.1% | **4.5×** |
| IRAQ2 | 10.6% | 0.8% | **13×** |

Uniform sampling of 400 names (~2% of the list) yields only ~28% probability that any sentinel fires
during a quiet three-week window. Stratifying the *same* 400 names lifts effective coverage to ~13%
and the probability to ~88%.

**The weighting cannot steer the outcome.** Selection is on *program membership*, which is public
and fixed at seal time; which entries OFAC will remove remains unknown. Within every stratum,
ordering is by SHA-256 of the entry `uid` — deterministic, documented, and reproducible above. No
name was chosen by hand.

## Honest limitations

- The counterparty book is **synthetic**. Real Treasury data triggers the system, and real public
  records back the non-sentinel strata, but no actual NGO's payment ledger is involved. Every
  synthetic layer is labelled on screen in the demo.
- Sentinels are labelled `sentinel=true` in-product and disclosed here and in the README. They exist
  to catch real delistings; saying so is the point.
- If no removal intersects this book before the submission deadline, the demo falls back to a
  **clearly labelled replay** of the real 2026-08-07 removals with OFAC's own Recent Actions page
  and the corresponding Federal Register notice on screen. It is never presented as live.
