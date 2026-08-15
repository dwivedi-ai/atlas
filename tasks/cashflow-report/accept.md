The change is accepted when all of the following hold, run from the repository
root with the job's virtualenv on PATH:

1. `python3 -m pytest` passes with no failures and no errors.
2. The suite is LARGER than before the change: more tests are collected than the
   156 the unmodified repository collects. (The work order asks for tests; this is
   the mechanical floor for that, and it cannot check they are meaningful.)
3. `python3 -m ledgerline.cli reports` lists `cashflow`.
4. `python3 -m ledgerline.cli report cashflow tests/data/sample.ledger --period 2024-Q4 --json`
   exits 0 and prints a JSON object whose `columns` are exactly
   `["month", "currency", "inflow", "outflow", "net"]`.
5. In that JSON, `meta.months` is `["2024-10", "2024-11", "2024-12"]`, and every
   row's first field matches one of those labels.
6. The USD rows of the same report over all dates net to the ledger's own USD
   balance for `assets.cash`: summing the `net` column across USD rows gives
   47233.75.

Do not check where the report's code was placed, which module registers it, or
what any file's contents look like. Only the behaviour above is graded.
