"""Matcher scoring tests.

The signal tests use hand-built entries so they assert on the *mechanism* rather than
on whoever happens to be on the list this week. The end-to-end tests use the real
publication, because that is what the demo screens.
"""

import pathlib

import pytest

from interdict.matcher import T_HI, T_LO, Matcher, score_pair
from interdict.ofac import Name, SdnEntry, parse_sdn

SDN = pathlib.Path(__file__).resolve().parents[1] / "data" / "SDN.XML"


def entry(uid="1", sdn_type="Individual", names=(), dobs=()):
    return SdnEntry(uid=uid, sdn_type=sdn_type,
                    primary_name=names[0].text if names else "",
                    names=tuple(names), programs=(), dobs=tuple(dobs))


def test_exact_name_scores_one():
    score, _ = score_pair("Abu Abbas", Name("Abu ABBAS", "primary", "primary"), None, ())
    assert score == 1.0


def test_weak_alias_is_downweighted_not_ignored():
    strong, _ = score_pair("Acme Trading", Name("Acme Trading", "strong", "a.k.a."), None, ())
    weak, _ = score_pair("Acme Trading", Name("Acme Trading", "weak", "a.k.a."), None, ())
    assert weak < strong
    # Downweighted, but still a real signal -- a weak alias is evidence, not noise.
    assert weak > T_LO


def test_corroborating_dob_raises_score():
    from interdict.normalize import parse_dob
    dob = parse_dob("10 Dec 1948")
    with_dob, comp = score_pair("Abu Abbas", Name("Abu ABBAS", "primary", "primary"),
                                dob, ("10 Dec 1948",))
    assert comp.dob_signal == "corroborated"
    assert with_dob == 1.0


def test_disjoint_dob_cuts_score_hard():
    from interdict.normalize import parse_dob
    dob = parse_dob("3 Mar 1990")
    score, comp = score_pair("Abu Abbas", Name("Abu ABBAS", "primary", "primary"),
                             dob, ("10 Dec 1948",))
    assert comp.dob_signal == "disjoint"
    # A perfect name with a contradicting date must fall below the adjudication bar.
    assert score < T_HI


def test_circa_dob_still_corroborates():
    from interdict.normalize import parse_dob
    score, comp = score_pair("Abu Abbas", Name("Abu ABBAS", "primary", "primary"),
                             parse_dob("1952"), ("circa 1951",))
    assert comp.dob_signal == "corroborated"


def test_missing_dob_is_inert_not_penalised():
    _, comp = score_pair("Abu Abbas", Name("Abu ABBAS", "primary", "primary"), None, ())
    assert comp.dob_signal == "unavailable"


def test_person_vs_entity_type_mismatch_is_downweighted():
    consistent, c1 = score_pair("Abu Abbas", Name("Abu ABBAS", "primary", "primary"),
                                None, (), cand_sdn_type="Individual", query_is_person=True)
    mismatch, c2 = score_pair("Abu Abbas", Name("Abu ABBAS", "primary", "primary"),
                              None, (), cand_sdn_type="Entity", query_is_person=True)
    assert c1.type_signal == "consistent" and c2.type_signal == "mismatch"
    assert mismatch < consistent


def test_type_signal_inert_when_caller_does_not_say():
    _, comp = score_pair("Abu Abbas", Name("Abu ABBAS", "primary", "primary"), None, ())
    assert comp.type_signal == "unknown"


def test_scoring_is_deterministic():
    args = ("Ibrahim Al-Rashid", Name("Ibrāhīm al Rashid", "strong", "a.k.a."), None, ())
    assert score_pair(*args) == score_pair(*args)


def test_below_t_lo_is_never_returned():
    m = Matcher([entry(names=[Name("Completely Different Person", "primary", "primary")])])
    assert m.screen("Jennifer Marie Thompson") == []


def test_blocking_recall_for_reordered_name():
    m = Matcher([entry(names=[Name("Ibrahim Rashid", "primary", "primary")])])
    assert m.screen("Rashid Ibrahim"), "reordered name fell out of the candidate set"


def test_results_are_sorted_best_first():
    m = Matcher([
        entry(uid="a", names=[Name("Acme Trading Group", "primary", "primary")]),
        entry(uid="b", names=[Name("Acme Trading", "primary", "primary")]),
    ])
    res = m.screen("Acme Trading")
    assert [r.score for r in res] == sorted((r.score for r in res), reverse=True)


# ---------------------------------------------------------------------------
# Against the real publication
# ---------------------------------------------------------------------------

realdata = pytest.mark.skipif(not SDN.exists(), reason="data/SDN.XML not fetched")


@pytest.fixture(scope="module")
def live():
    return Matcher(parse_sdn(SDN)[0])


@realdata
def test_real_sdn_primary_name_is_a_hit(live):
    res = live.screen("Abu ABBAS")
    assert res and res[0].score >= T_HI
    assert res[0].sdn_uid == "2674"


@realdata
def test_real_strong_alias_resolves_to_same_entry(live):
    res = live.screen("Muhammad Zaydan")
    assert res[0].sdn_uid == "2674"


@realdata
def test_disjoint_dob_removes_the_hit_entirely(live):
    """The regression this signal was added for.

    Cutting the Individual's score for a contradicting DOB must not promote the
    same-named Entity -- which carries no DOB and so cannot be disconfirmed -- into
    the top slot as a spurious hit.
    """
    res = live.screen("Abu Abbas", "3 Mar 1990")
    assert not res or res[0].score < T_HI


@realdata
def test_ordinary_grantee_does_not_hit(live):
    res = live.screen("Jennifer Marie Thompson")
    assert not res or res[0].score < T_HI
