"""Flat-CSV import and export.

The flat form has one row per posting and repeats the transaction fields on
every row of a group; consecutive rows sharing ``(date, ref, description)``
belong to the same transaction. It is the format finance teams actually hand
over, so the importer is forgiving about column order and about extra columns.

Export optionally re-denominates every amount into one currency. That path calls
:func:`ledgerline.fx.convert`, which is the ordinary pair-table conversion used
throughout this package.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence, TextIO

from .errors import LedgerError, ParseError
from .fx import RateTable, convert
from .ledger import Ledger
from .model import Posting, Transaction
from .money import Money

POSTING_COLUMNS: tuple[str, ...] = (
    "date",
    "ref",
    "description",
    "tags",
    "account",
    "amount",
    "currency",
)

REQUIRED_COLUMNS: frozenset[str] = frozenset({"date", "account", "amount", "currency"})


def _normalise_header(fieldnames: Sequence[str] | None) -> list[str]:
    if not fieldnames:
        raise ParseError("CSV has no header row")
    header = [str(name).strip().lower() for name in fieldnames]
    missing = REQUIRED_COLUMNS - set(header)
    if missing:
        raise ParseError(f"CSV is missing required column(s): {', '.join(sorted(missing))}")
    return header


def _row_key(row: Mapping[str, str]) -> tuple[str, str, str]:
    return (
        str(row.get("date", "")).strip(),
        str(row.get("ref", "") or "").strip(),
        str(row.get("description", "") or "").strip(),
    )


def _tags(raw: object) -> frozenset[str]:
    text = str(raw or "").replace(";", " ").replace(",", " ")
    return frozenset(part.lstrip("#") for part in text.split() if part.strip())


def read_rows(rows: Iterable[Mapping[str, str]]) -> list[Transaction]:
    """Group flat posting rows into transactions, preserving input order."""
    groups: list[tuple[tuple[str, str, str], list[Mapping[str, str]]]] = []
    for row in rows:
        key = _row_key(row)
        if groups and groups[-1][0] == key:
            groups[-1][1].append(row)
        else:
            groups.append((key, [row]))

    out: list[Transaction] = []
    for (raw_date, ref, description), members in groups:
        try:
            when = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ParseError(f"not a date: {raw_date!r}") from exc
        postings = []
        for index, member in enumerate(members, start=1):
            currency = str(member.get("currency", "")).strip().upper()
            if not currency:
                raise ParseError(f"row {index} of {raw_date} has no currency")
            postings.append(
                Posting(
                    account=str(member["account"]).strip(),
                    amount=Money.parse(str(member["amount"]), currency),
                    tags=_tags(member.get("tags")),
                )
            )
        out.append(
            Transaction(
                date=when,
                description=description or "(imported)",
                postings=tuple(postings),
                ref=ref,
            )
        )
    return out


def read_file(path: str | Path) -> list[Transaction]:
    """Read a flat CSV from disk."""
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        _normalise_header(reader.fieldnames)
        return read_rows([{k.strip().lower(): v for k, v in row.items()} for row in reader])


def read_string(text: str) -> list[Transaction]:
    reader = csv.DictReader(io.StringIO(text))
    _normalise_header(reader.fieldnames)
    return read_rows([{k.strip().lower(): v for k, v in row.items()} for row in reader])


def to_rows(
    ledger: Ledger,
    *,
    currency: str | None = None,
    rates: RateTable | None = None,
) -> list[dict]:
    """Flatten a ledger into posting rows.

    When ``currency`` is given every amount is converted into it with
    :func:`ledgerline.fx.convert`, which needs ``rates``.
    """
    if currency and rates is None:
        raise LedgerError("converting an export needs a rate table")
    target = currency.upper() if currency else None
    rows: list[dict] = []
    for transaction in ledger:
        for posting in transaction.postings:
            amount = posting.amount
            if target and amount.currency != target:
                amount = convert(amount, target, rates)  # type: ignore[arg-type]
            rows.append(
                {
                    "date": transaction.date.isoformat(),
                    "ref": transaction.ref,
                    "description": transaction.description,
                    "tags": " ".join(sorted(posting.tags | transaction.tags)),
                    "account": posting.account,
                    "amount": amount.format(with_currency=False),
                    "currency": amount.currency,
                }
            )
    return rows


def write(
    handle: TextIO,
    ledger: Ledger,
    *,
    currency: str | None = None,
    rates: RateTable | None = None,
) -> int:
    """Write a ledger as flat CSV, returning the number of posting rows."""
    rows = to_rows(ledger, currency=currency, rates=rates)
    writer = csv.DictWriter(handle, fieldnames=list(POSTING_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return len(rows)


def dumps(
    ledger: Ledger, *, currency: str | None = None, rates: RateTable | None = None
) -> str:
    buffer = io.StringIO()
    write(buffer, ledger, currency=currency, rates=rates)
    return buffer.getvalue()
