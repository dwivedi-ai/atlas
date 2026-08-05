# Testing

```console
$ python3 -m pytest
```

`pytest.ini` sets `testpaths = tests` and `-q`. The root `conftest.py` puts the
repository root on `sys.path`, which is what lets `import ledgerline` work
without installing anything.

## Fixtures

`tests/conftest.py` provides four:

| fixture | scope | what it is |
|---|---|---|
| `data_dir` | session | `tests/data/` as a `Path` |
| `chart` | session | `ChartOfAccounts` loaded from `chart.csv` |
| `ledger` | function | `Ledger` loaded from `sample.ledger`, with the chart |
| `rates` | session | `RateTable` loaded from `rates.csv` |

`ledger` is function-scoped because a `Ledger` is mutable — `add` appends and
re-sorts — and a test that added a transaction to a session-scoped ledger would
be visible to every test that ran after it.

## The data

`tests/data/sample.ledger` is one quarter of a small consultancy: 22
transactions across October to December 2024, in USD with EUR and GBP operating
currencies. It validates clean — zero errors *and* zero warnings — and
`test_validate.py::test_sample_ledger_is_clean` pins that. Adding a transaction
that trips a rule will break that test, which is the intent.

`tests/data/chart.csv` declares every account the sample uses, including every
parent, so `implied_parents()` is empty.

`tests/data/rates.csv` is deliberately sparse and deliberately
triangulation-consistent; see `docs/fx.md`.

## Derived fixtures

`tests/data/generated/` holds files produced by `scripts/gen_fixtures.py`:

- `payroll-2024.ledger` — twelve months of rent and payroll, with the annual
  salary uplift landing in October.
- `rates-extended.csv` — a table that prices every currency against USD,
  including the pairs `rates.csv` leaves out.

The generator is deterministic: no clock, no randomness, no environment lookups,
so its output is a pure function of the constants at the top of the file.
`scripts/regen_fixtures.sh` re-runs it in place, and
`python3 scripts/gen_fixtures.py --check` reports staleness without writing
anything (exit 1 when something is stale).

## What the suite covers

| file | subject |
|---|---|
| `test_money.py` | parsing, exact arithmetic, the single rounding point |
| `test_accounts.py` | codes, types, hierarchy, chart loading |
| `test_model.py` | postings, per-currency balancing, elision |
| `test_parser.py` | the text format, including its error paths |
| `test_periods.py` | the four period forms and their arithmetic |
| `test_ledger.py` | indexes, filters, balances, running balances |
| `test_fx.py` | both conversion paths and the rate table |
| `test_validate.py` | every rule, on data built to trip exactly one |
| `test_reports.py` | the registry and each report's `meta` |
| `test_io.py` | CSV round-trip, JSON shape |
| `test_cli.py` | every subcommand, through `main` |
| `test_textui.py` | alignment and truncation |
| `test_generated_fixtures.py` | the derived fixtures stay loadable |

## Conventions

- Assert on `meta`, not on rendered whitespace. A report's columns are allowed
  to move; its numbers are not.
- Build the smallest ledger that trips the rule under test. `test_validate.py`
  has a two-line `_pair` helper for exactly this.
- Use `tmp_path` for anything that writes. Nothing in the suite may write inside
  the repository.
- No network, no clock. A test that depends on `date.today()` will start failing
  on a date nobody chose.
