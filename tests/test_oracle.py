"""Oracle client tests -- the scope pin, the batch payload, and the failure modes.

Every test here runs against an httpx.MockTransport standing in for yente. That is
deliberate: yente is the thing this project does *not* control, so what these tests
verify is our half of the contract -- that we ask the right endpoint, send the right
query, read the answer faithfully, and never let a broken or silent oracle look like
an oracle that agreed.
"""

import json

import httpx
import pytest

from interdict.oracle import SCOPE, YENTE_URL, Oracle, _sdn_uid

BASE = "http://yente.test"


def _oracle(handler, **kwargs) -> tuple[Oracle, list[httpx.Request]]:
    """An Oracle whose transport is a recorder wrapped around `handler`."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    oracle = Oracle(base_url=kwargs.pop("base_url", BASE), **kwargs)
    oracle._client.close()
    oracle._client = httpx.Client(transport=httpx.MockTransport(record))
    return oracle, seen


def _responds(body: dict, status: int = 200):
    return lambda request: httpx.Response(status, json=body)


def _match_body(**responses) -> dict:
    return {"responses": responses}


def _result(**overrides) -> dict:
    base = {"id": "Q334775", "caption": "Abu ABBAS", "score": 0.94,
            "referents": ["ofac-2674"]}
    base.update(overrides)
    return base


def _sent(request: httpx.Request) -> dict:
    return json.loads(request.content)


# ---------------------------------------------------------------------------
# The scope pin (audit F6) -- the single most load-bearing property in the module
# ---------------------------------------------------------------------------

def test_the_scope_constant_is_the_ofac_list_and_not_yentes_default():
    """`default` spans 465 datasets; matching against it would inflate every
    agreement number this project publishes."""
    assert SCOPE == "us_ofac_sdn"


def test_match_calls_the_ofac_scoped_endpoint_and_nothing_else():
    oracle, seen = _oracle(_responds(_match_body()))
    oracle.match({"1": {"name": "Abu Abbas"}})
    assert seen[0].url.path == "/match/us_ofac_sdn"


def test_a_trailing_slash_on_the_base_url_does_not_produce_a_double_slash():
    oracle, seen = _oracle(_responds(_match_body()), base_url=BASE + "/")
    assert oracle.base_url == BASE
    oracle.match({"1": {"name": "Abu Abbas"}})
    assert str(seen[0].url).startswith(f"{BASE}/match/{SCOPE}?")


def test_the_default_base_url_is_the_configured_yente_url():
    with Oracle() as oracle:
        assert oracle.base_url == YENTE_URL.rstrip("/")


# ---------------------------------------------------------------------------
# The request we send
# ---------------------------------------------------------------------------

def test_an_empty_batch_never_touches_the_network():
    oracle, seen = _oracle(_responds(_match_body()))
    assert oracle.match({}) == {}
    assert seen == [], "an empty batch must not cost an HTTP round trip"


def test_a_whole_batch_travels_in_one_request():
    """One call per screening run, not one per name -- a grade that takes all night
    is a grade nobody collects."""
    oracle, seen = _oracle(_responds(_match_body()))
    oracle.match({"a": {"name": "One"}, "b": {"name": "Two"}, "c": {"name": "Three"}})
    assert len(seen) == 1
    assert set(_sent(seen[0])["queries"]) == {"a", "b", "c"}


def test_threshold_and_limit_are_sent_as_query_parameters():
    oracle, seen = _oracle(_responds(_match_body()))
    oracle.match({"1": {"name": "Abu Abbas"}}, threshold=0.42, limit=9)
    assert seen[0].url.params["threshold"] == "0.42"
    assert seen[0].url.params["limit"] == "9"


def test_threshold_and_limit_have_defaults_so_a_caller_cannot_omit_them():
    oracle, seen = _oracle(_responds(_match_body()))
    oracle.match({"1": {"name": "Abu Abbas"}})
    assert seen[0].url.params["threshold"] == "0.7"
    assert seen[0].url.params["limit"] == "5"


def test_a_query_defaults_to_the_person_schema():
    oracle, seen = _oracle(_responds(_match_body()))
    oracle.match({"1": {"name": "Abu Abbas"}})
    assert _sent(seen[0])["queries"]["1"]["schema"] == "Person"


def test_a_caller_supplied_schema_overrides_the_person_default():
    oracle, seen = _oracle(_responds(_match_body()))
    oracle.match({"1": {"name": "Acme Trading", "schema": "Company"}})
    assert _sent(seen[0])["queries"]["1"]["schema"] == "Company"


def test_a_dob_is_sent_to_yente_as_a_birth_date_property():
    oracle, seen = _oracle(_responds(_match_body()))
    oracle.match({"1": {"name": "Abu Abbas", "dob": "1948-12-10"}})
    props = _sent(seen[0])["queries"]["1"]["properties"]
    assert props == {"name": ["Abu Abbas"], "birthDate": ["1948-12-10"]}


def test_a_missing_dob_is_omitted_rather_than_sent_as_null():
    """yente scores a null birthDate as a supplied-but-empty property, which is a
    different question from the one we are asking."""
    oracle, seen = _oracle(_responds(_match_body()))
    oracle.match({"1": {"name": "Abu Abbas"}})
    assert _sent(seen[0])["queries"]["1"]["properties"] == {"name": ["Abu Abbas"]}


def test_an_empty_dob_string_is_omitted_too():
    oracle, seen = _oracle(_responds(_match_body()))
    oracle.match({"1": {"name": "Abu Abbas", "dob": ""}})
    assert "birthDate" not in _sent(seen[0])["queries"]["1"]["properties"]


# ---------------------------------------------------------------------------
# The answer we read back
# ---------------------------------------------------------------------------

def test_a_hit_carries_score_caption_and_canonical_id_through_unchanged():
    body = _match_body(**{"1": {"results": [_result()]}})
    oracle, _ = _oracle(_responds(body))
    hit = oracle.match({"1": {"name": "Abu Abbas"}})["1"][0]
    assert (hit.score, hit.caption, hit.canonical_id) == (0.94, "Abu ABBAS", "Q334775")


def test_every_result_in_a_response_becomes_a_hit_in_order():
    body = _match_body(**{"1": {"results": [
        _result(id="Q1", score=0.9), _result(id="Q2", score=0.8),
    ]}})
    oracle, _ = _oracle(_responds(body))
    assert [h.canonical_id for h in oracle.match({"1": {"name": "x"}})["1"]] == ["Q1", "Q2"]


def test_a_missing_score_reads_as_zero_rather_than_crashing():
    body = _match_body(**{"1": {"results": [{"id": "Q1", "referents": ["ofac-1"]}]}})
    oracle, _ = _oracle(_responds(body))
    hit = oracle.match({"1": {"name": "x"}})["1"][0]
    assert hit.score == 0.0 and hit.caption == ""


def test_an_integer_score_is_coerced_to_a_float():
    body = _match_body(**{"1": {"results": [_result(score=1)]}})
    oracle, _ = _oracle(_responds(body))
    assert isinstance(oracle.match({"1": {"name": "x"}})["1"][0].score, float)


def test_a_query_yente_found_nothing_for_maps_to_an_empty_list():
    """The disagreement case: yente answered, and its answer was 'no match'. That is
    a real grade and must be distinguishable from yente not answering at all."""
    body = _match_body(**{"1": {"results": []}})
    oracle, _ = _oracle(_responds(body))
    assert oracle.match({"1": {"name": "Jennifer Marie Thompson"}}) == {"1": []}


def test_a_key_yente_omits_entirely_is_absent_from_the_result():
    body = _match_body(**{"a": {"results": [_result()]}})
    oracle, _ = _oracle(_responds(body))
    out = oracle.match({"a": {"name": "Abu Abbas"}, "b": {"name": "Someone Else"}})
    assert "b" not in out, "an unanswered key must not be fabricated as a clear"


def test_a_response_body_with_no_responses_block_yields_no_grades_at_all():
    oracle, _ = _oracle(_responds({}))
    assert oracle.match({"1": {"name": "Abu Abbas"}}) == {}


def test_a_result_with_no_results_key_yields_no_hits():
    oracle, _ = _oracle(_responds(_match_body(**{"1": {}})))
    assert oracle.match({"1": {"name": "x"}}) == {"1": []}


# ---------------------------------------------------------------------------
# Resolving the OFAC uid -- the join key back onto our own screening
# ---------------------------------------------------------------------------

def test_the_uid_comes_from_the_ofac_referent():
    assert _sdn_uid({"referents": ["ofac-2674"]}) == "2674"


def test_a_non_ofac_referent_is_ignored():
    """yente entities carry Wikidata and EU-list referents too; attributing one of
    those as an SDN uid would corrupt the join onto our own results."""
    assert _sdn_uid({"referents": ["wd-Q334775", "eu-fsf-1", "ofac-2674"]}) == "2674"


def test_the_uid_falls_back_to_the_ofac_detail_url():
    result = {"referents": ["wd-Q334775"],
              "properties": {"sourceUrl": [
                  "https://sanctionssearch.ofac.treas.gov/Details.aspx?id=2674"]}}
    assert _sdn_uid(result) == "2674"


def test_a_source_url_that_is_not_an_ofac_detail_page_is_not_mined_for_a_uid():
    result = {"properties": {"sourceUrl": ["https://example.org/press-release?id=99"]}}
    assert _sdn_uid(result) is None


def test_an_entity_with_nothing_ofac_about_it_has_no_uid():
    assert _sdn_uid({"id": "Q1", "caption": "Someone"}) is None


def test_null_referents_and_null_properties_do_not_crash():
    """yente emits explicit nulls for absent blocks, which `.get(k, default)` will
    hand straight back as None."""
    assert _sdn_uid({"referents": None, "properties": None}) is None


def test_the_uid_survives_the_round_trip_into_a_hit():
    body = _match_body(**{"1": {"results": [_result(referents=["ofac-2674"])]}})
    oracle, _ = _oracle(_responds(body))
    assert oracle.match({"1": {"name": "Abu Abbas"}})["1"][0].sdn_uid == "2674"


def test_an_unidentifiable_hit_reports_a_none_uid_rather_than_guessing():
    body = _match_body(**{"1": {"results": [_result(referents=["wd-Q1"])]}})
    oracle, _ = _oracle(_responds(body))
    assert oracle.match({"1": {"name": "Abu Abbas"}})["1"][0].sdn_uid is None


# ---------------------------------------------------------------------------
# Failure modes -- "the oracle is down" must never read as "the oracle agreed"
# ---------------------------------------------------------------------------

def test_an_http_error_from_yente_raises_instead_of_returning_no_matches():
    oracle, _ = _oracle(_responds({"detail": "boom"}, status=500))
    with pytest.raises(httpx.HTTPStatusError):
        oracle.match({"1": {"name": "Abu Abbas"}})


def test_a_missing_scope_is_a_hard_error_not_a_silent_clear():
    """If the us_ofac_sdn index is not built, yente answers 404. Swallowing that
    would grade every name as 'oracle saw nothing'."""
    oracle, _ = _oracle(_responds({"detail": "dataset not found"}, status=404))
    with pytest.raises(httpx.HTTPStatusError):
        oracle.match({"1": {"name": "Abu Abbas"}})


def test_an_unreachable_yente_raises_out_of_match():
    def dead(request):
        raise httpx.ConnectError("connection refused", request=request)

    oracle, _ = _oracle(dead)
    with pytest.raises(httpx.ConnectError):
        oracle.match({"1": {"name": "Abu Abbas"}})


def test_healthy_is_true_when_readyz_answers_two_hundred():
    oracle, seen = _oracle(lambda request: httpx.Response(200, text="ok"))
    assert oracle.healthy() is True
    assert seen[0].url.path == "/readyz"


def test_healthy_is_false_while_yente_is_still_indexing():
    oracle, _ = _oracle(lambda request: httpx.Response(503, text="not ready"))
    assert oracle.healthy() is False


def test_healthy_is_false_rather_than_raising_when_yente_is_unreachable():
    """Callers gate on this before a run; it has to answer, not explode."""
    def dead(request):
        raise httpx.ConnectError("connection refused", request=request)

    oracle, _ = _oracle(dead)
    assert oracle.healthy() is False


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_close_closes_the_underlying_http_client():
    oracle, _ = _oracle(_responds(_match_body()))
    oracle.close()
    assert oracle._client.is_closed


def test_the_context_manager_yields_the_oracle_and_closes_it_on_exit():
    oracle, _ = _oracle(_responds(_match_body()))
    with oracle as entered:
        assert entered is oracle
        assert not oracle._client.is_closed
    assert oracle._client.is_closed


def test_the_context_manager_closes_the_client_even_when_the_body_raises():
    oracle, _ = _oracle(_responds(_match_body()))
    with pytest.raises(RuntimeError):
        with oracle:
            raise RuntimeError("screening blew up")
    assert oracle._client.is_closed
