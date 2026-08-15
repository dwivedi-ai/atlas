# Contributing

## Before you start

Read `docs/overview.md` for the shape of the package and
`docs/internal/architecture.md` for why the module boundaries fall where they
do. Most review comments on this repository are boundary comments — the code was
right, it was in the wrong module.

## The loop

```console
$ python3 -m pytest
$ scripts/check_ledger.sh
```

Both are fast. There is no linter configured and no formatter enforced; match
the file you are editing.

## House style

- **Docstrings are imperative and start with a one-line summary.** Modules get a
  paragraph explaining what they own; functions get a line, plus a paragraph
  when there is a reason a reader would not guess.
- **Explicit imports.** No wildcard imports anywhere, including in tests.
- **Type hints on public functions.** `from __future__ import annotations` is at
  the top of every module, so hints are strings and cost nothing at runtime.
- **Keep the public surface small.** A helper with one caller stays private and
  stays next to its caller. `textui.truncate` is public because two modules use
  it; `reports._signed` is private because one does.
- **Name a repeated literal.** If a constant appears twice, it gets a name at
  module level. `money.DEFAULT_EXPONENT` exists for exactly this reason.
- **Comment the reason, not the mechanism.** `# invalidate the index` is noise;
  `# an account statement came out empty because the ledger mentioned EUR first`
  is worth its line.

## Tests

Every change that fixes a bug adds the test that would have caught it, in the
file named after the module. Every new public function gets at least one test
for its happy path and one for the failure it is documented to raise.

Tests do not touch the network, do not read the clock, and do not write inside
the repository — `tmp_path` exists.

## Commits

Present tense subject, under seventy characters, no trailing period:

```
Fix account-statement currency default for chartless ledgers
```

A body is optional and should say why, not what; the diff already says what.
Group unrelated changes into separate commits, and do not reformat a file you
are otherwise not touching — a whitespace-only diff hides the one line that
matters.

## Things to raise rather than decide

- Adding a sixth root segment to `accounts.ROOT_TYPES`. It changes what a valid
  account code means for every existing document.
- Changing the rounding mode in `money._round_half_even`. Every number in the
  package flows through it.
- Adding a runtime dependency. There are none, and the absence is a feature: the
  package runs anywhere a Python 3 does.

## Review

The checklist a reviewer works from is `docs/internal/review-checklist.md`.
Reading it before opening a change is cheaper than reading it afterwards.
