# Runbook

What to do when a number is wrong. Ordered by how often it turns out to be the
answer.

## "The trial balance does not balance"

`meta["residual"]` non-zero means an unbalanced transaction reached the ledger,
which the parser should have made impossible.

1. Re-parse the document directly: `parser.parse_file(path)`. If it raises, the
   ledger you are looking at was not built from that file.
2. If it parses, someone built the ledger through `Ledger.add` with a
   hand-constructed `Transaction`. `add` calls `require_balanced`, so check
   whether the caller caught and swallowed the exception.
3. Check for a mixed-currency transaction where the elided posting balanced the
   wrong currency. `docs/ledger-format.md` covers the ordering rule.

## "The report is missing rows"

Almost always a currency filter, and almost always implicit.

1. `account-statement` filters to one currency. Without an explicit
   `--currency`, it uses the chart's currency for the account, and without a
   chart it uses whatever that account's own postings mostly use.
2. `trial-balance --currency X` narrows rows rather than converting; a EUR
   account will not appear in a USD-filtered view.
3. A period filter with a `START:END` range is inclusive at both ends. Check the
   end date is the one you meant.

## "The report is missing recent transactions"

Check whether anything added to the ledger after the report ran. Indexes are
invalidated on `add`, but a `Report` already produced is a snapshot; re-run it.

## "Conversion is off by a cent"

Expected, and usually correct. `fx.sum_in` converts each amount and then adds, so
a twenty-row report rounds twenty times. Converting the total instead would
round once and give a different answer; neither is more right, and the one the
package does is the one that makes each row self-consistent with the total shown
beside it.

If the difference is larger than a few minor units, compare `table.direct` and
`table.bridge` for the pair. A feed whose cross rates disagree with its USD legs
will produce genuinely different answers depending on which path was taken; see
`docs/fx.md`.

## "A validation error appeared with no data change"

Check whether a chart was attached that was not attached before. Several rules
are no-ops without one.

## "The CLI exits 1 and prints nothing alarming"

Exit 1 means validation errors, not a crash. Warnings alone exit 0. Run with
`--json` and read `summary`.

## Escalation

There is nobody to escalate to. Open an issue with the smallest ledger document
that reproduces it — usually three lines — and the exact command.
