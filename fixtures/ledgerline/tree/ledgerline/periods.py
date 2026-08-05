"""Fiscal periods: years, quarters, months, and explicit date ranges.

A :class:`Period` is a half-open interval on dates, ``start <= d <= end``, with a
canonical string form that round-trips:

===============  ==========================================
``2024``         the calendar year
``2024-Q4``      October, November, December 2024
``2024-10``      one month
``2024-10-01:2024-10-15``  an explicit inclusive range
===============  ==========================================

The fiscal year is assumed to start in January. ``FISCAL_YEAR_START_MONTH``
exists so a caller can shift it, and every quarter helper reads it rather than
hard-coding 1.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator

from .errors import PeriodError

FISCAL_YEAR_START_MONTH = 1

YEAR_RE = re.compile(r"^(\d{4})$")
QUARTER_RE = re.compile(r"^(\d{4})-[Qq]([1-4])$")
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})$")

KIND_YEAR = "year"
KIND_QUARTER = "quarter"
KIND_MONTH = "month"
KIND_RANGE = "range"


def end_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def quarter_of(when: date) -> int:
    """1-4 for the quarter containing ``when``, honouring the fiscal offset."""
    shifted = (when.month - FISCAL_YEAR_START_MONTH) % 12
    return shifted // 3 + 1


def fiscal_year_of(when: date) -> int:
    """The fiscal year label containing ``when``."""
    if when.month >= FISCAL_YEAR_START_MONTH:
        return when.year
    return when.year - 1


@dataclass(frozen=True, order=True)
class Period:
    """An inclusive date range with a canonical label."""

    start: date
    end: date
    kind: str = KIND_RANGE
    label: str = ""

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise PeriodError(f"period ends before it starts: {self.start}..{self.end}")
        if not self.label:
            object.__setattr__(self, "label", f"{self.start.isoformat()}:{self.end.isoformat()}")

    # -- construction --------------------------------------------------------
    @classmethod
    def year(cls, year: int) -> "Period":
        start = date(year, FISCAL_YEAR_START_MONTH, 1)
        end = start.replace(year=year + 1) - timedelta(days=1)
        return cls(start, end, KIND_YEAR, str(year))

    @classmethod
    def quarter(cls, year: int, index: int) -> "Period":
        if not 1 <= index <= 4:
            raise PeriodError(f"quarter must be 1..4, got {index}")
        offset = (index - 1) * 3
        month = (FISCAL_YEAR_START_MONTH - 1 + offset) % 12 + 1
        start_year = year + (FISCAL_YEAR_START_MONTH - 1 + offset) // 12
        start = date(start_year, month, 1)
        last_month_offset = offset + 2
        last_month = (FISCAL_YEAR_START_MONTH - 1 + last_month_offset) % 12 + 1
        last_year = year + (FISCAL_YEAR_START_MONTH - 1 + last_month_offset) // 12
        return cls(start, end_of_month(last_year, last_month), KIND_QUARTER, f"{year}-Q{index}")

    @classmethod
    def month(cls, year: int, month: int) -> "Period":
        return cls(
            date(year, month, 1), end_of_month(year, month), KIND_MONTH, f"{year}-{month:02d}"
        )

    @classmethod
    def parse(cls, text: str) -> "Period":
        """Parse any of the four canonical forms."""
        raw = str(text).strip()
        if not raw:
            raise PeriodError("empty period")
        match = YEAR_RE.match(raw)
        if match:
            return cls.year(int(match.group(1)))
        match = QUARTER_RE.match(raw)
        if match:
            return cls.quarter(int(match.group(1)), int(match.group(2)))
        match = MONTH_RE.match(raw)
        if match:
            return cls.month(int(match.group(1)), int(match.group(2)))
        match = RANGE_RE.match(raw)
        if match:
            start = date.fromisoformat(match.group(1))
            end = date.fromisoformat(match.group(2))
            return cls(start, end, KIND_RANGE)
        raise PeriodError(f"cannot read a period from {text!r}")

    # -- queries -------------------------------------------------------------
    def contains(self, when: date) -> bool:
        return self.start <= when <= self.end

    def __contains__(self, when: object) -> bool:
        return isinstance(when, date) and self.contains(when)

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def months(self) -> list["Period"]:
        """Every calendar month overlapping this period, in order."""
        out: list[Period] = []
        year, month = self.start.year, self.start.month
        while date(year, month, 1) <= self.end:
            out.append(Period.month(year, month))
            month += 1
            if month == 13:
                year, month = year + 1, 1
        return out

    def next(self) -> "Period":
        """The following period of the same kind."""
        if self.kind == KIND_YEAR:
            return Period.year(int(self.label) + 1)
        if self.kind == KIND_QUARTER:
            year, index = int(self.label[:4]), int(self.label[-1])
            return Period.quarter(year + 1, 1) if index == 4 else Period.quarter(year, index + 1)
        if self.kind == KIND_MONTH:
            year, month = int(self.label[:4]), int(self.label[5:7])
            return Period.month(year + 1, 1) if month == 12 else Period.month(year, month + 1)
        span = self.days
        return Period(self.end + timedelta(days=1), self.end + timedelta(days=span))

    def previous(self) -> "Period":
        if self.kind == KIND_YEAR:
            return Period.year(int(self.label) - 1)
        if self.kind == KIND_QUARTER:
            year, index = int(self.label[:4]), int(self.label[-1])
            return Period.quarter(year - 1, 4) if index == 1 else Period.quarter(year, index - 1)
        if self.kind == KIND_MONTH:
            year, month = int(self.label[:4]), int(self.label[5:7])
            return Period.month(year - 1, 12) if month == 1 else Period.month(year, month - 1)
        span = self.days
        return Period(self.start - timedelta(days=span), self.start - timedelta(days=1))

    def __str__(self) -> str:
        return self.label


def iter_periods(start: Period, count: int) -> Iterator[Period]:
    """Yield ``count`` consecutive periods beginning at ``start``."""
    current = start
    for _ in range(max(0, count)):
        yield current
        current = current.next()


def enclosing_year(period: Period) -> Period:
    """The fiscal year that contains ``period.start``."""
    return Period.year(fiscal_year_of(period.start))
