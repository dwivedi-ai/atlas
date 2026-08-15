#!/usr/bin/env bash
# Regenerate the derived fixtures under tests/data/generated.
#
# The generator is deterministic, so on an up-to-date checkout this leaves the
# working tree unchanged and prints "0 changed".
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

PYTHON="${PYTHON:-python3}"

cd "$ROOT"
exec "$PYTHON" scripts/gen_fixtures.py --out tests/data/generated "$@"
