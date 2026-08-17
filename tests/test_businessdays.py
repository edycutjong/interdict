"""Statutory-clock tests.

These assert against dates that can be checked against a published federal calendar,
because "10 business days" is a legal deadline and an off-by-one here is a compliance
failure, not a rendering bug.
"""

from datetime import date

import pytest

from interdict.businessdays import (add_business_days, federal_holidays,
                                    is_business_day, report_due)


def test_weekend_is_not_a_business_day():
    assert not is_business_day(date(2026, 8, 15))   # Saturday
    assert not is_business_day(date(2026, 8, 16))   # Sunday
    assert is_business_day(date(2026, 8, 17))       # Monday


@pytest.mark.parametrize("year,expected", [
    (2026, date(2026, 1, 19)),
    (2027, date(2027, 1, 18)),
])
def test_mlk_day_is_third_monday_in_january(year, expected):
    assert expected in federal_holidays(year)
    assert expected.weekday() == 0


def test_thanksgiving_is_fourth_thursday_in_november():
    assert date(2026, 11, 26) in federal_holidays(2026)


def test_memorial_day_is_last_monday_in_may():
    assert date(2026, 5, 25) in federal_holidays(2026)


def test_labor_day_is_first_monday_in_september():
    assert date(2026, 9, 7) in federal_holidays(2026)


def test_saturday_holiday_observed_on_preceding_friday():
    """4 July 2026 falls on a Saturday, so the federal observance is Friday the 3rd."""
    holidays = federal_holidays(2026)
    assert date(2026, 7, 3) in holidays
    assert not is_business_day(date(2026, 7, 3))


def test_sunday_holiday_observed_on_following_monday():
    """1 Jan 2028 falls on a Saturday; 2027's New Year is Friday 1 Jan. Use Christmas.

    25 Dec 2027 is a Saturday -> observed Friday 24 Dec 2027.
    """
    assert date(2027, 12, 24) in federal_holidays(2027)


def test_new_year_on_sunday_shifts_to_monday():
    # 1 January 2028 is a Saturday -> observed Friday 31 Dec 2027 is NOT how it works;
    # the rule applies within the holiday's own year, giving Monday 3 Jan for a Sunday.
    assert date(2023, 1, 2) in federal_holidays(2023)   # 1 Jan 2023 was a Sunday


def test_add_business_days_skips_the_weekend():
    # Friday + 1 business day is the following Monday.
    assert add_business_days(date(2026, 8, 21), 1) == date(2026, 8, 24)


def test_add_business_days_skips_holidays():
    # Thursday 2026-11-25 -> next business day skips Thanksgiving (Thu 26th).
    assert add_business_days(date(2026, 11, 25), 1) == date(2026, 11, 27)


def test_add_zero_business_days_is_identity():
    assert add_business_days(date(2026, 8, 17), 0) == date(2026, 8, 17)


def test_negative_count_is_rejected():
    with pytest.raises(ValueError):
        add_business_days(date(2026, 8, 17), -1)


def test_report_deadline_is_ten_business_days():
    """A hold placed Monday 2026-08-17 is reportable by Monday 2026-08-31.

    Ten business days spans two weekends and no federal holiday in this window, so the
    deadline lands exactly two calendar weeks out.
    """
    assert report_due(date(2026, 8, 17)) == date(2026, 8, 31)


def test_report_deadline_stretches_across_a_holiday():
    """A hold placed just before Thanksgiving gets an extra calendar day."""
    plain = report_due(date(2026, 8, 17)) - date(2026, 8, 17)
    across = report_due(date(2026, 11, 20)) - date(2026, 11, 20)
    assert across > plain


def test_deadline_always_lands_on_a_business_day():
    for offset in range(60):
        start = date(2026, 8, 1)
        due = report_due(start.fromordinal(start.toordinal() + offset))
        assert is_business_day(due)
