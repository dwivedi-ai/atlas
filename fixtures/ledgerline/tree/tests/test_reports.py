"""Built-in and deployment-local reports."""

from __future__ import annotations

import pytest

from ledgerline.errors import ReportError, UnknownReport
from ledgerline.ledger import Ledger
from ledgerline.periods import Period
from ledgerline.reports import ReportOptions, available, describe, get, register, run


def test_registry_lists_builtins_and_locals():
    names = available()
    assert "trial-balance" in names
    assert "aging" in names  # contributed by ledgerline.local_reports


def test_describe_records_the_origin():
    origins = {row["name"]: row["origin"] for row in describe()}
    assert origins["trial-balance"] == "builtin"
    assert origins["aging"] == "local"


def test_unknown_report_raises():
    with pytest.raises(UnknownReport):
        get("no-such-report")


def test_trial_balance_covers_every_used_account(ledger: Ledger):
    report = run("trial-balance", ledger, ReportOptions(period=Period.parse("2024-Q4")))
    assert set(report.column("account")) == set(ledger.accounts_used())
    assert report.meta["converted"] is False


def test_trial_balance_currency_filter_narrows_rows(ledger: Ledger):
    report = run("trial-balance", ledger, ReportOptions(currency="EUR"))
    assert set(report.column("currency")) == {"EUR"}


def test_trial_balance_residual_is_zero_per_currency(ledger: Ledger):
    report = run("trial-balance", ledger, ReportOptions())
    assert set(report.meta["residual"].values()) == {0}


def test_account_statement_needs_an_account(ledger: Ledger):
    with pytest.raises(ReportError):
        run("account-statement", ledger, ReportOptions())


def test_account_statement_closes_at_the_ledger_balance(ledger: Ledger):
    report = run(
        "account-statement", ledger, ReportOptions(account="assets.cash.checking")
    )
    assert report.meta["closing"] == ledger.balance("assets.cash.checking").minor


def test_account_statement_limit_keeps_the_tail(ledger: Ledger):
    full = run("account-statement", ledger, ReportOptions(account="assets.cash.checking"))
    tail = run(
        "account-statement",
        ledger,
        ReportOptions(account="assets.cash.checking", limit=3),
    )
    assert tail.rows == full.rows[-3:]


def test_income_statement_nets_income_against_expense(ledger: Ledger):
    report = run("income-statement", ledger, ReportOptions(period=Period.parse("2024-Q4")))
    sections = report.column("section")
    assert "income" in sections and "expense" in sections and "net" in sections
    assert set(report.meta["net"]) == {"USD", "EUR", "GBP"}


def test_balance_sheet_only_shows_balance_sheet_types(ledger: Ledger):
    report = run("balance-sheet", ledger, ReportOptions())
    assert set(report.column("section")) <= {"asset", "liability", "equity"}


def test_transaction_log_limit(ledger: Ledger):
    report = run("transaction-log", ledger, ReportOptions(limit=5))
    assert len(report) == 5


def test_aging_buckets_are_ordered(ledger: Ledger):
    report = run("aging", ledger, ReportOptions(period=Period.parse("2024-Q4")))
    assert report.meta["buckets"] == ["0-30", "0-60", "0-90", "90+"]
    assert report.meta["root"] == "assets.receivable"


def test_stale_accounts_uses_the_window(ledger: Ledger):
    report = run("stale-accounts", ledger, ReportOptions(limit=30))
    assert report.meta["window_days"] == 30
    assert "account" in report.columns


def test_report_column_lookup_is_checked(ledger: Ledger):
    report = run("trial-balance", ledger, ReportOptions())
    with pytest.raises(ReportError):
        report.column("nope")


def test_register_replaces_a_name(ledger: Ledger):
    marker = object()

    def fake(_ledger, _options):
        return marker

    original = get("transaction-log").fn
    try:
        register("transaction-log", fake)
        assert run("transaction-log", ledger) is marker
    finally:
        register("transaction-log", original)
