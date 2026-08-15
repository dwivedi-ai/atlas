"""Reader for the ``.ledger`` text format.

The format is line oriented and indentation sensitive::

    ; a comment
    2024-10-01 Opening balances  #opening  ^INV-1
        assets.cash.checking        1500.00 USD
        equity.opening

A transaction header starts in column 0 with an ISO date. Postings are indented
by at least one space and carry ``account amount currency``. Exactly one posting
per transaction may omit its amount, in which case it takes whatever balances
the entry. Tags start with ``#`` and may appear on a header or on a posting; a
reference starts with ``^`` and may only appear on a header.

Every failure raises :class:`~ledgerline.errors.ParseError` carrying the 1-based
line number, because a ledger with a typo on line 400 is otherwise unpleasant.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .errors import LedgerError, ParseError
from .model import Posting, Transaction, TransactionBuilder, elide_balance
from .money import Money

#: Stand-in stored on a posting whose amount was elided; replaced before the
#: transaction is built, so it never escapes the parser.
_PLACEHOLDER = Money(0, "USD")

COMMENT_PREFIXES = (";", "//")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\b")
TAG_RE = re.compile(r"(?<!\S)#([A-Za-z0-9][A-Za-z0-9_-]*)")
REF_RE = re.compile(r"(?<!\S)\^(\S+)")
AMOUNT_RE = re.compile(
    r"""^(?P<amount>[-+(]?\s*[\d][\d,_.]*\)?)\s+(?P<currency>[A-Za-z]{3,4})$"""
)


def _strip_comment(line: str) -> str:
    """Remove a trailing comment introduced by ``;`` or ``//``."""
    cut = len(line)
    for prefix in COMMENT_PREFIXES:
        idx = line.find(prefix)
        if idx != -1:
            cut = min(cut, idx)
    return line[:cut].rstrip()


def _parse_date(text: str, line_no: int) -> date:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ParseError(f"not a date: {text!r}", line_no, text) from exc


def _extract_tags(text: str) -> tuple[str, set[str]]:
    tags = {m.group(1) for m in TAG_RE.finditer(text)}
    return TAG_RE.sub("", text).strip(), tags


def _extract_ref(text: str) -> tuple[str, str]:
    match = REF_RE.search(text)
    if not match:
        return text, ""
    return REF_RE.sub("", text).strip(), match.group(1)


