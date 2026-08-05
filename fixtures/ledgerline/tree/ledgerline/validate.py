"""Ledger validation: a list of rules, each returning zero or more issues.

Validation never raises for a data problem — it returns
:class:`Issue` records so a caller can show all of them at once. Only a
programming error (an unknown rule name) raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable, Sequence

from .accounts import ChartOfAccounts
from .errors import LedgerError
from .ledger import Ledger
from .model import Transaction

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITIES = (SEVERITY_ERROR, SEVERITY_WARNING)


@dataclass(frozen=True)
class Issue:
    """One validation finding."""

    code: str
    severity: str
    message: str
    line_no: int = 0
    ref: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity == SEVERITY_ERROR

    def __str__(self) -> str:
        where = f" (line {self.line_no})" if self.line_no else ""
        return f"{self.severity}: {self.code}: {self.message}{where}"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "line_no": self.line_no,
            "ref": self.ref,
        }


RuleFn = Callable[[Ledger], list[Issue]]
_RULES: dict[str, RuleFn] = {}


def rule(name: str) -> Callable[[RuleFn], RuleFn]:
    """Register a validation rule under ``name``."""

    def decorate(fn: RuleFn) -> RuleFn:
        _RULES[name] = fn
        return fn

    return decorate


def rule_names() -> list[str]:
    return sorted(_RULES)


# ── the rules ────────────────────────────────────────────────────────────────
@rule("balanced")
def check_balanced(ledger: Ledger) -> list[Issue]:
    """Every transaction sums to zero in each currency it mentions."""
    issues: list[Issue] = []
    for transaction in ledger:
        if transaction.is_balanced:
            continue
        residual = ", ".join(m.format() for m in transaction.sums().values() if m.minor)
        issues.append(
            Issue(
                code="unbalanced",
                severity=SEVERITY_ERROR,
                message=f"{transaction.description!r} leaves {residual}",
                line_no=transaction.line_no,
                ref=transaction.ref,
            )
        )
    return issues


@rule("known-accounts")
def check_known_accounts(ledger: Ledger) -> list[Issue]:
    """Every posted account exists in the chart, when a chart is attached."""
    if ledger.chart is None:
        return []
    missing = ledger.chart.missing(ledger.accounts_used())
    return [
        Issue(
            code="unknown-account",
            severity=SEVERITY_ERROR,
            message=f"{code} is not in the chart of accounts",
        )
        for code in missing
    ]


@rule("closed-accounts")
def check_closed_accounts(ledger: Ledger) -> list[Issue]:
    """No posting lands on an account the chart marks as closed."""
    if ledger.chart is None:
        return []
    issues: list[Issue] = []
    for transaction in ledger:
        for posting in transaction.postings:
            if posting.account not in ledger.chart:
                continue
            if ledger.chart.get(posting.account).closed:
                issues.append(
                    Issue(
                        code="closed-account",
                        severity=SEVERITY_ERROR,
                        message=f"{posting.account} is closed but was posted to",
                        line_no=posting.line_no,
                        ref=transaction.ref,
                    )
                )
    return issues


@rule("duplicate-refs")
def check_duplicate_refs(ledger: Ledger) -> list[Issue]:
    """A non-empty reference identifies at most one transaction."""
    seen: dict[str, Transaction] = {}
    issues: list[Issue] = []
    for transaction in ledger:
        if not transaction.ref:
            continue
        previous = seen.get(transaction.ref)
        if previous is not None:
            issues.append(
                Issue(
                    code="duplicate-ref",
                    severity=SEVERITY_WARNING,
                    message=(
                        f"reference {transaction.ref} is used by both "
                        f"{previous.date.isoformat()} and {transaction.date.isoformat()}"
                    ),
                    line_no=transaction.line_no,
                    ref=transaction.ref,
                )
            )
        else:
            seen[transaction.ref] = transaction
    return issues


@rule("single-currency-per-account")
def check_single_currency(ledger: Ledger) -> list[Issue]:
    """An account holds one currency unless the chart says otherwise."""
    issues: list[Issue] = []
    for account in ledger.accounts_used():
        currencies = sorted(ledger.balances_by_currency(account, include_children=False))
        if len(currencies) > 1:
            issues.append(
                Issue(
                    code="mixed-currency-account",
                    severity=SEVERITY_WARNING,
                    message=f"{account} holds {', '.join(currencies)}",
                )
            )
    return issues


@rule("chronological")
def check_chronological(ledger: Ledger) -> list[Issue]:
    """Transactions are stored in date order (the ledger sorts on insert)."""
    issues: list[Issue] = []
    previous: date | None = None
    for transaction in ledger:
        if previous is not None and transaction.date < previous:
            issues.append(
                Issue(
                    code="out-of-order",
                    severity=SEVERITY_ERROR,
                    message=f"{transaction.date.isoformat()} follows {previous.isoformat()}",
                    line_no=transaction.line_no,
                )
            )
        previous = transaction.date
    return issues


@rule("empty-description")
def check_descriptions(ledger: Ledger) -> list[Issue]:
    """Every transaction carries a description."""
    return [
        Issue(
            code="empty-description",
            severity=SEVERITY_WARNING,
            message=f"transaction on {t.date.isoformat()} has no description",
            line_no=t.line_no,
        )
        for t in ledger
        if not t.description.strip()
    ]


# ── driving the rules ────────────────────────────────────────────────────────
def validate(ledger: Ledger, rules: Sequence[str] | None = None) -> list[Issue]:
    """Run ``rules`` (default: all of them) and return every issue found."""
    names = list(rules) if rules else rule_names()
    unknown = [name for name in names if name not in _RULES]
    if unknown:
        raise LedgerError(f"unknown validation rule(s): {', '.join(unknown)}")
    issues: list[Issue] = []
    for name in names:
        issues.extend(_RULES[name](ledger))
    return issues


def errors(issues: Iterable[Issue]) -> list[Issue]:
    return [i for i in issues if i.is_error]


def warnings(issues: Iterable[Issue]) -> list[Issue]:
    return [i for i in issues if not i.is_error]


def summarise(issues: Sequence[Issue]) -> dict:
    return {
        "total": len(issues),
        "errors": len(errors(issues)),
        "warnings": len(warnings(issues)),
        "codes": sorted({i.code for i in issues}),
    }


def validate_chart(chart: ChartOfAccounts) -> list[Issue]:
    """Chart-level checks that do not need a ledger."""
    return [
        Issue(
            code="implied-parent",
            severity=SEVERITY_WARNING,
            message=f"{code} is referenced by a child but never declared",
        )
        for code in chart.implied_parents()
    ]
