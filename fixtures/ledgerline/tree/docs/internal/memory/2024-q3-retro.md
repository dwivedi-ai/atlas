# Retro — 2024 Q3

Covering 0.5.0 through 0.6.3: `fx`, the chart of accounts, the August incident.

## What went well

**One rounding point.** `Money.scale` landed with `fx` and the discipline held.
Every conversion in the package computes an exact `Fraction` and hands it over.
The first version of `fx` did its own rounding and produced the off-by-a-cent
bug within a week, which made the argument for D2 without anyone having to make
it.

**The chart being optional.** It would have been easy to make `Ledger` require
one. Keeping it optional meant the parser's tests never grew a chart, and it
made "posting to an undeclared account" a representable state — which is what
lets the validator report it instead of the parser refusing to load the file.

**The incident write-up.** `incident-2024-08-11.md` was written the same week,
while the reasoning was still available. Its most useful paragraph is the last
one, which is about a cache added for an unmeasured problem, and that paragraph
is now quoted in review more often than the incident is remembered.

## What did not

**The cache.** See above. Added speculatively, cost two nights of a consumer's
numbers, removed nothing when the fix landed because the fix was to invalidate
it properly rather than to delete it. It is still there. It is still buying
about a tenth of a second on a document nobody has.

**Rate tables shipped without a consistency check.** `tests/data/rates.csv` was
hand-written to be triangulation-consistent and nothing verified that until
0.7.1. Between 0.5.0 and 0.7.1 an edit to any rate could have silently made the
two conversion paths disagree, and the test that would have caught it takes four
lines.

**D6 was re-litigated.** Balance assertions in the format came back, was argued
for a week, and landed exactly where it had in 0.4.0. The decision log entry
existed; nobody read it. The fix is not a process, it is that the entry now says
"raised again in Q3 and not reopened" so the next person can see the argument
already happened twice.

## Actions

- [x] Test that the fixture rate table triangulates consistently.
- [x] Note the re-litigation in D6.
- [ ] Decide whether the ledger's index cache earns its keep. Leaning no.
- [ ] `sample.ledger` still avoids the mixed-currency elision corner rather than
      exercising it. Carried from Q2.
