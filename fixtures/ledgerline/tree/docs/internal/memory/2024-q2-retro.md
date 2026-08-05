# Retro — 2024 Q2

Covering 0.2.0 through 0.4.0: periods, the CLI, validation.

## What went well

**Splitting `textui` out of the reports.** Done in 0.3.0 while there were only
two reports, and it has paid for itself every release since. Report tests assert
on values; renderer tests assert on alignment; neither has ever had to change
because of the other.

**Validation returning issues instead of raising.** The first sketch raised on
the first problem. Someone pointed out that a person fixing a ledger wants the
whole list, and that a validator which stops at the first error trains people to
run it in a loop. The `Issue` record has not changed shape since.

**Periods as a value type.** Parsing four textual forms into one immutable range
turned every "does this transaction belong in the report" question into
`period.contains(date)`. The alternative — passing start and end dates around —
had already leaked into two function signatures before it was pulled back.

## What did not

**The CLI grew flags before it grew a shape.** `report` accreted `--account`,
`--currency` and `--limit` as separate keyword arguments threaded through three
layers. `ReportOptions` in 0.7.0 fixed it, two releases later than it should
have. The signal we ignored: the third time a flag had to be added in four
places.

**Elided amounts were specified late.** The feature landed in 0.4.0 and the
multi-currency interaction — which currency does an elided posting balance? —
was decided by whatever the implementation happened to do, then documented
afterwards. It is documented now, and the sample data avoids the corner
entirely, which is a smell.

**Nobody wrote down why `Decimal` was rejected** until the question came back in
Q3 and had to be re-argued from memory. `docs/internal/decisions.md` exists
because of this retro.

## Actions

- [x] Write the decision log, retroactively for D1 and D4.
- [x] Give the report entry points one options object.
- [ ] Decide the elided-amount rule for mixed-currency transactions properly,
      rather than documenting the implementation. Still open; see
      `open-questions.md`.
