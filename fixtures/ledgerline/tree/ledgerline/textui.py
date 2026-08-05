"""Plain-text rendering: aligned tables and small formatting helpers.

Kept separate from :mod:`ledgerline.reports` so a report can be tested for its
numbers without asserting on whitespace.
"""

from __future__ import annotations

from typing import Sequence

#: Columns whose values are right-aligned when the header says so.
NUMERIC_COLUMNS = frozenset({"amount", "balance", "debit", "credit", "total", "days_idle"})

BOX_HORIZONTAL = "-"
COLUMN_GAP = 2


def column_widths(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[int]:
    widths = [len(str(cell)) for cell in header]
    for row in rows:
        for index, cell in enumerate(row):
            if index >= len(widths):
                widths.append(len(str(cell)))
            else:
                widths[index] = max(widths[index], len(str(cell)))
    return widths


def render_table(
    header: Sequence[str], rows: Sequence[Sequence[str]], *, rule: bool = True
) -> str:
    """Render a table with a header rule and per-column alignment."""
    widths = column_widths(header, rows)
    gap = " " * COLUMN_GAP
    right = {i for i, name in enumerate(header) if str(name).lower() in NUMERIC_COLUMNS}

    def line(cells: Sequence[str]) -> str:
        parts = []
        for index, width in enumerate(widths):
            cell = str(cells[index]) if index < len(cells) else ""
            parts.append(cell.rjust(width) if index in right else cell.ljust(width))
        return gap.join(parts).rstrip()

    out = [line(header)]
    if rule:
        out.append(gap.join(BOX_HORIZONTAL * width for width in widths))
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def render_report(report: object) -> str:
    """Render anything with ``title``, ``columns`` and ``rows`` attributes."""
    title = getattr(report, "title", "")
    columns = getattr(report, "columns", ())
    rows = getattr(report, "rows", [])
    body = render_table(list(columns), list(rows))
    return f"{title}\n{BOX_HORIZONTAL * len(title)}\n{body}\n" if title else body + "\n"


def bullet_list(items: Sequence[str], marker: str = "-") -> str:
    return "\n".join(f"{marker} {item}" for item in items)


def indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def truncate(text: str, width: int, ellipsis: str = "...") -> str:
    text = str(text)
    if width <= 0 or len(text) <= width:
        return text
    if width <= len(ellipsis):
        return text[:width]
    return text[: width - len(ellipsis)] + ellipsis
