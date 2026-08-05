#!/usr/bin/env bash
# run_job.sh — run a brewed job's matrix.
#
# RESPONSIBILITY
#   Walk the cell list once, in the order the frozen schedule says, at the
#   concurrency the backend policy allows, and print one status line per finished
#   cell. Every cell is claimed atomically, so a resumed or double-launched job
#   never runs the same cell twice.
#
# TWO THINGS CHANGED THAT ARE NOT COSMETIC
#   1. CELL ORDER. The old nested `for task; for env; for rep` loops ran every
#      cell of one arm before any cell of the next, so ARM WAS PERFECTLY
#      CONFOUNDED WITH WALL CLOCK — a model deployment or a slow afternoon lands
#      entirely on whichever arms ran late. When $JOB_DIR/schedule.json exists,
#      the order comes from lib/wur/schedule.py's blocked randomisation instead.
#      With no schedule.json the old nesting is walked verbatim, so a ladder job
#      behaves exactly as it did.
#   2. PARALLELISM. claude is no longer clamped to 1. The clamp existed because
#      setup_run.sh mutated the global ~/.claude/settings.json per run; that
#      mutation is deleted, replaced by per-run CLAUDE_CONFIG_DIR + --settings,
#      and 4-way concurrency was measured clean against a real repo (spike S3).
#      gemini and agy stay serial — they still share global CLI state.
#
# Input (environment):
#   JOB_DIR   — path to jobs/<job_id>   (required)
#   JOBS      — requested concurrency (default: job.yaml parallelism.jobs, else 4);
#               capped per backend by the policy table below
set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${JOB_DIR:?JOB_DIR is required}"
JOB_DIR="$(cd "$JOB_DIR" && pwd)"

field() { python3 "$LIB_DIR/jobspec.py" field "$JOB_DIR" "$1"; }

JOB_ID="$(field job_id)"
MODEL="$(field model)"
BACKEND_SPEC="$(field agent.backend)"
EFFORT="$(field agent.effort)"
EXPERIMENT="$(field experiment)"; [[ -n "$EXPERIMENT" ]] || EXPERIMENT="ladder"
REPS="$(field reps)";        [[ -n "$REPS" ]] || REPS=1
MAX_SECONDS="$(field max_seconds)"; [[ -n "$MAX_SECONDS" ]] || MAX_SECONDS=0
MATRIX_SEED="$(field matrix_seed)"
BUDGET_STEPS="$(field budget.steps)"; [[ -n "$BUDGET_STEPS" ]] || BUDGET_STEPS=30
PROBE_LO="$(field probe.lo)";                 [[ -n "$PROBE_LO" ]] || PROBE_LO=1
PROBE_HI="$(field probe.hi)";                 [[ -n "$PROBE_HI" ]] || PROBE_HI=3
PROBE_MAX_PROBES="$(field probe.max_probes)"; [[ -n "$PROBE_MAX_PROBES" ]] || PROBE_MAX_PROBES=24
PROBE_SALT="$(field probe.salt)";             [[ -n "$PROBE_SALT" ]] || PROBE_SALT="wur-v1"
GATE_TIMEOUT_MS="$(field probe.gate_timeout_ms)"; [[ -n "$GATE_TIMEOUT_MS" ]] || GATE_TIMEOUT_MS=300000

