"""JSON serialisation of a ledger.

The current output is the bare payload: a JSON array of transaction objects, in
ledger order. Amounts are emitted twice — once as a decimal string in major
units for humans, once as an integer count of minor units for machines — so a
consumer never has to know a currency's exponent to round-trip a value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence, TextIO

from .ledger import Ledger
from .model import Transaction

#: Bumped whenever the shape of a transaction object changes.
PAYLOAD_VERSION = "3"

JSON_INDENT = 2


def transaction_to_jsonable(transaction: Transaction) -> dict:
    """One transaction as plain JSON-able data."""
    return transaction.to_dict()


def to_jsonable(ledger: Ledger) -> list[dict]:
    """The whole ledger as a list of transaction objects."""
    return [transaction_to_jsonable(t) for t in ledger]


def dumps(ledger: Ledger, *, indent: int | None = JSON_INDENT) -> str:
    """Serialise a ledger to a JSON string."""
    return json.dumps(to_jsonable(ledger), indent=indent, sort_keys=False) + "\n"


def dump(ledger: Ledger, handle: TextIO, *, indent: int | None = JSON_INDENT) -> int:
    text = dumps(ledger, indent=indent)
    handle.write(text)
    return len(text)


def write_file(ledger: Ledger, path: str | Path, *, indent: int | None = JSON_INDENT) -> Path:
    path = Path(path)
    path.write_text(dumps(ledger, indent=indent), encoding="utf-8")
    return path


def loads(text: str) -> list[dict]:
    """Read back whatever :func:`dumps` wrote, without rebuilding a Ledger.

    Accepts either a bare array or an object carrying the payload under a
    ``transactions`` key, so a consumer that has already been given a wrapped
    document keeps working.
    """
    data: Any = json.loads(text)
    if isinstance(data, list):
        return list(data)
    if isinstance(data, dict):
        payload = data.get("transactions")
        if isinstance(payload, list):
            return list(payload)
    raise ValueError("not a ledgerline JSON document")


def summarise(payload: Sequence[dict]) -> dict:
    """Cheap statistics over a decoded payload, used by the CLI's export summary."""
    dates = [str(item.get("date", "")) for item in payload if item.get("date")]
    currencies = sorted(
        {
            str(posting.get("currency", ""))
            for item in payload
            for posting in item.get("postings", [])
            if posting.get("currency")
        }
    )
    return {
        "payload_version": PAYLOAD_VERSION,
        "count": len(payload),
        "first_date": min(dates) if dates else None,
        "last_date": max(dates) if dates else None,
        "currencies": currencies,
    }
