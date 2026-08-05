`ledgerline export --format json` emits a bare JSON array of transactions. A
consumer receiving two exports of overlapping date ranges has nothing to
deduplicate on. Wrap the payload in an envelope.

The JSON export becomes a single object with these four keys:

- `schema` — a string identifying the envelope shape.
- `generated_at` — an ISO-8601 UTC timestamp of when the export was produced.
- `source_system` — a non-empty string identifying the producer of the export.
- `transactions` — the array that is emitted today, unchanged.

`jsonio.loads` already accepts either a bare array or an object carrying the
payload under a `transactions` key, so the reader half needs no work. The tests
that pin the current bare-array shape are part of the contract you are changing
and must be updated with it; `python3 -m pytest` must pass when you are done.

    python3 -m ledgerline.cli export tests/data/sample.ledger --format json

The CSV export is unaffected.
