#!/usr/bin/env bash
# run.sh — exp-runner entry point.
#
# A generalized coding-agent runner: give it a repo, a task, a plain-English
# acceptance description, and a model. It brews an isolated environment, runs the
# agent against the task in an isolated git worktree, has a mechanical battery
# grade the result, and writes everything into jobs/<job_id>/.
#
# It runs one of TWO experiments:
#   --experiment ladder  (default)  the 7-rung context ladder — "does richer
#                                   context help?" — unchanged in every respect.
#   --experiment wur                Workspace Uptake & Retention — "does a fact
#                                   planted in the workspace cross available →
#                                   read → used → retained?"
#
# Usage
#   ./run.sh                       # interactive wizard (asks the 4 questions)
#   ./run.sh --repo <url> --task-file t.txt --accept-file a.txt --model codex
#   ./run.sh --path /home/me/repo --task "..." --accept "..." --model claude
#   ./run.sh --job <job_id>        # re-run an existing (already-authored) job
#   ./run.sh --job <job_id> --brew-only    # just brew, don't run the agent
#   ./run.sh --experiment wur --backend claude --model claude-sonnet-5 \
#            --path fixtures/ledgerline/tree --conditions d1,d2,d3,ctrl \
#            --facts-file facts.yaml --matrix-seed 1 --jobs 4
#
# Flags
#   --repo <url> | --path <dir>    Repository source (one required, unless --job).
#   --ref <ref>                    Branch/tag/SHA (default: HEAD).
#   --task <text> | --task-file F  The task handed to the agent.
#   --accept <text> | --accept-file F   The NL acceptance handed to the judge.
#   --tasks-file <f>               YAML/JSON list of {id?, task, accept} for a multi-task job.
#   --experiment <ladder|wur>      Which instrument to run (default: ladder).
#   --backend <claude|codex|gemini|agy>  CLI adapter (v2 two-level selection).
#   --model <name>                 Concrete model id, or a loose name (claude, codex…).
#   --envs <E0,...,E6>             LADDER: context environments (default: all 7).
#   --conditions <a,b,c>           WUR: arm ids (default: the 12 of §7.1 + ctrl-np).
#   --baseline <arm>               The arm every other arm is differenced against.
#   --facts-file <f>               WUR: the frozen fact registry to install.
#   --matrix-seed <n>              WUR: seeds the blocked randomisation of cell order.
#   --budget-steps <n>             WUR: step budget in tool calls (default 30).
#   --pointer-regime <none|prose|import>   WUR: job-level pointer default.
#   --no-canary                    WUR: skip the per-arm autoload assay (costs API calls).
#   --jobs <n>                     Max concurrent cells (capped per backend; claude 4).
#   --reps <n>                     Runs per task × condition (default: 1).
#   --no-analyze                   Skip the self-analysis reflection pass (ladder only).
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

# ── Resolve the interpreter ONCE, here, for every downstream script ──────────
# install.sh falls back to a private .runner-venv when the system python is
# PEP 668 externally-managed (the common case on modern distros). Every script
# under lib/ calls a bare `python3`, so without this they resolve to the system
# interpreter and die on `import yaml`. Prepending to PATH at the single entry
# point fixes all ~40 call sites at once, because they inherit our environment.
if [[ -x "$HERE/.runner-venv/bin/python3" ]]; then
  export PATH="$HERE/.runner-venv/bin:$PATH"
fi

usage() { sed -n '2,52p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
err() { printf '\033[31m%s\033[0m\n' "$*" >&2; }

# Fail loudly and immediately rather than 200 lines later inside a subshell whose
# stderr is redirected to a per-run file nobody reads.
if ! python3 -c 'import yaml, jsonschema' 2>/dev/null; then
  err "python3 cannot import yaml/jsonschema — run ./install.sh first."
  err "  (interpreter checked: $(command -v python3 || echo 'none on PATH'))"
  exit 1
fi

REPO=""; PATH_SRC=""; REF=""; TASK=""; TASK_FILE=""; ACCEPT=""; ACCEPT_FILE=""; TASKS_FILE=""
MODEL=""; REPS=""; MAXS=""; BUILD_STACK=""; BUILD_CMD=""; JOB_ID=""; JOB=""; ENVS=""; JOBS_N=""
BREW_ONLY=0; REBUILD_VENV=0; NO_ANALYZE=0; LADDER=0
EXPERIMENT=""; BACKEND=""; CONDITIONS=""; BASELINE=""; FACTS_FILE=""; MATRIX_SEED=""
POINTER_REGIME=""; BUDGET_STEPS=""; NO_CANARY=0

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
    --experiment)   EXPERIMENT="${2:?}"; shift 2;;
    --backend)      BACKEND="${2:?}"; shift 2;;
    --model)        MODEL="${2:?}"; shift 2;;
    --envs)         ENVS="${2:?}"; shift 2;;
    --conditions)   CONDITIONS="${2:?}"; shift 2;;
    --baseline)     BASELINE="${2:?}"; shift 2;;
    --facts-file)   FACTS_FILE="${2:?}"; shift 2;;
    --matrix-seed)  MATRIX_SEED="${2:?}"; shift 2;;
    --budget-steps) BUDGET_STEPS="${2:?}"; shift 2;;
    --pointer-regime) POINTER_REGIME="${2:?}"; shift 2;;
    --no-canary)    NO_CANARY=1; shift;;
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

