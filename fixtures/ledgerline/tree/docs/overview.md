# Overview

ledgerline exists to answer one question well: *what is the balance of this
account, over this period, in this currency?* Everything else in the package is
in service of making that answer exact, reproducible and cheap to check.

## The shape of the thing

A document in the `.ledger` text format is parsed into `Transaction` records.
Each transaction owns an ordered tuple of `Posting`s, and each posting names an
account and carries a `Money`. A `Ledger` collects transactions in date order
and maintains two indexes — by account and by month — that the reports read.

```
text  ->  parser  ->  [Transaction]  ->  Ledger  ->  reports  ->  textui
                          |                 |
                       validate          fx (optional)
```

Nothing in that chain mutates anything upstream of it. A report never edits a
ledger; the ledger never edits a transaction; a transaction is frozen once
built. That is what makes it safe to hand the same `Ledger` to five reports.

## The four invariants

1. **Money is integral.** A `Money` is a signed count of minor units plus a
   currency code. Addition and subtraction are exact. The only operation that
   can round is `Money.scale`, which takes an exact `Fraction` and rounds
   half-to-even exactly once. If you find a `float` anywhere near a balance,
   that is a bug worth a test.

2. **Double entry is per currency.** A transaction balances when its postings
   sum to zero in *every* currency it mentions, not when some notional
   converted total is zero. A multi-currency transaction is therefore several
   independent balancing problems that happen to share a date.

3. **The chart is advisory.** A `Ledger` works without a `ChartOfAccounts`;
   attaching one turns on the account-existence and closed-account rules and
   lets balances default to the right currency. Validation is the only place
   the chart is mandatory for anything.

4. **Presentation is the last step.** Reports return rows of strings and a
   `meta` dict; `textui` turns those into aligned text. A report is tested on
   its numbers, never on its whitespace.

## Account codes

Codes are dotted paths — `assets.cash.checking` — and the first segment decides
the account type, which decides the normal balance. Rolling a subtree up is a
prefix match, so `ledger.balance("assets.cash")` is the sum of everything
beneath it. There is no separate parent pointer to keep in sync, and no way for
the hierarchy to disagree with the code.

The five types are `asset`, `liability`, `equity`, `income`, `expense`. Assets
and expenses increase on the debit side; the other three increase on the credit
side. Reports present each account in its own natural direction, which is why a
credit balance shows as positive on the trial balance rather than as a
confusing minus sign.

## Periods

`Period` parses four forms — `2024`, `2024-Q4`, `2024-10` and an explicit
`START:END` range — and every report takes an optional one. Filtering a ledger
by a period returns a new ledger sharing the same transaction objects, so it is
cheap; nothing is copied but the list.

The fiscal year is assumed to start in January. `periods.FISCAL_YEAR_START_MONTH`
is the single place that assumption lives, and both the quarter helpers and
`Period.year` read it.

## Currencies

`fx.RateTable` holds ordered-pair rates, and the table is deliberately sparse: a
vendor feed prices the liquid pairs and leaves the rest to be worked out. Two
conversion functions exist. `convert()` applies the quoted pair. Both round once
at the end through `Money.scale`, so neither accumulates drift. See
`docs/fx.md` for the full picture, including what happens when the table has no
entry for the pair you asked for.

## What this is not

It is not an accounting system. There is no journal locking, no audit trail, no
period close, no tax anything. It is a library for reading a ledger file and
computing correct numbers from it, and the deliberate smallness is what keeps
the invariants above checkable in an afternoon.
