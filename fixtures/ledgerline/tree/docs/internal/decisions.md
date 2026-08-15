# Decision log

One entry per decision that was argued rather than assumed. Reversals get an
entry of their own rather than an edit.

## D1 — Integer minor units, not `Decimal` (0.1.0)

**Decided:** every monetary amount is a signed integer count of minor units.

`Decimal` carries a global, mutable precision context. Two `Decimal`s that print
the same can compare unequal. An integer has neither problem. `Decimal` survives
in `Money.parse`, where turning a decimal string into an exact ratio is exactly
what it is for.

**Consequence:** every currency needs an exponent, and `money.EXPONENTS` has to
know about JPY. Accepted.

## D2 — One rounding point (0.5.0)

**Decided:** `Money.scale` is the only operation in the package that can round.

The alternative — letting `fx` multiply and round itself — was in the tree for
one release and produced the classic bug: a conversion that rounded to minor
units, then a total that rounded again, off by a cent for exactly the inputs
nobody tested.

**Consequence:** `fx` is thin, and a helper that wants to convert must produce a
`Fraction`. This is a real constraint on future conversion code and is meant to
be.

## D3 — Reject `float` rates outright (0.7.1)

**Decided:** `fx.as_fraction` raises on a `float` rather than converting it.

Accepting a float silently stores the nearest double, so `0.1` becomes
`0.1000000000000000055511151231257827`. Every subsequent number inherits it.
The error message names the accepted types.

**Consequence:** a caller with a float has to decide what they meant. That is
the point.

## D4 — Per-currency balancing, no implicit conversion (0.1.0, reaffirmed 0.5.0)

**Decided:** a transaction balances within each currency independently, and the
parser never consults a rate table.

A rate applied at parse time is invisible afterwards: the document says the
entry balanced, and nothing records what rate made it so. Rejected again in
0.5.0 when `fx` landed and the question came back.

## D5 — The chart is optional (0.6.0)

**Decided:** `Ledger.chart` may be `None`; the rules that need it return no
issues rather than raising.

The alternative made every parser test construct a chart. See
`architecture.md` for the "cannot check" versus "is fine" distinction, which is
the one genuine cost.

## D6 — No balance assertions in the format (0.4.0)

**Decided:** no `= 1234.00 USD` syntax.

It is genuinely useful and it puts test logic inside data. A test file can
assert anything the syntax could, with better error messages and no format
change. Raised again in the 2024-Q3 retro and not reopened.

## D7 — Report registry over module `getattr` (0.7.0)

**Decided:** reports are registered by name into a dict.

Looking report functions up with `getattr(reports, name.replace("-", "_"))`
meant every private helper in the module was addressable as a report, and a
typo produced an `AttributeError` from somewhere unhelpful. The registry also
made the deployment-local module possible without a plugin system.

**Consequence:** registration is a side effect of import, so `local_reports`
must be imported for its reports to exist. `reports._load_local` does that
lazily and tolerates the module's absence.

## D8 — `register` replaces rather than raises (0.7.0)

**Decided:** a second registration of the same name wins.

Considered and rejected: raising on a duplicate. Replacement is what makes a
deployment able to override a built-in, which is the only reason the local
module exists. The `origin` field on `ReportSpec` keeps the override visible in
`reports.describe()`.

## D9 — No plugin directory (0.7.0)

**Decided:** one lazily imported module, not a discovery mechanism.

A directory needs discovery, load order, and a policy for a plugin that fails to
import. One import inside a `try` needs none of those and covers the case that
actually occurs.
