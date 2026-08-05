"""Table rendering."""

from __future__ import annotations

from ledgerline.reports import ReportOptions, run
from ledgerline.textui import bullet_list, column_widths, indent, render_report, render_table, truncate


def test_column_widths_track_the_widest_cell():
    assert column_widths(["a", "bb"], [["xxx", "y"]]) == [3, 2]


def test_render_table_aligns_numeric_columns_right():
    text = render_table(["account", "amount"], [["cash", "1.00"], ["rent", "22.00"]])
    lines = text.splitlines()
    assert lines[0].startswith("account")
    assert lines[2].endswith(" 1.00")
    assert lines[3].endswith("22.00")


def test_render_table_without_a_rule():
    assert len(render_table(["a"], [["b"]], rule=False).splitlines()) == 2


def test_render_report_titles_and_underlines(ledger):
    report = run("trial-balance", ledger, ReportOptions())
    text = render_report(report)
    first, second = text.splitlines()[:2]
    assert len(second) == len(first)
    assert set(second) == {"-"}


def test_ragged_rows_do_not_crash():
    text = render_table(["a", "b"], [["only-one"]])
    assert "only-one" in text


def test_bullet_list_and_indent():
    assert bullet_list(["x", "y"]) == "- x\n- y"
    assert indent("a\n\nb") == "    a\n\n    b"


def test_truncate():
    assert truncate("abcdef", 4) == "a..."
    assert truncate("abc", 10) == "abc"
    assert truncate("abcdef", 2) == "ab"
