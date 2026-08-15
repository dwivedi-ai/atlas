# Accounts and the chart

## Codes

An account code is a dotted path of segments, each made of letters, digits,
hyphens and underscores:

```
assets.cash.checking
liabilities.payable.hosting
expenses.bank-fees
```

The code *is* the hierarchy. `accounts.parents_of` splits it, `accounts.is_under`
tests containment, and `Ledger.balance` rolls a subtree up by prefix. There is no
parent field, so the tree cannot disagree with the codes.

## Types and normal balances

The first segment determines the type through `accounts.ROOT_TYPES`:

| first segment | type |
|---|---|
| `assets` | `asset` |
| `liabilities` | `liability` |
| `equity` | `equity` |
| `income`, `revenue` | `income` |
| `expenses` | `expense` |

A code whose first segment is not in that table raises immediately, at `Account`
construction time. Adding a sixth root means editing `ROOT_TYPES` and
`NORMAL_BALANCE` together; they are two dicts rather than one because the second
is keyed by type, not by segment, and several segments map to one type.

Normal balance is `+1` for assets and expenses (a debit increases them) and `-1`
for liabilities, equity and income. Reports use `NORMAL_BALANCE` to present each
line in its own natural direction, which is why income shows as a positive
number on the income statement even though it is stored as a credit.

## The chart CSV

`ChartOfAccounts.load` reads a CSV with these columns:

```
code,name,currency,closed
assets.cash.checking,Operating checking,USD,false
assets.cash.petty,Petty cash,USD,true
```

- `name` defaults to the last segment of the code when blank.
- `currency` is the account's home currency. It is what `Ledger.balance` falls
  back to when the ledger itself is ambiguous.
- `closed` accepts `1`, `true` or `yes`, case-insensitively; anything else is
  false. A posting to a closed account is a validation *error*.

Duplicate codes raise `DuplicateAccount` on load rather than silently taking the
last one, because a chart with two definitions of `assets.cash` is a merge
accident and not a preference.

## Declaring parents

Nothing forces you to declare `assets` and `assets.cash` in order to declare
`assets.cash.checking`. `ChartOfAccounts.implied_parents` lists the ancestors
that are referenced but never declared, and `validate.validate_chart` turns each
one into a warning. The sample chart in `tests/data/chart.csv` declares every
parent, which is why its `implied_parents()` is empty and why
`chart.children("assets.cash")` returns something useful.

## Multi-currency accounts

An account may hold more than one currency — nothing prevents it — but the
`single-currency-per-account` validation rule warns when one does, and
`Ledger.balance` refuses to guess:

```python
ledger.balance("assets")                  # raises: the subtree holds USD, EUR and GBP
ledger.balance("assets", currency="EUR")  # fine
```

The convention this repository follows in its own sample data is one currency
per leaf account, with the currency in the code where it would otherwise be
ambiguous (`assets.cash.eur`, `equity.opening.usd`). It is a convention, not a
rule, and the validator's warning is the only thing that enforces it.

## Choosing depth

Three or four segments is usually right. Deeper trees make prefix rollups more
useful but make the codes hard to type; shallower ones push the distinction into
the description, where nothing can aggregate it. The sample chart uses:

```
assets.cash.checking          three segments, one leaf per real bank account
assets.receivable.acme        three segments, one leaf per counterparty
expenses.rent                 two segments, no useful subdivision
```
