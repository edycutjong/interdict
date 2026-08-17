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
