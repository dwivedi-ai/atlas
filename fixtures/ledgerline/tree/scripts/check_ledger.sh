#!/usr/bin/env bash
# Validate one ledger against the sample chart of accounts.
#
#   scripts/check_ledger.sh [LEDGER]
#
# Exits 0 when the ledger has no errors, 1 when it has, 3 on a hard failure.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

LEDGER="${1:-tests/data/sample.ledger}"
CHART="${CHART:-tests/data/chart.csv}"
PYTHON="${PYTHON:-python3}"

cd "$ROOT"
exec "$PYTHON" -m ledgerline.cli validate "$LEDGER" --chart "$CHART"