if [[ -n "$EXPERIMENT" && "$EXPERIMENT" != "ladder" && "$EXPERIMENT" != "wur" ]]; then
  err "--experiment must be ladder or wur (got '$EXPERIMENT')"; exit 1
fi

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
  [[ -n "$BACKEND" ]]     && ARGS+=(--backend "$BACKEND")
  [[ -n "$REPS" ]]        && ARGS+=(--reps "$REPS")
  [[ -n "$MAXS" ]]        && ARGS+=(--max-seconds "$MAXS")
  [[ -n "$BUILD_STACK" ]] && ARGS+=(--build-stack "$BUILD_STACK")
  [[ -n "$BUILD_CMD" ]]   && ARGS+=(--build-cmd "$BUILD_CMD")
  [[ "$NO_ANALYZE" == "1" ]] && ARGS+=(--no-analyze)
  [[ -n "$ENVS" ]]        && ARGS+=(--envs "$ENVS")
  [[ "$LADDER" == "1" ]]  && ARGS+=(--ladder)
  [[ -n "$EXPERIMENT" ]]  && ARGS+=(--experiment "$EXPERIMENT")
  [[ -n "$CONDITIONS" ]]  && ARGS+=(--conditions "$CONDITIONS")
  [[ -n "$BASELINE" ]]    && ARGS+=(--baseline "$BASELINE")
  [[ -n "$FACTS_FILE" ]]  && ARGS+=(--facts-file "$(cd "$(dirname "$FACTS_FILE")" && pwd)/$(basename "$FACTS_FILE")")
  [[ -n "$MATRIX_SEED" ]] && ARGS+=(--matrix-seed "$MATRIX_SEED")
  [[ -n "$POINTER_REGIME" ]] && ARGS+=(--pointer-regime "$POINTER_REGIME")
  [[ -n "$JOB_ID" ]]      && ARGS+=(--job-id "$JOB_ID")
  JOB_DIR="$(python3 "$LIB/jobspec.py" "${ARGS[@]}")"
  echo "Authored job: $JOB_DIR"
else
  # No source flags → interactive wizard.
  JOB_DIR="$(python3 "$LIB/wizard.py")" || exit 1
  JOB_DIR="$(printf '%s\n' "$JOB_DIR" | tail -1)"
fi

# Flags that arrive with --job (a spec authored earlier) are written into it, so
# `--job X --jobs 4 --matrix-seed 7` behaves the same as authoring with them.
jset() { python3 "$LIB/jobspec.py" set "$JOB_DIR" "$1" "$2" >/dev/null; }
if [[ -n "$JOB" ]]; then
  [[ -n "$MATRIX_SEED" ]]    && jset matrix_seed "$MATRIX_SEED"
  [[ -n "$CONDITIONS" ]]     && jset conditions "$CONDITIONS"
  [[ -n "$FACTS_FILE" ]]     && jset facts_file "$(cd "$(dirname "$FACTS_FILE")" && pwd)/$(basename "$FACTS_FILE")"
  [[ -n "$POINTER_REGIME" ]] && jset pointer_regime "$POINTER_REGIME"
fi
[[ -n "$BUDGET_STEPS" ]] && jset budget.steps "$BUDGET_STEPS"
[[ -n "$JOBS_N" ]]       && jset parallelism.jobs "$JOBS_N"

# The experiment is a property of the job, not of this invocation — a wur job
# re-run without --experiment must still be a wur job.
EXPERIMENT="$(python3 "$LIB/jobspec.py" field "$JOB_DIR" experiment)"
[[ -n "$EXPERIMENT" ]] || EXPERIMENT="ladder"
echo "Experiment: $EXPERIMENT"

# ── Brew (idempotent) ────────────────────────────────────────────────────────
echo ""
echo "==> Brewing $JOB_DIR"
JOB_DIR="$JOB_DIR" REBUILD_VENV="$REBUILD_VENV" bash "$LIB/brew.sh"

