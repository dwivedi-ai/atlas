#!/usr/bin/env bash
# run_agent.sh — invoke one headless coding agent against the task in a workspace.
#
# Writes the agent's exit code to $RUN_DIR/agent_exit_code and ALWAYS returns 0, so
# the caller can read it without `set -e` aborting on a nonzero agent. run_job.sh
# depends on that contract: a failed agent is a data point, not a broken matrix.
#
# THE CLAUDE BRANCH FORKS ON EXPERIMENT
#   ladder  →  `claude --print --output-format json` exactly as before, plus an
#              explicit --session-id so teardown can find the transcript by id
#              instead of by newest-mtime (which picks the wrong file the moment
#              two claude runs share a projects/ directory — and claude is no
#              longer clamped to --jobs 1).
#   wur     →  lib/wur/driver.py, which owns the child from launch to exit: it is
#              the child's PARENT, never a pipe stage, because a tap in the pipe
#              means a tap crash kills the agent with SIGPIPE and truncates the
#              only copy of the raw stream.
#
# Input (environment):
#   RUN_DIR      — the run directory                        (required)
#   AGENT_ID     — claude-sonnet-5 | codex | gemini-* | agy-*  (required)
#   TASK_PROMPT  — the task handed to the agent              (required)
#   MAX_SECONDS  — per-run timeout; 0 = none                 (default 0)
#   EXPERIMENT   — ladder | wur                              (default ladder)
#   BACKEND/MODEL/TASK_ID/REP/CONDITION_ID                   (optional, wur)
#
# Output (in RUN_DIR):
#   transcript.jsonl   (codex: the agent's JSONL stdout IS the transcript)
#   stream.jsonl       (claude+wur: VERBATIM child stdout, written by the driver)
#   agent_stdout.txt   (claude+ladder: the --output-format json result)
#   agent_stdout.json  (gemini: the -o json {session_id,response,stats} object)
#   agent_stderr.txt
#   agent_exit_code
set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${RUN_DIR:?RUN_DIR is required}"
: "${AGENT_ID:?AGENT_ID is required}"
: "${TASK_PROMPT:?TASK_PROMPT is required}"
: "${MAX_SECONDS:=0}"
: "${EXPERIMENT:=ladder}"

WORKSPACE="$RUN_DIR/workspace"
[[ -d "$WORKSPACE" ]] || { echo "ERROR: workspace missing: $WORKSPACE" >&2; exit 1; }

RUN_ID="$(basename "$RUN_DIR")"
# The driver writes agent_exit_code itself; a stale file from a previous attempt
# must not be mistaken for this attempt's result.
rm -f "$RUN_DIR/agent_exit_code"

# Deterministic session id, byte-identical to lib/wur/driver.py::_uuid_for and to
# setup_run.sh. Recomputed here rather than read, so this script still works when
# invoked standalone against a run dir whose run_meta.json is gone.
SESSION_UUID="$(python3 - "$RUN_ID" <<'PY' 2>/dev/null || true
import hashlib, sys, uuid
print(str(uuid.UUID(bytes=hashlib.sha256(("wur-session|" + sys.argv[1]).encode()).digest()[:16],
                    version=4)))
PY
)"

# Is this the WUR driver path? Decided HERE, in the parent, because the subshell
# below cannot report it back: its status is captured by `) || AGENT_EXIT_CODE=$?`
# and any assignment inside it is discarded. (That is also why ${PIPESTATUS[0]} is
# not used anywhere in this file — it would be read outside the subshell that set it.)
USE_DRIVER=0
if [[ "$EXPERIMENT" == "wur" ]] \
   && { [[ "$AGENT_ID" == claude-* ]] || [[ "${BACKEND:-}" == "claude" ]]; } \
   && [[ -f "$LIB_DIR/wur/driver.py" ]]; then
  USE_DRIVER=1
  printf '%s' "$TASK_PROMPT" > "$RUN_DIR/task_prompt.txt"
fi

