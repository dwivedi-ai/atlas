#!/usr/bin/env bash
# setup_run.sh — prepare one isolated run.
#
# Generalized from experiments/l4/harness/scripts/setup_run.sh. Differences:
#   • the repo runs AS-IS — no context stripping, no environment overlay;
#   • the bare repo + venv are per-job (JOB_DIR), not a shared experiment dir;
#   • the venv symlink is excluded from the captured diff so it can't pollute
#     a user repo's patch.
#
# Input (environment):
#   JOB_DIR   — path to jobs/<job_id>   (required)
#   RUN_ID    — unique run id           (required)
#   AGENT_ID  — codex | claude-sonnet-4-6  (required)
#   REP       — replication integer     (default 1)
#
# Output:
#   $JOB_DIR/runs/$RUN_ID/workspace/   — git worktree at the pinned SHA
#   $JOB_DIR/runs/$RUN_ID/run_meta.json
#   ~/.claude/settings.json            — logging hooks merged in (claude only; backed up)
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${JOB_DIR:?JOB_DIR is required}"
: "${RUN_ID:?RUN_ID is required}"
: "${AGENT_ID:?AGENT_ID is required}"
: "${REP:=1}"
: "${TASK_ID:=t1}"
: "${ENV_ID:=E0}"

JOB_DIR="$(cd "$JOB_DIR" && pwd)"
BARE="$JOB_DIR/repo.git"
VENV="$JOB_DIR/.venv"
LOCK="$JOB_DIR/.worktree.lock"
RUN_DIR="$JOB_DIR/runs/$RUN_ID"
WORKSPACE="$RUN_DIR/workspace"

field() { python3 "$LIB_DIR/jobspec.py" field "$JOB_DIR" "$1"; }
SHA="$(field repo.pinned_sha)"
[[ -n "$SHA" ]] || { echo "ERROR: repo.pinned_sha not set — run brew first" >&2; exit 1; }
[[ -d "$BARE" ]] || { echo "ERROR: bare repo missing at $BARE — run brew first" >&2; exit 1; }

mkdir -p "$RUN_DIR"
echo "==> Setting up run: $RUN_ID"

# ── Isolated worktree at the pinned SHA (flock: worktree metadata is repo-wide) ──
echo "--> Creating worktree at $SHA"
(
  flock 200
  git --git-dir="$BARE" worktree add --detach "$WORKSPACE" "$SHA"
) 200>"$LOCK"

# ── Apply the context environment overlay (multi-env "ladder" jobs only) ─────
# To compare across context conditions cleanly we (1) strip the repo's own
# orienting docs so E0 is genuinely bare, (2) drop in this environment's
# overlay (E1 +README, E2 +AGENTS, …), then (3) COMMIT that as the run's
# baseline — so the agent's solution diff is taken relative to the env context,
# never mixing the injected docs into the captured patch. Single-env (E0-only)
# jobs skip all of this and run the repo exactly as-is.
if [[ "${MULTIENV:-0}" == "1" ]]; then
  echo "--> Applying environment overlay: $ENV_ID"
  ( cd "$WORKSPACE"
    for f in README.md AGENTS.md CLAUDE.md GEMINI.md PROJECT.md PLAN.md PROGRESS.md \
             OBJECTIVES.md CONTRIBUTING.md DEVELOPING.md; do rm -f "$f"; done
    rm -rf .agents   # agy's native context dir (.agents/AGENTS.md) — strip so E0 is bare
    find . -path ./.git -prune -o \( -name AGENTS.md -o -name CLAUDE.md -o -name GEMINI.md \) -print -delete 2>/dev/null || true
  )
  OVERLAY_DIR="$JOB_DIR/environments/$ENV_ID"
  if [[ -d "$OVERLAY_DIR" ]] && [[ -n "$(find "$OVERLAY_DIR" -type f 2>/dev/null)" ]]; then
    cp -r "$OVERLAY_DIR"/. "$WORKSPACE/"
  fi
  ( cd "$WORKSPACE"
    git add -A
    git -c user.email=exp-runner@local -c user.name=exp-runner commit -q \
      -m "env-baseline:$ENV_ID" --allow-empty
  )
  echo "--> Committed env baseline for $ENV_ID (solution diff is relative to this)"
fi

# ── Provide the hermetic venv to the agent as ./venv, but keep it out of the diff ──
if [[ -x "$VENV/bin/python" ]]; then
  ln -sf "$VENV" "$WORKSPACE/venv"
  EXCLUDE_FILE="$(git -C "$WORKSPACE" rev-parse --git-path info/exclude)"
  grep -qxF '/venv' "$EXCLUDE_FILE" 2>/dev/null || echo '/venv' >> "$EXCLUDE_FILE"
  echo "--> Linked venv -> ./venv (git-excluded)"
