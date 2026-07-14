#!/usr/bin/env bash
# visualize.sh — launch the exp-runner jobs dashboard.
#
# A zero-dependency, read-only web view over jobs/ (viz/server.py, stdlib
# http.server). Nothing is generated or mutated — it just reflects the
# telemetry each run already wrote.
#
#   ./visualize.sh                # http://127.0.0.1:8000
#   ./visualize.sh --port 8080    # custom port
#   ./visualize.sh --host 0.0.0.0 # bind all interfaces (use with care)
#
# numpy is used only for bootstrap confidence intervals when present; without
# it the dashboard still runs (bars just show point estimates).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

while [ $# -gt 0 ]; do
  case "$1" in
    --port)   PORT="$2"; shift 2 ;;
    --port=*) PORT="${1#*=}"; shift ;;
    --host)   HOST="$2"; shift 2 ;;
    --host=*) HOST="${1#*=}"; shift ;;
    -h|--help)
      sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "error: python3 is required" >&2; exit 1; }

if [ ! -d "$HERE/jobs" ] || [ -z "$(ls -A "$HERE/jobs" 2>/dev/null | grep -v '^\.gitkeep$' || true)" ]; then
  echo "note: no jobs found under $HERE/jobs — run ./run.sh first, or the Atlas will be empty." >&2
fi

echo "exp-runner dashboard → http://${HOST}:${PORT}/   (Ctrl-C to stop)"
exec python3 "$HERE/viz/server.py" --host "$HOST" --port "$PORT"
