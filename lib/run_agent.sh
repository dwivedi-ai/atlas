#!/usr/bin/env bash
# run_agent.sh — invoke one headless coding agent against the task in a workspace.
#
# Ported verbatim (flags-wise) from experiments/l4/harness/scripts/run_experiment.sh.
# Writes the agent's exit code to $RUN_DIR/agent_exit_code and always returns 0 so
# the caller can read it without `set -e` aborting on a nonzero agent.
#
# Input (environment):
#   RUN_DIR      — the run directory                        (required)
#   AGENT_ID     — codex | claude-sonnet-4-6 | gemini-2.5-* (required)
#   TASK_PROMPT  — the task handed to the agent              (required)
#   MAX_SECONDS  — per-run timeout; 0 = none                 (default 0)
#
# Output (in RUN_DIR):
#   transcript.jsonl   (codex: the agent's JSONL stdout IS the transcript)
#   agent_stdout.txt   (claude: the --output-format json result)
#   agent_stdout.json  (gemini: the -o json {session_id,response,stats} object;
#                       the transcript is located from ~/.gemini/tmp in teardown)
#   agent_stderr.txt
#   agent_exit_code
set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${RUN_DIR:?RUN_DIR is required}"
: "${AGENT_ID:?AGENT_ID is required}"
: "${TASK_PROMPT:?TASK_PROMPT is required}"
: "${MAX_SECONDS:=0}"

WORKSPACE="$RUN_DIR/workspace"
[[ -d "$WORKSPACE" ]] || { echo "ERROR: workspace missing: $WORKSPACE" >&2; exit 1; }

echo "--> Invoking $AGENT_ID in $WORKSPACE"
AGENT_EXIT_CODE=0
(
  cd "$WORKSPACE"
  export EXPERIMENT_RUN_ID="$(basename "$RUN_DIR")"
  export EXPERIMENT_RUNS_DIR="$(cd "$RUN_DIR/.." && pwd)"
  if [[ "$AGENT_ID" == claude-* ]]; then
    # Strip any ambient ANTHROPIC_API_KEY so headless claude uses the SUBSCRIPTION
    # login (not a stray/bogus API key). bypassPermissions: headless --print cannot
    # prompt for approvals, so without it every Edit/Write/Bash would be denied.
    timeout "$MAX_SECONDS" env -u ANTHROPIC_API_KEY claude \
      --model "$AGENT_ID" --output-format json --print \
      --permission-mode bypassPermissions "$TASK_PROMPT" \
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

echo "$AGENT_EXIT_CODE" > "$RUN_DIR/agent_exit_code"
echo "--> [$(basename "$RUN_DIR")] agent exited: code=$AGENT_EXIT_CODE"
exit 0
