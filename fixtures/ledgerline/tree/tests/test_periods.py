"""Fiscal period arithmetic."""

from __future__ import annotations

from datetime import date

import pytest

from ledgerline.errors import PeriodError
from ledgerline.periods import Period, iter_periods, quarter_of


def test_parse_year():
    period = Period.parse("2024")
    assert (period.start, period.end) == (date(2024, 1, 1), date(2024, 12, 31))
    assert period.kind == "year"


def test_parse_quarter():
    period = Period.parse("2024-Q4")
    assert (period.start, period.end) == (date(2024, 10, 1), date(2024, 12, 31))
    assert period.label == "2024-Q4"


def test_parse_month_handles_february_in_a_leap_year():
    period = Period.parse("2024-02")
    assert period.end == date(2024, 2, 29)
    assert period.days == 29


def test_parse_explicit_range():
    period = Period.parse("2024-03-05:2024-03-09")
    assert period.days == 5
    assert period.kind == "range"


def test_parse_rejects_nonsense():
    for text in ("", "Q4", "2024-13", "2024-Q5", "not-a-period"):
        with pytest.raises(PeriodError):
            Period.parse(text)


def test_containment():
    period = Period.parse("2024-Q4")
    assert date(2024, 12, 31) in period
    assert date(2025, 1, 1) not in period


def test_next_and_previous_wrap_the_year():
    assert Period.parse("2024-Q4").next().label == "2025-Q1"
    assert Period.parse("2024-Q1").previous().label == "2023-Q4"
    assert Period.parse("2024-12").next().label == "2025-01"
    assert Period.parse("2024-01").previous().label == "2023-12"


def test_next_of_an_explicit_range_keeps_its_length():
    period = Period.parse("2024-03-05:2024-03-09")
    following = period.next()
    assert following.start == date(2024, 3, 10)
    assert following.days == period.days


def test_months_of_a_quarter():
    assert [p.label for p in Period.parse("2024-Q4").months()] == [
        "2024-10",
        "2024-11",
        "2024-12",
    ]


def test_months_of_a_year_is_twelve():
    assert len(Period.parse("2024").months()) == 12


def test_quarter_of():
    assert quarter_of(date(2024, 1, 31)) == 1
    assert quarter_of(date(2024, 7, 1)) == 3
    assert quarter_of(date(2024, 12, 31)) == 4


def test_iter_periods():
    labels = [p.label for p in iter_periods(Period.parse("2024-Q3"), 3)]
    assert labels == ["2024-Q3", "2024-Q4", "2025-Q1"]


def test_backwards_range_is_rejected():
    with pytest.raises(PeriodError):
        Period(date(2024, 5, 1), date(2024, 4, 1))
