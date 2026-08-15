# Roadmap

Ordered by how often the absence has actually cost someone something, not by how
interesting the work is.

## Near term

**Converted trial balance.** Reporting a multi-currency entity currently means
reading three groups of rows and adding them up by hand. The report should take
a target currency and a rate table and produce one set of rows. The plumbing is
already there — `ReportOptions.rates` and the `--rates` flag both exist and are
currently unread by every built-in report. What is missing is the report change
and a decision about how to present the rate used. See `docs/fx.md` for the
conversion paths available.

**Monthly breakdowns.** Several reports would be more useful with a `--by-month`
axis: cash movement per month, expenses per month, income per month. `Period.months()`
already produces the axis and `Ledger.index_by_month` already exists, so this is
presentation work rather than computation work.

**Export envelope.** The JSON export is a bare array. Anything consuming it
downstream has nowhere to put a schema version, a generation timestamp or a
producer identifier, which makes deduplicating two exports of overlapping date
ranges guesswork. `jsonio.loads` already accepts a wrapped document with a
`transactions` key, so the reader half is done.

## Medium term

**As-at rate tables.** A `RateTable` is a snapshot. Historical reporting needs a
rate per date, which means a table-of-tables and a lookup policy (nearest
earlier? exact only?). The policy is the hard part.

**Opening balances per period.** `account-statement` starts from zero rather
than from the closing balance of the previous period. Carrying a balance
forward needs a notion of where a period starts that survives filtering, which
today it does not.

**Chart validation as a rule.** `validate.validate_chart` is a separate function
because it does not need a ledger. It should probably be a rule that tolerates a
`None` ledger, so `ledgerline validate` reports chart problems too.

## Long term, maybe never

**Balance assertions in the format.** Rejected once already (see
`docs/internal/decisions.md`), but the argument keeps coming back.

**A plugin directory for reports.** Also rejected once. `local_reports.py`
covers the case that actually occurs.

**A second output format.** Nobody has asked for HTML, and the moment there is a
second renderer the `Report` contract has to grow types.

## Not planned

- Journal locking, period close, audit trails. This is a library, not a system
  of record.
- Any runtime dependency. The absence is load-bearing: it is why the package
  runs anywhere a Python 3 does, including inside other people's build steps.
- Automatic currency conversion at parse time. A rate that was implicit when the
  document was read cannot be audited afterwards.