class LedgerParser:
    """Stateful line-by-line reader. One instance per document."""

    def __init__(self, source: str, *, filename: str = "<string>") -> None:
        self.source = source
        self.filename = filename
        self._builder: TransactionBuilder | None = None
        self._blank_index: int | None = None
        self._blank_currency: str | None = None

    # -- public API ----------------------------------------------------------
    def parse(self) -> list[Transaction]:
        return list(self.iter_transactions())

    def iter_transactions(self) -> Iterator[Transaction]:
        for line_no, raw in enumerate(self.source.splitlines(), start=1):
            line = _strip_comment(raw)
            if not line.strip():
                continue
            if line[0].isspace():
                self._posting_line(line, line_no)
            else:
                finished = self._finish()
                if finished is not None:
                    yield finished
                self._header_line(line, line_no)
        finished = self._finish()
        if finished is not None:
            yield finished

    # -- internals -----------------------------------------------------------
    def _header_line(self, line: str, line_no: int) -> None:
        match = DATE_RE.match(line)
        if not match:
            raise ParseError("expected a transaction header starting with a date", line_no, line)
        when = _parse_date(match.group(0), line_no)
        rest = line[match.end() :].strip()
        rest, ref = _extract_ref(rest)
        description, tags = _extract_tags(rest)
        if not description:
            raise ParseError("transaction header needs a description", line_no, line)
        self._builder = TransactionBuilder(
            date=when, description=description, tags=tags, ref=ref, line_no=line_no
        )
        self._blank_index = None
        self._blank_currency = None

    def _posting_line(self, line: str, line_no: int) -> None:
        if self._builder is None:
            raise ParseError("posting appears before any transaction header", line_no, line)
        body, tags = _extract_tags(line.strip())
        parts = body.split()
        if not parts:
            raise ParseError("empty posting", line_no, line)
        account = parts[0]
        remainder = " ".join(parts[1:]).strip()
        if not remainder:
            if self._blank_index is not None:
                raise ParseError(
                    "only one posting per transaction may omit its amount", line_no, line
                )
            self._blank_index = len(self._builder.postings)
            self._builder.add(
                Posting(account=account, amount=_PLACEHOLDER, tags=frozenset(tags), line_no=line_no)
            )
            return
        amount_match = AMOUNT_RE.match(remainder)
        if not amount_match:
            raise ParseError(f"cannot read an amount from {remainder!r}", line_no, line)
        currency = amount_match.group("currency").upper()
        try:
            amount = _money(amount_match.group("amount"), currency)
        except LedgerError as exc:
            raise ParseError(str(exc), line_no, line) from exc
        if self._blank_currency is None:
            self._blank_currency = currency
        self._builder.add(
            Posting(account=account, amount=amount, tags=frozenset(tags), line_no=line_no)
        )

    def _finish(self) -> Transaction | None:
        builder, self._builder = self._builder, None
        if builder is None:
            return None
        if not builder.postings:
            raise ParseError(
                f"transaction {builder.description!r} has no postings", builder.line_no, ""
            )
        if self._blank_index is not None:
            if self._blank_currency is None:
                raise ParseError(
                    "an elided amount needs at least one priced posting",
                    builder.line_no,
                    builder.description,
                )
            try:
                filled = elide_balance(
                    builder.postings, self._blank_index, self._blank_currency
                )
            except LedgerError as exc:
                raise ParseError(str(exc), builder.line_no, builder.description) from exc
            builder.postings[self._blank_index] = filled
        transaction = builder.build()
        if not transaction.is_balanced:
            residual = ", ".join(m.format() for m in transaction.sums().values() if m.minor)
            raise ParseError(
                f"transaction {transaction.description!r} does not balance ({residual})",
                builder.line_no,
                builder.description,
            )
        return transaction


def _money(text: str, currency: str) -> Money:
    return Money.parse(text, currency)


def parse_string(source: str, *, filename: str = "<string>") -> list[Transaction]:
    """Parse a ledger document held in memory."""
    return LedgerParser(source, filename=filename).parse()


def parse_file(path: str | Path) -> list[Transaction]:
    """Parse a ledger document from disk."""
    path = Path(path)
    return LedgerParser(path.read_text(encoding="utf-8"), filename=str(path)).parse()


def parse_files(paths: Iterable[str | Path]) -> list[Transaction]:
    """Parse several documents and return them in date order."""
    out: list[Transaction] = []
    for path in paths:
        out.extend(parse_file(path))
    return sorted(out, key=lambda t: (t.date, t.line_no))


def dump_string(transactions: Sequence[Transaction]) -> str:
    """Render transactions back into the text format.

    Round-tripping is exact for documents this package produced: amounts are
    written in major units with the currency's own exponent, and no amount is
    ever elided on the way out.
    """
    chunks: list[str] = []
    for transaction in transactions:
        header = f"{transaction.date.isoformat()} {transaction.description}"
        if transaction.tags:
            header += "  " + " ".join(f"#{t}" for t in sorted(transaction.tags))
        if transaction.ref:
            header += f"  ^{transaction.ref}"
        lines = [header]
        width = max(len(p.account) for p in transaction.postings) + 4
        for posting in transaction.postings:
            amount = posting.amount.format(with_currency=False)
            line = f"    {posting.account.ljust(width)}{amount} {posting.amount.currency}"
            if posting.tags:
                line += "  " + " ".join(f"#{t}" for t in sorted(posting.tags))
            lines.append(line.rstrip())
        chunks.append("\n".join(lines))
    return "\n\n".join(chunks) + "\n" if chunks else ""
