# Changelog

Newest first. Dates are the release date, not the merge date.

## 0.7.2 — 2024-11-18

- Fix `account-statement` picking the wrong default currency for a ledger with
  no chart attached: it took the ledger's first currency alphabetically rather
  than the account's own, so a USD statement on a ledger that also held EUR came
  out empty. `Ledger._default_currency` now consults the account's own postings
  before falling back.
- `textui.render_table` no longer crashes on a ragged row.

## 0.7.1 — 2024-10-30

- `fx.as_fraction` rejects `float` outright instead of accepting it and storing
  the nearest double.
- Added `fx.missing_pairs`, which lists the ordered pairs a table cannot quote
  directly. Useful for deciding whether a feed is worth triangulating around.

## 0.7.0 — 2024-10-09

- Report registry: `reports.register` / `reports.get` / `reports.available`,
  with lazy loading of `ledgerline.local_reports`. Report functions used to be
  looked up by `getattr` on the module, which meant every private helper was
  addressable as a report name.
- `Report.meta` added, and the built-in reports moved everything numeric into
  it. The test suite now asserts on `meta` rather than on rendered columns.
- **Breaking:** report functions take `(ledger, options)` rather than
  `(ledger, **kwargs)`.

## 0.6.3 — 2024-09-22

- `Period` gained the explicit `START:END` range form.
- `Period.next()` and `.previous()` wrap the year correctly for quarters and
  months.

## 0.6.0 — 2024-08-30

- Chart of accounts: `ChartOfAccounts`, the CSV loader, the `closed` flag, and
  the validation rules that depend on them.
- **Breaking:** `Ledger(transactions)` is now `Ledger.from_transactions(...)`;
  the bare constructor takes keyword fields.

## 0.5.1 — 2024-08-12

- Fix the index-invalidation bug behind the 2024-08-11 incident: `Ledger.add`
  did not clear the by-account index, so a report run after an insert saw the
  pre-insert view. See `docs/internal/memory/incident-2024-08-11.md`.

## 0.5.0 — 2024-07-19

- `fx` module: `RateTable`, `convert`, `convert_via_bridge`, exact `Fraction`
  rates throughout.
- `Money.scale` became the single rounding point in the package.

## 0.4.0 — 2024-06-25

- Validation rule set, `Issue` records, and the `validate` subcommand.
- Elided posting amounts in the text format.

## 0.3.0 — 2024-05-30

- The CLI, with `report`, `export` and `accounts`.
- `textui` split out of the reports so numbers and whitespace could be tested
  separately.

## 0.2.0 — 2024-05-02

- Periods, the by-month index, period filtering.

## 0.1.0 — 2024-04-15

- First cut: `Money`, `Posting`, `Transaction`, the parser, and a trial balance.
