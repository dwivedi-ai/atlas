"""The :class:`Ledger` container: transactions plus the indexes reports need.

A ledger owns its transactions in date order and keeps two derived indexes — by
account code and by month — which are invalidated on every mutation and rebuilt
lazily. Everything else in the package reads a ledger and writes nothing back.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from .accounts import ChartOfAccounts, is_under
from .errors import LedgerError
from .model import Posting, Transaction
from .money import Money, split_by_currency
from .periods import Period


@dataclass
class Ledger:
    """An ordered collection of balanced transactions."""

    transactions: list[Transaction] = field(default_factory=list)
    chart: ChartOfAccounts | None = None
    name: str = "ledger"

    _by_account: dict[str, list[int]] | None = field(default=None, repr=False, compare=False)
    _by_month: dict[str, list[int]] | None = field(default=None, repr=False, compare=False)

    # -- construction --------------------------------------------------------
    @classmethod
    def from_transactions(
        cls, transactions: Iterable[Transaction], chart: ChartOfAccounts | None = None
    ) -> "Ledger":
        ledger = cls(chart=chart)
        for transaction in transactions:
            ledger.add(transaction)
        return ledger

    @classmethod
    def load(cls, path: str | Path, chart: ChartOfAccounts | None = None) -> "Ledger":
        from .parser import parse_file  # local import keeps the module graph acyclic

        ledger = cls.from_transactions(parse_file(path), chart)
        ledger.name = Path(path).stem
        return ledger

    def add(self, transaction: Transaction) -> "Ledger":
        transaction.require_balanced()
        self.transactions.append(transaction)
        self.transactions.sort(key=lambda t: (t.date, t.line_no))
        self._invalidate()
        return self

    def extend(self, transactions: Iterable[Transaction]) -> "Ledger":
        for transaction in transactions:
            self.add(transaction)
        return self

    def _invalidate(self) -> None:
        self._by_account = None
        self._by_month = None

    # -- container protocol --------------------------------------------------
    def __len__(self) -> int:
        return len(self.transactions)

    def __iter__(self) -> Iterator[Transaction]:
        return iter(self.transactions)

    def __getitem__(self, index: int) -> Transaction:
        return self.transactions[index]

    @property
    def is_empty(self) -> bool:
        return not self.transactions

    # -- indexes -------------------------------------------------------------
    def index_by_account(self) -> dict[str, list[int]]:
        if self._by_account is None:
            index: dict[str, list[int]] = OrderedDict()
            for position, transaction in enumerate(self.transactions):
                for account in transaction.accounts():
                    index.setdefault(account, []).append(position)
            self._by_account = index
        return self._by_account

    def index_by_month(self) -> dict[str, list[int]]:
        if self._by_month is None:
            index: dict[str, list[int]] = OrderedDict()
            for position, transaction in enumerate(self.transactions):
                key = f"{transaction.date.year}-{transaction.date.month:02d}"
                index.setdefault(key, []).append(position)
            self._by_month = index
        return self._by_month

    # -- queries -------------------------------------------------------------
    def accounts_used(self) -> list[str]:
        return sorted(self.index_by_account())

    def currencies(self) -> list[str]:
        seen: list[str] = []
        for transaction in self.transactions:
            for currency in transaction.currencies():
                if currency not in seen:
                    seen.append(currency)
        return sorted(seen)

    def date_range(self) -> tuple[date, date] | None:
        if not self.transactions:
            return None
        return self.transactions[0].date, self.transactions[-1].date

    def filter(self, predicate: Callable[[Transaction], bool]) -> "Ledger":
        """A new ledger holding the transactions matching ``predicate``."""
        out = Ledger(chart=self.chart, name=self.name)
        out.transactions = [t for t in self.transactions if predicate(t)]
        return out

    def in_period(self, period: Period | None) -> "Ledger":
        if period is None:
            return self
        return self.filter(lambda t: period.contains(t.date))

    def with_tag(self, tag: str) -> "Ledger":
        return self.filter(lambda t: t.has_tag(tag))

    def without_tag(self, tag: str) -> "Ledger":
        return self.filter(lambda t: not t.has_tag(tag))

    def postings(self, account: str | None = None, *, include_children: bool = True) -> list[Posting]:
        """Every posting, optionally restricted to one account subtree."""
        out: list[Posting] = []
        for transaction in self.transactions:
            for posting in transaction.postings:
                if account is None:
                    out.append(posting)
                elif include_children and is_under(posting.account, account):
                    out.append(posting)
                elif not include_children and posting.account == account:
                    out.append(posting)
        return out

    # -- balances ------------------------------------------------------------
    def balance(
        self,
        account: str,
        *,
        period: Period | None = None,
        include_children: bool = True,
        currency: str | None = None,
    ) -> Money:
        """Net balance of ``account`` over ``period``.

        Raises when the subtree mixes currencies and no ``currency`` was named —
        silently picking one would be a reporting bug that nobody notices until
        the numbers are wrong.
        """
        scope = self.in_period(period)
        amounts = [p.amount for p in scope.postings(account, include_children=include_children)]
        if currency is not None:
            amounts = [a for a in amounts if a.currency == currency.upper()]
        if not amounts:
            return Money.zero(currency or self._default_currency(account))
        sums = split_by_currency(amounts)
        if len(sums) > 1:
            raise LedgerError(
                f"{account} holds {sorted(sums)} — pass currency= to pick one, "
                "or convert with ledgerline.fx first"
            )
        return next(iter(sums.values()))

    def balances_by_currency(
        self, account: str, *, period: Period | None = None, include_children: bool = True
    ) -> dict[str, Money]:
        scope = self.in_period(period)
        return split_by_currency(
            p.amount for p in scope.postings(account, include_children=include_children)
        )

    def balances(
        self, *, period: Period | None = None, leaves_only: bool = True
    ) -> "OrderedDict[str, dict[str, Money]]":
        """Balance of every account that appears in the ledger, per currency."""
        scope = self.in_period(period)
        out: "OrderedDict[str, dict[str, Money]]" = OrderedDict()
        for account in scope.accounts_used():
            out[account] = scope.balances_by_currency(
                account, include_children=not leaves_only
            )
        return out

    def running_balance(
        self, account: str, *, period: Period | None = None, currency: str | None = None
    ) -> list[tuple[date, Money]]:
        """Cumulative balance of ``account`` after each transaction touching it."""
        scope = self.in_period(period)
        target = (currency or self._default_currency(account)).upper()
        running = Money.zero(target)
        out: list[tuple[date, Money]] = []
        for transaction in scope.transactions:
            delta = [
                p.amount
                for p in transaction.postings
                if is_under(p.account, account) and p.amount.currency == target
            ]
            if not delta:
                continue
            for amount in delta:
                running = running + amount
            out.append((transaction.date, running))
        return out

    def _default_currency(self, account: str) -> str:
        """Best guess at the currency ``account`` is kept in.

        The chart wins when it knows the account; otherwise the account's own
        postings decide, and only a ledger with no postings at all falls back to
        the ledger-wide list. Taking the ledger-wide answer first was a real bug:
        an account statement for a USD account came out empty because the ledger
        happened to mention EUR first.
        """
        if self.chart is not None and account in self.chart:
            return self.chart.get(account).currency
        own = self.postings(account)
        if own:
            counts: dict[str, int] = {}
            for posting in own:
                counts[posting.currency] = counts.get(posting.currency, 0) + 1
            return max(counts, key=lambda code: (counts[code], code))
        currencies = self.currencies()
        return currencies[0] if currencies else "USD"

    # -- serialisation -------------------------------------------------------
    def to_dict(self) -> dict:
        span = self.date_range()
        return {
            "name": self.name,
            "count": len(self.transactions),
            "start": span[0].isoformat() if span else None,
            "end": span[1].isoformat() if span else None,
            "currencies": self.currencies(),
            "transactions": [t.to_dict() for t in self.transactions],
        }


def merge(ledgers: Sequence[Ledger], name: str = "merged") -> Ledger:
    """Concatenate ledgers, keeping date order and the first non-null chart."""
    chart = next((led.chart for led in ledgers if led.chart is not None), None)
    out = Ledger(chart=chart, name=name)
    for led in ledgers:
        out.extend(led.transactions)
    return out
