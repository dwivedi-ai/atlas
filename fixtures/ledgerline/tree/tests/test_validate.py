"""Validation rules."""

from __future__ import annotations

from datetime import date

import pytest

from ledgerline.accounts import Account, ChartOfAccounts
from ledgerline.errors import LedgerError
from ledgerline.ledger import Ledger
from ledgerline.model import Posting, Transaction
from ledgerline.money import Money
from ledgerline.validate import (
    errors,
    rule_names,
    summarise,
    validate,
    validate_chart,
    warnings,
)


def _pair(account: str, other: str, amount: int, currency: str = "USD", ref: str = "") -> Transaction:
    return Transaction(
        date=date(2024, 5, 1),
        description="entry",
        postings=(
            Posting(account, Money(amount, currency)),
            Posting(other, Money(-amount, currency)),
        ),
        ref=ref,
    )


def test_sample_ledger_is_clean(ledger: Ledger):
    issues = validate(ledger)
    assert errors(issues) == []
    assert warnings(issues) == []


def test_rule_names_are_registered():
    assert "balanced" in rule_names()
    assert "known-accounts" in rule_names()


def test_unknown_rule_raises(ledger: Ledger):
    with pytest.raises(LedgerError):
        validate(ledger, ["no-such-rule"])


def test_unknown_account_is_an_error(chart: ChartOfAccounts):
    ledger = Ledger(chart=chart).add(_pair("expenses.rent", "assets.moon", 100))
    codes = [i.code for i in validate(ledger, ["known-accounts"])]
    assert codes == ["unknown-account"]


def test_no_chart_means_no_account_checks():
    ledger = Ledger().add(_pair("expenses.rent", "assets.moon", 100))
    assert validate(ledger, ["known-accounts"]) == []


def test_closed_account_is_an_error(chart: ChartOfAccounts):
    ledger = Ledger(chart=chart).add(_pair("assets.cash.petty", "expenses.rent", 100))
    issues = validate(ledger, ["closed-accounts"])
    assert [i.code for i in issues] == ["closed-account"]
    assert issues[0].is_error


def test_duplicate_reference_is_a_warning(chart: ChartOfAccounts):
    ledger = Ledger(chart=chart)
    ledger.add(_pair("expenses.rent", "assets.cash.checking", 100, ref="R-1"))
    ledger.add(_pair("expenses.travel", "assets.cash.checking", 200, ref="R-1"))
    issues = validate(ledger, ["duplicate-refs"])
    assert len(issues) == 1
    assert not issues[0].is_error


def test_mixed_currency_account_is_a_warning(chart: ChartOfAccounts):
    ledger = Ledger(chart=chart)
    ledger.add(_pair("assets.cash.checking", "equity.opening.usd", 100, "USD"))
    ledger.add(_pair("assets.cash.checking", "equity.opening.eur", 100, "EUR"))
    issues = validate(ledger, ["single-currency-per-account"])
    assert any(i.code == "mixed-currency-account" for i in issues)


def test_summarise_counts_by_severity(chart: ChartOfAccounts):
    ledger = Ledger(chart=chart)
    ledger.add(_pair("assets.cash.petty", "expenses.rent", 100))
    ledger.add(_pair("expenses.rent", "assets.moon", 100))
    summary = summarise(validate(ledger))
    assert summary["errors"] >= 2
    assert summary["total"] == summary["errors"] + summary["warnings"]


def test_issue_renders_with_a_line_number(chart: ChartOfAccounts):
    ledger = Ledger(chart=chart).add(_pair("assets.cash.petty", "expenses.rent", 100))
    text = str(validate(ledger, ["closed-accounts"])[0])
    assert text.startswith("error: closed-account")


def test_validate_chart_flags_implied_parents():
    chart = ChartOfAccounts()
    chart.add(Account("assets.cash.checking"))
    codes = [i.code for i in validate_chart(chart)]
    assert codes == ["implied-parent", "implied-parent"]
