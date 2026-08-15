# Onboarding notes

Written by the third person to work on this, for the fourth. Deliberately
informal and deliberately opinionated; the authoritative documents are under
`docs/` and `docs/internal/`.

## Read in this order

1. `README.md` — five minutes, mostly to see the shape.
2. `docs/overview.md` — the four invariants. Everything else follows from them.
3. `ledgerline/money.py` — the whole file. It is short and it is the bottom of
   the stack; if you understand `scale` you understand the package's attitude.
4. `docs/ledger-format.md` — then open `tests/data/sample.ledger` beside it.
5. `docs/internal/decisions.md` — skim. Come back when you disagree with
   something.

Skip `docs/internal/performance-notes.md` until something is slow. Skip
`docs/glossary.md` until a word confuses you; it is a reference, not a read.

## Things that surprised me

**The parser enforces balancing.** I expected validation to. It means a
`Transaction` in memory is always balanced, which is why `Ledger.add` can
require it and why nothing downstream ever checks.

**`Ledger.filter` shares objects.** Filtering three times is free. I wrote a
copy-avoiding version of a report before noticing the copies were not there.

**Reports return strings.** I fought this for a day. The argument in
`docs/internal/architecture.md` is right: the moment a row holds a `Money`,
`textui` has to know what a `Money` is.

**There is no CI.** The suite runs in under a second, so the loop is `pytest` in
a terminal beside the editor. It works better than it sounds.

## Things I got wrong

Used `round()` on a converted amount because it was "obviously" the same as
`Money.scale`. It is not: `round()` takes a float, and by the time you have a
float you have already lost. Review caught it in the first pass.

Added a helper to `ledger.py` that belonged in `reports.py`, on the grounds that
the ledger had the data. The rule of thumb I have since internalised: if it
produces something a person reads, it is a report; if it answers a question
another function asks, it is a ledger query.

Put a number in a report's rendered column and asserted on it in a test. It
passed, then broke when a column moved. Numbers go in `meta`.

## Where the bodies are

- `parser._finish` is the densest function in the package. It closes a
  transaction, fills an elision and validates balance, and it is where a format
  change hurts most.
- `reports._totals_by_type` is quietly load-bearing for two reports and has one
  test each.
- `money._round_half_even` is written out longhand rather than delegated. Do not
  "simplify" it to `round()`; see above.