mapfile -t TASK_IDS < <(python3 "$LIB_DIR/jobspec.py" tasks "$JOB_DIR")
[[ ${#TASK_IDS[@]} -gt 0 ]] || { echo "ERROR: job has no tasks" >&2; exit 1; }
mapfile -t ARM_IDS < <(python3 "$LIB_DIR/jobspec.py" conditions "$JOB_DIR")
[[ ${#ARM_IDS[@]} -gt 0 ]] || ARM_IDS=(E0)

# OVERLAY_MODE replaces the old MULTIENV boolean. A ladder job with one rung ran
# the repo as-is; that is still `none`. wur always overlays — its control arms
# plant a fact-free NOTES.md, which is the whole point of `ctrl` vs `ctrl-nofile`.
if [[ "$EXPERIMENT" == "wur" ]]; then
  OVERLAY_MODE=wur
elif [[ ${#ARM_IDS[@]} -gt 1 ]]; then
  OVERLAY_MODE=ladder
else
  OVERLAY_MODE=none
fi

# ── Resolve the agent identity (one source of truth: lib/agent.py) ───────────
# This replaced a duplicate bash `case` that hard-pinned claude-sonnet-4-6 and had
# to be edited in lockstep with resolve_agent_id every time a model was added.
AGENT_JSON="$(python3 "$LIB_DIR/agent.py" resolve \
                ${MODEL:+--model "$MODEL"} ${BACKEND_SPEC:+--backend "$BACKEND_SPEC"} \
                ${EFFORT:+--effort "$EFFORT"} --json)" || {
  echo "ERROR: could not resolve the agent for this job (model='$MODEL' backend='$BACKEND_SPEC')" >&2
  exit 1
}
_agent_field() { python3 -c 'import json,sys;print(json.loads(sys.argv[1]).get(sys.argv[2]) or "")' "$AGENT_JSON" "$1"; }
BACKEND="$(_agent_field backend)"
AGENT_ID="$(_agent_field agent_id)"
AGENT_DASH="$(_agent_field agent_dash)"
AGENT_MODEL="$(_agent_field model)"
AGENT_CLI="$(_agent_field cli)"
[[ -n "$AGENT_ID" ]] || { echo "ERROR: agent.py resolve returned no agent_id" >&2; exit 1; }

command -v "$AGENT_CLI" >/dev/null || {
  echo "ERROR: '$AGENT_CLI' not found on PATH. Authenticate the $BACKEND CLI first." >&2
  exit 1
}

# ── Backend concurrency policy ───────────────────────────────────────────────
# A table, not a hard clamp. claude 4 is MEASURED (S3: four worktrees, four
# isolated CLAUDE_CONFIG_DIRs, four --settings files, all clean); 6 is the
# untested ceiling, so the table stops at 4 unless job.yaml says otherwise.
# gemini and agy remain 1: both still share global CLI state that no per-run
# directory isolates.
case "$BACKEND" in
  claude) POLICY_CAP=4;;
  codex)  POLICY_CAP=8;;
  gemini) POLICY_CAP=1;;
  agy)    POLICY_CAP=1;;
  *)      POLICY_CAP=1;;
esac
SPEC_CAP="$(field "parallelism.per_backend.$BACKEND")"
[[ -n "$SPEC_CAP" ]] && POLICY_CAP="$SPEC_CAP"
if [[ -z "${JOBS:-}" ]]; then
  JOBS="$(field parallelism.jobs)"
  [[ -n "$JOBS" ]] || JOBS=4
fi
[[ "$JOBS" =~ ^[0-9]+$ ]] || JOBS=1
(( JOBS < 1 )) && JOBS=1
if (( JOBS > POLICY_CAP )); then
  echo "NOTE: $BACKEND concurrency policy caps this job at $POLICY_CAP (requested $JOBS)." >&2
  JOBS="$POLICY_CAP"
fi

# ── Build the cell list ──────────────────────────────────────────────────────
# CELL = task|arm|rep|cell_index|run_order_index
SCHED="$JOB_DIR/schedule.json"
CELLS=()
if [[ -f "$SCHED" ]]; then
  mapfile -t CELLS < <(python3 - "$SCHED" <<'PY'
import json, sys
sched = json.load(open(sys.argv[1]))
for c in sched["cells"]:
    print("|".join(str(x) for x in (c["task_id"], c["condition"], c["rep"],
                                    c.get("cell_index", ""), c.get("run_order_index", ""))))
PY
)
  SCHED_NOTE="schedule.json ($(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["design_sha256"][:12])' "$SCHED" 2>/dev/null || echo unknown))"
else
  # No frozen schedule: walk the historical nesting verbatim so a ladder job's
  # cell order is exactly what it has always been.
  i=0
  for TID in "${TASK_IDS[@]}"; do
    for ARM in "${ARM_IDS[@]}"; do
      for REP in $(seq 1 "$REPS"); do
        CELLS+=("$TID|$ARM|$REP|$i|$i")
        i=$((i+1))
      done
    done
  done
  SCHED_NOTE="nested loops (no schedule.json)"
fi
TOTAL=${#CELLS[@]}
[[ $TOTAL -gt 0 ]] || { echo "ERROR: the matrix is empty" >&2; exit 1; }

ACTUAL="$JOB_DIR/schedule_actual.jsonl"
ACTUAL_LOCK="$JOB_DIR/.schedule_actual.lock"

echo "================================================================"
echo " Running job: $JOB_ID"
echo " Experiment:  $EXPERIMENT   overlay: $OVERLAY_MODE"
echo " Agent:       $AGENT_ID  (backend=$BACKEND model=$AGENT_MODEL)  parallelism: $JOBS"
echo " Tasks:       ${TASK_IDS[*]}"
echo " Conditions:  ${ARM_IDS[*]}"
echo " Order:       $SCHED_NOTE"
echo " Reps:        $REPS   →   $TOTAL cells total"
echo "================================================================"

STATUS_DIR="$(mktemp -d)"; DONE_DIR="$(mktemp -d)"
trap 'rm -rf "$STATUS_DIR" "$DONE_DIR"' EXIT

# ── schedule_actual.jsonl — the REALIZED order, one flock'd writer at a time ──
# V8 measured unlocked appends past PIPE_BUF interleaving into unparseable lines.
_actual() {   # _actual <json-object-as-one-line>
  (
    flock 202
    printf '%s\n' "$1" >> "$ACTUAL"
  ) 202>"$ACTUAL_LOCK"
}

_actual_row() {  # _actual_row <event> <run_id> <tid> <arm> <rep> <cell_idx> <order_idx> <conc> [extra-json]
  python3 - "$@" <<'PY'
import json, sys, time
ev, run_id, tid, arm, rep, cell, order, conc = sys.argv[1:9]
extra = json.loads(sys.argv[9]) if len(sys.argv) > 9 and sys.argv[9] else {}
def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": ev,
       "run_id": run_id, "task_id": tid, "condition": arm, "rep": _i(rep),
       "cell_index": _i(cell), "run_order_index": _i(order),
       "concurrency_at_launch": _i(conc)}
row.update(extra)
print(json.dumps(row))
PY
}

run_cell() {
  local TID="$1" ARM="$2" REP="$3" CELL_INDEX="$4" ORDER_INDEX="$5" CONC="$6"
  local RUN_ID="${JOB_ID}-${TID}-${ARM}-${AGENT_DASH}-r$(printf '%03d' "$REP")"
  local RUN_DIR
  if [[ "$EXPERIMENT" == "wur" ]]; then
    RUN_DIR="${ATLAS_RUNS_ROOT:-/tmp/atlas-runs}/$JOB_ID/$RUN_ID"
  else
    RUN_DIR="$JOB_DIR/runs/$RUN_ID"
  fi
  local TASK_PROMPT; TASK_PROMPT="$(python3 "$LIB_DIR/jobspec.py" task-text "$JOB_DIR" "$TID")"

  if [[ -f "$RUN_DIR/.run_done" ]]; then
    echo "skipped" > "$STATUS_DIR/$RUN_ID"
    _status_line "$RUN_ID" "SKIP" ""
    return 0
  fi
  mkdir -p "$RUN_DIR"
  # $JOB_DIR/runs/<run_id> stays the discovery path for report.py / figures.py /
  # viz, even when the run itself lives under $ATLAS_RUNS_ROOT (V9: no secret may
  # be an ancestor of a workspace). preflight resolves symlinks, so this link
  # does not put $JOB_DIR back on the workspace's `..` path.
  if [[ "$RUN_DIR" != "$JOB_DIR/runs/$RUN_ID" ]]; then
    mkdir -p "$JOB_DIR/runs"
    [[ -e "$JOB_DIR/runs/$RUN_ID" && ! -L "$JOB_DIR/runs/$RUN_ID" ]] \
      || ln -sfn "$RUN_DIR" "$JOB_DIR/runs/$RUN_ID"
  fi

  # ── Claim the cell (atomic mkdir) ──────────────────────────────────────────
  # mkdir is the one filesystem primitive that is atomic and fails if the target
  # exists, on every filesystem worth running on. Two workers therefore cannot
  # both own a cell. A claim whose owner PID is gone is stale and is taken over,
  # so a crashed matrix resumes instead of skipping the cell forever.
  if ! mkdir "$RUN_DIR/.claim" 2>/dev/null; then
    local OWNER=""
    [[ -f "$RUN_DIR/.claim/owner" ]] && OWNER="$(cat "$RUN_DIR/.claim/owner" 2>/dev/null)"
    local OWNER_PID="${OWNER%% *}"
    if [[ -n "$OWNER_PID" ]] && kill -0 "$OWNER_PID" 2>/dev/null; then
      echo "claimed" > "$STATUS_DIR/$RUN_ID"
      _status_line "$RUN_ID" "CLAIMED" "(pid $OWNER_PID)"
      return 0
    fi
    rm -rf "$RUN_DIR/.claim"
    mkdir "$RUN_DIR/.claim" 2>/dev/null || { _status_line "$RUN_ID" "CLAIM-FAIL" ""; return 0; }
  fi
  echo "$$ $(hostname) $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$RUN_DIR/.claim/owner"

  echo "failed" > "$STATUS_DIR/$RUN_ID"   # pessimistic until success
  _actual "$(_actual_row launch "$RUN_ID" "$TID" "$ARM" "$REP" "$CELL_INDEX" "$ORDER_INDEX" "$CONC")"

  local PROBE_ENABLED
  PROBE_ENABLED="$(python3 "$LIB_DIR/jobspec.py" condition-field "$JOB_DIR" "$ARM" probe 2>/dev/null || echo true)"
  [[ -n "$PROBE_ENABLED" ]] || PROBE_ENABLED=true

  if ! JOB_DIR="$JOB_DIR" RUN_ID="$RUN_ID" RUN_DIR="$RUN_DIR" AGENT_ID="$AGENT_ID" REP="$REP" \
       TASK_ID="$TID" ENV_ID="$ARM" CONDITION_ID="$ARM" TASK_PROMPT="$TASK_PROMPT" \
       EXPERIMENT="$EXPERIMENT" OVERLAY_MODE="$OVERLAY_MODE" \
       BACKEND="$BACKEND" MODEL="$AGENT_MODEL" \
       BUDGET_STEPS="$BUDGET_STEPS" PROBE_ENABLED="$PROBE_ENABLED" \
       PROBE_LO="$PROBE_LO" PROBE_HI="$PROBE_HI" PROBE_MAX_PROBES="$PROBE_MAX_PROBES" \
       PROBE_SALT="$PROBE_SALT" GATE_TIMEOUT_MS="$GATE_TIMEOUT_MS" \
       MATRIX_SEED="$MATRIX_SEED" CELL_INDEX="$CELL_INDEX" RUN_ORDER_INDEX="$ORDER_INDEX" \
       CONCURRENCY_AT_LAUNCH="$CONC" ATTEMPT=1 \
       bash "$LIB_DIR/setup_run.sh" >/dev/null 2>"$RUN_DIR/setup.err"; then
    _actual "$(_actual_row setup_failed "$RUN_ID" "$TID" "$ARM" "$REP" "$CELL_INDEX" "$ORDER_INDEX" "$CONC")"
    _status_line "$RUN_ID" "SETUP-FAIL" "(see $RUN_DIR/setup.err)"; return 0
  fi

  # ── Preflight H1..H12 — fail closed, no API call (wur only) ────────────────
  # A run that starts dirty produces a row indistinguishable from a clean one, and
  # no downstream analysis can tell them apart. Blocking the cell is cheaper.
  if [[ "$EXPERIMENT" == "wur" && -f "$LIB_DIR/wur/preflight.py" ]]; then
    local PF_ARGS=(--run-dir "$RUN_DIR" --job-dir "$JOB_DIR" --arm "$ARM"
                   --run-id "$RUN_ID" --job-id "$JOB_ID")
    # H8 ("the nonce IS in baseline_sha for a fact arm, and is NOT for a control
    # arm") is unverifiable without the arm's manifest, and an unverifiable plant
    # is an excluded run, not a miss (§4.1). plant.py emits the manifest at one of
    # two depths depending on whether the registry carries one fact or one per task.
    local MPATH
    for MPATH in "$JOB_DIR/.registry/conditions/$TID/$ARM/manifest.json" \
                 "$JOB_DIR/.registry/conditions/$ARM/manifest.json"; do
      [[ -f "$MPATH" ]] && { PF_ARGS+=(--manifest "$MPATH"); break; }
    done
    if ! python3 "$LIB_DIR/wur/preflight.py" "${PF_ARGS[@]}" \
           >/dev/null 2>"$RUN_DIR/preflight.err"; then
      _actual "$(_actual_row preflight_failed "$RUN_ID" "$TID" "$ARM" "$REP" "$CELL_INDEX" "$ORDER_INDEX" "$CONC")"
      _status_line "$RUN_ID" "HYGIENE-FAIL" "(see $RUN_DIR/hygiene.json)"; return 0
    fi
  fi

  RUN_DIR="$RUN_DIR" AGENT_ID="$AGENT_ID" TASK_PROMPT="$TASK_PROMPT" MAX_SECONDS="$MAX_SECONDS" \
    EXPERIMENT="$EXPERIMENT" BACKEND="$BACKEND" MODEL="$AGENT_MODEL" \
    TASK_ID="$TID" REP="$REP" CONDITION_ID="$ARM" JOB_DIR="$JOB_DIR" \
    bash "$LIB_DIR/run_agent.sh" >/dev/null 2>&1 || true

  if JOB_DIR="$JOB_DIR" RUN_ID="$RUN_ID" RUN_DIR="$RUN_DIR" AGENT_ID="$AGENT_ID" TASK_ID="$TID" \
       TASK_PROMPT="$TASK_PROMPT" EXPERIMENT="$EXPERIMENT" CONDITION_ID="$ARM" \
       bash "$LIB_DIR/teardown_run.sh" >/dev/null 2>&1; then
    echo "completed" > "$STATUS_DIR/$RUN_ID"
  fi
  local V="?"; local TOK=""
  [[ -f "$RUN_DIR/judge.json" ]] && V="$(python3 -c "import json;print(json.load(open('$RUN_DIR/judge.json'))['verdict'])" 2>/dev/null || echo '?')"
  [[ -f "$RUN_DIR/run_record.json" ]] && TOK="$(python3 -c "import json;print(f\"{json.load(open('$RUN_DIR/run_record.json'))['tokens']['total_input']:,} tok\")" 2>/dev/null || echo '')"
  _actual "$(_actual_row finished "$RUN_ID" "$TID" "$ARM" "$REP" "$CELL_INDEX" "$ORDER_INDEX" "$CONC" "$(python3 -c 'import json,sys;print(json.dumps({"verdict":sys.argv[1]}))' "$V")")"
  _status_line "$RUN_ID" "$V" "$TOK"
}

# live status: print one line per FINISHED cell with a monotonic done-count.
_status_line() {
  local rid="$1" verdict="$2" extra="$3"
  : > "$DONE_DIR/$rid"                       # mark this cell finished
  local n; n=$(ls "$DONE_DIR" 2>/dev/null | wc -l)
  printf '[%2d/%2d] %-44s %-9s %s\n' "$n" "$TOTAL" "$rid" "$verdict" "$extra"
}

# ── Launch (sliding window) ─────────────────────────────────────────────────
echo "Launching $TOTAL cells (≤$JOBS at once)…"
running=0
for cell in "${CELLS[@]}"; do
  IFS='|' read -r TID ARM REP CELL_INDEX ORDER_INDEX <<< "$cell"
  if [[ "$JOBS" -le 1 ]]; then
    run_cell "$TID" "$ARM" "$REP" "$CELL_INDEX" "$ORDER_INDEX" 1
  else
    run_cell "$TID" "$ARM" "$REP" "$CELL_INDEX" "$ORDER_INDEX" "$((running+1))" &
    running=$((running+1))
    if (( running >= JOBS )); then wait -n 2>/dev/null || true; running=$((running-1)); fi
  fi
done
wait || true

# ── Tally ───────────────────────────────────────────────────────────────────
C=0; F=0; S=0
for f in "$STATUS_DIR"/*; do
  [[ -e "$f" ]] || continue
  case "$(cat "$f")" in completed) C=$((C+1));; skipped|claimed) S=$((S+1));; *) F=$((F+1));; esac
done
echo ""
echo "================================================================"
echo " Job done. Completed: $C  Skipped: $S  Failed: $F  (of $TOTAL)"
echo " Runs under: $JOB_DIR/runs/"
[[ -f "$ACTUAL" ]] && echo " Realized order: $ACTUAL"
echo "================================================================"
