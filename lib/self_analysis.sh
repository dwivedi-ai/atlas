#!/usr/bin/env bash
# self_analysis.sh — the reflection pass.
#
# After a run is graded, invoke the SAME agent one more time in the still-present
# workspace and ask it to honestly analyze its own result. It writes Markdown to
# agent-analysis/ANALYSIS.md (a workspace-relative, sandbox-safe path); the caller
# (teardown) copies that out to the job-level agent-analysis/<run-id>.md rollup and
# the run dir's analysis.md. If the agent produced no file, we synthesize one from
# its final message so an analysis always exists.
#
# Must run AFTER git.patch is captured and AFTER grading, but BEFORE worktree
# removal (it needs the workspace), so the graded solution / saved patch stay pure.
#
# Input (environment):
#   JOB_DIR   — path to jobs/<job_id>        (required)
#   RUN_ID    — the run id                    (required)
#   AGENT_ID  — codex | claude-sonnet-4-6     (required)
#   MAX_SECONDS — per-call timeout; 0 = none  (default 0)
set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${JOB_DIR:?JOB_DIR is required}"
: "${RUN_ID:?RUN_ID is required}"
: "${AGENT_ID:?AGENT_ID is required}"
: "${MAX_SECONDS:=0}"

JOB_DIR="$(cd "$JOB_DIR" && pwd)"
RUN_DIR="$JOB_DIR/runs/$RUN_ID"
WORKSPACE="$RUN_DIR/workspace"

field() { python3 "$LIB_DIR/jobspec.py" field "$JOB_DIR" "$1"; }
# Prefer the exact task this run was given; fall back to the job's first task.
TASK="${TASK_PROMPT:-$(field task)}"

# The reflection normally runs in the live workspace (agent can `git diff` itself).
# If the workspace is already gone (e.g. backfilling a finished job), run in a temp
# dir and embed the saved git.patch in the prompt instead.
POSTHOC=0; TMPDIR_SELF=""
if [[ ! -d "$WORKSPACE" ]]; then
  [[ -s "$RUN_DIR/git.patch" ]] || { echo "WARN: no workspace and no git.patch — skipping self-analysis" >&2; exit 0; }
  POSTHOC=1
  TMPDIR_SELF="$(mktemp -d)"
  WORKSPACE="$TMPDIR_SELF"
fi

# ── Build the reflection prompt (grade folded in if the judge ran) ───────────
GRADE_BLURB=""
if [[ -f "$RUN_DIR/judge.json" ]]; then
  GRADE_BLURB="$(python3 - "$RUN_DIR/judge.json" <<'PY' 2>/dev/null || true
import json, sys
d = json.load(open(sys.argv[1]))
v = d.get("verdict"); s = d.get("score")
crit = d.get("criteria", [])
lines = [f"An automated judge scored your solution: verdict={v}, score={s}."]
for c in crit:
    mark = "met" if c.get("met") else "NOT met"
    lines.append(f"  - [{mark}] {c.get('criterion','')}")
print("\n".join(lines))
PY
)"
fi

REL_OUT="agent-analysis/ANALYSIS.md"
if [[ "$POSTHOC" == "1" ]]; then
  DIFF_BLOCK="Here is the unified diff of exactly what you changed (your solution):

--- DIFF ---
$(head -c 12000 "$RUN_DIR/git.patch")
--- END DIFF ---"
else
  DIFF_BLOCK="Your changes are already applied in this directory — run \`git diff\` to review exactly what you did."
fi
PROMPT="You attempted the following coding task:

--- TASK ---
${TASK}
--- END TASK ---

${DIFF_BLOCK}
${GRADE_BLURB:+
${GRADE_BLURB}
}
Now write an honest self-analysis of YOUR OWN result. Be candid, not promotional. Cover:
1. What you changed and why (the approach).
2. Whether you believe you actually met the task's intent, and your confidence: low / medium / high.
3. Anything you got wrong, skipped, or are unsure about.
4. Risks or edge cases a human reviewer should double-check.

