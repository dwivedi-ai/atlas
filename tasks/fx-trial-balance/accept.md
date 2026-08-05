The change is accepted when all of the following hold, run from the repository
root with the job's virtualenv on PATH:

1. `python3 -m pytest` passes with no failures and no errors, and reports at
   least as many tests as before the change.
2. `python3 -m ledgerline.cli report trial-balance tests/data/sample.ledger --rates tests/data/rates.csv --convert-to USD --period 2024-Q4 --json`
   exits 0, and in the resulting JSON every row's `currency` field is `USD`.
3. In that JSON, `meta.converted` is `true`.
4. The same command with `--convert-to EUR` exits 0 and yields rows whose
   `currency` is `EUR` throughout.
5. Running it with `--convert-to USD` but no `--rates` exits non-zero and prints
   a message naming the missing rate table, rather than a traceback.
6. Without `--convert-to`, the report is unchanged: the same command minus that
   flag still yields rows in USD, EUR and GBP.

Do not check which function performed the conversion, which module it lives in,
or what any file's contents look like. Only the behaviour above is graded.
