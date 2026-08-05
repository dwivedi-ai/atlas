"""The chart of accounts: dotted codes, account types, and normal balances.

Account codes are dotted paths — ``assets.cash.checking`` — so the hierarchy is
carried by the code itself and no separate parent pointer has to be kept in
sync. The first segment determines the account type, which in turn determines
the *normal balance*: assets and expenses increase on the debit side, everything
else on the credit side.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .errors import DuplicateAccount, LedgerError, UnknownAccount

ASSET = "asset"
LIABILITY = "liability"
EQUITY = "equity"
INCOME = "income"
EXPENSE = "expense"

ACCOUNT_TYPES: tuple[str, ...] = (ASSET, LIABILITY, EQUITY, INCOME, EXPENSE)

#: First code segment -> account type. The plural forms are what the text format
#: uses; the singular forms are the canonical type names.
ROOT_TYPES: dict[str, str] = {
    "assets": ASSET,
    "liabilities": LIABILITY,
    "equity": EQUITY,
    "income": INCOME,
    "revenue": INCOME,
    "expenses": EXPENSE,
}

#: +1 when a debit increases the account, -1 when a credit does.
NORMAL_BALANCE: dict[str, int] = {
    ASSET: 1,
    EXPENSE: 1,
    LIABILITY: -1,
    EQUITY: -1,
    INCOME: -1,
}

#: Types that appear on the balance sheet rather than the income statement.
BALANCE_SHEET_TYPES: tuple[str, ...] = (ASSET, LIABILITY, EQUITY)
INCOME_STATEMENT_TYPES: tuple[str, ...] = (INCOME, EXPENSE)


def type_of(code: str) -> str:
    """Account type implied by the first segment of ``code``."""
    root = str(code).split(".", 1)[0].strip().lower()
    try:
        return ROOT_TYPES[root]
    except KeyError:
        raise LedgerError(
            f"account code {code!r} does not start with a known root "
            f"({', '.join(sorted(ROOT_TYPES))})"
        ) from None


def parents_of(code: str) -> list[str]:
    """Every ancestor code of ``code``, shallowest first, excluding itself."""
    parts = str(code).split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts) - 1)]


def is_under(code: str, ancestor: str) -> bool:
    """True when ``code`` is ``ancestor`` or sits beneath it."""
    return code == ancestor or code.startswith(ancestor + ".")


@dataclass(frozen=True)
class Account:
    """One account in the chart."""

    code: str
    name: str = ""
    currency: str = "USD"
    closed: bool = False

    def __post_init__(self) -> None:
        code = str(self.code).strip()
        if not code:
            raise LedgerError("account code must be non-empty")
        for part in code.split("."):
            if not part or not all(ch.isalnum() or ch in "-_" for ch in part):
                raise LedgerError(f"malformed account code: {self.code!r}")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "name", str(self.name) or code.rsplit(".", 1)[-1])
        object.__setattr__(self, "currency", str(self.currency).upper())
        type_of(code)  # raises early on an unknown root

    @property
    def type(self) -> str:
        return type_of(self.code)

    @property
    def normal_balance(self) -> int:
        return NORMAL_BALANCE[self.type]

    @property
    def depth(self) -> int:
        return self.code.count(".")

    @property
    def leaf(self) -> str:
        return self.code.rsplit(".", 1)[-1]


@dataclass
class ChartOfAccounts:
    """An ordered collection of :class:`Account` keyed by code."""

    accounts: dict[str, Account] = field(default_factory=dict)

    # -- construction --------------------------------------------------------
    @classmethod
    def from_rows(cls, rows: Iterable[dict]) -> "ChartOfAccounts":
        chart = cls()
        for row in rows:
            chart.add(
                Account(
                    code=row["code"],
                    name=row.get("name", ""),
                    currency=row.get("currency") or "USD",
                    closed=str(row.get("closed", "")).strip().lower() in ("1", "true", "yes"),
                )
            )
        return chart

    @classmethod
    def load(cls, path: str | Path) -> "ChartOfAccounts":
        """Read a chart from a CSV with ``code,name,currency[,closed]`` columns."""
        with Path(path).open(newline="", encoding="utf-8") as handle:
            return cls.from_rows(csv.DictReader(handle))

    def add(self, account: Account) -> Account:
        if account.code in self.accounts:
            raise DuplicateAccount(f"account declared twice: {account.code}")
        self.accounts[account.code] = account
        return account

    # -- lookup --------------------------------------------------------------
    def __contains__(self, code: object) -> bool:
        return str(code) in self.accounts

    def __len__(self) -> int:
        return len(self.accounts)

    def __iter__(self) -> Iterator[Account]:
        return iter(self.sorted())

    def get(self, code: str) -> Account:
        try:
            return self.accounts[code]
        except KeyError:
            raise UnknownAccount(f"no such account: {code}") from None

    def sorted(self) -> list[Account]:
        return [self.accounts[c] for c in sorted(self.accounts)]

    def codes(self) -> list[str]:
        return sorted(self.accounts)

    def of_type(self, account_type: str) -> list[Account]:
        return [a for a in self.sorted() if a.type == account_type]

    def children(self, code: str) -> list[Account]:
        """Direct children of ``code``."""
        prefix = code + "."
        return [
            a
            for a in self.sorted()
            if a.code.startswith(prefix) and "." not in a.code[len(prefix) :]
        ]

    def descendants(self, code: str) -> list[Account]:
        return [a for a in self.sorted() if is_under(a.code, code) and a.code != code]

    def roots(self) -> list[Account]:
        return [a for a in self.sorted() if "." not in a.code]

    def open_accounts(self) -> list[Account]:
        return [a for a in self.sorted() if not a.closed]

    def missing(self, codes: Iterable[str]) -> list[str]:
        """Codes from ``codes`` that are not in this chart, de-duplicated."""
        seen: list[str] = []
        for code in codes:
            if code not in self.accounts and code not in seen:
                seen.append(code)
        return seen

    def implied_parents(self) -> list[str]:
        """Ancestor codes referenced by the chart but never declared."""
        out: list[str] = []
        for code in self.accounts:
            for parent in parents_of(code):
                if parent not in self.accounts and parent not in out:
                    out.append(parent)
        return sorted(out)

    def to_rows(self) -> list[dict]:
        return [
            {
                "code": a.code,
                "name": a.name,
                "currency": a.currency,
                "closed": "true" if a.closed else "false",
            }
            for a in self.sorted()
        ]
