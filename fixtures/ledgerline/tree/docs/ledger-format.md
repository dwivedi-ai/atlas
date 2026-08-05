# The `.ledger` text format

The format is line oriented and indentation sensitive. It is meant to be
readable in a diff and typeable by hand, which rules out most of the syntax a
richer format would want.

## A complete example

```
; Comments start with a semicolon or a double slash and run to end of line.

2024-10-01 Opening balances USD  #opening  ^OB-USD
    assets.cash.checking          42000.00 USD
    assets.cash.savings           15000.00 USD
    equity.opening.usd           -57000.00 USD

2024-10-03 Rent for October  #recurring
    expenses.rent                  3200.00 USD
    assets.cash.checking
```

## Headers

A transaction header starts in column 0 with an ISO date, `YYYY-MM-DD`, followed
by a description. Two optional decorations may appear anywhere after the date:

- `#tag` — zero or more tags. Tags are free-form; nothing validates them.
- `^REF` — at most one reference. References are expected to be unique across a
  document; a repeat is reported as a warning, not an error, because
  re-invoicing under one reference is a real thing people do.

The description is whatever remains once tags and the reference are removed. An
empty description is a parse error, not a warning: an entry nobody can identify
is worse than a missing entry.

## Postings

A posting line is indented by at least one space and reads

```
    ACCOUNT     AMOUNT CURRENCY   [#tag ...]
```

The account is a dotted code (see `docs/accounts.md`). The amount accepts a
leading sign, thousands separators (`,` or `_`), a currency symbol, and the
accounting-style bracketed negative — `(1,234.50)` is `-1234.50`. The currency
is a three or four letter code and decides how many minor units the amount has:
`1200 JPY` is 1,200 minor units because JPY has no subdivision, while `12.00
USD` is also 1,200.

### The elided amount

Exactly one posting per transaction may omit its amount entirely, as
`assets.cash.checking` does in the example above. It then takes whatever
balances the transaction. Two omissions in one transaction is a parse error —
the system of equations is underdetermined and guessing is worse than failing.

In a multi-currency transaction the elided posting balances only the currency of
the *first priced posting in that transaction*. This is the one corner of the
format where the reading order matters, and it is why the mixed-currency
entries in `tests/data/sample.ledger` are written as separate transactions per
currency rather than as one combined entry.

## Balancing

A transaction must sum to zero in every currency it mentions, and the parser
enforces this as it closes each transaction. That means a malformed document
fails at the offending line rather than at report time, with the line number
attached:

```
ParseError: transaction 'Broken' does not balance (0.50 USD) (line 14)
```

## Round-tripping

`parser.dump_string` writes transactions back out. The round trip is exact for
anything this package produced: amounts are written in major units at the
currency's own exponent, tags are sorted, and no amount is ever elided on the
way out. It is *not* byte-exact against a hand-written file — comments and
column alignment are not preserved, and were never intended to be.

## Things the format deliberately does not have

- **Includes.** One document is one file. Use `parser.parse_files` and merge.
- **Automatic currency conversion.** A transaction that mixes currencies must
  balance in each; there is no implicit rate lookup at parse time, because a
  rate that was implicit at parse time is unauditable afterwards.
- **Account declarations.** The chart of accounts is a separate CSV. A ledger
  document that names an account the chart has never heard of parses fine and
  fails validation, which is the correct split of responsibilities.
- **Balance assertions.** No `= 1234.00 USD` syntax. Assert in a test instead.
