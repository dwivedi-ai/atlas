# Migration log

Breaking changes, in the order consumers hit them. Each entry is what a consumer
had to do, not what we did.

## 0.7.0 — report functions take `(ledger, options)`

**Was:**

```python
report = reports.trial_balance(ledger, period=period, currency="USD")
```

**Now:**

```python
report = reports.run("trial-balance", ledger, ReportOptions(period=period, currency="USD"))
```

Calling the function directly still works, but the second argument is a
`ReportOptions` and is no longer optional in practice — every built-in reads at
least `options.period`.

The registry is the intended entry point. Direct calls skip the
`local_reports` load, so a name a deployment has overridden will silently
resolve to the built-in.

## 0.7.0 — report values moved to `meta`

Anything that parsed a rendered column to get at a number should read `meta`
instead. The columns are still there and still strings; they are no longer a
stable interface. `meta` is.

Concretely: `trial-balance` gained `residual`, `currencies` and `converted`;
`account-statement` gained `closing`; `income-statement` gained `net`.

## 0.6.0 — `Ledger(transactions)` became `Ledger.from_transactions(...)`

The bare constructor now takes keyword fields (`transactions`, `chart`, `name`),
which means the positional form silently did the wrong thing for exactly one
release. `from_transactions` also validates each transaction as it inserts,
which the old positional constructor did not.

If you were relying on the old constructor to build a ledger containing an
unbalanced transaction — some consumers were, to feed the validator — build the
list and hand it to the validator directly; `Ledger` will not hold one.

## 0.5.0 — `fx` arrived; nothing was implicit before or after

No migration, listed because people expect one. Conversion was never implicit
and did not become implicit. If your 0.4.x code summed across currencies, it was
already wrong and the 0.5.0 `CurrencyMismatch` is the first time anything said
so.

## 0.4.0 — a posting may omit its amount

Documents written for 0.3.x parse unchanged. Documents written for 0.4.0 do not
parse under 0.3.x. This bit one consumer who had pinned the library and not the
documents.
