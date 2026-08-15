"""End-to-end CLI behaviour, driven through ``cli.main`` rather than a subprocess."""

from __future__ import annotations

import io
import json

from ledgerline.cli import EXIT_ERROR, EXIT_ISSUES, EXIT_OK, EXIT_USAGE, main


def _run(argv, ) -> tuple[int, str]:
    buffer = io.StringIO()
    code = main(argv, out=buffer)
    return code, buffer.getvalue()


def test_no_command_prints_help():
    code, text = _run([])
    assert code == EXIT_USAGE
    assert "usage" in text.lower()


def test_reports_lists_names():
    code, text = _run(["reports"])
    assert code == EXIT_OK
    assert "trial-balance" in text.split()


def test_validate_clean_ledger(data_dir):
    code, text = _run(
        ["validate", str(data_dir / "sample.ledger"), "--chart", str(data_dir / "chart.csv")]
    )
    assert code == EXIT_OK
    assert "0 error(s)" in text


def test_validate_json_output(data_dir):
    code, text = _run(["validate", str(data_dir / "sample.ledger"), "--json"])
    assert code == EXIT_OK
    payload = json.loads(text)
    assert payload["summary"]["errors"] == 0


def test_validate_reports_errors(tmp_path, data_dir):
    broken = tmp_path / "broken.ledger"
    broken.write_text(
        "2024-01-01 Bad account\n"
        "    assets.moon             1.00 USD\n"
        "    assets.cash.checking   -1.00 USD\n",
        encoding="utf-8",
    )
    code, text = _run(["validate", str(broken), "--chart", str(data_dir / "chart.csv")])
    assert code == EXIT_ISSUES
    assert "unknown-account" in text


def test_report_renders_a_table(data_dir):
    code, text = _run(["report", "trial-balance", str(data_dir / "sample.ledger")])
    assert code == EXIT_OK
    assert "Trial balance" in text
    assert "assets.cash.checking" in text


def test_report_json(data_dir):
    code, text = _run(
        ["report", "trial-balance", str(data_dir / "sample.ledger"), "--json", "--period", "2024-Q4"]
    )
    assert code == EXIT_OK
    payload = json.loads(text)
    assert payload["columns"] == ["account", "type", "currency", "balance"]


def test_report_with_rates_loads_the_table(data_dir):
    code, _ = _run(
        [
            "report",
            "transaction-log",
            str(data_dir / "sample.ledger"),
            "--rates",
            str(data_dir / "rates.csv"),
            "--limit",
            "2",
        ]
    )
    assert code == EXIT_OK


def test_unknown_report_is_an_error(data_dir):
    code, _ = _run(["report", "nope", str(data_dir / "sample.ledger")])
    assert code == EXIT_ERROR


def test_export_json_to_stdout(data_dir):
    code, text = _run(["export", str(data_dir / "sample.ledger")])
    assert code == EXIT_OK
    assert isinstance(json.loads(text), list)


def test_export_csv_to_a_file(tmp_path, data_dir):
    target = tmp_path / "out.csv"
    code, text = _run(
        ["export", str(data_dir / "sample.ledger"), "--format", "csv", "--out", str(target)]
    )
    assert code == EXIT_OK
    assert target.read_text().startswith("date,ref,description")
    assert "wrote" in text


def test_accounts_filters_by_type(data_dir):
    code, text = _run(["accounts", str(data_dir / "chart.csv"), "--type", "expense"])
    assert code == EXIT_OK
    assert "expenses.rent" in text
    assert "assets.cash.checking" not in text


def test_missing_file_is_reported(data_dir):
    code, _ = _run(["validate", str(data_dir / "nope.ledger")])
    assert code == EXIT_ERROR
