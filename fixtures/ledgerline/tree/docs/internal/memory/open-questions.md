# Open questions

Things nobody has decided. Kept here rather than in issues because they are
arguments, not tasks, and an argument in an issue tracker rots.

## The elided amount in a mixed-currency transaction

Today the elided posting balances the currency of the first *priced* posting in
the transaction. That is what the implementation happened to do in 0.4.0 and it
was documented afterwards rather than chosen.

Three candidates:

1. **Keep it.** Order-dependent, but predictable and already documented.
2. **Balance the currency the elided posting's account is declared in.** Needs a
   chart, which the parser deliberately does not have.
3. **Refuse.** Make an elision in a mixed-currency transaction a parse error.
   Strictest, and would reject documents that parse today.

Carried since the Q2 retro. Nobody has hit it in real data, which is both why it
is still open and why (3) is tempting.

## Does the index cache earn its keep?

A full rebuild of both indexes is about a tenth of a second on the largest
document anyone has produced. The cache exists, was the direct cause of the
2024-08 incident, and is currently correct. Removing it removes a class of bug
and costs a measurement nobody would notice.

The argument against removing it: the next document might be ten times larger,
and the invalidation is now correct and tested.

## Should chart validation be a rule?

`validate.validate_chart` is a separate function because it does not need a
ledger, so `ledgerline validate` does not run it and a chart with undeclared
parents produces no output from the CLI. Making it a rule means rules have to
tolerate a `None` ledger, which is a shape change for one caller.

## What does `include_closed` mean?

`ReportOptions.include_closed` exists and no built-in report reads it. The
intended meaning is "show accounts the chart marks closed even when they have no
activity in the period", which is a different question from "show accounts that
were posted to despite being closed" — and the second is a validation error, not
a report option. Until a report needs it, the field is a promise nobody has
kept.

## Per-row versus per-total rounding

`fx.sum_in` converts each amount and adds, so an *n*-row report rounds *n*
times. Converting the total instead rounds once. Each row is then consistent
with the total beside it under the current choice, and not under the other one.
Both are defensible; the current one is undocumented outside `docs/fx.md` and
one runbook entry.

## How large can a `RateTable` legitimately be?

`missing_pairs` is O(n²) in the number of currencies and is called by nothing on
a hot path. If a table ever prices thirty currencies, several assumptions in
`fx` about sparseness stop being true, starting with the idea that a missing
pair is unusual enough to raise.
