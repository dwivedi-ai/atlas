"""The derived fixtures under ``tests/data/generated`` must stay loadable.

These assert shape, not freshness: the generator lives in
``scripts/gen_fixtures.py`` and the release process is what re-runs it.
"""

from __future__ import annotations

from ledgerline.fx import RateTable
from ledgerline.ledger import Ledger
from ledgerline.parser import parse_file
from ledgerline.periods import Period


def test_payroll_fixture_parses(data_dir):
    transactions = parse_file(data_dir / "generated" / "payroll-2024.ledger")
    assert len(transactions) == 24
    assert all(t.is_balanced for t in transactions)


def test_payroll_fixture_covers_the_whole_year(data_dir):
    ledger = Ledger.from_transactions(parse_file(data_dir / "generated" / "payroll-2024.ledger"))
    assert len(ledger.index_by_month()) == 12
    assert len(ledger.in_period(Period.parse("2024-Q4"))) == 6


def test_payroll_fixture_carries_the_annual_uplift(data_dir):
    ledger = Ledger.from_transactions(parse_file(data_dir / "generated" / "payroll-2024.ledger"))
    september = ledger.in_period(Period.parse("2024-09")).balance("expenses.salaries")
    october = ledger.in_period(Period.parse("2024-10")).balance("expenses.salaries")
    assert october.minor > september.minor


def test_extended_rate_fixture_prices_everything_against_usd(data_dir):
    rates = RateTable.load(data_dir / "generated" / "rates-extended.csv")
    for code in ("EUR", "GBP", "JPY", "CHF"):
        assert rates.usd_leg(code) > 0