if [[ "$BREW_ONLY" == "1" ]]; then
  echo ""
  echo "Brew-only: done. Job ready at $JOB_DIR"
  exit 0
fi

PINNED_SHA="$(python3 "$LIB/jobspec.py" field "$JOB_DIR" repo.pinned_sha)"

if [[ "$EXPERIMENT" == "wur" ]]; then
  # ══ WUR chain (§5.2) ═══════════════════════════════════════════════════════
  # context_gen.py is DELIBERATELY NOT RUN here. Its _compose_env raises KeyError
  # on any arm that is not a ladder rung, and on an arm that happens to be named
  # like one it rmtree's the hand-authored overlay before regenerating it. The
  # fact/plant chain below produces this experiment's overlays instead.
  python3 "$LIB/jobspec.py" validate "$JOB_DIR" --strict || {
    err "job.yaml is not a complete wur spec — see the message above."; exit 3; }

  REGISTRY="$JOB_DIR/.registry/facts.yaml"
  FACTS_SRC="$(python3 "$LIB/jobspec.py" field "$JOB_DIR" facts_file)"

  echo ""
  echo "==> Installing the fact registry"
  if [[ ! -f "$REGISTRY" ]]; then
    [[ -n "$FACTS_SRC" && -f "$FACTS_SRC" ]] || {
      err "facts_file '$FACTS_SRC' not found — a wur job needs a frozen fact registry."; exit 3; }
    python3 - "$LIB" "$FACTS_SRC" "$JOB_DIR" <<'PY' || { err "registry install failed"; exit 3; }
