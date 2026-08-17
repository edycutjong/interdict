"""Parser tests against the real, committed OFAC publication.

These run against data/SDN.XML rather than a fixture on purpose: the claims this
project makes on camera ("4,393 weak aliases", "19,199 records") are claims about the
real feed, and a fixture would let them quietly drift from it.
"""

import pathlib

import pytest

from interdict.ofac import WEAK, parse_sdn

SDN = pathlib.Path(__file__).resolve().parents[1] / "data" / "SDN.XML"

pytestmark = pytest.mark.skipif(
    not SDN.exists(),
    reason="data/SDN.XML is gitignored (27MB); run `make fetch-sdn` first",
)


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
    assert pub["publish_date"] == "08/07/2026"


def test_record_count_matches_publication_header(parsed):
    entries, pub = parsed
    assert len(entries) == int(pub["record_count"]) == 19199


def test_weak_alias_count_is_as_published(parsed):
    """The 4,393 figure is quoted in the README and the demo. Pin it."""
    entries, _ = parsed
    weak = sum(1 for e in entries for n in e.names if n.category == WEAK)
    assert weak == 4393


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
