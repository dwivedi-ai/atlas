# Reports

A report is a pure function

```python
def my_report(ledger: Ledger, options: ReportOptions) -> Report: ...
```

`Report` carries a name, a title, a tuple of column names, a list of string rows
and a free-form `meta` dict. Rows are strings because the only consumer is a
renderer; anything a caller might want to compute on belongs in `meta`, where it
stays typed.

## Options

One `ReportOptions` serves every report, so the CLI does not have to know which
report takes which flag:

| field | meaning |
|---|---|
| `period` | a `Period`, or `None` for all dates |
| `account` | an account code, for the reports that need one |
| `currency` | a currency code; the meaning is per report |
| `rates` | a `RateTable`, for reports that convert |
| `include_closed` | include accounts the chart marks closed |
| `limit` | row cap; the reports that honour it keep the tail |

`options.scoped(ledger)` is the one-liner every report starts with — it applies
`period` and returns the narrowed ledger.

## The registry

Names are bound to functions by `reports.register(name, fn, doc=..., origin=...)`
and resolved by `reports.get(name)`. Resolution loads two sources in order:

1. the built-ins, registered at the bottom of `ledgerline/reports.py`;
2. `ledgerline/local_reports.py`, imported lazily on the first lookup.

`register` replaces an existing name rather than raising, which is what makes the
second source able to override the first. `reports.describe()` reports the
`origin` of each name, so which is which stays visible.

## The built-ins

### `trial-balance`

Closing balance of every account that appears in the ledger, one row per
(account, currency). `meta["residual"]` is the net per currency and should be
zero everywhere — a non-zero residual means an unbalanced transaction got past
the parser, which is a bug in this package rather than in your data.

`options.currency` narrows the rows to one currency. It does **not** convert
anything: a ledger holding USD, EUR and GBP reports three groups of rows, and
asking for one currency shows you that group only. `meta["converted"]` is
`False` to make that explicit to anything consuming the JSON form. Converting a
trial balance into a single reporting currency is a real need and is not
implemented; see `docs/roadmap.md`.

### `account-statement`

Every transaction touching one account, with a running balance. Requires
`options.account`. The currency is `options.currency`, else the account's chart
currency, else whatever the account's own postings mostly use.
`meta["closing"]` is the final balance in minor units.

### `income-statement`

Income and expense totals for the period, per currency, plus a `net` row. Income
is stored as a credit and expense as a debit, so the profit for the period is
the negated sum of both sides — that negation lives in exactly one place in
`reports.income_statement` and is the sort of line worth reading twice.

### `balance-sheet`

Asset, liability and equity totals as at the end of the period. No conversion,
so a multi-currency entity gets a row per currency per section.

### `transaction-log`

A flat log: date, reference, description, tags, posting count. Mostly useful for
eyeballing what a filter actually selected.

## Deployment-local reports

`ledgerline/local_reports.py` is imported by the registry at lookup time and
registers whatever it likes. In this checkout it contributes `aging` and
`stale-accounts`. The module is expected to differ between deployments, and
nothing in the package imports it directly — only `reports._load_local` does,
inside a `try` that tolerates its absence.

## Writing one

```python
from ledgerline.reports import Report, ReportOptions, register

def entries_per_day(ledger, options):
    scope = options.scoped(ledger)
    rows = [[day.isoformat(), str(n)] for day, n in _count_by_day(scope)]
    return Report(
        name="entries-per-day",
        title=f"Entries per day ({options.period or 'all dates'})",
        columns=("date", "entries"),
        rows=rows,
        meta={"days": len(rows)},
    )

register("entries-per-day", entries_per_day, doc=entries_per_day.__doc__ or "")
```

Two things to get right: return strings in `rows`, and put anything numeric a
caller might assert on into `meta`. The suite for the built-ins asserts almost
entirely on `meta`, which is why it survives changes to column order.
