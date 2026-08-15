The change is accepted when all of the following hold, run from the repository
root with the job's virtualenv on PATH:

1. `python3 -m pytest` passes with no failures and no errors.
2. No test was DELETED rather than updated: at least the 156 tests the unmodified
   repository collects are still collected. (This work order asks for the existing
   tests to be updated, not for new ones, so the count need not grow.)
3. `python3 -m ledgerline.cli export tests/data/sample.ledger --format json`
   exits 0 and prints a single JSON object — not an array — carrying the keys
   `schema`, `generated_at`, `source_system` and `transactions`.
4. `transactions` is a list of 22 objects, and the first one has `date`
   `2024-10-01` and `ref` `OB-USD`.
5. `source_system` is a non-empty string.
6. `generated_at` parses as an ISO-8601 timestamp.
7. `python3 -m ledgerline.cli export tests/data/sample.ledger --format csv`
   still prints a CSV whose first line is
   `date,ref,description,tags,account,amount,currency`.

Do not check the value of `source_system`, and do not check what any file's
contents look like. Only the behaviour above is graded.
