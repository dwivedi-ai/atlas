#!/usr/bin/env bash
# run_job.sh — run a brewed job's matrix: task × environment × rep.
#
# Mirrors the l1-doxed harness: codex cells run CONCURRENTLY (sliding window capped
# by --jobs), claude cells run SERIAL (it installs a global ~/.claude settings hook
# per run that races under parallelism). Prints live status as each cell lands.
#
# Input (environment):
#   JOB_DIR   — path to jobs/<job_id>   (required)
#   JOBS      — max concurrent cells for codex (default 4; forced 1 for claude)
set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${JOB_DIR:?JOB_DIR is required}"
JOB_DIR="$(cd "$JOB_DIR" && pwd)"
: "${JOBS:=4}"

field() { python3 "$LIB_DIR/jobspec.py" field "$JOB_DIR" "$1"; }

JOB_ID="$(field job_id)"
MODEL="$(field model)"
REPS="$(field reps)";        [[ -n "$REPS" ]] || REPS=1
MAX_SECONDS="$(field max_seconds)"; [[ -n "$MAX_SECONDS" ]] || MAX_SECONDS=0
mapfile -t TASK_IDS < <(python3 "$LIB_DIR/jobspec.py" tasks "$JOB_DIR")
[[ ${#TASK_IDS[@]} -gt 0 ]] || { echo "ERROR: job has no tasks" >&2; exit 1; }
mapfile -t ENV_IDS < <(python3 "$LIB_DIR/jobspec.py" envs "$JOB_DIR")
[[ ${#ENV_IDS[@]} -gt 0 ]] || ENV_IDS=(E0)
MULTIENV=0; [[ ${#ENV_IDS[@]} -gt 1 ]] && MULTIENV=1

case "$MODEL" in
  codex|openai)                                  AGENT_ID="codex";;
  claude|sonnet|claude-sonnet|claude-sonnet-4-6) AGENT_ID="claude-sonnet-4-6";;
  *) echo "ERROR: unknown model: '$MODEL' (use codex|claude)" >&2; exit 1;;
esac
AGENT_DASH="$(echo "$AGENT_ID" | tr '.' '-')"

if [[ "$AGENT_ID" == codex ]]; then
  command -v codex >/dev/null || { echo "ERROR: 'codex' not found on PATH. Run 'codex login' first." >&2; exit 1; }
else
  command -v claude >/dev/null || { echo "ERROR: 'claude' not found on PATH. Run 'claude login' first." >&2; exit 1; }
  if [[ "$JOBS" != "1" ]]; then echo "NOTE: claude is serial (hook race) — forcing --jobs 1." >&2; fi
  JOBS=1
fi

# ── Build the flat cell list ────────────────────────────────────────────────
CELLS=()
for TID in "${TASK_IDS[@]}"; do
  for ENV_ID in "${ENV_IDS[@]}"; do
    for REP in $(seq 1 "$REPS"); do
      CELLS+=("$TID|$ENV_ID|$REP")
    done
  done
done
TOTAL=${#CELLS[@]}

echo "================================================================"
echo " Running job: $JOB_ID"
echo " Model:       $MODEL  ($AGENT_ID)   parallelism: $JOBS"
echo " Tasks:       ${TASK_IDS[*]}"
echo " Environments:${ENV_IDS[*]}"
echo " Reps:        $REPS   →   $TOTAL cells total"
echo "================================================================"

STATUS_DIR="$(mktemp -d)"; DONE_DIR="$(mktemp -d)"
trap 'rm -rf "$STATUS_DIR" "$DONE_DIR"' EXIT

run_cell() {
  local TID="$1" ENV_ID="$2" REP="$3"
  local RUN_ID="${JOB_ID}-${TID}-${ENV_ID}-${AGENT_DASH}-r$(printf '%03d' "$REP")"
  local RUN_DIR="$JOB_DIR/runs/$RUN_ID"
  local TASK_PROMPT; TASK_PROMPT="$(python3 "$LIB_DIR/jobspec.py" task-text "$JOB_DIR" "$TID")"

  if [[ -f "$RUN_DIR/.run_done" ]]; then
    echo "skipped" > "$STATUS_DIR/$RUN_ID"
    _status_line "$RUN_ID" "SKIP" ""
    return 0
  fi
  echo "failed" > "$STATUS_DIR/$RUN_ID"   # pessimistic until success
  mkdir -p "$RUN_DIR"

  if ! JOB_DIR="$JOB_DIR" RUN_ID="$RUN_ID" AGENT_ID="$AGENT_ID" REP="$REP" \
       TASK_ID="$TID" ENV_ID="$ENV_ID" MULTIENV="$MULTIENV" \
       bash "$LIB_DIR/setup_run.sh" >/dev/null 2>"$RUN_DIR/setup.err"; then
    _status_line "$RUN_ID" "SETUP-FAIL" "(see $RUN_DIR/setup.err)"; return 0
  fi

  RUN_DIR="$RUN_DIR" AGENT_ID="$AGENT_ID" TASK_PROMPT="$TASK_PROMPT" MAX_SECONDS="$MAX_SECONDS" \
    bash "$LIB_DIR/run_agent.sh" >/dev/null 2>&1 || true

  if JOB_DIR="$JOB_DIR" RUN_ID="$RUN_ID" AGENT_ID="$AGENT_ID" TASK_ID="$TID" TASK_PROMPT="$TASK_PROMPT" \
       bash "$LIB_DIR/teardown_run.sh" >/dev/null 2>&1; then
    echo "completed" > "$STATUS_DIR/$RUN_ID"
  fi
  local V="?"; local TOK=""
  [[ -f "$RUN_DIR/judge.json" ]] && V="$(python3 -c "import json;print(json.load(open('$RUN_DIR/judge.json'))['verdict'])" 2>/dev/null || echo '?')"
  [[ -f "$RUN_DIR/run_record.json" ]] && TOK="$(python3 -c "import json;print(f\"{json.load(open('$RUN_DIR/run_record.json'))['tokens']['total_input']:,} tok\")" 2>/dev/null || echo '')"
  _status_line "$RUN_ID" "$V" "$TOK"
}

# live status: print one line per FINISHED cell with a monotonic done-count.
_status_line() {
  local rid="$1" verdict="$2" extra="$3"
  : > "$DONE_DIR/$rid"                       # mark this cell finished
  local n; n=$(ls "$DONE_DIR" 2>/dev/null | wc -l)
  printf '[%2d/%2d] %-44s %-9s %s\n' "$n" "$TOTAL" "$rid" "$verdict" "$extra"
}

# ── Launch (sliding window for codex; serial for claude) ────────────────────
echo "Launching $TOTAL cells (≤$JOBS at once)…"
running=0
for cell in "${CELLS[@]}"; do
  IFS='|' read -r TID ENV_ID REP <<< "$cell"
  if [[ "$JOBS" -le 1 ]]; then
    run_cell "$TID" "$ENV_ID" "$REP"
  else
    run_cell "$TID" "$ENV_ID" "$REP" &
    running=$((running+1))
    if (( running >= JOBS )); then wait -n 2>/dev/null || true; running=$((running-1)); fi
  fi
done
wait || true

# ── Tally ───────────────────────────────────────────────────────────────────
C=0; F=0; S=0
for f in "$STATUS_DIR"/*; do
  [[ -e "$f" ]] || continue
  case "$(cat "$f")" in completed) C=$((C+1));; skipped) S=$((S+1));; *) F=$((F+1));; esac
done
echo ""
echo "================================================================"
echo " Job done. Completed: $C  Skipped: $S  Failed: $F  (of $TOTAL)"
echo " Runs under: $JOB_DIR/runs/"
echo "================================================================"
