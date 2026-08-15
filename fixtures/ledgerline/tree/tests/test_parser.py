"""The .ledger text format."""

from __future__ import annotations

from datetime import date

import pytest

from ledgerline.errors import ParseError
from ledgerline.money import Money
from ledgerline.parser import dump_string, parse_file, parse_string

SIMPLE = """
2024-01-02 Coffee  #petty
    expenses.supplies      4.50 USD
    assets.cash.checking  -4.50 USD
"""


def test_parses_a_simple_transaction():
    (transaction,) = parse_string(SIMPLE)
    assert transaction.date == date(2024, 1, 2)
    assert transaction.description == "Coffee"
    assert transaction.tags == frozenset({"petty"})
    assert len(transaction.postings) == 2


def test_elided_amount_is_inferred():
    (transaction,) = parse_string(
        "2024-01-02 Coffee\n    expenses.supplies  4.50 USD\n    assets.cash.checking\n"
    )
    assert transaction.postings[1].amount == Money(-450, "USD")
    assert transaction.is_balanced


def test_only_one_amount_may_be_elided():
    with pytest.raises(ParseError):
        parse_string(
            "2024-01-02 Coffee\n"
            "    expenses.supplies  4.50 USD\n"
            "    assets.cash.checking\n"
            "    assets.cash.savings\n"
        )


def test_reference_and_tags_are_split_off():
    (transaction,) = parse_string(
        "2024-01-02 Invoice  #invoice  ^INV-9\n"
        "    assets.receivable.acme  10.00 USD\n"
        "    income.consulting      -10.00 USD\n"
    )
    assert transaction.ref == "INV-9"
    assert transaction.description == "Invoice"
    assert "invoice" in transaction.tags


def test_comments_and_blank_lines_are_ignored():
    text = "; a comment\n\n" + SIMPLE + "\n; trailing\n"
    assert len(parse_string(text)) == 1


def test_unbalanced_transaction_is_rejected():
    with pytest.raises(ParseError) as excinfo:
        parse_string(
            "2024-01-02 Broken\n"
            "    expenses.supplies      4.50 USD\n"
            "    assets.cash.checking  -4.00 USD\n"
        )
    assert "balance" in str(excinfo.value)


def test_posting_before_header_is_rejected():
    with pytest.raises(ParseError):
        parse_string("    expenses.supplies  4.50 USD\n")


def test_bad_date_is_reported_with_a_line_number():
    with pytest.raises(ParseError) as excinfo:
        parse_string("2024-13-40 Nope\n    a.b 1.00 USD\n    c.d -1.00 USD\n")
    assert excinfo.value.line_no == 1


def test_header_needs_a_description():
    with pytest.raises(ParseError):
        parse_string("2024-01-02\n    expenses.supplies 1.00 USD\n")


def test_multi_currency_transaction_balances_per_currency():
    (transaction,) = parse_string(
        "2024-01-02 Split\n"
        "    assets.cash.checking   10.00 USD\n"
        "    equity.opening.usd    -10.00 USD\n"
        "    assets.cash.eur         5.00 EUR\n"
        "    equity.opening.eur     -5.00 EUR\n"
    )
    assert transaction.currencies() == ["USD", "EUR"]
    assert transaction.is_balanced


def test_sample_file_parses(data_dir):
    transactions = parse_file(data_dir / "sample.ledger")
    assert len(transactions) == 22
    assert all(t.is_balanced for t in transactions)


def test_dump_round_trips(data_dir):
    original = parse_file(data_dir / "sample.ledger")
    again = parse_string(dump_string(original))
    assert [t.to_dict() for t in again] == [t.to_dict() for t in original]
