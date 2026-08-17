"""Name normalisation and DOB parsing for the deterministic screening plane.

Everything here is pure and deterministic: same input, same output, no clock, no RNG,
no network. That is what lets `make challenge` reproduce any screening decision on any
machine, and what lets the matcher be graded against yente run-for-run.

DOB NOTE (verified against the live feed 2026-08-17, correcting the spec package):
`SDN.XML` does NOT carry `isApproximate` / `isDateRange` attributes -- 0 occurrences in
the 19,199-record publication. Those fields belong to OFAC's ADVANCED/enhanced export,
which this project does not consume. Date imprecision in this feed is expressed in the
*text* of `<dateOfBirth>`: "circa 1951", "1948 to 1950", bare years. So corroboration
parses the string and reasons about the resulting interval.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Honorifics, titles and organisational suffixes carry no identifying signal but wreck
# token-overlap scores. Stripped before comparison, never before display.
_NOISE_TOKENS = {
    "MR", "MRS", "MS", "DR", "PROF", "SIR", "SHEIKH", "SHAYKH", "HAJJI", "HAJI",
    "GENERAL", "GEN", "COL", "COLONEL", "CAPT", "CAPTAIN", "MAJ", "MAJOR", "LT",
    "THE", "AND", "OF", "FOR",
    "LLC", "LTD", "LIMITED", "INC", "INCORPORATED", "CO", "COMPANY", "CORP",
    "CORPORATION", "GMBH", "SA", "SAS", "BV", "NV", "PLC", "AG", "SRL", "SPA",
    "PJSC", "OJSC", "JSC", "OOO", "ZAO", "PT", "TBK", "AS", "AB", "OY",
}

# Arabic/Persian/Turkic name particles. These are real parts of a name but appear
# inconsistently across transliterations ("al-Rashid" / "Al Rashid" / "alRashid"),
# so they are folded to a canonical bare form rather than dropped.
_PARTICLES = {"AL", "EL", "AD", "AS", "ASH", "AR", "AN", "ABU", "ABD", "BIN", "BEN",
              "IBN", "BINT", "VAN", "VON", "DE", "DA", "DI", "DEL", "DELLA", "DOS"}

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def strip_diacritics(text: str) -> str:
    """NFKD-decompose and drop combining marks: Ibrahim == Ibrāhīm."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(name: str) -> str:
    """Canonical comparison form. Uppercase, de-accented, de-punctuated, de-noised."""
    text = strip_diacritics(name).upper()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def tokens(name: str, *, drop_particles: bool = False) -> list[str]:
    """Significant tokens of a name, in order.

    Particles are kept by default -- "ABD AL RAHMAN" and "RAHMAN" are genuinely
    different people, and dropping particles wholesale is how screening systems
    manufacture false positives. `drop_particles=True` exists only to build the
    coarse blocking key, where recall matters more than precision.
    """
    out = []
    for tok in normalize(name).split():
        if tok in _NOISE_TOKENS:
            continue
        if drop_particles and tok in _PARTICLES:
            continue
        if len(tok) == 1:  # stray initials survive normalisation but add noise
            continue
        out.append(tok)
    return out


def blocking_keys(name: str) -> set[str]:
    """Cheap candidate-generation keys.

    Blocking exists so a full-book re-screen does not become 400 x 24,576 string
    comparisons. Recall here is what bounds recall overall: a true hit that never
    enters the candidate set can never be scored, so these keys are deliberately
    generous and the precision work happens later in `score`.
    """
    keys: set[str] = set()
    toks = tokens(name, drop_particles=True)
    for tok in toks:
        if len(tok) >= 4:
            keys.add(tok[:4])      # prefix block
        keys.add(tok)              # exact-token block
    if toks:
        # Sorted-token fingerprint catches word-order permutations
        # ("RASHID IBRAHIM" vs "IBRAHIM RASHID").
        keys.add("|".join(sorted(toks)))
    return keys


# ---------------------------------------------------------------------------
# Dates of birth
# ---------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

_DAY_MON_YEAR = re.compile(r"^(\d{1,2})\s+([A-Z]{3})[A-Z]*\s+(\d{4})$")
_MON_YEAR = re.compile(r"^([A-Z]{3})[A-Z]*\s+(\d{4})$")
_YEAR = re.compile(r"^(\d{4})$")
_RANGE = re.compile(r"^(\d{4})\s*(?:TO|-|AND)\s*(\d{4})$")


@dataclass(frozen=True)
class DobInterval:
    """A date of birth as the interval it actually denotes.

    OFAC dates are frequently imprecise, and treating "circa 1951" as the point
    1951-01-01 produces confident nonsense. Representing every DOB as a [start, end]
    year interval means corroboration is an interval-overlap question, which is both
    honest and cheap.
    """

    start_year: int
    end_year: int
    precision: str  # 'day' | 'month' | 'year' | 'circa' | 'range'
    raw: str

    def overlaps(self, other: "DobInterval") -> bool:
        return self.start_year <= other.end_year and other.start_year <= self.end_year


def parse_dob(raw: str) -> DobInterval | None:
    """Parse an OFAC date-of-birth string into an interval. None if unparseable."""
    if not raw:
        return None

    text = normalize(raw)
    circa = False
    if text.startswith("CIRCA "):
        circa = True
        text = text[6:].strip()

    m = _RANGE.match(text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return DobInterval(min(lo, hi), max(lo, hi), "range", raw)

    m = _DAY_MON_YEAR.match(text)
    if m and m.group(2) in _MONTHS:
        year = int(m.group(3))
        # A circa-qualified exact date still means "about"; widen by a year either way.
        if circa:
            return DobInterval(year - 1, year + 1, "circa", raw)
        return DobInterval(year, year, "day", raw)

    m = _MON_YEAR.match(text)
    if m and m.group(1) in _MONTHS:
        year = int(m.group(2))
        return DobInterval(year, year, "circa" if circa else "month", raw)

    m = _YEAR.match(text)
    if m:
        year = int(m.group(1))
        # "circa 1951" in OFAC practice spans roughly a couple of years either side.
        if circa:
            return DobInterval(year - 2, year + 2, "circa", raw)
        return DobInterval(year, year, "year", raw)

    return None
