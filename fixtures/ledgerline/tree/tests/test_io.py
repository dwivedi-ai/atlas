"""CSV and JSON import/export."""

from __future__ import annotations

import json

import pytest

from ledgerline import csvio, jsonio
from ledgerline.errors import LedgerError, ParseError
from ledgerline.fx import RateTable
from ledgerline.ledger import Ledger
from ledgerline.money import Money

FLAT = """date,ref,description,tags,account,amount,currency
2024-02-01,INV-2,Consulting,invoice,assets.receivable.acme,500.00,USD
2024-02-01,INV-2,Consulting,invoice,income.consulting,-500.00,USD
2024-02-03,,Rent,,expenses.rent,1200.00,USD
2024-02-03,,Rent,,assets.cash.checking,-1200.00,USD
"""


def test_read_string_groups_rows_into_transactions():
    transactions = csvio.read_string(FLAT)
    assert len(transactions) == 2
    assert transactions[0].ref == "INV-2"
    assert len(transactions[1].postings) == 2


def test_missing_columns_are_reported():
    with pytest.raises(ParseError):
        csvio.read_string("date,account\n2024-01-01,assets.cash.checking\n")


def test_bad_date_is_reported():
    with pytest.raises(ParseError):
        csvio.read_string(FLAT.replace("2024-02-01", "01/02/2024"))


def test_export_round_trips(ledger: Ledger):
    text = csvio.dumps(ledger)
    transactions = csvio.read_string(text)
    assert len(transactions) == len(ledger)
    assert transactions[0].date == ledger[0].date


def test_export_can_convert(ledger: Ledger, rates: RateTable):
    rows = csvio.to_rows(ledger, currency="USD", rates=rates)
    assert {row["currency"] for row in rows} == {"USD"}


def test_converting_export_needs_rates(ledger: Ledger):
    with pytest.raises(LedgerError):
        csvio.to_rows(ledger, currency="USD")


def test_json_payload_is_a_bare_array(ledger: Ledger):
    payload = json.loads(jsonio.dumps(ledger))
    assert isinstance(payload, list)
    assert len(payload) == len(ledger)
    assert payload[0]["date"] == "2024-10-01"


def test_json_posting_carries_both_representations(ledger: Ledger):
    posting = json.loads(jsonio.dumps(ledger))[0]["postings"][0]
    assert posting["amount"] == "42000.00"
    assert posting["minor"] == 4200000
    assert Money(posting["minor"], posting["currency"]) == Money(4200000, "USD")


def test_loads_accepts_bare_and_wrapped(ledger: Ledger):
    text = jsonio.dumps(ledger)
    bare = jsonio.loads(text)
    wrapped = jsonio.loads(json.dumps({"transactions": bare}))
    assert bare == wrapped


def test_loads_rejects_other_shapes():
    with pytest.raises(ValueError):
        jsonio.loads('{"nope": 1}')


def test_summarise_reports_the_span(ledger: Ledger):
    summary = jsonio.summarise(jsonio.to_jsonable(ledger))
    assert summary["count"] == len(ledger)
    assert summary["first_date"] == "2024-10-01"
    assert summary["last_date"] == "2024-12-31"
    assert summary["currencies"] == ["EUR", "GBP", "USD"]


def test_write_file(tmp_path, ledger: Ledger):
    target = jsonio.write_file(ledger, tmp_path / "out.json")
    assert json.loads(target.read_text())[0]["ref"] == "OB-USD"
