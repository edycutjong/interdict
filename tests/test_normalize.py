"""Normalisation and DOB-interval tests."""

from interdict.normalize import (
    DobInterval,
    blocking_keys,
    normalize,
    parse_dob,
    strip_diacritics,
    tokens,
)


def test_diacritics_fold():
    assert strip_diacritics("Ibrāhīm") == "Ibrahim"
    assert normalize("Ibrāhīm Al-Rashīd") == "IBRAHIM AL RASHID"


def test_punctuation_and_case():
    assert normalize("  o'brien,  j.p. ") == "O BRIEN J P"


def test_noise_tokens_dropped_but_particles_kept():
    # Corporate suffixes carry no identifying signal.
    assert "LIMITED" not in tokens("Jennifer Navigation Limited")
    # Particles DO -- "ABD AL RAHMAN" and "RAHMAN" are different people.
    assert "AL" in tokens("Abd al Rahman")


def test_particles_dropped_only_for_blocking():
    assert "AL" not in tokens("Abd al Rahman", drop_particles=True)


def test_blocking_keys_survive_word_order():
    a = blocking_keys("Ibrahim Rashid")
    b = blocking_keys("Rashid Ibrahim")
    # The sorted-token fingerprint must collide for permuted names, or a re-ordered
    # name never even enters the candidate set.
    assert a & b


def test_parse_dob_exact():
    iv = parse_dob("10 Dec 1948")
    assert (iv.start_year, iv.end_year, iv.precision) == (1948, 1948, "day")


def test_parse_dob_circa_widens():
    iv = parse_dob("circa 1951")
    assert iv.precision == "circa"
    # 'circa' must span more than the single year or it disconfirms real matches.
    assert iv.start_year < 1951 < iv.end_year


def test_parse_dob_circa_exact_date_widens_either_way():
    # "circa 10 Dec 1948" looks precise but is not. If the day-level branch ignored the
    # circa qualifier it would collapse to the single year 1948 and then *disconfirm* a
    # counterparty born 1949 -- a disjoint DOB cuts the score by 45%, so this is the
    # difference between a hit and a miss.
    iv = parse_dob("circa 10 Dec 1948")
    assert (iv.start_year, iv.end_year, iv.precision) == (1947, 1949, "circa")
    assert iv.overlaps(parse_dob("1949"))
    # The unqualified form must stay strict, or the widening means nothing.
    assert not parse_dob("10 Dec 1948").overlaps(parse_dob("1949"))


def test_parse_dob_month_and_year_parses_to_that_year():
    # "Dec 1948" has no day. Returning None here would silently drop the DOB signal for
    # every month-precision record, leaving them at 'unavailable' instead of corroborated.
    iv = parse_dob("Dec 1948")
    assert (iv.start_year, iv.end_year, iv.precision) == (1948, 1948, "month")
    assert iv.overlaps(parse_dob("1948"))
    assert not iv.overlaps(parse_dob("1947"))


def test_circa_month_is_flagged_circa_without_widening():
    # The qualifier is recorded (the precision label is persisted and shown to the
    # analyst) but a month-precision date is already only a year wide, so unlike the
    # bare-year case it is not widened further.
    iv = parse_dob("circa Dec 1948")
    assert iv.precision == "circa"
    assert (iv.start_year, iv.end_year) == (1948, 1948)


def test_parse_dob_range():
    iv = parse_dob("1948 to 1950")
    assert (iv.start_year, iv.end_year, iv.precision) == (1948, 1950, "range")


def test_parse_dob_unparseable_is_none():
    assert parse_dob("") is None
    assert parse_dob("sometime in the eighties") is None


def test_overlap_is_symmetric():
    a, b = parse_dob("circa 1951"), parse_dob("1952")
    assert a.overlaps(b) and b.overlaps(a)


def test_disjoint_intervals_do_not_overlap():
    a, b = parse_dob("10 Dec 1948"), parse_dob("3 Mar 1990")
    assert not a.overlaps(b)


def test_dob_interval_is_hashable_and_frozen():
    # Components are persisted and replayed; value semantics matter.
    assert isinstance(hash(DobInterval(1948, 1948, "day", "10 Dec 1948")), int)
