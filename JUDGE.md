<div align="center">

<img src="docs/assets/icon.svg" width="88" alt="">

# For judges

**A page with one reader. Everything below is verifiable from a browser tab.**

[**Web version**](https://interdict.edycu.dev/judge/) · [**Repository**](https://github.com/edycutjong/interdict) · [**Landing page**](https://interdict.edycu.dev/) · [**Pitch deck**](https://interdict.edycu.dev/pitch-deck.html)

</div>

---

## The claim

> **When Treasury updates the OFAC list, it re-screens the whole payment book, holds true
> hits, clears lookalikes with written reasons, releases funds on delisting, and drafts the
> 10-day blocking report — unattended.**

One sentence, unchanged from the README, the landing page and the Devpost entry. If the rest
of this page does not support that sentence, the rest of this page is wrong.

---

## The 30-second path

No account, no key, no clone. Four clicks, in order.

| | Click | What you are looking at | Time |
|---|---|---|---|
| 1 | [**The number**](https://interdict.edycu.dev/#oracle) | Top-1 **0.995** against the independent oracle's **0.840**, measured on names that are *not* on the SDN list character-for-character. This is the only screening number worth reading. | 0:00 |
| 2 | [**The console**](docs/assets/screenshots/console-adjudications-dark.png) | Real adjudications with the model's written rationale, the citation, and the oracle's independent verdict on the same row. Not a mockup — captured from the running console. | 0:10 |
| 3 | [**The ground-truth grade**](#the-receipts) | 59 holds graded against strata the screening path cannot see: **1 missed hit in 60**, **0 frozen grantees in 41**. | 0:20 |
| 4 | [**PROVENANCE.md**](data/PROVENANCE.md) | The SHA-256 of the OFAC publication every number above was computed against. Hash it yourself from Treasury's own file. | 0:25 |

**There is no hosted application, and that is stated rather than worked around.** The fleet,
Postgres and yente run locally — the free tier does not extend to Cloud Run and no billing
account was available. So the browser path above is *evidence*, and
[`make reproduce`](#the-reproduce-command) is *execution*. Neither is dressed up as the other.

---

## The receipts

Every number here is printed by a command in [`DEMO.md`](DEMO.md), not typed in by hand.
All figures are against the **08/07/2026 OFAC publication — 19,199 records, 4,393
OFAC-flagged weak aliases**, the snapshot the sentinel book is sealed to.

### Screening — the number that is not a string-equality test

| | value | |
|---|---|---|
| **top-1** (true uid ranked first) | **0.995** | 400 counterparties, every name deterministically perturbed |
| recall (true uid present) | 1.000 | |
| **yente's own recall** | **0.840** | the independent oracle, on the same set — the bar to clear |

Perturbations are transliterations from OFAC's own alias lists, reordering, dropped
particles and transcription confusables, seeded from SHA-256 so the set is byte-identical on
your machine. Screening the names *verbatim* scores 1.000; that number is near-tautological
and is not reported.

### Decision quality — graded against ground truth the screening path cannot see

```
  sentinel    HOLD     30/30       lookalike   CLEAR    25/25
  variant     HOLD     29/30       ordinary    CLEAR    16/16

  MISSED HITS  1/60           FROZEN GRANTEES  0/41
```

101-row stratified sample · 59 held · **$1,181,434.51 frozen** · 0 quarantined · ledger chain
INTACT · adjudicated by `gemini-3.5-flash-lite`.

### Agreement with the independent oracle, on every decision

| | count |
|---|---|
| both flagged a hit | **58** |
| we held, yente missed | 1 |
| **we cleared, yente flagged** | **0** |

The zero is the point. Under strict liability the dangerous direction is being *more
permissive* than the oracle you are checked against.

### Engineering

| | |
|---|---|
| tests | **351 passing** · **100% coverage** (948/948 statements) |
| screening latency | p50 **9.3 ms** · p95 **68.8 ms** · p99 **106.7 ms** |
| full book (400 counterparties) | **7.6 s** |
| second model | **`gemma-4-31b-it` agreed 59/59** — but read the limitation: all 59 are HOLD confirmations, so it has not been asked to discriminate either |
| ledger | **3,765 entries, chain INTACT**, mirrored to Cloud Firestore with `seq` / `prev_hash` / `entry_hash` so the chain verifies from the cloud copy alone |
| OFAC delta archived by content hash | `9403f40d9496…` — [`data/PROVENANCE.md`](data/PROVENANCE.md) |

---

## The reproduce command

**The real path.** This runs the thing being judged.

```bash
make reproduce
```

Installs, brings up Postgres + Elasticsearch + yente, indexes the oracle, applies the schema,
runs the 351 tests, and prints the perturbed screening number.

For the adjudication and interdiction legs:

```bash
export GEMINI_API_KEY=...          # free, no billing: https://aistudio.google.com/apikey

python scripts/load_book.py --truncate
python scripts/run_rescreen.py     # what the timer starts on its own
python scripts/adjudication_quality.py
python -m interdict.console        # evidence console on :8080
```

Screen a name we never chose — this is the point of the command:

```bash
make challenge NAME="Ibrahim Al Rashid"
make challenge NAME="Abu Abbas" DOB="3 Mar 1990"    # contradicting DOB kills the hit
```

> **`run_rescreen.py` refuses to run without a Gemini key.** It does not fall back to a test
> double, because a reproduce command that quietly disables the thing being judged is worse
> than one that fails loudly.

### CI / deterministic replay — *not* the product

`--offline` runs the deterministic plane alone and labels itself as such on every line of
output. That is what CI uses. It is listed here only so it can never be mistaken for the
demo path above.

---

## Honest limitations

Six are listed in the [README](README.md#-known-limitations). These three matter most to
anyone scoring this:

1. **The model has never issued a CLEAR.** Every clear in the graded book came from the
   deterministic plane, because a contradicting date of birth ends the question before
   adjudication is reached. So the adjudicator is exercised on *confirmation*, not on
   discrimination. The grade above should be read with that in mind.
2. **Decision quality is a 101-row stratified sample, not the full 536-row book.** Free-tier
   Gemini allows a fixed number of requests per model per project per day; the full book
   would take several days of quota. The *screening* numbers are unaffected — those are
   measured across all 400.
3. **The release leg is a labelled REPLAY and says so on screen.** The eight delisted parties
   are already gone from the 08/07 publication, so a live release cannot be staged against
   today's list without pretending. Their uids, names, programmes and removal actions all
   come from Treasury's archived delta; the payment book they sit in is synthetic and
   labelled everywhere it appears.

Also worth stating: **nothing runs on Google Cloud compute.** Firestore holds the audit
trail; the agents, Postgres and yente run locally. And yente's own recall on the perturbed
set is 0.840, so part of the agreement gap in the table above is the oracle missing, not us.

---

## What is real, and what is not

**Real — none of it ours:** the OFAC SDN publication (08/07/2026, 19,199 records), the
`/changes/latest` delta and its 18 additions / 8 removals, every alias category including the
4,393 OFAC-flagged weak aliases, every date of birth, every sanctions programme, and the
delisting actions. Archived by content hash in [`data/archive/`](data/archive/).

**Ours, and labelled synthetic everywhere it appears:** the payment book. A real NGO's
grantee ledger is not ours to publish. Counterparties carry an `origin` column and both the
console and the demo label them on screen.

---

## Links

| | |
|---|---|
| **Repository** | https://github.com/edycutjong/interdict |
| **Landing page** | https://interdict.edycu.dev/ |
| **This page, on the web** | https://interdict.edycu.dev/judge/ |
| **Pitch deck** | https://interdict.edycu.dev/pitch-deck.html |
| **Full demo walkthrough** | [`DEMO.md`](DEMO.md) — nine beats, each with the command above its output |
| **Demo video** | https://youtu.be/C1VFGSwS7w4 — 3:43, unedited live execution, Cloud Firestore console on screen |

Built solo for the **All Things Agentic Hackathon**, *Fortified Enterprise Fleet* track.
`gemini-3.5-flash-lite` · Google GenAI SDK · Cloud Firestore · Postgres 16 · OpenSanctions
yente. MIT licensed.
