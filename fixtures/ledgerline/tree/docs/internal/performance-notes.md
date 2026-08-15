# Performance notes

Measured on the largest document anyone has handed this package: 41,000
transactions, 96,000 postings, 11 MB of text. Numbers are from a laptop and are
here for ratios, not for absolutes.

| operation | time |
|---|---|
| parse | 1.9 s |
| build `Ledger` (sorted insert per transaction) | 6.4 s |
| build `Ledger` via `from_transactions` after a bulk sort | 0.4 s |
| `index_by_account` | 0.11 s |
| `trial-balance` | 0.35 s |
| `account-statement` for one leaf | 0.30 s |
| JSON export | 2.1 s |

## The one real problem

`Ledger.add` sorts the whole list on every insert. For 41,000 transactions
that is 41,000 sorts of an almost-sorted list — Timsort makes each one nearly
linear, which is why it is 6.4 s rather than hours, but it is still quadratic in
the number of insertions.

`from_transactions` has the same shape, since it calls `add` in a loop. The fix
is to append without sorting and sort once at the end, with a flag marking the
list dirty. It has not been done because nobody's real document is large enough
for six seconds to matter, and the flag introduces a state where iteration order
is undefined.

If you are the person for whom it matters: sort the input first. An
already-sorted input makes each insert's sort trivial and the whole build drops
to 0.4 s, which is the second row above.

## The things that are not problems

- **Index rebuilds.** A full rebuild of both indexes is 0.11 s on the large
  document, so partial invalidation would buy nothing and cost a state space.
- **Prefix matching for rollups.** `is_under` is two string operations. Building
  a tree structure to avoid them was tried and was slower, because the tree had
  to be rebuilt on every filter.
- **`Fraction` arithmetic in `fx`.** Fractions are slow in absolute terms and
  irrelevant here: a report performs one conversion per row, not per posting.
- **String rows in reports.** Formatting 41,000 amounts is 0.2 s of the 0.35 s
  trial balance. It is the largest single component and it is still nothing.

## Memory

The large document occupies about 210 MB as `Transaction` objects, roughly 20x
its text size. Frozen dataclasses with `frozenset` tags are not compact. If it
ever matters, `__slots__` on `Posting` is the first thing to try; it was
measured at a 25% reduction and was not kept, because `slots=True` on a frozen
dataclass interacts badly with the `object.__setattr__` normalisation in
`__post_init__`.
