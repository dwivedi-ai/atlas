"""Shared fixtures: the sample chart, ledger and rate table."""

from __future__ import annotations

from pathlib import Path

import pytest

from ledgerline.accounts import ChartOfAccounts
from ledgerline.fx import RateTable
from ledgerline.ledger import Ledger

DATA = Path(__file__).resolve().parent / "data"


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return DATA


@pytest.fixture(scope="session")
def chart() -> ChartOfAccounts:
    return ChartOfAccounts.load(DATA / "chart.csv")


@pytest.fixture()
def ledger(chart: ChartOfAccounts) -> Ledger:
    return Ledger.load(DATA / "sample.ledger", chart)


@pytest.fixture(scope="session")
def rates() -> RateTable:
    return RateTable.load(DATA / "rates.csv")
