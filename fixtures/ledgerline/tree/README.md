# ledgerline

A small, dependency-free double-entry ledger toolkit: a text format, a parser, a
validator, a handful of reports and a CLI. Everything is standard library only,
and every monetary amount is an integer count of minor units, so no balance in
this repository has ever met a float.

```console
$ python3 -m ledgerline.cli validate tests/data/sample.ledger --chart tests/data/chart.csv
22 transactions, 0 error(s), 0 warning(s)

$ python3 -m ledgerline.cli report trial-balance tests/data/sample.ledger --period 2024-Q4
Trial balance (2024-Q4)
-----------------------
account                       type       currency   balance
----------------------------  ---------  --------  --------
assets.cash.checking          asset      USD       32233.75
...
```

## Layout

```
ledgerline/          the package
  errors.py          the exception hierarchy
  money.py           Money: integer minor units, one rounding point
  accounts.py        dotted account codes, types, normal balances
  model.py           Posting and Transaction
  parser.py          the .ledger text format
  periods.py         years, quarters, months, explicit ranges
  ledger.py          the Ledger container and its indexes
  fx.py              rate tables and the two conversion paths
  validate.py        the rule set
  reports.py         report definitions and the name registry
  local_reports.py   reports contributed by this deployment
  csvio.py           flat-CSV import and export
  jsonio.py          JSON export
  textui.py          aligned plain-text tables
  cli.py             the command line entry point
scripts/             maintenance scripts
tests/               pytest suite and its data
docs/                everything below the surface
```

## Running the tests

```console
$ python3 -m pytest
```

The suite is fast and hermetic: it reads only `tests/data/`, writes only into
pytest's `tmp_path`, and never reaches the network.

## Where to read next

- `docs/overview.md` — what the pieces are and how they fit together.
- `docs/ledger-format.md` — the text format, in full.
- `docs/reports.md` — the report registry and each built-in report.
- `docs/cli.md` — every subcommand and flag.
- `docs/internal/` — architecture, decisions and operational notes.

## Status

`0.7.2`. The public surface is the package's `__init__` exports plus the CLI;
anything else may move between minor versions. See `docs/changelog.md`.
