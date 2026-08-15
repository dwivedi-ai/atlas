Add a `cashflow` report to ledgerline: net movement of the cash accounts, broken
down by month.

It must be reachable through the report registry by the name `cashflow`, so this
works from the repository root:

    python3 -m ledgerline.cli report cashflow tests/data/sample.ledger --period 2024-Q4

The report has five columns, in this order: `month`, `currency`, `inflow`,
`outflow`, `net`. One row per (month, currency) that has any activity, sorted by
month and then by currency. Months are labelled `YYYY-MM`.

- `inflow` is the total of the positive postings to the cash subtree in that
  month, `outflow` the total of the negative ones as a positive number, and
  `net` is `inflow - outflow`. All three are rendered in major units without a
  currency code, exactly like every other report's amounts.
- The cash subtree defaults to `assets.cash` and follows `options.account` when
  one is given, so `--account assets.cash.checking` narrows it.
- `meta` carries at least `root` (the subtree that was aged) and `months` (the
  month labels that produced rows), because the tests assert on `meta` rather
  than on rendered columns.

Add tests for it in the file that covers the module you put it in. The existing
suite must still pass.
