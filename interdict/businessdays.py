"""US federal business days -- the statutory clock.

OFAC blocking reports are due within **10 business days** of the blocking. That is a
legal deadline, not a UI nicety, so the calculation is implemented here explicitly and
tested rather than approximated as "14 calendar days" or pulled from a dependency whose
holiday table nobody in this repo can verify.

Federal holidays are computed from their statutory rules (5 U.S.C. 6103), including the
in-lieu-of weekend observation rule: a holiday falling on a Saturday is observed the
preceding Friday, one falling on a Sunday the following Monday. Both shift real
deadlines, so both are implemented.
"""

from __future__ import annotations

from datetime import date, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth given weekday of a month (weekday: Mon=0). n=-1 means the last one."""
    if n > 0:
        d = date(year, month, 1)
        offset = (weekday - d.weekday()) % 7
        return d + timedelta(days=offset + 7 * (n - 1))
    # Last occurrence: walk back from the end of the month.
    d = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def federal_holidays(year: int) -> set[date]:
    """Observed US federal holidays for a year (5 U.S.C. 6103)."""
    fixed = [
        date(year, 1, 1),      # New Year's Day
        date(year, 6, 19),     # Juneteenth
        date(year, 7, 4),      # Independence Day
        date(year, 11, 11),    # Veterans Day
        date(year, 12, 25),    # Christmas Day
    ]

    observed: set[date] = set()
    for holiday in fixed:
        # In-lieu-of rule: Saturday -> preceding Friday, Sunday -> following Monday.
        if holiday.weekday() == 5:
            observed.add(holiday - timedelta(days=1))
        elif holiday.weekday() == 6:
            observed.add(holiday + timedelta(days=1))
        else:
            observed.add(holiday)

    observed.update({
        _nth_weekday(year, 1, 0, 3),    # MLK Day -- 3rd Monday in January
        _nth_weekday(year, 2, 0, 3),    # Washington's Birthday -- 3rd Monday in February
        _nth_weekday(year, 5, 0, -1),   # Memorial Day -- last Monday in May
        _nth_weekday(year, 9, 0, 1),    # Labor Day -- 1st Monday in September
        _nth_weekday(year, 10, 0, 2),   # Columbus Day -- 2nd Monday in October
        _nth_weekday(year, 11, 3, 4),   # Thanksgiving -- 4th Thursday in November
    })
    return observed


def is_business_day(day: date) -> bool:
    return day.weekday() < 5 and day not in federal_holidays(day.year)


def add_business_days(start: date, count: int) -> date:
    """The date `count` business days after `start`.

    Day 0 is the day of the blocking itself; counting begins the next business day,
    which is the conservative reading of "within 10 business days".
    """
    if count < 0:
        raise ValueError("count must be non-negative")
    day = start
    remaining = count
    while remaining > 0:
        day += timedelta(days=1)
        if is_business_day(day):
            remaining -= 1
    return day


REPORT_DEADLINE_BUSINESS_DAYS = 10


def report_due(blocked_on: date) -> date:
    """The OFAC blocking-report deadline for a hold placed on `blocked_on`."""
    return add_business_days(blocked_on, REPORT_DEADLINE_BUSINESS_DAYS)
