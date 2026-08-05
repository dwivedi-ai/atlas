# Data model

## The records

```
Money        (minor: int, currency: str)                       frozen
Posting      (account, amount: Money, tags, note, line_no)     frozen
Transaction  (date, description, postings, tags, ref, line_no) frozen
Account      (code, name, currency, closed)                    frozen
Period       (start, end, kind, label)                         frozen
Ledger       (transactions, chart, name)                       mutable
RateTable    (pairs, label)                                    mutable
```

Everything below `Ledger` is frozen. That is not decoration: a `Ledger.filter`
returns a new ledger *sharing the same transaction objects*, which is only safe
because nothing can mutate one. Filtering is therefore O(n) in pointers rather
than in deep copies, and a report that filters three times costs nothing.

`Ledger` is mutable because it is built incrementally, and `RateTable` because
it is loaded row by row. Neither is shared across a filter.

## Identity and equality

`Money` is a frozen ordered dataclass, so equality is structural and two
`Money(100, "USD")` are equal and interchangeable. `Transaction` equality is
also structural, which is what makes the parser round-trip test
(`test_parser.py::test_dump_round_trips`) able to compare dictionaries rather
than objects — it compares `to_dict()` output because a `Posting` carries
`line_no`, which legitimately differs after a round trip.

That `line_no` is the one field in the model that is about provenance rather
than about accounting. It exists so a validation issue can point at a line, and
it is excluded from every comparison that matters by going through `to_dict`.

## Tags

Tags are `frozenset[str]` and are normalised on construction: a leading `#` is
stripped, so `#payroll` and `payroll` are the same tag. They live on both
transactions and postings, and `Transaction.all_tags()` unions them.

Nothing validates a tag. There is no controlled vocabulary and no plan for one;
the moment tags are validated, they stop being the place people put things that
do not fit anywhere else, which is their entire value.

## Currencies in a transaction

A transaction's postings may span currencies. `sums()` returns the net per
currency and `is_balanced` requires every one of them to be zero. So a
four-posting transaction with two USD legs and two EUR legs is *two* independent
balancing problems that share a date and a description.

The elided-amount rule interacts with this: the elided posting balances the
currency of the first priced posting in the transaction, so a mixed transaction
with an elision is order-dependent. The sample data avoids the situation
entirely by splitting per currency, and `docs/ledger-format.md` says so.

## What is not modelled

- **No account object on a posting.** A posting carries an account *code*, a
  string. Resolving it against a chart is validation's job. This keeps the
  parser independent of the chart and makes a posting to an undeclared account
  representable, which is necessary for the validator to be able to report it.
- **No transaction identity.** `ref` is a label, not a key: it is optional, and
  duplicates are a warning. Nothing in the package indexes by it.
- **No amounts on transactions.** A transaction's "amount" is ambiguous the
  moment there are more than two postings. Ask for `amount_for(account)`.
- **No currency object.** A currency is a string plus an entry in
  `money.EXPONENTS`. A class would carry a name and a symbol nobody needs.
