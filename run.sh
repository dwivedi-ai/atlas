#!/usr/bin/env bash
# run.sh — exp-runner entry point.
#
# A generalized coding-agent runner: give it a repo, a task, a plain-English
# acceptance description, and a model (codex|claude). It brews an isolated
# environment, runs the agent against the task in an isolated git worktree, has
# an LLM judge grade the result, and writes everything into jobs/<job_id>/.
#
# Usage
#   ./run.sh                       # interactive wizard (asks the 4 questions)
#   ./run.sh --repo <url> --task-file t.txt --accept-file a.txt --model codex
#   ./run.sh --path /home/me/repo --task "..." --accept "..." --model claude
#   ./run.sh --job <job_id>        # re-run an existing (already-authored) job
#   ./run.sh --job <job_id> --brew-only    # just brew, don't run the agent
#
# Flags
#   --repo <url> | --path <dir>    Repository source (one required, unless --job).
#   --ref <ref>                    Branch/tag/SHA (default: HEAD).
#   --task <text> | --task-file F  The task handed to the agent.
#   --accept <text> | --accept-file F   The NL acceptance handed to the judge.
#   --tasks-file <f>               YAML/JSON list of {id?, task, accept} for a multi-task job.
#   --model <codex|claude>         Agent (default: codex).
#   --envs <E0,...,E6>            Context environments (default: all 7, E0..E6).
#   --jobs <n>                     Max concurrent cells for codex (default: 4; claude forced 1).
#   --reps <n>                     Runs per task × environment (default: 1).
#   --no-analyze                   Skip the self-analysis reflection pass (on by default).
#   --max-seconds <n>              Per-run timeout; 0 = none (default).
#   --build-stack <auto|python|node|none>   Override stack detection.
#   --build-cmd <cmd>              Extra build command.
#   --job <job_id>                 Use an existing jobs/<job_id>/job.yaml.
#   --job-id <id>                  Name the job folder (default: derived from repo).
#   --brew-only                    Stop after the brew step.
#   --rebuild-venv                 Force-rebuild the job's .venv during brew.
#   -h, --help                     Show this help.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HERE/lib"
JOBS="$HERE/jobs"

usage() { sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
err() { printf '\033[31m%s\033[0m\n' "$*" >&2; }

REPO=""; PATH_SRC=""; REF=""; TASK=""; TASK_FILE=""; ACCEPT=""; ACCEPT_FILE=""; TASKS_FILE=""
MODEL=""; REPS=""; MAXS=""; BUILD_STACK=""; BUILD_CMD=""; JOB_ID=""; JOB=""; ENVS=""; JOBS_N=""
BREW_ONLY=0; REBUILD_VENV=0; NO_ANALYZE=0; LADDER=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)         REPO="${2:?}"; shift 2;;
    --path)         PATH_SRC="${2:?}"; shift 2;;
    --ref)          REF="${2:?}"; shift 2;;
    --task)         TASK="${2:?}"; shift 2;;
    --task-file)    TASK_FILE="${2:?}"; shift 2;;
    --accept)       ACCEPT="${2:?}"; shift 2;;
    --accept-file)  ACCEPT_FILE="${2:?}"; shift 2;;
    --tasks-file)   TASKS_FILE="${2:?}"; shift 2;;
    --model)        MODEL="${2:?}"; shift 2;;
    --envs)         ENVS="${2:?}"; shift 2;;
    --ladder)       LADDER=1; shift;;
    --jobs)         JOBS_N="${2:?}"; shift 2;;
    --reps)         REPS="${2:?}"; shift 2;;
    --max-seconds)  MAXS="${2:?}"; shift 2;;
    --build-stack)  BUILD_STACK="${2:?}"; shift 2;;
    --build-cmd)    BUILD_CMD="${2:?}"; shift 2;;
    --job)          JOB="${2:?}"; shift 2;;
    --job-id)       JOB_ID="${2:?}"; shift 2;;
    --brew-only)    BREW_ONLY=1; shift;;
    --no-analyze)   NO_ANALYZE=1; shift;;
    --rebuild-venv) REBUILD_VENV=1; shift;;
    -h|--help)      usage; exit 0;;
    *) err "unknown argument: $1"; echo; usage; exit 1;;
  esac
done

# ── Resolve the job folder ───────────────────────────────────────────────────
if [[ -n "$JOB" ]]; then
  JOB_DIR="$JOBS/$JOB"
  [[ -f "$JOB_DIR/job.yaml" ]] || { err "no such job: $JOB_DIR/job.yaml"; exit 1; }
  echo "Using existing job: $JOB_DIR"
