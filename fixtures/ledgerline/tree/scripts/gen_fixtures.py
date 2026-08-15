#!/usr/bin/env python3
"""Regenerate the derived test fixtures under ``tests/data/generated``.

Deterministic by construction: the output is a pure function of the constants in
this file, with no clock, no randomness and no environment lookups, so running
it twice produces byte-identical files and a re-run shows up as an empty diff.

Usage::

    python3 scripts/gen_fixtures.py [--out tests/data/generated] [--check]

``--check`` regenerates into memory and reports which files would change,
without writing anything; it exits 1 when something is stale.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "tests" / "data" / "generated"

HEADER = (
    "; GENERATED FILE — produced by scripts/gen_fixtures.py.\n"
    "; Edit the generator, not this file.\n"
)

MONTHS = (
    (1, "January"),
    (2, "February"),
    (3, "March"),
    (4, "April"),
    (5, "May"),
    (6, "June"),
    (7, "July"),
    (8, "August"),
    (9, "September"),
    (10, "October"),
    (11, "November"),
    (12, "December"),
)

#: Gross monthly salary cost per month, in USD major units. Flat for three
#: quarters, then the annual uplift lands in October.
GROSS_BY_MONTH: dict[int, str] = {
    month: ("17600.00" if month <= 9 else "18400.00") for month, _ in MONTHS
}

#: Rent is fixed for the year; the December entry carries the closing tag.
RENT = "3200.00"

PAY_DAY = 15
RENT_DAY = 3


def payroll_ledger(year: int = 2024) -> str:
    """A twelve-month payroll and rent ledger, one transaction per event."""
    chunks: list[str] = [HEADER.rstrip("\n"), ""]
    for month, name in MONTHS:
        gross = GROSS_BY_MONTH[month]
        chunks.append(
            f"{year}-{month:02d}-{RENT_DAY:02d} Rent for {name}  #recurring\n"
            f"    expenses.rent                  {RENT} USD\n"
            f"    assets.cash.checking"
        )
        chunks.append("")
        tags = "  #payroll  #closing" if month == 12 else "  #payroll"
        chunks.append(
            f"{year}-{month:02d}-{PAY_DAY:02d} {name} payroll{tags}\n"
            f"    expenses.salaries             {gross} USD\n"
            f"    assets.cash.checking"
        )
        chunks.append("")
    return "\n".join(chunks).rstrip("\n") + "\n"


def extended_rates() -> str:
    """A rate CSV that prices every currency against USD, including the gaps."""
    rows = [
        ("USD", "EUR", "0.8"),
        ("EUR", "USD", "1.25"),
        ("USD", "GBP", "0.5"),
        ("GBP", "USD", "2"),
        ("USD", "JPY", "125"),
        ("JPY", "USD", "0.008"),
        ("USD", "CHF", "0.9"),
        ("CHF", "USD", "1.111111111"),
    ]
    lines = ["base,quote,rate"] + [",".join(row) for row in rows]
    return "\n".join(lines) + "\n"


ARTIFACTS = {
    "payroll-2024.ledger": payroll_ledger,
    "rates-extended.csv": extended_rates,
}


def render_all() -> dict[str, str]:
    return {name: fn() for name, fn in sorted(ARTIFACTS.items())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument(
        "--check", action="store_true", help="report stale files instead of writing"
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    rendered = render_all()
    stale: list[str] = []
    for name, text in rendered.items():
        target = out_dir / name
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == text:
            continue
        stale.append(name)
        if not args.check:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    if args.check:
        for name in stale:
            print(f"stale: {name}")
        print(f"{len(rendered) - len(stale)}/{len(rendered)} fixtures up to date")
        return 1 if stale else 0

    for name in stale:
        print(f"wrote {out_dir / name}")
    print(f"{len(rendered)} fixture(s) regenerated, {len(stale)} changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
