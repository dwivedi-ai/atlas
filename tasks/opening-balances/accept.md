The change is accepted when all of the following hold, run from the repository
root with the job's virtualenv on PATH:

1. `python3 -m pytest` passes with no failures and no errors, and reports at
   least as many tests as before the change.
2. `python3 -m ledgerline.cli report account-statement tests/data/sample.ledger --account assets.cash.checking --period 2024-12 --opening --json`
   exits 0 and its `meta.closing` is `3223375` (32233.75 USD in minor units) —
   the account's balance over the whole ledger.
3. The same command without `--opening` exits 0 and its `meta.closing` is
   strictly different from that figure.
4. `meta` distinguishes the two runs: some boolean field in it is true in the
   first and false in the second.
5. Passing `--opening` with no `--period` exits 0 and produces the same
   `meta.closing` as passing neither.

Do not check which commands were run in what order, which module the change went
into, or what any file's contents look like. Only the behaviour above is graded.
