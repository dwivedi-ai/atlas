"""Report definitions and the name -> function registry.

A report is a pure function from a :class:`~ledgerline.ledger.Ledger` plus
:class:`ReportOptions` to a :class:`Report` — a title, a column list and a list
of string rows. Nothing here prints; :mod:`ledgerline.textui` does that.

Reports are looked up by name through :func:`get`, which resolves the built-ins
declared at the bottom of this module and then the deployment-local module
:mod:`ledgerline.local_reports`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from .accounts import (
    BALANCE_SHEET_TYPES,
    INCOME_STATEMENT_TYPES,
    NORMAL_BALANCE,
    type_of,
)
from .errors import ReportError, UnknownReport
from .ledger import Ledger
from .money import Money
from .periods import Period


@dataclass
class ReportOptions:
    """Everything a report may be parameterised by.

    One options object serves every report so the CLI does not have to know
    which report takes which flag.
    """

    period: Period | None = None
    account: str | None = None
    currency: str | None = None
    rates: object | None = None
    include_closed: bool = False
    limit: int = 0

    def scoped(self, ledger: Ledger) -> Ledger:
        """The ledger restricted to :attr:`period`."""
        return ledger.in_period(self.period)


@dataclass
class Report:
    """A rendered-but-untyped table."""

    name: str
    title: str
    columns: tuple[str, ...]
    rows: list[list[str]] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rows)

    def column(self, name: str) -> list[str]:
        try:
            index = self.columns.index(name)
        except ValueError:
            raise ReportError(f"{self.name}: no column {name!r}") from None
        return [row[index] for row in self.rows]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "columns": list(self.columns),
            "rows": [list(r) for r in self.rows],
            "meta": dict(self.meta),
        }


ReportFn = Callable[[Ledger, ReportOptions], Report]


@dataclass(frozen=True)
class ReportSpec:
    name: str
    fn: ReportFn
    doc: str = ""
    origin: str = "builtin"


_REGISTRY: dict[str, ReportSpec] = {}
_LOCAL_LOADED = False


def register(name: str, fn: ReportFn, *, doc: str = "", origin: str = "builtin") -> ReportSpec:
    """Bind ``name`` to ``fn``. A second registration of a name replaces it."""
    spec = ReportSpec(name=name, fn=fn, doc=doc.strip(), origin=origin)
    _REGISTRY[name] = spec
    return spec


def _load_local() -> None:
    """Import the deployment-local report module exactly once."""
    global _LOCAL_LOADED
    if _LOCAL_LOADED:
        return
    _LOCAL_LOADED = True
    try:
        from . import local_reports  # noqa: F401  (imported for its side effect)
    except ImportError:  # pragma: no cover - a deployment may not ship one
        pass


def get(name: str) -> ReportSpec:
    """Resolve a report name to its spec."""
    _load_local()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownReport(
            f"no report named {name!r}; available: {', '.join(available())}"
        ) from None


def run(name: str, ledger: Ledger, options: ReportOptions | None = None) -> Report:
    """Look up ``name`` and run it."""
    spec = get(name)
    return spec.fn(ledger, options or ReportOptions())


def available() -> list[str]:
    """Every registered report name, sorted."""
    _load_local()
    return sorted(_REGISTRY)


def describe() -> list[dict]:
    _load_local()
    return [
        {"name": s.name, "doc": s.doc, "origin": s.origin}
        for s in sorted(_REGISTRY.values(), key=lambda s: s.name)
    ]


# ── helpers shared by the built-ins ──────────────────────────────────────────
def _fmt(amount: Money) -> str:
    return amount.format(with_currency=False)


def _period_label(options: ReportOptions) -> str:
    return options.period.label if options.period else "all dates"


def _signed(code: str, amount: Money) -> Money:
    """Present an amount in its account's natural direction."""
    return amount if NORMAL_BALANCE[type_of(code)] > 0 else -amount


def _totals_by_type(
    ledger: Ledger, types: Iterable[str], options: ReportOptions
) -> dict[str, dict[str, Money]]:
    scope = options.scoped(ledger)
    wanted = set(types)
    out: dict[str, dict[str, Money]] = {}
    for account, per_currency in scope.balances().items():
        kind = type_of(account)
        if kind not in wanted:
            continue
        bucket = out.setdefault(kind, {})
        for currency, amount in per_currency.items():
            bucket[currency] = (
                bucket[currency] + amount if currency in bucket else amount
            )
    return out


