`account-statement` always starts its running balance at zero, so a statement
for a single month reads as though the account had been opened on the first of
that month. Add a brought-forward opening balance.

Add an `--opening` flag to `ledgerline report`. When it is given together with
`--period`, the running balance starts from the account's closing balance
strictly before the start of the period, rather than from zero:

    python3 -m ledgerline.cli report account-statement tests/data/sample.ledger \
        --account assets.cash.checking --period 2024-12 --opening

- With `--opening` and a period, the final balance of the statement equals the
  account's balance over the whole ledger.
- Without `--opening`, behaviour is exactly as it is today.
- `--opening` without `--period` has nothing to bring forward and is a no-op.
- `meta` records whether an opening balance was applied, because the tests
  assert on `meta` rather than on rendered columns.

Add tests. The existing suite must still pass.
