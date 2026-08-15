"""Postings and transactions."""

from __future__ import annotations

from datetime import date

import pytest

from ledgerline.errors import LedgerError, UnbalancedTransaction
from ledgerline.model import Posting, Transaction, TransactionBuilder, elide_balance
from ledgerline.money import Money


def _posting(account: str, minor: int, currency: str = "USD") -> Posting:
    return Posting(account, Money(minor, currency))


def test_posting_normalises_tags():
    posting = Posting("expenses.rent", Money(1, "USD"), tags=frozenset({"#recurring"}))
    assert posting.tags == frozenset({"recurring"})


def test_posting_requires_an_account():
    with pytest.raises(LedgerError):
        Posting("  ", Money(1, "USD"))


def test_debit_and_credit_helpers():
    assert _posting("expenses.rent", 10).is_debit
    assert _posting("assets.cash.checking", -10).is_credit


def test_transaction_needs_postings():
    with pytest.raises(LedgerError):
        Transaction(date=date(2024, 1, 1), description="empty")


def test_balance_is_per_currency():
    transaction = Transaction(
        date=date(2024, 1, 1),
        description="split",
        postings=(
            _posting("assets.cash.checking", 100),
            _posting("equity.opening.usd", -100),
            _posting("assets.cash.eur", 50, "EUR"),
            _posting("equity.opening.eur", -50, "EUR"),
        ),
    )
    assert transaction.is_balanced
    assert transaction.currencies() == ["USD", "EUR"]


def test_require_balanced_raises_with_the_residual():
    transaction = Transaction(
        date=date(2024, 1, 1),
        description="off by one",
        postings=(_posting("expenses.rent", 100), _posting("assets.cash.checking", -99)),
    )
    with pytest.raises(UnbalancedTransaction) as excinfo:
        transaction.require_balanced()
    assert "0.01" in str(excinfo.value)


def test_amount_for_rolls_up_children():
    transaction = Transaction(
        date=date(2024, 1, 1),
        description="two cash legs",
        postings=(
            _posting("assets.cash.checking", 100),
            _posting("assets.cash.savings", 50),
            _posting("equity.opening.usd", -150),
        ),
    )
    assert transaction.amount_for("assets.cash", include_children=True) == Money(150, "USD")
    assert transaction.amount_for("assets.cash") is None


def test_amount_for_refuses_a_mixed_currency_rollup():
    transaction = Transaction(
        date=date(2024, 1, 1),
        description="mixed",
        postings=(
            _posting("assets.cash.checking", 100),
            _posting("equity.opening.usd", -100),
            _posting("assets.cash.eur", 50, "EUR"),
            _posting("equity.opening.eur", -50, "EUR"),
        ),
    )
    with pytest.raises(LedgerError):
        transaction.amount_for("assets.cash", include_children=True)


def test_tags_are_collected_from_both_levels():
    transaction = Transaction(
        date=date(2024, 1, 1),
        description="tagged",
        postings=(
            Posting("expenses.rent", Money(1, "USD"), tags=frozenset({"leg"})),
            _posting("assets.cash.checking", -1),
        ),
        tags=frozenset({"head"}),
    )
    assert transaction.all_tags() == {"head", "leg"}
    assert transaction.has_tag("#leg")
    assert not transaction.has_tag("other")


def test_elide_balance_fills_the_gap():
    postings = [
        _posting("expenses.rent", 100),
        Posting("assets.cash.checking", Money(0, "USD")),
    ]
    filled = elide_balance(postings, 1, "USD")
    assert filled.amount == Money(-100, "USD")


def test_elide_balance_needs_a_priced_leg():
    postings = [Posting("assets.cash.checking", Money(0, "EUR"))]
    with pytest.raises(LedgerError):
        elide_balance(postings, 0, "USD")


def test_builder_produces_a_transaction():
    builder = TransactionBuilder(date=date(2024, 1, 1), description="built", ref="R")
    builder.add(_posting("expenses.rent", 5)).add(_posting("assets.cash.checking", -5))
    transaction = builder.build()
    assert transaction.ref == "R"
    assert transaction.is_balanced


def test_to_dict_is_json_ready():
    transaction = Transaction(
        date=date(2024, 1, 1),
        description="d",
        postings=(_posting("expenses.rent", 5), _posting("assets.cash.checking", -5)),
    )
    payload = transaction.to_dict()
    assert payload["date"] == "2024-01-01"
    assert payload["postings"][0]["minor"] == 5
