"""Currency conversion: the pair table, the USD bridge, and rounding."""

from __future__ import annotations

from fractions import Fraction

import pytest

from ledgerline.errors import LedgerError, MissingRate
from ledgerline.fx import (
    RateTable,
    as_fraction,
    convert,
    convert_via_bridge,
    missing_pairs,
    sum_in,
)
from ledgerline.money import Money


def test_as_fraction_is_exact():
    assert as_fraction("0.1") == Fraction(1, 10)
    assert as_fraction("5/8") == Fraction(5, 8)
    with pytest.raises(LedgerError):
        as_fraction(0.1)


def test_direct_rate(rates: RateTable):
    assert rates.direct("USD", "EUR") == Fraction(4, 5)
    assert rates.direct("EUR", "EUR") == 1


def test_direct_rate_can_be_missing(rates: RateTable):
    assert not rates.has_direct("EUR", "JPY")
    with pytest.raises(MissingRate):
        rates.direct("EUR", "JPY")


def test_bridge_covers_pairs_the_feed_does_not_quote(rates: RateTable):
    assert rates.bridge("EUR", "JPY") == Fraction(125) / Fraction(4, 5)


def test_bridge_agrees_with_the_direct_table_where_both_exist(rates: RateTable):
    for base, quote in [("USD", "EUR"), ("EUR", "GBP"), ("GBP", "USD"), ("USD", "JPY")]:
        assert rates.direct(base, quote) == rates.bridge(base, quote)


def test_both_conversion_paths_agree_on_the_same_amount(rates: RateTable):
    amount = Money.parse("1234.56", "EUR")
    assert convert(amount, "GBP", rates) == convert_via_bridge(amount, "GBP", rates)


def test_conversion_re_denominates_minor_units(rates: RateTable):
    assert convert_via_bridge(Money.parse("100.00", "USD"), "JPY", rates) == Money(12500, "JPY")


def test_conversion_is_a_no_op_within_one_currency(rates: RateTable):
    amount = Money(999, "USD")
    assert convert(amount, "USD", rates) is amount
    assert convert_via_bridge(amount, "usd", rates) is amount


def test_sum_in_totals_a_mixed_sequence(rates: RateTable):
    amounts = [Money.parse("10.00", "USD"), Money.parse("8.00", "EUR")]
    assert sum_in(amounts, "USD", rates) == Money.parse("20.00", "USD")


def test_usd_leg_can_be_inverted(rates: RateTable):
    table = RateTable().set("EUR", "USD", as_fraction("1.25"))
    assert table.usd_leg("EUR") == Fraction(4, 5)


def test_unpriced_currency_is_reported(rates: RateTable):
    with pytest.raises(MissingRate):
        rates.usd_leg("SEK")


def test_missing_pairs_lists_the_gaps(rates: RateTable):
    gaps = missing_pairs(rates, ["EUR", "JPY"])
    assert ("EUR", "JPY") in gaps and ("JPY", "EUR") in gaps


def test_rates_reject_non_positive_values():
    with pytest.raises(LedgerError):
        RateTable().set("USD", "EUR", Fraction(0))


def test_table_rows_round_trip(rates: RateTable):
    rebuilt = RateTable.from_rows(rates.to_rows())
    assert rebuilt.direct("EUR", "GBP") == rates.direct("EUR", "GBP")
