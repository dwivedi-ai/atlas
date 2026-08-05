"""Exception hierarchy for ledgerline.

Every failure raised by this package derives from :class:`LedgerError`, so a
caller that wants "anything ledgerline could not do" has exactly one thing to
catch. The CLI maps the leaf classes onto distinct exit codes.
"""

from __future__ import annotations


class LedgerError(Exception):
    """Base class for every error this package raises."""


class ParseError(LedgerError):
    """A ledger document could not be parsed.

    Carries the 1-based line number and the offending source line so the CLI can
    print a caret-style diagnostic without re-reading the file.
    """

    def __init__(self, message: str, line_no: int = 0, line: str = "") -> None:
        self.line_no = int(line_no)
        self.line = line
        where = f" (line {self.line_no})" if self.line_no else ""
        super().__init__(f"{message}{where}")


class ValidationError(LedgerError):
    """A parsed document violates a ledger invariant."""


class UnbalancedTransaction(ValidationError):
    """The postings of one transaction do not sum to zero in some currency."""


class UnknownAccount(ValidationError):
    """A posting names an account that is not in the chart of accounts."""


class DuplicateAccount(ValidationError):
    """The chart of accounts declares the same code twice."""


class CurrencyMismatch(LedgerError):
    """Two amounts in different currencies were combined without conversion."""


class MissingRate(LedgerError):
    """The rate table has no entry for the requested conversion."""


class PeriodError(LedgerError):
    """A period specification could not be understood."""


class ReportError(LedgerError):
    """A report could not be produced."""


class UnknownReport(ReportError):
    """No report is registered under the requested name."""
