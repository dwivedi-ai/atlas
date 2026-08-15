"""Money: parsing, arithmetic, and the single rounding point."""

from __future__ import annotations

from fractions import Fraction

import pytest

from ledgerline.errors import CurrencyMismatch, LedgerError
from ledgerline.money import Money, exponent_of, split_by_currency, total


def test_parse_plain_decimal():
    assert Money.parse("12.34", "USD") == Money(1234, "USD")


def test_parse_applies_the_currency_exponent():
    assert Money.parse("1200", "JPY") == Money(1200, "JPY")
    assert exponent_of("JPY") == 0


def test_parse_accepts_accounting_negatives_and_separators():
    assert Money.parse("(1,234.50)", "USD") == Money(-123450, "USD")
    assert Money.parse("-1_000.00", "USD") == Money(-100000, "USD")


def test_parse_strips_symbols():
    assert Money.parse("$19.99", "USD") == Money(1999, "USD")


def test_parse_rejects_nonsense():
    with pytest.raises(LedgerError):
        Money.parse("twelve", "USD")
    with pytest.raises(LedgerError):
        Money.parse("", "USD")


def test_addition_requires_one_currency():
    with pytest.raises(CurrencyMismatch):
        Money(100, "USD") + Money(100, "EUR")


def test_arithmetic_is_exact():
    acc = Money.zero("USD")
    for _ in range(1000):
        acc = acc + Money.parse("0.01", "USD")
    assert acc == Money(1000, "USD")


def test_scale_rounds_half_to_even():
    assert Money(5, "USD").scale(Fraction(1, 2)) == Money(2, "USD")
    assert Money(7, "USD").scale(Fraction(1, 2)) == Money(4, "USD")


def test_scale_re_denominates_across_exponents():
    # 100.00 USD at 125 JPY per USD is 12,500 JPY, which has no minor units.
    assert Money(10000, "USD").scale(Fraction(125), currency="JPY") == Money(12500, "JPY")


def test_format_round_trips_through_parse():
    original = Money(-123456, "EUR")
    assert Money.parse(original.format(with_currency=False), "EUR") == original


def test_format_grouping():
    assert Money(123456789, "USD").format(grouping=True) == "1,234,567.89 USD"


def test_total_and_split():
    amounts = [Money(100, "USD"), Money(250, "USD"), Money(700, "EUR")]
    assert total(amounts[:2]) == Money(350, "USD")
    assert split_by_currency(amounts) == {"USD": Money(350, "USD"), "EUR": Money(700, "EUR")}


def test_total_of_empty_needs_a_currency():
    with pytest.raises(LedgerError):
        total([])
    assert total([], "USD") == Money.zero("USD")


def test_currency_code_is_validated():
    with pytest.raises(LedgerError):
        Money(1, "US1")