Write it as Markdown to the file \`${REL_OUT}\` (create the agent-analysis/ directory if needed).
Do NOT modify any other file — this step is reflection only."

mkdir -p "$WORKSPACE/agent-analysis"
echo "--> Self-analysis: invoking $AGENT_ID for reflection"

ANALYSIS_EXIT=0
(
  cd "$WORKSPACE"
  if [[ "$AGENT_ID" == claude-* ]]; then
    timeout "$MAX_SECONDS" env -u ANTHROPIC_API_KEY claude \
      --model "$AGENT_ID" --output-format json --print \
      --permission-mode bypassPermissions "$PROMPT" \
      > "$RUN_DIR/analysis_stdout.json" 2>> "$RUN_DIR/analysis_stderr.txt"
  elif [[ "$AGENT_ID" == "codex" ]]; then
    timeout "$MAX_SECONDS" codex exec \
      -C "$WORKSPACE" --sandbox workspace-write \
      --dangerously-bypass-approvals-and-sandbox --ephemeral --json \
      "$PROMPT" < /dev/null \
      > "$RUN_DIR/analysis_transcript.jsonl" 2>> "$RUN_DIR/analysis_stderr.txt"
  elif [[ "$AGENT_ID" == gemini-* ]]; then
    timeout "$MAX_SECONDS" env GEMINI_CLI_TRUST_WORKSPACE=true gemini \
      -p "$PROMPT" --model "$AGENT_ID" \
      --approval-mode yolo --skip-trust --output-format json \
      < /dev/null \
      > "$RUN_DIR/analysis_stdout.json" 2>> "$RUN_DIR/analysis_stderr.txt"
  elif [[ "$AGENT_ID" == agy* ]]; then
    AGY_HOME="$RUN_DIR/agy_home"
    AGY_MODEL="$(python3 "$LIB_DIR/agy.py" cli-model "$AGENT_ID")"
    GD_ARGS=(); [[ -d "$AGY_HOME/antigravity-cli" ]] && GD_ARGS=(--gemini_dir "$AGY_HOME")
    timeout "$MAX_SECONDS" agy -p "$PROMPT" \
      --model "$AGY_MODEL" --dangerously-skip-permissions \
      --add-dir "$WORKSPACE" "${GD_ARGS[@]}" \
      --print-timeout "${AGY_PRINT_TIMEOUT:-10m}" --log-file "$RUN_DIR/analysis_agy.log" \
      < /dev/null \
      > "$RUN_DIR/analysis_stdout.txt" 2>> "$RUN_DIR/analysis_stderr.txt"
  fi
) || ANALYSIS_EXIT=$?
echo "--> Self-analysis agent exited: code=$ANALYSIS_EXIT"

# ── Fallback: synthesize ANALYSIS.md from the agent's final message ──────────
WS_FILE="$WORKSPACE/$REL_OUT"
if [[ ! -s "$WS_FILE" ]]; then
  echo "--> Agent wrote no analysis file; synthesizing from final message" >&2
  python3 - "$AGENT_ID" "$RUN_DIR" "$WS_FILE" <<'PY' || true
import json, sys, os
agent_id, run_dir, out = sys.argv[1], sys.argv[2], sys.argv[3]
text = ""
try:
    if agent_id.startswith("claude-"):
        d = json.load(open(os.path.join(run_dir, "analysis_stdout.json")))
        text = d.get("result") or d.get("text") or ""
    elif agent_id.startswith("gemini-"):
        d = json.load(open(os.path.join(run_dir, "analysis_stdout.json")))
        text = d.get("response") or ""
    elif agent_id.startswith("agy"):
        # agy stdout is narrative; the reflection text is the tail after any task noise.
        raw = open(os.path.join(run_dir, "analysis_stdout.txt")).read()
        text = "\n".join(l for l in raw.splitlines()
                         if not l.startswith(("Condition ", "Received message from task",
                                              "--- OUTPUT ---", "--- STATUS ---")))
    else:  # codex JSONL — take the last agent/assistant message
        last = ""
        for line in open(os.path.join(run_dir, "analysis_transcript.jsonl")):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            msg = ev.get("msg", ev)
            if isinstance(msg, dict) and msg.get("type") in ("agent_message", "assistant"):
                last = msg.get("message") or msg.get("text") or last
            elif ev.get("type") in ("agent_message", "assistant"):
                last = ev.get("message") or ev.get("text") or last
        text = last
except Exception as e:
    text = f"(could not recover analysis text: {e})"
os.makedirs(os.path.dirname(out), exist_ok=True)
header = "# Agent self-analysis (recovered from final message — no file was written)\n\n"
open(out, "w").write(header + (text or "(the agent produced no analysis)") + "\n")
PY
fi

# ── Copy out: job-level rollup + run-dir copy ────────────────────────────────
if [[ -s "$WS_FILE" ]]; then
  mkdir -p "$JOB_DIR/agent-analysis"
  cp "$WS_FILE" "$JOB_DIR/agent-analysis/${RUN_ID}.md"
  cp "$WS_FILE" "$RUN_DIR/analysis.md"
  echo "--> Self-analysis saved: agent-analysis/${RUN_ID}.md"
else
  echo "WARN: no self-analysis produced for $RUN_ID" >&2
fi
[[ -n "$TMPDIR_SELF" ]] && rm -rf "$TMPDIR_SELF"
exit 0
