"""Ledger container, indexes and balances."""

from __future__ import annotations

from datetime import date

import pytest

from ledgerline.errors import LedgerError, UnbalancedTransaction
from ledgerline.ledger import Ledger, merge
from ledgerline.model import Posting, Transaction
from ledgerline.money import Money
from ledgerline.periods import Period


def _txn(day: int, amount: int = 100, account: str = "expenses.rent") -> Transaction:
    return Transaction(
        date=date(2024, 10, day),
        description=f"entry {day}",
        postings=(
            Posting(account, Money(amount, "USD")),
            Posting("assets.cash.checking", Money(-amount, "USD")),
        ),
    )


def test_add_keeps_date_order():
    ledger = Ledger()
    ledger.add(_txn(9)).add(_txn(2)).add(_txn(5))
    assert [t.date.day for t in ledger] == [2, 5, 9]


def test_unbalanced_transactions_are_refused():
    bad = Transaction(
        date=date(2024, 1, 1),
        description="bad",
        postings=(Posting("expenses.rent", Money(1, "USD")),),
    )
    with pytest.raises(UnbalancedTransaction):
        Ledger().add(bad)


def test_index_by_account_is_rebuilt_after_a_mutation():
    ledger = Ledger().add(_txn(1))
    assert "expenses.rent" in ledger.index_by_account()
    ledger.add(_txn(2, account="expenses.travel"))
    assert "expenses.travel" in ledger.index_by_account()


def test_index_by_month(ledger: Ledger):
    months = ledger.index_by_month()
    assert sorted(months) == ["2024-10", "2024-11", "2024-12"]
    assert sum(len(v) for v in months.values()) == len(ledger)


def test_balance_rolls_up_children(ledger: Ledger):
    checking = ledger.balance("assets.cash.checking")
    cash_tree = ledger.balances_by_currency("assets.cash")
    assert cash_tree["USD"].minor >= checking.minor


def test_balance_of_a_mixed_subtree_needs_a_currency(ledger: Ledger):
    with pytest.raises(LedgerError):
        ledger.balance("assets")
    assert ledger.balance("assets", currency="EUR").currency == "EUR"


def test_period_filter(ledger: Ledger):
    october = ledger.in_period(Period.parse("2024-10"))
    assert len(october) == 8
    assert all(t.date.month == 10 for t in october)


def test_tag_filters(ledger: Ledger):
    assert len(ledger.with_tag("payroll")) == 3
    assert len(ledger.without_tag("payroll")) == len(ledger) - 3


def test_running_balance_is_cumulative(ledger: Ledger):
    series = ledger.running_balance("assets.cash.checking")
    assert series[-1][1] == ledger.balance("assets.cash.checking")
    assert [when for when, _ in series] == sorted(when for when, _ in series)


def test_currencies_and_date_range(ledger: Ledger):
    assert ledger.currencies() == ["EUR", "GBP", "USD"]
    assert ledger.date_range() == (date(2024, 10, 1), date(2024, 12, 31))


def test_balances_leaves_only(ledger: Ledger):
    balances = ledger.balances()
    assert "assets.cash.checking" in balances
    assert "assets" not in balances


def test_merge_preserves_order_and_chart(ledger: Ledger):
    other = Ledger().add(_txn(4))
    merged = merge([ledger, other], name="merged")
    assert len(merged) == len(ledger) + 1
    assert merged.chart is ledger.chart
    assert [t.date for t in merged] == sorted(t.date for t in merged)


def test_empty_ledger_is_harmless():
    empty = Ledger()
    assert empty.is_empty
    assert empty.date_range() is None
    assert empty.balance("assets.cash.checking") == Money.zero("USD")


def test_to_dict_shape(ledger: Ledger):
    payload = ledger.to_dict()
    assert payload["count"] == len(ledger)
    assert payload["start"] == "2024-10-01"
    assert len(payload["transactions"]) == len(ledger)
