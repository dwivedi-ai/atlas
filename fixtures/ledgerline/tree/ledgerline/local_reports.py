"""Deployment-local reports.

:mod:`ledgerline.reports` imports this module the first time a report name is
resolved, so anything registered here is visible to the CLI without touching the
built-in table. The module is expected to differ between deployments.
"""

from __future__ import annotations

from datetime import date, timedelta

from .accounts import is_under
from .ledger import Ledger
from .money import Money
from .reports import Report, ReportOptions, register

#: Upper bound in days for each ageing bucket; the last bucket is open ended.
AGING_BUCKETS: tuple[int, ...] = (30, 60, 90)


def _bucket_for(age_days: int) -> str:
    for edge in AGING_BUCKETS:
        if age_days <= edge:
            return f"0-{edge}"
    return f"{AGING_BUCKETS[-1]}+"


def aging(ledger: Ledger, options: ReportOptions) -> Report:
    """Net receivable movement bucketed by the age of the posting.

    Payments net against the invoices they settle, so a bucket can come out
    negative when a customer paid inside it; that is the intended reading.

    ``options.account`` selects the subtree to age (default
    ``assets.receivable``); the as-at date is the end of ``options.period`` when
    one is given, otherwise the newest transaction date in the ledger.
    """
    root = options.account or "assets.receivable"
    scope = options.scoped(ledger)
    span = scope.date_range()
    as_at: date = options.period.end if options.period else (span[1] if span else date.today())

    buckets: dict[str, dict[str, Money]] = {}
    for transaction in scope:
        for posting in transaction.postings:
            if not is_under(posting.account, root):
                continue
            age = (as_at - transaction.date).days
            bucket = buckets.setdefault(_bucket_for(age), {})
            currency = posting.amount.currency
            bucket[currency] = (
                bucket[currency] + posting.amount if currency in bucket else posting.amount
            )

    order = [f"0-{edge}" for edge in AGING_BUCKETS] + [f"{AGING_BUCKETS[-1]}+"]
    rows = [
        [name, currency, amount.format(with_currency=False)]
        for name in order
        for currency, amount in sorted(buckets.get(name, {}).items())
    ]
    return Report(
        name="aging",
        title=f"Receivable ageing as at {as_at.isoformat()}",
        columns=("bucket", "currency", "amount"),
        rows=rows,
        meta={"root": root, "as_at": as_at.isoformat(), "buckets": order},
    )


def stale_accounts(ledger: Ledger, options: ReportOptions) -> Report:
    """Accounts with no activity in the ``limit`` days before the as-at date."""
    window = options.limit or 180
    scope = options.scoped(ledger)
    span = scope.date_range()
    as_at: date = options.period.end if options.period else (span[1] if span else date.today())
    cutoff = as_at - timedelta(days=window)

    last_seen: dict[str, date] = {}
    for transaction in scope:
        for account in transaction.accounts():
            previous = last_seen.get(account)
            if previous is None or transaction.date > previous:
                last_seen[account] = transaction.date
    rows = [
        [account, when.isoformat(), str((as_at - when).days)]
        for account, when in sorted(last_seen.items())
        if when < cutoff
    ]
    return Report(
        name="stale-accounts",
        title=f"Accounts idle for more than {window} days",
        columns=("account", "last_seen", "days_idle"),
        rows=rows,
        meta={"window_days": window, "as_at": as_at.isoformat()},
    )


register("aging", aging, doc=aging.__doc__ or "", origin="local")
register("stale-accounts", stale_accounts, doc=stale_accounts.__doc__ or "", origin="local")
