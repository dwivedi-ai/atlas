# Glossary

Terms as this repository uses them. Accounting vocabulary is overloaded and the
overloading is where most of the confusion in a ledger codebase comes from.

**Account** — a named bucket, identified by a dotted code. See
`docs/accounts.md`.

**Amount** — a `Money`: a signed integer count of minor units plus a currency
code. Never a float, never a `Decimal` outside `Money.parse`.

**Balance** — the net of every posting to an account (and, by default, its
descendants) over a period. A balance is always in one currency; a subtree
holding several has several balances, and asking for one without saying which
raises.

**Bridge currency** — the currency a triangulated conversion passes through.
`fx.BRIDGE_CURRENCY` is USD.

**Chart of accounts** — the CSV declaring which accounts exist, what they are
called, what currency they are kept in and whether they are closed. Advisory:
a ledger works without one.

**Closing** — used here only as a tag on year-end adjustment entries. This
package has no period-close machinery; nothing is locked and nothing is rolled
forward.

**Credit** — a negative amount in a posting. Increases liability, equity and
income accounts.

**Debit** — a positive amount in a posting. Increases asset and expense
accounts.

**Elided amount** — the one posting per transaction allowed to omit its amount,
which then takes whatever balances the entry. See `docs/ledger-format.md`.

**Exponent** — how many decimal places a currency's minor unit implies. Two for
USD, zero for JPY. Lives in `money.EXPONENTS`.

**Issue** — one finding from validation: a code, a severity, a message and
optionally a line number. Validation returns a list of these and never raises
for a data problem.

**Ledger** — the container: transactions in date order, plus the by-account and
by-month indexes, plus an optional chart.

**Minor unit** — the smallest indivisible amount of a currency. Cents for USD,
whole yen for JPY.

**Normal balance** — the side on which an account type increases: `+1` for
assets and expenses, `-1` for the rest. `accounts.NORMAL_BALANCE`.

**Period** — an inclusive date range with a canonical label: `2024`, `2024-Q4`,
`2024-10`, or `START:END`.

**Posting** — one leg of a transaction: an account, an amount, optional tags.

**Rate table** — an ordered-pair mapping to exact `Fraction`s. Deliberately
sparse.

**Reference** — the `^REF` decoration on a transaction header. Expected unique;
a repeat is a warning.

**Report** — a pure function from a ledger plus options to a table of strings
and a `meta` dict.

**Residual** — what a set of postings fails to sum to. Zero in a balanced
transaction, and reported by `trial-balance` in `meta["residual"]`.

**Rollup** — summing an account together with everything beneath it, by prefix
match on the code.

**Tag** — a `#word` on a header or a posting. Free-form; nothing validates them.

**Transaction** — a dated, described, balanced group of postings. Frozen once
built.
