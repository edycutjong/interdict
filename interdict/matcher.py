"""Plane 1 -- the deterministic matcher. No LLM, no network, no randomness.

This plane exists twice over. It is the screening engine, and it is *also the oracle
for the Gemini plane*: the orchestrator compares every adjudication against the
deterministic score at the inter-agent routing boundary and quarantines disagreements.
That second role is why this file may not call a model, and why every score ships with
its component breakdown persisted -- an oracle whose reasoning cannot be inspected is
not an oracle.

Scoring is a weighted blend of four independent name signals, then adjusted by two
OFAC-published signals we consume rather than invent:

  * alias category `weak`   -- OFAC's own flag for low-quality identifiers (4,393 of
                              them in the 08/07/2026 publication, verified). Matching
                              on a weak alias alone is the classic false-positive
                              generator, so it is downweighted, not ignored.
  * date of birth           -- corroborates or *disconfirms*. A disjoint DOB interval
                              is the single most useful false-positive killer in
                              sanctions screening, so it cuts the score hard.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from .normalize import DobInterval, blocking_keys, normalize, parse_dob, tokens
from .ofac import WEAK, Name, SdnEntry

# Tuned 2026-08-17 by `scripts/tune_thresholds.py` on 400 PERTURBED names -- measured,
# not chosen. Full sweep in data/thresholds.json. Both are module constants rather than
# run flags: an agreement number only means something against a pinned pair.
#
# WHY T_HI IS TUNED FOR RECALL, NOT FOR MAXIMUM MARGIN.
# The sweep's widest recall/false-hit margin sits at 0.82, and a naive reading would
# pick it. That would be tuning this plane as if it were the whole system. It is not:
# the adjudicator exists to CLEAR lookalikes, so precision is its job, and the two
# errors are nothing like symmetric --
#
#   a MISS      is a payment to a designated party under a strict-liability statute.
#               Nothing downstream can recover it; the system never sees the name again.
#   a FALSE HIT is one extra Gemini adjudication, which then clears it with a written
#               rationale. It costs a fraction of a cent and a line in the console.
#
# So this plane is deliberately run hot and the LLM plane supplies precision. Tuning
# both planes for precision would double-count the same conservatism and lose real hits
# to no purpose.
#
#   T_HI    recall   false_hit
#   0.85    0.855    0.043       previous value -- lost 14.5% of true hits
#   0.82    0.932    0.070       max margin
#   0.78    0.975    0.160       CHOSEN
#
# At 0.78 a full 400-counterparty re-screen sends ~64 extra candidates to adjudication.
# That is the cost of finding 12 percentage points more of the people we are legally
# required to find, and it is a trade worth making every time.
T_HI = 0.78   # >= this is a candidate hit and goes to adjudication

# T_LO is the auto-no-hit floor: below it nothing is persisted and nothing is ever
# adjudicated, so it is the one threshold where a mistake is genuinely invisible.
# Recall is 1.000 at both 0.60 and 0.62 and only starts falling at 0.64, so this sits
# at the last measured value that loses nothing.
T_LO = 0.62

_WEAK_ALIAS_FACTOR = 0.80
_DOB_DISJOINT_FACTOR = 0.55
_DOB_CORROBORATION_BONUS = 0.05
_TOKEN_MATCH_FLOOR = 0.87

# Type-consistency downweight. Found on 2026-08-17 while testing DOB disconfirmation:
# cutting an Individual's score for a disjoint DOB let an *Entity* record sharing the
# same name ("PLF-ABU ABBAS", which has no DOB and therefore cannot be disconfirmed)
# float to the top of a person query. Without this, every DOB cut hands the top slot to
# whichever same-named record happens to carry no date -- the disconfirmation signal
# would actively make ranking worse. A natural person and a vessel are not the same
# counterparty, and the feed tells us which is which.
_TYPE_MISMATCH_FACTOR = 0.85
_NON_PERSON_TYPES = frozenset({"Entity", "Vessel", "Aircraft"})


@dataclass(frozen=True)
class Components:
    """Per-signal breakdown, persisted to matches.components for replay and defence."""

    sort_ratio: float
    set_ratio: float
    jaro_winkler: float
    token_coverage: float
    base: float
    weak_alias: bool
    dob_signal: str          # 'corroborated' | 'disjoint' | 'unavailable'
    type_signal: str         # 'consistent' | 'mismatch' | 'unknown'
    matched_name: str
    matched_category: str
    matched_sdn_type: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Match:
    sdn_uid: str
    score: float
    components: Components


def _token_coverage(query_tokens: list[str], cand_tokens: list[str]) -> float:
    """Fraction of query tokens with a close counterpart in the candidate.

    Distinct from the ratio metrics: it answers "is every part of the queried name
    actually accounted for?", which is what stops a short query scoring highly against
    a much longer name that merely contains it.
    """
    if not query_tokens:
        return 0.0
    hits = 0
    for qt in query_tokens:
        best = max((JaroWinkler.similarity(qt, ct) for ct in cand_tokens), default=0.0)
        if best >= _TOKEN_MATCH_FLOOR:
            hits += 1
    return hits / len(query_tokens)


def score_pair(query_name: str, cand: Name,
               query_dob: DobInterval | None,
               cand_dobs: tuple[str, ...],
               cand_sdn_type: str = "",
               query_is_person: bool | None = None) -> tuple[float, Components]:
    """Score one query name against one candidate name. Pure."""
    nq, nc = normalize(query_name), normalize(cand.text)
    tq, tc = tokens(query_name), tokens(cand.text)

    sort_ratio = fuzz.token_sort_ratio(nq, nc) / 100.0
    set_ratio = fuzz.token_set_ratio(nq, nc) / 100.0
    jw = JaroWinkler.similarity(nq, nc)
    coverage = _token_coverage(tq, tc)

    # token_set_ratio is the most permissive of the four (a strict subset scores 1.0),
    # so it carries the least weight -- it is there to catch reorderings and dropped
    # particles, not to drive the decision.
    base = (0.50 * sort_ratio) + (0.25 * jw) + (0.15 * coverage) + (0.10 * set_ratio)

    score = base
    if cand.category == WEAK:
        score *= _WEAK_ALIAS_FACTOR

    dob_signal = "unavailable"
    if query_dob is not None and cand_dobs:
        intervals = [iv for iv in (parse_dob(d) for d in cand_dobs) if iv is not None]
        if intervals:
            if any(query_dob.overlaps(iv) for iv in intervals):
                dob_signal = "corroborated"
                score = min(1.0, score + _DOB_CORROBORATION_BONUS)
            else:
                dob_signal = "disjoint"
                score *= _DOB_DISJOINT_FACTOR

    # Applied AFTER the DOB adjustment so that a disconfirmed person cannot be
    # out-ranked by a same-named non-person that simply had no date to check.
    type_signal = "unknown"
    if query_is_person is not None and cand_sdn_type:
        cand_is_person = cand_sdn_type not in _NON_PERSON_TYPES
        if query_is_person == cand_is_person:
            type_signal = "consistent"
        else:
            type_signal = "mismatch"
            score *= _TYPE_MISMATCH_FACTOR

    components = Components(
        sort_ratio=round(sort_ratio, 4),
        set_ratio=round(set_ratio, 4),
        jaro_winkler=round(jw, 4),
        token_coverage=round(coverage, 4),
        base=round(base, 4),
        weak_alias=(cand.category == WEAK),
        dob_signal=dob_signal,
        type_signal=type_signal,
        matched_name=cand.text,
        matched_category=cand.category,
        matched_sdn_type=cand_sdn_type,
    )
    return round(min(1.0, score), 4), components


class Matcher:
    """Blocking index over an SDN publication.

    Built once per publication and reused across the whole book -- a full re-screen is
    400 x (a few dozen blocked candidates), not 400 x 24,576.
    """

    def __init__(self, entries: list[SdnEntry]):
        self.entries: dict[str, SdnEntry] = {e.uid: e for e in entries}
        self._index: dict[str, set[str]] = defaultdict(set)
        for entry in entries:
            for name in entry.names:
                for key in blocking_keys(name.text):
                    self._index[key].add(entry.uid)

    @property
    def name_count(self) -> int:
        return sum(len(e.names) for e in self.entries.values())

    def candidates(self, query_name: str) -> set[str]:
        uids: set[str] = set()
        for key in blocking_keys(query_name):
            uids |= self._index.get(key, set())
        return uids

    def screen(self, query_name: str, dob: str | None = None,
               limit: int = 10, is_person: bool | None = None) -> list[Match]:
        """Screen one counterparty against the publication.

        Returns candidates at or above T_LO, best first. Anything below T_LO is an
        auto-no-hit by definition and is never persisted or sent to adjudication.

        `is_person` drives the type-consistency signal. When the caller does not say,
        a supplied date of birth is taken as evidence that the counterparty is a natural
        person; with neither, the signal is recorded as 'unknown' and left inert rather
        than guessed.
        """
        query_dob = parse_dob(dob) if dob else None
        if is_person is None and dob:
            is_person = True
        results: list[Match] = []

        for uid in self.candidates(query_name):
            entry = self.entries[uid]
            best_score, best_components = 0.0, None
            for cand in entry.names:
                score, components = score_pair(
                    query_name, cand, query_dob, entry.dobs,
                    cand_sdn_type=entry.sdn_type, query_is_person=is_person)
                if score > best_score:
                    best_score, best_components = score, components
            if best_components is not None and best_score >= T_LO:
                results.append(Match(uid, best_score, best_components))

        results.sort(key=lambda m: (-m.score, m.sdn_uid))
        return results[:limit]
