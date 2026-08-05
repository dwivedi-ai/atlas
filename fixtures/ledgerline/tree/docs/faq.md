# Frequently asked questions

### Why integers instead of `Decimal`?

`Decimal` is exact, but it is exact at whatever precision the context happens to
carry, and the context is global mutable state. An integer count of minor units
has no context, no precision to get wrong, and no way to accidentally compare
`Decimal("1.10")` against `Decimal("1.1")` and get a surprise. `Decimal` still
appears in one place — `Money.parse` — where it does what it is genuinely good
at, turning a decimal string into an exact ratio.

### Why does `Ledger.balance("assets")` raise?

Because the subtree holds USD, EUR and GBP, and there is no correct single
answer. Returning one currency's total would be silently wrong; converting would
require a rate table the ledger does not have and a date it cannot choose. Pass
`currency=` to pick a side, or convert explicitly.

### Why does the trial balance not convert?

The same reason. Conversion needs a rate table and a policy about which rate,
and neither belongs to the ledger. `--rates` is plumbed through the CLI into
`ReportOptions.rates` so a converting report has somewhere to get its table
from; no built-in report reads it yet.

### Why is the rate table sparse?

Because real vendor feeds are. Quoting every ordered pair among *n* currencies
means *n(n-1)* numbers that all have to stay consistent with each other, and
they do not. `docs/fx.md` covers what that costs and what to do about it.

### Why is `local_reports.py` in the package rather than a plugin directory?

Because a plugin directory needs a discovery mechanism, a load order, and an
error policy for a plugin that fails to import — four decisions to buy something
one lazy import already does. `reports._load_local` imports one module inside a
`try` and moves on if it is not there.

### Can I have two facts about the same account in one transaction?

Yes. Nothing stops several postings naming the same account; they simply net.
`Transaction.amount_for` returns the net, and raises only if the same account
was posted in two currencies within one transaction.

### Why does a duplicate reference warn instead of erroring?

Because re-invoicing under one reference is a real practice, and a validator
that refuses to load an otherwise fine document over a bookkeeping convention
gets switched off. The warning is there; the decision is yours.

### Why does the parser reject an empty description but not an empty tag list?

A transaction nobody can identify is unusable in every report; a transaction with
no tags is the common case. Errors are for things that make downstream work
impossible, warnings for things that make it harder.

### Is there a way to assert a balance inside a ledger document?

No. Other ledger formats have a `= 1234.00 USD` assertion syntax, and it is
genuinely useful, but it puts test logic inside data. Write a test.

### How do I get at the numbers behind a rendered report?

Use `--json`, or call `reports.run` directly and read `report.meta`. The rendered
columns are strings and are allowed to change; `meta` is the stable surface.

### Why is `python3 -m ledgerline.cli` so long?

Because the package is deliberately not installed during development, so there
is no console script on `PATH`. Alias it.
