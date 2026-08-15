# Review checklist

Work through it in order. Most changes fail on the first three.

## Boundaries

- [ ] Does the change respect the import order in `architecture.md`? Nothing
      imports upward.
- [ ] Is the new code in the module that owns the concept, rather than the
      module that happens to call it?
- [ ] If it rounds, does it round through `Money.scale`, exactly once?

## Correctness

- [ ] Does any arithmetic touch a `float`? Rates come through `fx.as_fraction`;
      amounts are integers.
- [ ] Is the per-currency invariant preserved? A total across currencies without
      an explicit conversion is a bug even when the test passes.
- [ ] Does a new report put its numbers in `meta` and its strings in `rows`?
- [ ] Does a mutation of `Ledger` invalidate the indexes?

## Tests

- [ ] Is there a test that fails without the change?
- [ ] For a bug fix, is the test the one that would have caught it, in the file
      named after the module?
- [ ] Does anything write inside the repository, read the clock, or reach the
      network? None of the three is acceptable.
- [ ] Does the suite still pass in under a couple of seconds?

## Data

- [ ] If `tests/data/sample.ledger` changed, does it still validate with zero
      errors *and* zero warnings?
- [ ] If `tests/data/rates.csv` changed, is the table still
      triangulation-consistent? There is a test; make sure it ran.
- [ ] If `tests/data/generated/` changed, did the generator change in the same
      commit?

## Style

- [ ] Imperative docstrings, one-line summary first.
- [ ] Explicit imports.
- [ ] A literal used twice has a name.
- [ ] Comments say why, not what.
- [ ] No reformatting of lines the change does not otherwise touch.

## Documentation

- [ ] Does a public API change update the relevant file under `docs/`?
- [ ] Does a decision that was argued get an entry in `decisions.md`?
- [ ] Does a breaking change get marked as such in `docs/changelog.md`?
