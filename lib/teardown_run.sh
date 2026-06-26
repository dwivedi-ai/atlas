#!/usr/bin/env bash
# teardown_run.sh — collect artifacts after the agent exits, then clean up.
#
# Generalized from experiments/l4/harness/scripts/teardown_run.sh. Phase 2 scope:
# capture the diff + transcript, restore Claude settings, remove the worktree.
# Grading (Phase 3) and telemetry (Phase 4) plug in at the marked points; they run
# against the workspace and so must happen BEFORE the worktree is removed.
#
# Input (environment):
#   JOB_DIR   — path to jobs/<job_id>   (required)
#   RUN_ID    — the run id              (required)
#   AGENT_ID  — codex | claude-sonnet-4-6  (required)
#
# Output (in $JOB_DIR/runs/$RUN_ID):
#   git.patch          — unified diff of the agent's changes (venv excluded)
#   transcript.jsonl   — agent transcript
#   .run_done          — completion sentinel (resume marker)
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${JOB_DIR:?JOB_DIR is required}"
: "${RUN_ID:?RUN_ID is required}"
: "${AGENT_ID:?AGENT_ID is required}"
: "${TASK_ID:=t1}"

JOB_DIR="$(cd "$JOB_DIR" && pwd)"
BARE="$JOB_DIR/repo.git"
LOCK="$JOB_DIR/.worktree.lock"
RUN_DIR="$JOB_DIR/runs/$RUN_ID"
WORKSPACE="$RUN_DIR/workspace"
AGENT_EXIT_CODE="$(cat "$RUN_DIR/agent_exit_code" 2>/dev/null || echo 0)"

echo "==> Tearing down run: $RUN_ID"

# ── 1. Capture git diff (tracked changes + new files; exclude the venv symlink) ──
echo "--> Capturing git patch"
(
  cd "$WORKSPACE"
  git diff HEAD > "$RUN_DIR/git.patch" 2>/dev/null || true
  git ls-files --others --exclude-standard \
      -x venv -x 'venv/**' -x '__pycache__' -x '**/__pycache__/**' -x '*.pyc' \
    | xargs -I{} git diff --no-index /dev/null {} >> "$RUN_DIR/git.patch" 2>/dev/null || true
)

# ── 2. Locate / verify the transcript ────────────────────────────────────────
echo "--> Locating transcript"
if [[ "$AGENT_ID" == claude-* ]]; then
  # Claude Code writes sessions to ~/.claude/projects/<slug>/ where <slug> is the
  # absolute workspace path with every non-alphanumeric char replaced by '-'.
  WORKSPACE_SLUG=$(printf '%s' "$WORKSPACE" | sed 's/[^a-zA-Z0-9]/-/g')
  TRANSCRIPT_DIR="$HOME/.claude/projects/$WORKSPACE_SLUG"
  if [[ ! -d "$TRANSCRIPT_DIR" ]]; then
    ALT=$(ls -td "$HOME/.claude/projects/"*-workspace 2>/dev/null | head -1 || true)
    [[ -n "$ALT" ]] && TRANSCRIPT_DIR="$ALT"
  fi
  if [[ -d "$TRANSCRIPT_DIR" ]]; then
    LATEST=$(ls -t "$TRANSCRIPT_DIR"/*.jsonl 2>/dev/null | head -1 || true)
    if [[ -n "$LATEST" ]]; then
      cp "$LATEST" "$RUN_DIR/transcript.jsonl"
      echo "--> Transcript: $LATEST"
    else
      echo '{"type":"error","message":"no transcript found"}' > "$RUN_DIR/transcript.jsonl"
    fi
  else
    echo '{"type":"error","message":"transcript dir not found"}' > "$RUN_DIR/transcript.jsonl"
  fi
elif [[ "$AGENT_ID" == "codex" ]]; then
  if [[ ! -s "$RUN_DIR/transcript.jsonl" ]]; then
    echo '{"type":"error","message":"codex produced no output"}' > "$RUN_DIR/transcript.jsonl"
  else
    echo "--> Codex transcript: $(wc -l < "$RUN_DIR/transcript.jsonl") events"
  fi
fi

# ── 3. Grade (judge.py --grade against this task's battery) — runs in workspace ──
if [[ -f "$LIB_DIR/judge.py" && -d "$JOB_DIR/grader/$TASK_ID" ]]; then
  echo "--> Grading (judge, task=$TASK_ID)"
  JOB_DIR="$JOB_DIR" python3 "$LIB_DIR/judge.py" --grade \
    --job-dir "$JOB_DIR" --run-id "$RUN_ID" --task-id "$TASK_ID" --agent-exit-code "$AGENT_EXIT_CODE" \
    || echo "WARN: grading failed" >&2
else
  echo "--> Grading skipped (no grader for task '$TASK_ID')"
fi

# ── 3b. Self-analysis (the reflection pass) — needs the workspace, runs after grade ──
ANALYZE="$(python3 "$LIB_DIR/jobspec.py" field "$JOB_DIR" analyze)"
if [[ "$ANALYZE" == "False" ]]; then
  echo "--> Self-analysis disabled (analyze: false)"
else
  MAX_SECONDS="$(python3 "$LIB_DIR/jobspec.py" field "$JOB_DIR" max_seconds)"; [[ -n "$MAX_SECONDS" ]] || MAX_SECONDS=0
  JOB_DIR="$JOB_DIR" RUN_ID="$RUN_ID" AGENT_ID="$AGENT_ID" MAX_SECONDS="$MAX_SECONDS" \
    TASK_ID="$TASK_ID" TASK_PROMPT="${TASK_PROMPT:-}" \
    bash "$LIB_DIR/self_analysis.sh" || echo "WARN: self-analysis failed" >&2
fi

# ── 4. Telemetry ─────────────────────────────────────────────────────────────
echo "--> Extracting telemetry"
TIMESTAMP_END=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
python3 - "$RUN_DIR/run_meta.json" "$TIMESTAMP_END" <<'EOF' || true
import json, sys
meta_path, ts_end = sys.argv[1], sys.argv[2]
with open(meta_path) as f: meta = json.load(f)
meta["timestamp_end"] = ts_end
with open(meta_path, "w") as f: json.dump(meta, f, indent=2)
EOF
python3 "$LIB_DIR/telemetry.py" --run-dir "$RUN_DIR" || echo "WARN: telemetry extraction failed" >&2

# ── 4b. Per-run report ───────────────────────────────────────────────────────
if [[ -f "$LIB_DIR/report.py" ]]; then
  python3 "$LIB_DIR/report.py" --run-dir "$RUN_DIR" || echo "WARN: report failed" >&2
fi

# ── 5. Restore Claude settings ───────────────────────────────────────────────
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
CLAUDE_SETTINGS_BACKUP="$RUN_DIR/claude_settings_backup.json"
if [[ -f "$CLAUDE_SETTINGS_BACKUP" ]]; then
  cp "$CLAUDE_SETTINGS_BACKUP" "$CLAUDE_SETTINGS"
  echo "--> Restored Claude settings"
fi

# ── 6. Remove the worktree (keep git.patch) ──────────────────────────────────
echo "--> Removing worktree"
(
  flock 200
  git --git-dir="$BARE" worktree remove --force "$WORKSPACE" 2>/dev/null || rm -rf "$WORKSPACE"
) 200>"$LOCK"

date -u +%Y-%m-%dT%H:%M:%SZ > "$RUN_DIR/.run_done"
echo "==> Teardown complete: $RUN_DIR"
