"""Command line interface.

::

    ledgerline validate  LEDGER [--chart CHART] [--rule NAME ...]
    ledgerline report    NAME LEDGER [--chart C] [--period P] [--account A]
                                     [--currency C] [--limit N] [--json]
    ledgerline export    LEDGER [--format json|csv] [--out PATH]
    ledgerline accounts  CHART [--type TYPE]
    ledgerline reports

``main`` returns an exit code instead of calling :func:`sys.exit`, so the tests
can drive it directly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .accounts import ACCOUNT_TYPES, ChartOfAccounts
from .csvio import dumps as csv_dumps
from .errors import LedgerError, ParseError, ReportError
from .fx import RateTable
from .jsonio import dumps as json_dumps
from .ledger import Ledger
from .periods import Period
from .reports import ReportOptions, available, run
from .textui import render_report, render_table
from .validate import summarise, validate

EXIT_OK = 0
EXIT_ISSUES = 1
EXIT_USAGE = 2
EXIT_ERROR = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledgerline", description="A small double-entry ledger")
    parser.add_argument("--version", action="version", version=f"ledgerline {__version__}")
    sub = parser.add_subparsers(dest="command")

    # `--chart` is shared; the positionals are declared per subcommand so their
    # order matches the documented usage rather than the order argparse would
    # inherit them in from a parent parser.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--chart", help="path to a chart-of-accounts CSV")

    p_validate = sub.add_parser("validate", parents=[common], help="check a ledger")
    p_validate.add_argument("ledger", help="path to a .ledger document")
    p_validate.add_argument("--rule", action="append", default=[], help="run only these rules")
    p_validate.add_argument("--json", action="store_true", help="emit JSON")

    p_report = sub.add_parser("report", parents=[common], help="run a report")
    p_report.add_argument("name", help="report name (see `ledgerline reports`)")
    p_report.add_argument("ledger", help="path to a .ledger document")
    p_report.add_argument("--period", help="2024 | 2024-Q4 | 2024-10 | START:END")
    p_report.add_argument("--account", help="account code, for reports that take one")
    p_report.add_argument("--currency", help="restrict the report to one currency")
    p_report.add_argument("--rates", help="path to a base,quote,rate CSV")
    p_report.add_argument("--limit", type=int, default=0)
    p_report.add_argument("--json", action="store_true", help="emit JSON")

    p_export = sub.add_parser("export", parents=[common], help="export a ledger")
    p_export.add_argument("ledger", help="path to a .ledger document")
    p_export.add_argument("--format", default="json", choices=("json", "csv"))
    p_export.add_argument("--out", help="write here instead of stdout")

    p_accounts = sub.add_parser("accounts", help="list a chart of accounts")
    p_accounts.add_argument("chart", help="path to a chart-of-accounts CSV")
    p_accounts.add_argument("--type", choices=ACCOUNT_TYPES, help="filter by account type")

    sub.add_parser("reports", help="list the available reports")
    return parser


def _load(args: argparse.Namespace) -> Ledger:
    chart = ChartOfAccounts.load(args.chart) if getattr(args, "chart", None) else None
    return Ledger.load(args.ledger, chart)


def _options(args: argparse.Namespace) -> ReportOptions:
    return ReportOptions(
        period=Period.parse(args.period) if args.period else None,
        account=args.account,
        currency=args.currency,
        rates=RateTable.load(args.rates) if args.rates else None,
        limit=args.limit,
    )


def cmd_validate(args: argparse.Namespace, out) -> int:
    ledger = _load(args)
    issues = validate(ledger, args.rule or None)
    if args.json:
        print(
            json.dumps(
                {"summary": summarise(issues), "issues": [i.to_dict() for i in issues]}, indent=2
            ),
            file=out,
        )
    else:
        for issue in issues:
            print(str(issue), file=out)
        summary = summarise(issues)
        print(
            f"{len(ledger)} transactions, {summary['errors']} error(s), "
            f"{summary['warnings']} warning(s)",
            file=out,
        )
    return EXIT_ISSUES if any(i.is_error for i in issues) else EXIT_OK


def cmd_report(args: argparse.Namespace, out) -> int:
    ledger = _load(args)
    report = run(args.name, ledger, _options(args))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2), file=out)
    else:
        print(render_report(report), file=out, end="")
    return EXIT_OK


def cmd_export(args: argparse.Namespace, out) -> int:
    ledger = _load(args)
    text = json_dumps(ledger) if args.format == "json" else csv_dumps(ledger)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(text)} bytes)", file=out)
    else:
        print(text, file=out, end="")
    return EXIT_OK


def cmd_accounts(args: argparse.Namespace, out) -> int:
    chart = ChartOfAccounts.load(args.chart)
    accounts = chart.of_type(args.type) if args.type else chart.sorted()
    rows = [[a.code, a.type, a.currency, "closed" if a.closed else "open"] for a in accounts]
    print(render_table(["account", "type", "currency", "state"], rows), file=out)
    return EXIT_OK


def cmd_reports(args: argparse.Namespace, out) -> int:
    for name in available():
        print(name, file=out)
    return EXIT_OK


COMMANDS = {
    "validate": cmd_validate,
    "report": cmd_report,
    "export": cmd_export,
    "accounts": cmd_accounts,
    "reports": cmd_reports,
}


def main(argv: Sequence[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.command:
        parser.print_help(out)
        return EXIT_USAGE
    try:
        return COMMANDS[args.command](args, out)
    except (ParseError, ReportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(f"error: no such file: {exc.filename}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
