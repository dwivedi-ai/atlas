"""Chart of accounts: codes, types, hierarchy."""

from __future__ import annotations

import pytest

from ledgerline.accounts import (
    ASSET,
    EXPENSE,
    INCOME,
    Account,
    ChartOfAccounts,
    is_under,
    parents_of,
    type_of,
)
from ledgerline.errors import DuplicateAccount, LedgerError, UnknownAccount


def test_type_comes_from_the_first_segment():
    assert type_of("assets.cash.checking") == ASSET
    assert type_of("expenses.rent") == EXPENSE
    assert type_of("revenue.other") == INCOME


def test_unknown_root_is_rejected():
    with pytest.raises(LedgerError):
        type_of("sundry.things")


def test_parents_and_containment():
    assert parents_of("a.b.c") == ["a", "a.b"]
    assert is_under("assets.cash.checking", "assets.cash")
    assert is_under("assets.cash", "assets.cash")
    assert not is_under("assets.cashflow", "assets.cash")


def test_account_defaults_its_name_to_the_leaf():
    account = Account(code="assets.cash.checking")
    assert account.name == "checking"
    assert account.depth == 2
    assert account.normal_balance == 1


def test_malformed_codes_are_rejected():
    with pytest.raises(LedgerError):
        Account(code="assets..cash")
    with pytest.raises(LedgerError):
        Account(code="assets.ca sh")


def test_chart_loads_and_indexes(chart: ChartOfAccounts):
    assert len(chart) > 20
    assert "assets.cash.checking" in chart
    assert chart.get("assets.cash.checking").currency == "USD"
    with pytest.raises(UnknownAccount):
        chart.get("assets.cash.nope")


def test_duplicate_codes_are_rejected():
    chart = ChartOfAccounts()
    chart.add(Account("assets.cash"))
    with pytest.raises(DuplicateAccount):
        chart.add(Account("assets.cash"))


def test_children_are_direct_only(chart: ChartOfAccounts):
    codes = [a.code for a in chart.children("assets.cash")]
    assert "assets.cash.checking" in codes
    assert all(code.count(".") == 2 for code in codes)


def test_descendants_include_grandchildren(chart: ChartOfAccounts):
    codes = [a.code for a in chart.descendants("assets")]
    assert "assets.cash.checking" in codes
    assert "assets" not in codes


def test_closed_accounts_are_excluded_from_open(chart: ChartOfAccounts):
    assert chart.get("assets.cash.petty").closed
    assert "assets.cash.petty" not in [a.code for a in chart.open_accounts()]


def test_missing_reports_unknown_codes(chart: ChartOfAccounts):
    assert chart.missing(["assets.cash.checking", "assets.moon", "assets.moon"]) == [
        "assets.moon"
    ]


def test_sample_chart_declares_every_parent(chart: ChartOfAccounts):
    assert chart.implied_parents() == []


def test_rows_round_trip(chart: ChartOfAccounts):
    rebuilt = ChartOfAccounts.from_rows(chart.to_rows())
    assert rebuilt.codes() == chart.codes()
    assert rebuilt.get("assets.cash.petty").closed
