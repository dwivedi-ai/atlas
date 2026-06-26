#!/usr/bin/env bash
# log_tool_event.sh
#
# Responsibility: Claude Code PreToolUse/PostToolUse/Stop hook.
#                 Appends one JSON line per tool event to hook_events.jsonl.
#                 Provides sub-second wall-clock timestamps not available in
#                 the transcript alone.
#
# Inputs:
#   $1                    — hook type: "pre" | "post" | "stop"
#   stdin                 — JSON payload from Claude Code hook system
#   EXPERIMENT_RUN_ID     — set by setup_run.sh before agent launch
#   EXPERIMENT_RUNS_DIR   — set by setup_run.sh before agent launch
#
# Outputs:
#   runs/{EXPERIMENT_RUN_ID}/hook_events.jsonl  — appended with one JSON line
#
# Schema of each appended line:
#   {"ts": "ISO8601ms", "hook": "pre|post|stop", "tool": "ToolName",
#    "input_summary": "truncated input", "exit_code": null|int}

HOOK_TYPE="${1:-unknown}"
PAYLOAD=$(cat)  # JSON from Claude Code hook system

# Exit silently if not in an experiment run
if [[ -z "${EXPERIMENT_RUN_ID:-}" ]] || [[ -z "${EXPERIMENT_RUNS_DIR:-}" ]]; then
  exit 0
fi

EVENT_LOG="${EXPERIMENT_RUNS_DIR}/${EXPERIMENT_RUN_ID}/hook_events.jsonl"
TS=$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ" 2>/dev/null || date -u +"%Y-%m-%dT%H:%M:%SZ")

# Extract tool name from payload (graceful — payload may vary by Claude Code version)
TOOL_NAME=$(echo "$PAYLOAD" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('tool_name', d.get('tool', '')))
except Exception:
    print('')
" 2>/dev/null || echo "")

# Extract a short summary of input (first 120 chars of JSON-encoded input)
INPUT_SUMMARY=$(echo "$PAYLOAD" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    inp = d.get('tool_input', d.get('input', {}))
    s = json.dumps(inp)
    print(s[:120] + ('...' if len(s) > 120 else ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

# Append event line
python3 -c "
import json, sys
line = {
    'ts': '$TS',
    'hook': '$HOOK_TYPE',
    'tool': '$TOOL_NAME',
    'input_summary': $(echo "$INPUT_SUMMARY" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read().strip()))" 2>/dev/null || echo '""'),
    'exit_code': None
}
print(json.dumps(line))
" >> "$EVENT_LOG" 2>/dev/null || true

# Always exit 0 — hooks must not block agent operation
exit 0
