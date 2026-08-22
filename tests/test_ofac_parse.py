"""Parser tests against the real, committed OFAC publication.

These run against data/SDN.XML rather than a fixture on purpose: the claims this
project makes on camera ("4,393 weak aliases", "19,199 records") are claims about the
real feed, and a fixture would let them quietly drift from it.

WHAT THE PINNED FIGURES ARE CLAIMS ABOUT. They are claims about ONE publication -- the
Standard Action of 08/07/2026, fetched 2026-08-12 and hashed in data/archive/index.json --
not about the SDN list in perpetuity. Treasury publishes again every time it designates or
delists anybody, and it did exactly that on 08/20/2026, taking the list to 19,249 records.
Asserting the frozen numbers against whatever the live endpoint happens to serve therefore
converts every OFAC action into a red CI run, which trains everyone to ignore the red.

It also asks the wrong question. "Does the feed still say 19,199" is Treasury's business.
"Does our parser still agree with whatever the feed says, and does it still find the
schema quirks we depend on" is ours. So the exact figures are asserted when the fetched
publication IS the pinned one, self-consistency is asserted always, and a newer
publication is reported loudly rather than failed -- with both dates named, so a number
that has gone stale in the README or in the demo script is visible instead of silent.
"""

import pathlib
import warnings

import pytest

from interdict.ofac import WEAK, parse_sdn

SDN = pathlib.Path(__file__).resolve().parents[1] / "data" / "SDN.XML"

# The publication every quoted figure in README.md and DEMO.md refers to.
PINNED_DATE = "08/07/2026"
PINNED_RECORDS = 19199
PINNED_WEAK = 4393

pytestmark = pytest.mark.skipif(
    not SDN.exists(),
    reason="data/SDN.XML is gitignored (27MB); run `make fetch-sdn` first",
)


def _is_pinned(pub) -> bool:
    """True when the fetched publication is the one the quoted figures describe.

    Warns rather than fails otherwise: a superseded pin is a documentation task, not a
    parser defect, and it must not be discovered as a mystery CI failure.
    """
    if pub["publish_date"] == PINNED_DATE:
        return True
    warnings.warn(
        f"OFAC has published since the pinned figures were taken: live publication is "
        f"{pub['publish_date']} with {pub['record_count']} records, pinned is "
        f"{PINNED_DATE} with {PINNED_RECORDS}. Exact-figure assertions are relaxed to "
        f"self-consistency. Every '{PINNED_RECORDS:,} records' / '{PINNED_WEAK:,} weak "
        f"aliases' in README.md and DEMO.md must stay labelled {PINNED_DATE}.",
        stacklevel=2,
    )
    return False


@pytest.fixture(scope="module")
def parsed():
    return parse_sdn(SDN)


def test_ofac_schema_typo_is_pinned(parsed):
    """OFAC misspells `publishInformation` as `publshInformation`.

    If Treasury ever corrects the typo we want a loud failure here, not a silently
    empty publication date propagating into the ledger.
    """
    _, pub = parsed
    assert pub["publish_date"], "publication date empty -- did the OFAC typo get fixed?"
    # A date, in Treasury's format, from the misspelled element. Not a specific date:
    # pinning the value here would mean this regression guard fires for the wrong reason
    # every time OFAC publishes, and the thing it guards is the element name.
    assert len(pub["publish_date"].split("/")) == 3, pub["publish_date"]


def test_record_count_matches_publication_header(parsed):
    entries, pub = parsed
    assert len(entries) == int(pub["record_count"])
    if _is_pinned(pub):
        assert len(entries) == PINNED_RECORDS


def test_weak_alias_count_is_as_published(parsed):
    """The 4,393 figure is quoted in the README and the demo. Pin it to its publication."""
    entries, pub = parsed
    weak = sum(1 for e in entries for n in e.names if n.category == WEAK)
    if _is_pinned(pub):
        assert weak == PINNED_WEAK
    else:
        # The claim that survives a republication: OFAC still flags a substantial minority
        # of aliases weak, and the matcher's downweighting still has something to act on.
        assert weak > 1000, weak


def test_every_entry_has_a_uid_and_a_name(parsed):
    entries, _ = parsed
    assert all(e.uid for e in entries)
    assert all(e.names for e in entries)


def test_aliases_are_captured_with_categories(parsed):
    entries, _ = parsed
    by_uid = {e.uid: e for e in entries}
    abbas = by_uid["2674"]
    assert abbas.sdn_type == "Individual"
    assert any(n.text == "Muhammad ZAYDAN" and n.category == "strong" for n in abbas.names)
    assert "10 Dec 1948" in abbas.dobs


def test_sdn_types_are_the_four_ofac_publishes(parsed):
    entries, _ = parsed
    assert {e.sdn_type for e in entries} == {"Individual", "Entity", "Vessel", "Aircraft"}


# ---------------------------------------------------------------------------
# The delta feed -- a different schema entirely, and the RELEASE-leg evidence
# ---------------------------------------------------------------------------

DELTA = (pathlib.Path(__file__).resolve().parents[1] / "data" / "archive" /
         "delta-20260812T221237+0000-9403f40d9496.xml")

delta_only = pytest.mark.skipif(not DELTA.exists(), reason="archived delta missing")


@pytest.fixture(scope="module")
def delta():
    from interdict.ofac import parse_delta
    return parse_delta(DELTA)


@delta_only
def test_delta_action_counts_match_the_publication(delta):
    """The 2026-08-07 Standard Action published 18 adds and 8 removes."""
    assert len(delta) == 26
    assert sum(a.action == "add" for a in delta) == 18
    assert sum(a.action == "remove" for a in delta) == 8


@delta_only
def test_delta_names_are_full_names_not_first_names(delta):
    """Regression: the delta carries names as translations with formattedFullName.

    Walking for the first tag containing 'name' finds formattedFirstName and yields
    half a name, which then matches nothing. uid 11753 is SATIZABAL RENGIFO, Mario
    German -- not 'Mario German'.
    """
    by_uid = {a.uid: a for a in delta}
    assert by_uid["11753"].name == "SATIZABAL RENGIFO, Mario German"


@delta_only
def test_delta_carries_entity_type_and_programs(delta):
    by_uid = {a.uid: a for a in delta}
    assert by_uid["11753"].entity_type == "Individual"
    assert by_uid["16607"].entity_type == "Entity"
    assert by_uid["11812"].programs == ("SDNTK",)


@delta_only
def test_removed_parties_are_absent_from_the_publication(delta, parsed):
    """Internal consistency: a removal in the delta really did leave the list.

    This is what makes the release leg honest -- the delisting is Treasury's, not ours.
    """
    entries, _ = parsed
    uids = {e.uid for e in entries}
    removed = [a.uid for a in delta if a.action == "remove"]
    assert removed and not (set(removed) & uids)


@delta_only
def test_added_parties_are_present_in_the_publication(delta, parsed):
    entries, _ = parsed
    uids = {e.uid for e in entries}
    added = [a.uid for a in delta if a.action == "add"]
    assert added and set(added) <= uids