echo "--> Invoking $AGENT_ID in $WORKSPACE (experiment=$EXPERIMENT driver=$USE_DRIVER)"
AGENT_EXIT_CODE=0
(
  cd "$WORKSPACE"
  export EXPERIMENT_RUN_ID="$RUN_ID"
  export EXPERIMENT_RUNS_DIR="$(cd "$RUN_DIR/.." && pwd)"
  if [[ "$USE_DRIVER" == "1" ]]; then
    # env -u ANTHROPIC_API_KEY: the child goes down the SUBSCRIPTION path, not the
    # API path. The driver re-scrubs it too, but the scrub belongs here as well so
    # a driver invoked by hand from this script inherits the same billing route.
    # No `timeout` around the driver unless one was asked for: the driver owns its
    # own watchdogs, and SIGTERMing the parent would orphan the claude child.
    DRIVER_ARGS=(--run-dir "$RUN_DIR" --task-prompt-file "$RUN_DIR/task_prompt.txt"
                 --run-id "$RUN_ID")
    [[ -n "${MODEL:-}" ]] && DRIVER_ARGS+=(--model "$MODEL")
    [[ -n "${TASK_ID:-}" ]] && DRIVER_ARGS+=(--task-id "$TASK_ID")
    [[ -n "${REP:-}" ]] && DRIVER_ARGS+=(--rep "$REP")
    [[ -n "${CONDITION_ID:-}" ]] && DRIVER_ARGS+=(--condition-id "$CONDITION_ID")
    [[ -n "$SESSION_UUID" ]] && DRIVER_ARGS+=(--session-id "$SESSION_UUID")
    if [[ "$MAX_SECONDS" != "0" ]]; then
      DRIVER_ARGS+=(--max-seconds "$MAX_SECONDS")
      env -u ANTHROPIC_API_KEY timeout "$((MAX_SECONDS + 180))" \
        python3 "$LIB_DIR/wur/driver.py" "${DRIVER_ARGS[@]}" \
        >> "$RUN_DIR/driver_stdout.txt" 2>> "$RUN_DIR/agent_stderr.txt"
    else
      env -u ANTHROPIC_API_KEY \
        python3 "$LIB_DIR/wur/driver.py" "${DRIVER_ARGS[@]}" \
        >> "$RUN_DIR/driver_stdout.txt" 2>> "$RUN_DIR/agent_stderr.txt"
    fi
  elif [[ "$AGENT_ID" == claude-* ]]; then
    # Strip any ambient ANTHROPIC_API_KEY so headless claude uses the SUBSCRIPTION
    # login (not a stray/bogus API key). bypassPermissions: headless --print cannot
    # prompt for approvals, so without it every Edit/Write/Bash would be denied.
    # --session-id makes the transcript findable by id; < /dev/null skips the 3 s
    # "no stdin data received" stall every one-shot otherwise pays.
    SID_ARGS=()
    [[ -n "$SESSION_UUID" ]] && SID_ARGS=(--session-id "$SESSION_UUID")
    timeout "$MAX_SECONDS" env -u ANTHROPIC_API_KEY claude \
      --model "$AGENT_ID" --output-format json --print "${SID_ARGS[@]}" \
      --permission-mode bypassPermissions "$TASK_PROMPT" \
      < /dev/null \
      >> "$RUN_DIR/agent_stdout.txt" 2>> "$RUN_DIR/agent_stderr.txt"
  elif [[ "$AGENT_ID" == "codex" ]]; then
    # codex exec --json streams JSONL events to stdout — that IS the transcript.
    # -C pins cwd to the isolated worktree; --ephemeral avoids polluting ~/.codex.
    timeout "$MAX_SECONDS" codex exec \
      -C "$WORKSPACE" --sandbox workspace-write \
      --dangerously-bypass-approvals-and-sandbox --ephemeral --json \
      "$TASK_PROMPT" < /dev/null \
      > "$RUN_DIR/transcript.jsonl" 2>> "$RUN_DIR/agent_stderr.txt"
  elif [[ "$AGENT_ID" == gemini-* ]]; then
    # gemini -o json: stdout is the {session_id,response,stats} object, NOT the
    # transcript — teardown fishes the session .jsonl out of ~/.gemini/tmp by
    # session_id. --approval-mode yolo + --skip-trust (+ trust env) = the headless
    # auto-approval unlock (yolo alone silently downgrades in an untrusted folder).
    timeout "$MAX_SECONDS" env GEMINI_CLI_TRUST_WORKSPACE=true gemini \
      -p "$TASK_PROMPT" --model "$AGENT_ID" \
      --approval-mode yolo --skip-trust --output-format json \
      < /dev/null \
      > "$RUN_DIR/agent_stdout.json" 2>> "$RUN_DIR/agent_stderr.txt"
  elif [[ "$AGENT_ID" == agy* ]]; then
    # agy: --add-dir pins the workspace (cwd is IGNORED, so agy auto-loads .agents/AGENTS.md
    # there); --dangerously-skip-permissions auto-approves tools; per-run --gemini_dir isolates
    # global state (knowledge/conversations) so trials don't contaminate each other. stdout is
    # narrative (no JSON) — teardown locates transcript_full.jsonl + the conversation .db.
    AGY_HOME="$RUN_DIR/agy_home"
    AGY_MODEL="$(python3 "$LIB_DIR/agy.py" cli-model "$AGENT_ID")"
    GD_ARGS=()
    [[ -d "$AGY_HOME/antigravity-cli" ]] && GD_ARGS=(--gemini_dir "$AGY_HOME")
    timeout "$MAX_SECONDS" agy -p "$TASK_PROMPT" \
      --model "$AGY_MODEL" --dangerously-skip-permissions \
      --add-dir "$WORKSPACE" "${GD_ARGS[@]}" \
      --print-timeout "${AGY_PRINT_TIMEOUT:-10m}" --log-file "$RUN_DIR/agy.log" \
      < /dev/null \
      > "$RUN_DIR/agent_stdout.txt" 2>> "$RUN_DIR/agent_stderr.txt"
  else
    echo "ERROR: unknown agent: $AGENT_ID" >&2; exit 99
  fi
) || AGENT_EXIT_CODE=$?

# The driver writes its own agent_exit_code (it always exits 0 itself, so the
# subshell status above would overwrite the child's real code with a 0).
if [[ "$USE_DRIVER" == "1" && -s "$RUN_DIR/agent_exit_code" ]]; then
  AGENT_EXIT_CODE="$(cat "$RUN_DIR/agent_exit_code")"
else
  echo "$AGENT_EXIT_CODE" > "$RUN_DIR/agent_exit_code"
fi
echo "--> [$RUN_ID] agent exited: code=$AGENT_EXIT_CODE"
exit 0
