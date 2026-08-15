# Command line

There is no console-script entry point installed; the CLI is invoked as a
module:

```console
$ python3 -m ledgerline.cli --help
```

`cli.main(argv, out=...)` returns an exit code instead of calling `sys.exit`, so
the tests drive it in-process and capture its output as a string. The only thing
that reaches `sys.stderr` is an error message.

## Exit codes

| code | meaning |
|---|---|
| `0` | success |
| `1` | the ledger has validation *errors* (warnings alone do not count) |
| `2` | no subcommand was given |
| `3` | a parse, report or file error |

## `validate`

```console
$ python3 -m ledgerline.cli validate LEDGER [--chart CHART] [--rule NAME ...] [--json]
```

Runs every rule in `ledgerline.validate` unless `--rule` is repeated to select
a subset. Without `--chart`, the account-existence and closed-account rules are
skipped — they have nothing to check against.

`--json` emits `{"summary": ..., "issues": [...]}` and is the form to use from a
script; the plain form is one line per issue plus a count.

## `report`

```console
$ python3 -m ledgerline.cli report NAME LEDGER [--chart C] [--period P]
                                   [--account A] [--currency C] [--rates R]
                                   [--limit N] [--json]
```

`NAME` comes first, before the ledger path. `--period` takes any of the four
`Period` forms: `2024`, `2024-Q4`, `2024-10`, `2024-03-05:2024-03-09`.

`--rates` loads a `base,quote,rate` CSV into `options.rates`. Nothing in the
built-in reports reads it yet; it is plumbed because the flag is where a
converting report would get its table from.

`--json` emits the full `Report`, `meta` included, which is the only way to get
at the typed values behind the rendered columns.

## `export`

```console
$ python3 -m ledgerline.cli export LEDGER [--format json|csv] [--out PATH]
```

JSON is the default. The JSON form is a bare array of transaction objects, each
posting carrying both a decimal string and an integer minor-unit count. The CSV
form is the flat one-row-per-posting shape that `csvio` reads back.

Without `--out` the document goes to stdout, so it pipes.

## `accounts`

```console
$ python3 -m ledgerline.cli accounts CHART [--type TYPE]
```

Lists a chart as a table. `--type` takes one of `asset`, `liability`, `equity`,
`income`, `expense`.

## `reports`

Lists the registered report names, one per line, built-ins and
deployment-local ones together. Pipe it into `wc -l` if you only want the count.

## Scripts

`scripts/check_ledger.sh [LEDGER]` is a thin wrapper over `validate` that
defaults the chart to `tests/data/chart.csv`; it exists so the common case is
one word rather than three flags.

`scripts/regen_fixtures.sh` re-runs the fixture generator; see
`docs/testing.md` for what it generates.
