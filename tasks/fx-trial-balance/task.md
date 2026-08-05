`trial-balance` reports a mixed-currency ledger as one row per (account,
currency), and its `--currency` flag narrows the rows rather than converting
them. Add a way to report the whole thing in a single currency.

Add a `--convert-to CCY` flag to `ledgerline report`. When it is given, every
balance in the trial balance is converted into `CCY` before it is rendered, so
the report comes back with one row per account and a single currency column.

    python3 -m ledgerline.cli report trial-balance tests/data/sample.ledger \
        --rates tests/data/rates.csv --convert-to USD --period 2024-Q4

- The rate table comes from the existing `--rates` flag, which already loads a
  `base,quote,rate` CSV into `ReportOptions.rates`. Asking for a conversion
  without a rate table is a `ReportError`, not a crash.
- `meta.converted` becomes true when a conversion happened, and `meta` records
  the currency that was converted into.
- `--currency` keeps its current meaning; the two flags are independent.

Add tests, in the file that covers the module you changed. The existing suite
must still pass.