elif [[ -n "$REPO" || -n "$PATH_SRC" ]]; then
  # Flag mode: construct the job spec.
  ARGS=(create)
  [[ -n "$REPO" ]]        && ARGS+=(--repo "$REPO")
  [[ -n "$PATH_SRC" ]]    && ARGS+=(--path "$PATH_SRC")
  [[ -n "$REF" ]]         && ARGS+=(--ref "$REF")
  [[ -n "$TASK" ]]        && ARGS+=(--task "$TASK")
  [[ -n "$TASK_FILE" ]]   && ARGS+=(--task-file "$TASK_FILE")
  [[ -n "$ACCEPT" ]]      && ARGS+=(--accept "$ACCEPT")
  [[ -n "$ACCEPT_FILE" ]] && ARGS+=(--accept-file "$ACCEPT_FILE")
  [[ -n "$TASKS_FILE" ]]  && ARGS+=(--tasks-file "$TASKS_FILE")
  [[ -n "$MODEL" ]]       && ARGS+=(--model "$MODEL")
  [[ -n "$REPS" ]]        && ARGS+=(--reps "$REPS")
  [[ -n "$MAXS" ]]        && ARGS+=(--max-seconds "$MAXS")
  [[ -n "$BUILD_STACK" ]] && ARGS+=(--build-stack "$BUILD_STACK")
  [[ -n "$BUILD_CMD" ]]   && ARGS+=(--build-cmd "$BUILD_CMD")
  [[ "$NO_ANALYZE" == "1" ]] && ARGS+=(--no-analyze)
  [[ -n "$ENVS" ]]        && ARGS+=(--envs "$ENVS")
  [[ "$LADDER" == "1" ]]  && ARGS+=(--ladder)
  [[ -n "$JOB_ID" ]]      && ARGS+=(--job-id "$JOB_ID")
  JOB_DIR="$(python3 "$LIB/jobspec.py" "${ARGS[@]}")"
  echo "Authored job: $JOB_DIR"
else
  # No source flags → interactive wizard.
  JOB_DIR="$(python3 "$LIB/wizard.py")" || exit 1
  JOB_DIR="$(printf '%s\n' "$JOB_DIR" | tail -1)"
fi

# ── Brew (idempotent) ────────────────────────────────────────────────────────
echo ""
echo "==> Brewing $JOB_DIR"
JOB_DIR="$JOB_DIR" REBUILD_VENV="$REBUILD_VENV" bash "$LIB/brew.sh"

if [[ "$BREW_ONLY" == "1" ]]; then
  echo ""
  echo "Brew-only: done. Job ready at $JOB_DIR"
  exit 0
fi

# ── Build the context-environment overlays (once; only if the job uses E1+) ──
echo ""
echo "==> Preparing context environments: $(python3 "$LIB/jobspec.py" envs "$JOB_DIR" | tr '\n' ' ')"
python3 "$LIB/context_gen.py" --job-dir "$JOB_DIR" || {
  err "context generation failed — cannot build the environment ladder. Aborting."; exit 3
}

# ── Synthesize a grader per task (resume-safe; skips tasks already synthesized) ──
echo ""
for TID in $(python3 "$LIB/jobspec.py" tasks "$JOB_DIR"); do
  echo "==> Synthesizing grader for task '$TID'"
  python3 "$LIB/judge.py" --synthesize --job-dir "$JOB_DIR" --task-id "$TID" || {
    err "grader synthesis failed for task '$TID' — cannot grade. Aborting."; exit 3
  }
done

# ── Run the agent over the job's reps ────────────────────────────────────────
echo ""
echo "==> Running $JOB_DIR"
[[ -n "$JOBS_N" ]] && export JOBS="$JOBS_N"
JOB_DIR="$JOB_DIR" bash "$LIB/run_job.sh"

# ── Job-level scorecard + auto-generated figures ─────────────────────────────
python3 "$LIB/report.py" --job-dir "$JOB_DIR" >/dev/null 2>&1 || true
python3 "$LIB/figures.py" --job-dir "$JOB_DIR" >/dev/null 2>&1 || true

echo ""
echo "Done. Results for this job:"
echo "  $JOB_DIR/REPORT.md                          job scorecard (verdicts across tasks/reps)"
echo "  $JOB_DIR/agent-analysis/fig_*.svg           token-usage + file-access figures"
echo "  $JOB_DIR/agent-analysis/<run-id>.md         each run's agent self-analysis"
echo "  $JOB_DIR/runs/<run-id>/report.md            per-run report (criteria, cost, patch)"
echo "  $JOB_DIR/runs/<run-id>/git.patch            the solution (apply with: git apply git.patch)"
