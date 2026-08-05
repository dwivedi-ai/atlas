# Architecture

## The dependency order

The package is a layered stack and the layering is enforced by convention plus
one test that imports every module in isolation. Bottom to top:

```
errors
  money
    accounts        model
      periods         parser
        ledger
          fx    validate    reports
            csvio    jsonio    local_reports
              textui
                cli
```

Two rules keep it acyclic:

1. **No upward imports.** `money` does not know about `Money`'s eventual place
   in a `Posting`. `reports` does not know about the CLI.
2. **Late imports where a cycle would otherwise be real.** `Ledger.load` imports
   `parser` inside the method, and `reports._load_local` imports
   `local_reports` inside the function. Both are documented at the import.

## Why `money` is at the bottom

Because the rounding point has to be. If any layer above `Money` could round, an
audit of "where can a number change" would have to read the whole package
instead of one method. `Money.scale` is that method: it takes an exact
`Fraction`, applies the exponent shift for the target currency, and rounds
half-to-even exactly once. Everything else adds and subtracts integers.

The corollary is that `fx` is *thin*. It computes a `Fraction` and hands it to
`Money.scale`. It is not allowed to do arithmetic on minor units itself, and a
change that made it do so would be reverted on those grounds alone.

## Why the chart is optional

Because the two things it provides — account existence and a default currency —
are only needed by two consumers, and forcing every caller to construct one to
parse a file would make the parser's tests three lines longer for no benefit.
`Ledger.chart` being `None` is a supported state, and the rules that need it
return an empty issue list rather than raising.

This is also why `validate.check_known_accounts` returns `[]` rather than
complaining when there is no chart: "I cannot check this" and "this is fine" are
different, but the caller who did not attach a chart has already expressed which
one they meant.

## Why reports return strings

A `Report` is a table of strings plus a `meta` dict. That split exists because
two consumers want different things: `textui` wants aligned strings and does not
care what they mean, and a test wants typed values and does not care how they
look. Putting typed values in the rows would force `textui` to know about
`Money`; putting formatted strings in `meta` would make every assertion a string
comparison.

The cost is that a report formats its own numbers, which means `_fmt` is
duplicated in spirit across the built-ins. That was judged the smaller evil.

## Indexes and invalidation

`Ledger` caches two indexes and clears both in `_invalidate`, which `add` calls.
The 0.5.1 bug (see `memory/incident-2024-08-11.md`) was exactly this method not
being called. There is no partial invalidation and there should not be: an
index rebuild over a few thousand transactions is under a millisecond, and the
partial version has a state space nobody can hold in their head.

## Where the boundaries actually get argued

- **`reports` vs `local_reports`.** The registry makes this a runtime question
  rather than a structural one, which is the point.
- **`ledger` vs `reports`.** `Ledger.running_balance` is arguably a report. It
  lives on the ledger because two reports use it and because it is a query over
  the indexes, not a presentation.
- **`parser` vs `model`.** `elide_balance` lives in `model` even though only the
  parser calls it, because it is a statement about what a balanced transaction
  is, not about how the text format spells one.
