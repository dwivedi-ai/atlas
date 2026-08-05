"""Postings and transactions — the two records the whole package moves around.

A :class:`Transaction` owns an ordered list of :class:`Posting`. Double entry is
enforced per currency: the postings of one transaction must sum to zero in every
currency they mention. Nothing here touches the chart of accounts; account codes
are plain strings until :mod:`ledgerline.validate` resolves them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Sequence

from .errors import LedgerError, UnbalancedTransaction
from .money import Money, split_by_currency


@dataclass(frozen=True)
class Posting:
    """One leg of a transaction."""

    account: str
    amount: Money
    tags: frozenset[str] = frozenset()
    note: str = ""
    line_no: int = 0

    def __post_init__(self) -> None:
        if not str(self.account).strip():
            raise LedgerError("posting account must be non-empty")
        object.__setattr__(self, "account", str(self.account).strip())
        object.__setattr__(self, "tags", frozenset(str(t).lstrip("#") for t in self.tags))

    @property
    def currency(self) -> str:
        return self.amount.currency

    @property
    def is_debit(self) -> bool:
        return self.amount.minor > 0

    @property
    def is_credit(self) -> bool:
        return self.amount.minor < 0

    def with_amount(self, amount: Money) -> "Posting":
        return replace(self, amount=amount)

    def to_dict(self) -> dict:
        return {
            "account": self.account,
            "amount": str(self.amount.units),
            "currency": self.amount.currency,
            "minor": self.amount.minor,
            "tags": sorted(self.tags),
            "note": self.note,
        }


@dataclass(frozen=True)
class Transaction:
    """A dated, balanced group of postings."""

    date: date
    description: str
    postings: tuple[Posting, ...] = ()
    tags: frozenset[str] = frozenset()
    ref: str = ""
    line_no: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "postings", tuple(self.postings))
        object.__setattr__(self, "tags", frozenset(str(t).lstrip("#") for t in self.tags))
        object.__setattr__(self, "description", str(self.description).strip())
        if not self.postings:
            raise LedgerError(f"transaction {self.description!r} has no postings")

    # -- balance -------------------------------------------------------------
    def currencies(self) -> list[str]:
        seen: list[str] = []
        for posting in self.postings:
            if posting.currency not in seen:
                seen.append(posting.currency)
        return seen

    def sums(self) -> dict[str, Money]:
        """Net amount per currency; every value is zero in a balanced entry."""
        return split_by_currency(p.amount for p in self.postings)

    @property
    def is_balanced(self) -> bool:
        return all(amount.minor == 0 for amount in self.sums().values())

    def require_balanced(self) -> "Transaction":
        if not self.is_balanced:
            residual = ", ".join(
                m.format() for m in self.sums().values() if m.minor
            )
            raise UnbalancedTransaction(
                f"{self.date.isoformat()} {self.description!r} does not balance: {residual}"
            )
        return self

    # -- queries -------------------------------------------------------------
    def accounts(self) -> list[str]:
        seen: list[str] = []
        for posting in self.postings:
            if posting.account not in seen:
                seen.append(posting.account)
        return seen

    def amount_for(self, account: str, *, include_children: bool = False) -> Money | None:
        """Net amount posted to ``account`` in this transaction."""
        matched = [
            p
            for p in self.postings
            if p.account == account
            or (include_children and p.account.startswith(account + "."))
        ]
        if not matched:
            return None
        sums = split_by_currency(p.amount for p in matched)
        if len(sums) > 1:
            raise LedgerError(
                f"{account} has postings in {len(sums)} currencies in one transaction"
            )
        return next(iter(sums.values()))

    def has_tag(self, tag: str) -> bool:
        tag = tag.lstrip("#")
        return tag in self.tags or any(tag in p.tags for p in self.postings)

    def all_tags(self) -> set[str]:
        out = set(self.tags)
        for posting in self.postings:
            out |= set(posting.tags)
        return out

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "description": self.description,
            "ref": self.ref,
            "tags": sorted(self.tags),
            "postings": [p.to_dict() for p in self.postings],
        }


def elide_balance(postings: Sequence[Posting], blank_index: int, currency: str) -> Posting:
    """Return the posting that balances ``postings`` when one amount was elided.

    The text format lets exactly one posting omit its amount; the parser calls
    this to fill it in. Balancing is per currency, so an elided posting in a
    multi-currency transaction balances only the currency it was declared with.
    """
    others = [p for i, p in enumerate(postings) if i != blank_index]
    residual = split_by_currency(p.amount for p in others).get(currency)
    if residual is None:
        raise LedgerError(f"cannot infer an amount in {currency}: no other posting uses it")
    return postings[blank_index].with_amount(-residual)


@dataclass
class TransactionBuilder:
    """Incremental construction used by the parser and by the CSV importer."""

    date: date
    description: str
    tags: set[str] = field(default_factory=set)
    ref: str = ""
    line_no: int = 0
    postings: list[Posting] = field(default_factory=list)

    def add(self, posting: Posting) -> "TransactionBuilder":
        self.postings.append(posting)
        return self

    def build(self) -> Transaction:
        return Transaction(
            date=self.date,
            description=self.description,
            postings=tuple(self.postings),
            tags=frozenset(self.tags),
            ref=self.ref,
            line_no=self.line_no,
        )
