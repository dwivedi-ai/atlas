# Release process

Releases are cut by hand. There is no CI in this repository, which is a
deliberate consequence of having no runtime dependencies and a suite that runs
in under a second.

## Cutting a release

1. Decide the version. Semantic-ish: a breaking change to the package's public
   exports or to the CLI's flags bumps the minor while we are pre-1.0.
2. Update `__version__` in `ledgerline/__init__.py`.
3. Write the `docs/changelog.md` entry. Newest first, dated with the release
   date. Breaking changes are marked **Breaking** and say what to do instead.
4. Run the suite.
5. Commit as `Release 0.x.y`, tag `v0.x.y`.

## Derived fixtures

`tests/data/generated/` is checked in. It is produced by
`scripts/gen_fixtures.py`, whose output is a pure function of the constants at
the top of that file, so the checked-in copies and a fresh generation agree
whenever nobody has edited the generator.

The rule is therefore about the *generator*, not about the schedule: a change to
`gen_fixtures.py` and the regenerated files land in the same commit. A release
that touched the generator is worth a `python3 scripts/gen_fixtures.py --check`
before tagging, which prints what would change and exits non-zero if anything
would.

## Versioning the payload

`jsonio.PAYLOAD_VERSION` is separate from the package version and tracks the
shape of a transaction object in the JSON export. It is currently `"3"`. Bump it
when a field is added, removed or changes meaning; do not bump it for a package
release that leaves the shape alone.

## What is not automated

- No PyPI upload. The package is vendored by its consumers.
- No changelog generation from commit messages. The changelog is written for a
  reader, and commit subjects are written for a reviewer.
- No version bump script. Two files, once a month.