# ── the built-in reports ─────────────────────────────────────────────────────
def trial_balance(ledger: Ledger, options: ReportOptions) -> Report:
    """Closing balance of every account that appears in the ledger.

    Mixed-currency ledgers are reported one row per (account, currency);
    ``options.currency`` narrows the rows to a single currency but does not
    convert anything. See ``docs/reports.md`` for what that costs.
    """
    scope = options.scoped(ledger)
    wanted = options.currency.upper() if options.currency else None
    rows: list[list[str]] = []
    totals: dict[str, Money] = {}
    for account, per_currency in scope.balances().items():
        for currency, amount in per_currency.items():
            if wanted and currency != wanted:
                continue
            rows.append(
                [account, type_of(account), currency, _fmt(_signed(account, amount))]
            )
            totals[currency] = totals[currency] + amount if currency in totals else amount
    return Report(
        name="trial-balance",
        title=f"Trial balance ({_period_label(options)})",
        columns=("account", "type", "currency", "balance"),
        rows=rows,
        meta={
            "period": _period_label(options),
            "currencies": sorted(totals),
            "residual": {c: m.minor for c, m in totals.items()},
            "converted": False,
        },
    )


def account_statement(ledger: Ledger, options: ReportOptions) -> Report:
    """Every transaction touching one account, with a running balance."""
    if not options.account:
        raise ReportError("account-statement needs an account (pass --account)")
    scope = options.scoped(ledger)
    account = options.account
    currency = options.currency or scope._default_currency(account)
    rows: list[list[str]] = []
    running = Money.zero(currency)
    for transaction in scope:
        amount = transaction.amount_for(account, include_children=True)
        if amount is None or amount.currency != currency.upper():
            continue
        running = running + amount
        rows.append(
            [
                transaction.date.isoformat(),
                transaction.description,
                _fmt(amount),
                _fmt(running),
            ]
        )
    if options.limit:
        rows = rows[-options.limit :]
    return Report(
        name="account-statement",
        title=f"{account} ({_period_label(options)})",
        columns=("date", "description", "amount", "balance"),
        rows=rows,
        meta={"account": account, "currency": running.currency, "closing": running.minor},
    )


def income_statement(ledger: Ledger, options: ReportOptions) -> Report:
    """Income and expense totals for the period, per currency."""
    totals = _totals_by_type(ledger, INCOME_STATEMENT_TYPES, options)
    rows: list[list[str]] = []
    for kind in INCOME_STATEMENT_TYPES:
        for currency, amount in sorted(totals.get(kind, {}).items()):
            presented = amount if NORMAL_BALANCE[kind] > 0 else -amount
            rows.append([kind, currency, _fmt(presented)])
    # Income sits credit-negative and expense debit-positive, so the profit for
    # the period is the negated sum of both sides.
    net: dict[str, Money] = {}
    for per_currency in totals.values():
        for currency, amount in per_currency.items():
            signed = -amount
            net[currency] = net[currency] + signed if currency in net else signed
    for currency, amount in sorted(net.items()):
        rows.append(["net", currency, _fmt(amount)])
    return Report(
        name="income-statement",
        title=f"Income statement ({_period_label(options)})",
        columns=("section", "currency", "amount"),
        rows=rows,
        meta={"period": _period_label(options), "net": {c: m.minor for c, m in net.items()}},
    )


def balance_sheet(ledger: Ledger, options: ReportOptions) -> Report:
    """Asset, liability and equity totals as at the end of the period."""
    totals = _totals_by_type(ledger, BALANCE_SHEET_TYPES, options)
    rows: list[list[str]] = []
    for kind in BALANCE_SHEET_TYPES:
        for currency, amount in sorted(totals.get(kind, {}).items()):
            presented = amount if NORMAL_BALANCE[kind] > 0 else -amount
            rows.append([kind, currency, _fmt(presented)])
    return Report(
        name="balance-sheet",
        title=f"Balance sheet ({_period_label(options)})",
        columns=("section", "currency", "amount"),
        rows=rows,
        meta={"period": _period_label(options)},
    )


def transaction_log(ledger: Ledger, options: ReportOptions) -> Report:
    """A flat log of transactions, newest last."""
    scope = options.scoped(ledger)
    rows = [
        [
            t.date.isoformat(),
            t.ref or "-",
            t.description,
            ",".join(sorted(t.all_tags())) or "-",
            str(len(t.postings)),
        ]
        for t in scope
    ]
    if options.limit:
        rows = rows[-options.limit :]
    return Report(
        name="transaction-log",
        title=f"Transactions ({_period_label(options)})",
        columns=("date", "ref", "description", "tags", "postings"),
        rows=rows,
        meta={"count": len(rows)},
    )


register("trial-balance", trial_balance, doc=trial_balance.__doc__ or "")
register("account-statement", account_statement, doc=account_statement.__doc__ or "")
register("income-statement", income_statement, doc=income_statement.__doc__ or "")
register("balance-sheet", balance_sheet, doc=balance_sheet.__doc__ or "")
register("transaction-log", transaction_log, doc=transaction_log.__doc__ or "")