fi

# ── Back up Gemini settings (gemini only; no hook — restored in teardown) ──
# A reported headless bug can rewrite ~/.gemini/settings.json's auth type; backing it
# up + restoring it (mirrors the claude pattern) neutralizes that regardless.
if [[ "$AGENT_ID" == gemini-* ]]; then
  GEM_SETTINGS="$HOME/.gemini/settings.json"
  [[ -f "$GEM_SETTINGS" ]] && cp "$GEM_SETTINGS" "$RUN_DIR/gemini_settings_backup.json"
fi

# ── Seed an isolated agy home (agy only) so trials don't share global state ──
# --gemini_dir points agy's whole state base at this per-run dir; seed it with just the
# auth + onboarding files so it authenticates without a login and leaves knowledge/,
# conversations/, brain/ fresh per run (no cross-trial contamination). See AGY_DOCS.md §11.
if [[ "$AGENT_ID" == agy* ]]; then
  AGY_SRC="$HOME/.gemini/antigravity-cli"
  AGY_HOME="$RUN_DIR/agy_home"
  mkdir -p "$AGY_HOME/antigravity-cli/cache"
  for f in antigravity-oauth-token settings.json jetski_state.pbtxt installation_id; do
    [[ -f "$AGY_SRC/$f" ]] && cp "$AGY_SRC/$f" "$AGY_HOME/antigravity-cli/$f"
  done
  for f in onboarding.json default_project_id.txt; do
    [[ -f "$AGY_SRC/cache/$f" ]] && cp "$AGY_SRC/cache/$f" "$AGY_HOME/antigravity-cli/cache/$f"
  done
  if [[ -f "$AGY_HOME/antigravity-cli/antigravity-oauth-token" ]]; then
    echo "--> Seeded isolated agy home: $AGY_HOME"
  else
    echo "--> WARN: no antigravity-oauth-token to seed — agy will use the shared ~/.gemini state" >&2
    rm -rf "$AGY_HOME"
  fi
fi

# ── Install Claude Code logging hooks (claude only; backed up, restored in teardown) ──
if [[ "$AGENT_ID" == claude-* ]]; then
  CLAUDE_SETTINGS="$HOME/.claude/settings.json"
  CLAUDE_SETTINGS_BACKUP="$RUN_DIR/claude_settings_backup.json"
  HOOKS_SETTINGS="$LIB_DIR/hooks/settings_template.json"
  HOOK_SCRIPT="$LIB_DIR/hooks/log_tool_event.sh"
  if [[ -f "$HOOK_SCRIPT" ]]; then
    echo "--> Installing Claude Code logging hooks"
    mkdir -p "$(dirname "$CLAUDE_SETTINGS")"
    [[ -f "$CLAUDE_SETTINGS" ]] && cp "$CLAUDE_SETTINGS" "$CLAUDE_SETTINGS_BACKUP"
    python3 - "$CLAUDE_SETTINGS" "$HOOKS_SETTINGS" "$HOOK_SCRIPT" "$RUN_DIR" <<'EOF'
import json, sys, os
settings_path, hooks_template_path, hook_script, run_dir = sys.argv[1:5]
existing = {}
if os.path.exists(settings_path):
    with open(settings_path) as f:
        existing = json.load(f)
with open(hooks_template_path) as f:
    hook_cfg = json.load(f)
hook_cfg = json.loads(
    json.dumps(hook_cfg).replace("__HOOK_SCRIPT__", hook_script).replace("__RUN_DIR__", run_dir)
)
existing["hooks"] = hook_cfg["hooks"]
with open(settings_path, "w") as f:
    json.dump(existing, f, indent=2)
print("hooks installed")
EOF
  fi
fi

# ── Run metadata ──────────────────────────────────────────────────────────────
TIMESTAMP_START=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
JOB_ID="$(field job_id)"
cat > "$RUN_DIR/run_meta.json" <<EOF
{
  "schema_version": "1",
  "run_id": "$RUN_ID",
  "job_id": "$JOB_ID",
  "task_id": "$TASK_ID",
  "env_id": "$ENV_ID",
  "replication": $REP,
  "timestamp_start": "$TIMESTAMP_START",
  "agent_id": "$AGENT_ID",
  "base_repo_sha": "$SHA",
  "repo_url": "$(field repo.url)",
  "repo_path": "$(field repo.path)",
  "workspace_path": "$WORKSPACE"
}
EOF

echo "==> Run $RUN_ID ready. Workspace: $WORKSPACE"
