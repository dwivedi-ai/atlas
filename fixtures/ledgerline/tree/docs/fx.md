# Currencies and conversion

## The rate table

`fx.RateTable` maps an ordered pair to a `Fraction`:

```
pairs[("USD", "EUR")] == Fraction(4, 5)   # one USD buys 0.80 EUR
```

Rates are read from a `base,quote,rate` CSV and parsed through `fx.as_fraction`,
which accepts a decimal string, an integer, a `Decimal` or a ratio like `5/8` —
and *rejects a float outright*. That refusal is deliberate: `0.1` is not
one tenth in binary, and a rate table that quietly stores the nearest double is
a rounding bug that only shows up on the third decimal place of a large total.

The table in `tests/data/rates.csv` is deliberately sparse. It prices USD
against EUR, GBP and JPY in both directions, and EUR against GBP, and that is
all. `EUR->JPY` is not in it. Sparse tables are what real vendor feeds look
like: the liquid pairs are quoted and the rest are expected to be worked out.

## The two conversion paths

```python
fx.convert(amount, "GBP", table)             # uses table.direct("EUR", "GBP")
fx.convert_via_bridge(amount, "GBP", table)  # uses table.usd_leg on both sides
```

`convert` looks up the ordered pair and raises `MissingRate` when the feed does
not carry it. `convert_via_bridge` triangulates through `fx.BRIDGE_CURRENCY`
(USD), so it works for any two currencies the table prices against USD at all —
including the pairs the feed never quotes.

Both apply the resulting `Fraction` through `Money.scale`, which rounds
half-to-even exactly once and re-denominates into the target currency's
exponent. Neither accumulates drift, and neither is more precise than the other:
for a pair the table quotes *consistently*, they return the same `Money`.

They differ in **which numbers in the table the answer depends on**. A direct
conversion depends on one quoted pair. A bridged conversion depends on two USD
legs. When a feed's cross rates are consistent with its USD legs the two agree;
when they are not, they disagree, and which one you called determines which
inconsistency you inherit.

`tests/test_fx.py::test_bridge_agrees_with_the_direct_table_where_both_exist`
pins the consistency of the fixture table, so a rate edit that breaks
triangulation fails the suite rather than quietly shifting a report.

Every conversion the package currently performs on its own — there is exactly
one, in `csvio.to_rows` — goes through `convert`.

## Exponents

`money.EXPONENTS` records how many minor units a currency has: two for USD, EUR,
GBP and CHF, zero for JPY and KRW. Anything absent defaults to two. Conversion
crosses exponents correctly because `Money.scale` applies the shift itself:

```python
Money(10000, "USD").scale(Fraction(125), currency="JPY") == Money(12500, "JPY")
#     100.00 USD                                            12,500 JPY
```

Note that the caller never touches the exponent. A conversion helper that
multiplied by the rate and then "fixed up" the decimal places would have two
rounding points, and the second one would be invisible.

## Totalling mixed currencies

`fx.sum_in(amounts, "USD", table)` converts each amount and adds. It converts
before adding, one item at a time, which means one rounding per item rather than
one rounding for the total. For a report of twenty accounts the difference is at
most a few minor units, but it is a real difference and it is the reason the
function exists rather than being three lines at each call site.

## What is not here

- **No rate history.** A `RateTable` is a snapshot; there is no as-at lookup.
  Reports that need a historical rate need a table per date, built by the
  caller.
- **No inverse synthesis.** `direct("EUR", "USD")` does not fall back to
  `1 / direct("USD", "EUR")`. Only `usd_leg` inverts, and only for the bridge
  legs, because a bid/ask spread makes the inverse of a quote the wrong number
  in general.
- **No currency validation beyond shape.** `Money` checks that a code is three
  or four letters. It does not know which codes exist.