import sys
from pathlib import Path
lib = Path(sys.argv[1])
sys.path.insert(0, str(lib)); sys.path.insert(0, str(lib / "wur"))
import facts
print(f"--> registry installed: {facts.install(sys.argv[2], sys.argv[3])}")
PY
  else
    echo "--> registry already installed: $REGISTRY"
  fi
  chmod 700 "$JOB_DIR/.registry"

  echo ""
  echo "==> Minting nonces (asserted absent from the pinned tree)"
  python3 "$LIB/wur/facts.py" --job-dir "$JOB_DIR" mint \
    --repo-sha "$PINNED_SHA" --repo-dir "$JOB_DIR/repo.git" --baseline-sha "$PINNED_SHA" \
    >/dev/null || { err "nonce minting failed — a nonce already occurs in the repo, or the registry is invalid."; exit 3; }

  echo ""
  echo "==> Rendering condition overlays"
  # --force is what keeps run.sh re-entrant. Every other step here is idempotent
  # (brew short-circuits, the registry install is guarded, finished cells carry
  # .run_done), but plant.py refuses to overwrite an existing overlay — so without
  # this a second run.sh on the same job aborts at exit 3 before reaching the
  # matrix, and resuming an interrupted matrix is impossible. The overlay is a
  # pure function of (registry, arm, task), so re-rendering reproduces it byte for
  # byte; `verify` on the next line re-asserts the plant landed either way.
  RENDER_ARGS=(render --job-dir "$JOB_DIR" --force)
  [[ -n "$CONDITIONS" ]] && RENDER_ARGS+=(--arms "$CONDITIONS")
  python3 "$LIB/wur/plant.py" "${RENDER_ARGS[@]}" >/dev/null \
    || { err "plant render failed."; exit 3; }
  python3 "$LIB/wur/plant.py" verify --job-dir "$JOB_DIR" >/dev/null \
    || { err "plant verification failed — the plant did not land in every arm."; exit 3; }
  echo "--> overlays + manifests under $JOB_DIR/.registry/conditions/"

  # The grader is synthesized against the PRE-overlay repo, so no planted text can
  # reach the acceptance criteria. Then the leak check proves it.
  echo ""
  for TID in $(python3 "$LIB/jobspec.py" tasks "$JOB_DIR"); do
    echo "==> Synthesizing grader for task '$TID'"
    python3 "$LIB/judge.py" --synthesize --job-dir "$JOB_DIR" --task-id "$TID" || {
      err "grader synthesis failed for task '$TID' — cannot grade. Aborting."; exit 3; }
  done

  echo ""
  echo "==> Leak check (no nonce in criteria / task / accept / probe / self-analysis text)"
  python3 "$LIB/wur/facts.py" --job-dir "$JOB_DIR" leak-check --repo-root "$HERE" >/dev/null \
    || { err "NONCE LEAK — a nonce reached text the agent will see. Aborting."; exit 3; }

  # ── Gate 1 / 1b: the prior check, at ZERO agent runs ───────────────────────
  echo ""
  echo "==> Prior check (Gate 1 pristine tree + Gate 1b near-miss patches)"
  PRISTINE="$(mktemp -d)"
  git --git-dir="$JOB_DIR/repo.git" worktree add --detach "$PRISTINE/base" "$PINNED_SHA" >/dev/null 2>&1 || true
  if [[ -d "$PRISTINE/base" ]]; then
    mkdir -p "$JOB_DIR/.registry/prior_check"
    python3 "$LIB/wur/verify_pack.py" --facts "$REGISTRY" --base-tree "$PRISTINE/base" \
      --prior-check --out-dir "$JOB_DIR/.registry/prior_check" \
      --quiet \
      || err "WARN: prior check reported problems — see $JOB_DIR/.registry/prior_check/"
    git --git-dir="$JOB_DIR/repo.git" worktree remove --force "$PRISTINE/base" >/dev/null 2>&1 || true
  else
    err "WARN: could not materialize a pristine tree — Gate 1 skipped."
  fi
  rm -rf "$PRISTINE"

  # ── The autoload canary, once per (job, arm) ───────────────────────────────
  if [[ "$NO_CANARY" == "1" ]]; then
    echo ""
    echo "==> Autoload canary SKIPPED (--no-canary). preflight H12 will fail closed."
  elif ! command -v claude >/dev/null; then
    echo ""
    err "WARN: 'claude' not on PATH — autoload canary skipped."
  else
    echo ""
    echo "==> Autoload canary (verbatim system/init + D0 assay, one run per arm)"
    for ARM in $(python3 "$LIB/jobspec.py" conditions "$JOB_DIR"); do
      CANARY_OUT="$JOB_DIR/.registry/conditions/$ARM"
      [[ -f "$CANARY_OUT/canary.json" ]] && { echo "--> $ARM: canary already recorded"; continue; }
      CTMP="$(mktemp -d)"
      git --git-dir="$JOB_DIR/repo.git" worktree add --detach "$CTMP/ws" "$PINNED_SHA" >/dev/null 2>&1 || true
      if [[ -d "$CTMP/ws" ]]; then
        OVL=""
        for cand in "$JOB_DIR/.registry/conditions/$ARM/overlay" \
                    "$JOB_DIR"/.registry/conditions/*/"$ARM"/overlay; do
          [[ -d "$cand" ]] && { OVL="$cand"; break; }
        done
        [[ -n "$OVL" ]] && cp -r "$OVL"/. "$CTMP/ws/"
        # The sentinel is what makes the assay FALSIFIABLE. Without it the canary
        # cannot tell "the model did not see the fact" from "there was nothing to
        # see", reports `no sentinel supplied — presence is unfalsifiable`, and
        # exits 1 — after which preflight H12 fails closed on EVERY run of this
        # arm. The manifest beside the overlay we just copied carries it:
        # `nonce` for a fact-present arm, `paired_nonce` for a control (whose
        # whole assertion is that the treatment's nonce is ABSENT here).
        SENTINEL=""
        if [[ -n "$OVL" && -f "$(dirname "$OVL")/manifest.json" ]]; then
          SENTINEL="$(python3 -c 'import json,sys
m=json.load(open(sys.argv[1]))
print(m.get("nonce") or m.get("paired_nonce") or "")' "$(dirname "$OVL")/manifest.json" 2>/dev/null || true)"
        fi
        mkdir -p "$CANARY_OUT"
        python3 "$LIB/wur/canary.py" --workspace "$CTMP/ws" --arm "$ARM" \
          ${SENTINEL:+--sentinel "$SENTINEL"} \
          --job-dir "$JOB_DIR" --out-dir "$CANARY_OUT" --home "$CTMP/home" >/dev/null \
          || err "WARN: canary failed for arm '$ARM' — preflight H12 will block every run of it"
        git --git-dir="$JOB_DIR/repo.git" worktree remove --force "$CTMP/ws" >/dev/null 2>&1 || true
      fi
      rm -rf "$CTMP"
    done
  fi

  # ── Freeze the run order ───────────────────────────────────────────────────
  echo ""
  echo "==> Freezing the schedule (blocked randomisation)"
  AGENT_DASH="$(python3 "$LIB/agent.py" resolve \
                  --model "$(python3 "$LIB/jobspec.py" field "$JOB_DIR" model)" \
                  --field agent_dash 2>/dev/null || true)"
  python3 "$LIB/wur/schedule.py" --job-dir "$JOB_DIR" ${AGENT_DASH:+--agent-dash "$AGENT_DASH"} \
    >/dev/null || { err "could not freeze schedule.json."; exit 3; }
else
  # ══ LADDER chain — unchanged ═══════════════════════════════════════════════
  echo ""
  echo "==> Preparing context environments: $(python3 "$LIB/jobspec.py" envs "$JOB_DIR" | tr '\n' ' ')"
  python3 "$LIB/context_gen.py" --job-dir "$JOB_DIR" || {
    err "context generation failed — cannot build the environment ladder. Aborting."; exit 3
  }

  echo ""
  for TID in $(python3 "$LIB/jobspec.py" tasks "$JOB_DIR"); do
    echo "==> Synthesizing grader for task '$TID'"
    python3 "$LIB/judge.py" --synthesize --job-dir "$JOB_DIR" --task-id "$TID" || {
      err "grader synthesis failed for task '$TID' — cannot grade. Aborting."; exit 3
    }
  done
fi

# ── Run the agent over the job's cells ───────────────────────────────────────
echo ""
echo "==> Running $JOB_DIR"
[[ -n "$JOBS_N" ]] && export JOBS="$JOBS_N"
JOB_DIR="$JOB_DIR" bash "$LIB/run_job.sh"

# ── Job-level scorecard + auto-generated figures ─────────────────────────────
python3 "$LIB/report.py" --job-dir "$JOB_DIR" >/dev/null 2>&1 || true
python3 "$LIB/figures.py" --job-dir "$JOB_DIR" >/dev/null 2>&1 || true

# The parquet rollup needs pyarrow/pandas, which live in .venv-analysis and
# deliberately NOT in the runner's own interpreter (a harness that needs pandas to
# record a run stops recording when a wheel fails to build). Run it when that env
# exists; otherwise the command is printed below and nothing is lost — aggregate
# reads only artifacts that are already on disk.
AGGREGATED=0
if [[ "$EXPERIMENT" == "wur" && -f "$LIB/wur/aggregate.py" ]]; then
  if [[ -x "$HERE/.venv-analysis/bin/python" ]]; then
    "$HERE/.venv-analysis/bin/python" "$LIB/wur/aggregate.py" --job-dir "$JOB_DIR" --quiet \
      && AGGREGATED=1 || err "WARN: aggregate failed — rerun it by hand, nothing is lost."
  elif python3 -c 'import pyarrow, pandas' 2>/dev/null; then
    python3 "$LIB/wur/aggregate.py" --job-dir "$JOB_DIR" --quiet \
      && AGGREGATED=1 || err "WARN: aggregate failed — rerun it by hand, nothing is lost."
  fi
fi

echo ""
echo "Done. Results for this job:"
echo "  $JOB_DIR/REPORT.md                                    job scorecard"
if [[ "$EXPERIMENT" == "wur" ]]; then
  echo "  $JOB_DIR/schedule.json / schedule_actual.jsonl        intended vs realized cell order"
  echo "  $JOB_DIR/runs/<run-id>/fact_trace.jsonl               the headline table (one row per run × fact)"
  echo "  $JOB_DIR/runs/<run-id>/probes.jsonl                   probe answers + the parse ladder"
  echo "  $JOB_DIR/runs/<run-id>/exposure.jsonl                 which channel got the fact into context"
  echo "  $JOB_DIR/runs/<run-id>/hygiene.json                   preflight H1..H12"
  echo ""
  if [[ "$AGGREGATED" == "1" ]]; then
    echo "  $JOB_DIR/analysis/{fact_trace,probes,events}.parquet  the rollup (already built)"
    echo "  Next:  jupyter lab analysis/uptake.ipynb"
  else
    echo "  Next:  ./install.sh   # bootstraps .venv-analysis (pyarrow/pandas/lifelines)"
    echo "         .venv-analysis/bin/python lib/wur/aggregate.py --job-dir $JOB_DIR"
  fi
else
  ANALYZE="$(python3 "$LIB/jobspec.py" field "$JOB_DIR" analyze)"
  echo "  $JOB_DIR/agent-analysis/fig_token_cost_by_env.png     token cost per environment"
  echo "  $JOB_DIR/agent-analysis/fig_file_access_by_env.png    file-access heatmap"
  if [[ "$ANALYZE" != "False" ]]; then
  echo "  $JOB_DIR/agent-analysis/<run-id>.md                   per-run agent self-analysis"
  else
  echo "  (per-run self-analysis is OFF for multi-environment jobs — kept lean; use a single env to enable)"
  fi
fi
echo "  $JOB_DIR/runs/<run-id>/report.md                      per-run report (criteria, cost, patch)"
echo "  $JOB_DIR/runs/<run-id>/git.patch                      the solution (git apply git.patch)"
