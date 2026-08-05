#!/usr/bin/env bash
# setup_run.sh — prepare one isolated run.
#
# RESPONSIBILITY
#   Build everything a single cell needs before any agent starts, and nothing the
#   agent can see that we did not choose: an isolated git worktree at the pinned
#   SHA, this arm's overlay committed as the run's BASELINE, a per-run
#   CLAUDE_CONFIG_DIR holding only a credentials copy, the per-run settings.json
#   that is the sole source of this run's hooks, the frozen probe plan, and
#   run_meta.json.
#
# WHAT CHANGED, AND WHY IT IS THE WHOLE REASON THE MATRIX CAN RUN IN PARALLEL
#   The previous version merged hook config into the GLOBAL ~/.claude/settings.json
#   at :117-145 and restored it in teardown. Two concurrent runs therefore raced on
#   one file, which is why run_job.sh clamped claude to --jobs 1 and why the main
#   matrix was 146 h serial instead of 36 h at 4-way. That block is DELETED. Its
#   replacement is per-run CLAUDE_CONFIG_DIR + per-run --settings, measured clean
#   4-way against a real 6,000-file repo (spike S3). Nothing here writes outside
#   $RUN_DIR, $JOB_DIR or the worktree metadata this script already flock'd.
#
# Input (environment):
#   JOB_DIR        path to jobs/<job_id>                        (required)
#   RUN_ID         unique run id                                (required)
#   AGENT_ID       claude-sonnet-5 | codex | gemini-* | agy-*   (required)
#   RUN_DIR        the run directory                            (default: see below)
#   REP            replication integer                          (default 1)
#   TASK_ID        task id                                      (default t1)
#   ENV_ID         the ARM: a ladder rung (E0..E6) or a wur arm (d2, ctrl, …)
#   CONDITION_ID   the arm, when it differs from ENV_ID         (default $ENV_ID)
#   EXPERIMENT     ladder | wur                                 (default ladder)
#   OVERLAY_MODE   none | ladder | wur                          (default: derived)
#   BACKEND/MODEL  the resolved two-level agent identity        (optional)
#   BUDGET_STEPS PROBE_ENABLED PROBE_LO PROBE_HI PROBE_MAX_PROBES PROBE_SALT
#   GATE_TIMEOUT_MS MATRIX_SEED CELL_INDEX RUN_ORDER_INDEX CONCURRENCY_AT_LAUNCH
#   ATTEMPT
#
# Output:
#   $RUN_DIR/workspace/       git worktree at the pinned SHA, overlay committed
#   $RUN_DIR/run_meta.json    v2 identity + factors + the six sha256 stamps
#   $RUN_DIR/claude_home/     CLAUDE_CONFIG_DIR, credentials-only, mode 700  (wur)
#   $RUN_DIR/settings.json    the ONLY source of this run's hooks             (wur)
#   $RUN_DIR/probe_plan.json  the frozen cadence, written BEFORE the child    (wur)
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${JOB_DIR:?JOB_DIR is required}"
: "${RUN_ID:?RUN_ID is required}"
: "${AGENT_ID:?AGENT_ID is required}"
: "${REP:=1}"
: "${TASK_ID:=t1}"
: "${ENV_ID:=E0}"
: "${EXPERIMENT:=ladder}"
: "${CONDITION_ID:=$ENV_ID}"
: "${ATTEMPT:=1}"

JOB_DIR="$(cd "$JOB_DIR" && pwd)"
BARE="$JOB_DIR/repo.git"
VENV="$JOB_DIR/.venv"
LOCK="$JOB_DIR/.worktree.lock"
CREDS_LOCK="$JOB_DIR/.creds.lock"

field() { python3 "$LIB_DIR/jobspec.py" field "$JOB_DIR" "$1"; }
SHA="$(field repo.pinned_sha)"
[[ -n "$SHA" ]] || { echo "ERROR: repo.pinned_sha not set — run brew first" >&2; exit 1; }
[[ -d "$BARE" ]] || { echo "ERROR: bare repo missing at $BARE — run brew first" >&2; exit 1; }
JOB_ID="$(field job_id)"

# ── Where the run lives ──────────────────────────────────────────────────────
# A wur run root must NOT sit under $JOB_DIR: the fact registry lives at
# $JOB_DIR/.registry/, and V9 measured the agent READING a registry three `..`
# hops above its workspace under bypassPermissions. preflight H2 fails closed if
# $JOB_DIR is an ancestor of the workspace, so §5.3 puts wur runs under
# $ATLAS_RUNS_ROOT and leaves a symlink behind at the legacy path so report.py,
# figures.py and viz/server.py keep discovering runs where they always have.
if [[ -z "${RUN_DIR:-}" ]]; then
  if [[ "$EXPERIMENT" == "wur" ]]; then
    RUN_DIR="${ATLAS_RUNS_ROOT:-/tmp/atlas-runs}/$JOB_ID/$RUN_ID"
  else
    RUN_DIR="$JOB_DIR/runs/$RUN_ID"
  fi
fi
mkdir -p "$RUN_DIR"
RUN_DIR="$(cd "$RUN_DIR" && pwd -P)"
LEGACY_LINK="$JOB_DIR/runs/$RUN_ID"
if [[ "$RUN_DIR" != "$(cd "$JOB_DIR" && pwd -P)/runs/$RUN_ID" ]]; then
  mkdir -p "$JOB_DIR/runs"
  [[ -e "$LEGACY_LINK" && ! -L "$LEGACY_LINK" ]] \
    || ln -sfn "$RUN_DIR" "$LEGACY_LINK"
fi
WORKSPACE="$RUN_DIR/workspace"

# OVERLAY_MODE replaces the old MULTIENV flag. `ladder` strips the repo's own
# orienting docs and drops in environments/<rung>/; `wur` strips the CLI's own
# context surfaces (V5) and drops in .registry/conditions/<arm>/overlay/; `none`
# runs the repo as-is. MULTIENV=1 is still honoured so a half-updated caller does
# not silently downgrade a ladder job to a bare one.
if [[ -z "${OVERLAY_MODE:-}" ]]; then
  if [[ "$EXPERIMENT" == "wur" ]]; then OVERLAY_MODE=wur
  elif [[ "${MULTIENV:-0}" == "1" ]]; then OVERLAY_MODE=ladder
  else OVERLAY_MODE=none
  fi
fi

echo "==> Setting up run: $RUN_ID  (experiment=$EXPERIMENT overlay=$OVERLAY_MODE arm=$CONDITION_ID)"

# ── Isolated worktree at the pinned SHA (flock: worktree metadata is repo-wide) ──
echo "--> Creating worktree at $SHA"
(
  flock 200
  # Drop registrations whose directories are gone. Without this, a run dir deleted
  # by hand (or by a wiped $ATLAS_RUNS_ROOT) makes every later attempt at the same
  # run_id fail with "missing but already registered worktree" — a resumable job
  # that can never resume. prune only forgets worktrees that no longer exist.
  git --git-dir="$BARE" worktree prune
  git --git-dir="$BARE" worktree add --detach "$WORKSPACE" "$SHA"
  # info/exclude lives in the COMMON git dir and is therefore shared by every
  # worktree of this bare repo — an unlocked append from four concurrent runs
  # interleaves and corrupts it. It belongs in this lock, not after it.
  EXCLUDE_FILE="$(git -C "$WORKSPACE" rev-parse --git-path info/exclude)"
  mkdir -p "$(dirname "$EXCLUDE_FILE")"
  grep -qxF '/venv' "$EXCLUDE_FILE" 2>/dev/null || echo '/venv' >> "$EXCLUDE_FILE"
) 200>"$LOCK"

# ── Strip (mode-gated) ───────────────────────────────────────────────────────
# Whatever we remove is recorded verbatim in run_meta.stripped[]: a context
# channel that was closed silently is indistinguishable from one that was never
# there, and the difference matters when a read-rate looks surprising.
STRIPPED_LOG="$RUN_DIR/.stripped"
: > "$STRIPPED_LOG"
_strip() {   # _strip <path relative to workspace> ...
  local rel
  for rel in "$@"; do
    if [[ -e "$WORKSPACE/$rel" || -L "$WORKSPACE/$rel" ]]; then
      rm -rf "${WORKSPACE:?}/$rel"
      printf '%s\n' "$rel" >> "$STRIPPED_LOG"
    fi
  done
}
_strip_find() {  # _strip_find <basename> ... — anywhere in the tree, .git excluded
  local name rel found="$STRIPPED_LOG.found"
  : > "$found"
  for name in "$@"; do
    ( cd "$WORKSPACE" && find . -path ./.git -prune -o -name "$name" -print 2>/dev/null ) \
      | sed 's|^\./||' >> "$found" || true
  done
  while IFS= read -r rel; do
    [[ -n "$rel" ]] || continue
    [[ -e "$WORKSPACE/$rel" || -L "$WORKSPACE/$rel" ]] || continue
    rm -rf "${WORKSPACE:?}/$rel"
    printf '%s\n' "$rel" >> "$STRIPPED_LOG"
  done < "$found"
  rm -f "$found"
}

case "$OVERLAY_MODE" in
  ladder)
    echo "--> Applying environment overlay: $ENV_ID"
    _strip README.md AGENTS.md CLAUDE.md GEMINI.md PROJECT.md PLAN.md PROGRESS.md \
            OBJECTIVES.md CONTRIBUTING.md DEVELOPING.md .agents
    _strip_find AGENTS.md CLAUDE.md GEMINI.md
    OVERLAY_DIR="$JOB_DIR/environments/$ENV_ID"
    BASELINE_MSG="env-baseline:$ENV_ID"
    ;;
  wur)
    echo "--> Applying condition overlay: $CONDITION_ID"
    # V5: `--setting-sources project` re-enables project settings, so a repo-owned
    # .claude/ or .cursor/ would inject uncontrolled context directly upstream of
    # the measurement. The carrier files go too: H7 asserts EXACTLY ONE NOTES.md
    # per workspace and CLAUDE.md present iff the arm carries a pointer, and a
    # fixture that shipped its own would break both.
    _strip .claude .cursor .agents
    _strip_find AGENTS.md CLAUDE.md GEMINI.md NOTES.md
    OVERLAY_DIR=""
    for cand in "$JOB_DIR/.registry/conditions/$TASK_ID/$CONDITION_ID/overlay" \
                "$JOB_DIR/.registry/conditions/$CONDITION_ID/overlay"; do
      [[ -d "$cand" ]] && { OVERLAY_DIR="$cand"; break; }
    done
    if [[ -z "$OVERLAY_DIR" ]]; then
      echo "ERROR: no plant overlay for arm '$CONDITION_ID' (task '$TASK_ID')." >&2
      echo "       run 'python3 $LIB_DIR/wur/plant.py render --job-dir $JOB_DIR' first." >&2
      exit 1
    fi
    MANIFEST_PATH="$(dirname "$OVERLAY_DIR")/manifest.json"
    BASELINE_MSG="wur-baseline:$CONDITION_ID"
    ;;
  *)
    OVERLAY_DIR=""
    BASELINE_MSG="baseline:$CONDITION_ID"
    ;;
esac

if [[ -n "$OVERLAY_DIR" && -d "$OVERLAY_DIR" ]] \
   && [[ -n "$(find "$OVERLAY_DIR" -type f 2>/dev/null)" ]]; then
  cp -r "$OVERLAY_DIR"/. "$WORKSPACE/"
fi

# ── The baseline commit — now UNCONDITIONAL ──────────────────────────────────
# The solution diff is taken relative to the POST-overlay tree, so injected
# context never leaks into git.patch. It is unconditional because teardown now
# diffs `$BASELINE_SHA` rather than `HEAD`: a run with no overlay still needs a
# baseline object to diff against, and on a no-overlay run the commit is empty,
# so its tree equals the pinned SHA's tree and the captured patch is unchanged.
( cd "$WORKSPACE"
  git add -A
  git -c user.email=exp-runner@local -c user.name=exp-runner commit -q \
    -m "$BASELINE_MSG" --allow-empty
)
BASELINE_SHA="$(git -C "$WORKSPACE" rev-parse HEAD)"
# A PER-RUN ref, not the flat `refs/atlas/baseline` of §5.2. Refs outside
# refs/worktree/ are SHARED by every worktree of the bare repo, so under four-way
# concurrency the flat name is last-writer-wins: three of four runs would lose the
# only pointer keeping their baseline commit reachable once their worktree is gone,
# and preflight H10 (which FAILS when refs/atlas/baseline != this run's
# baseline_sha) would fail three of them for a name collision rather than a real
# hygiene problem. Consequence, accepted: H10 records a WARN that the flat ref is
# unset. A warn is not a gate failure; a wrong ref would be.
git -C "$WORKSPACE" update-ref "refs/atlas/baseline-run/$RUN_ID" "$BASELINE_SHA"
echo "--> Baseline committed: ${BASELINE_SHA:0:12} ($BASELINE_MSG)"

# ── Provide the hermetic venv to the agent as ./venv, but keep it out of the diff ──
# (the /venv exclude line was appended inside the worktree lock, above)
if [[ -x "$VENV/bin/python" ]]; then
  ln -sfn "$VENV" "$WORKSPACE/venv"
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

# ── Per-run Claude home + settings + probe plan (claude only) ────────────────
# This is what replaced the global ~/.claude/settings.json mutation.
SESSION_UUID=""
CLI_VERSION=""
SETTINGS_PATH=""
if [[ "$AGENT_ID" == claude-* || "${BACKEND:-}" == "claude" ]]; then
  CLAUDE_HOME="$RUN_DIR/claude_home"
  mkdir -p "$CLAUDE_HOME"
  chmod 700 "$CLAUDE_HOME"
  # ONLY .credentials.json. Anything else copied out of ~/.claude is uncontrolled
  # context sitting directly upstream of the measurement (S2). The lock is on the
  # SOURCE: four concurrent readers of a file the CLI itself may be rewriting.
  (
    flock 201
    if [[ -f "$HOME/.claude/.credentials.json" ]]; then
      cp "$HOME/.claude/.credentials.json" "$CLAUDE_HOME/.credentials.json"
    fi
  ) 201>"$CREDS_LOCK"
  if [[ -f "$CLAUDE_HOME/.credentials.json" ]]; then
    chmod 600 "$CLAUDE_HOME/.credentials.json"
  else
    echo "--> WARN: no ~/.claude/.credentials.json to seed — the child will not authenticate" >&2
  fi

  SESSION_UUID="$(python3 - "$RUN_ID" <<'PY'
import hashlib, sys, uuid
# Byte-identical to lib/wur/driver.py::_uuid_for — teardown copies the transcript
# BY --session-id, so the id must be reconstructible if run_meta.json is lost.
print(str(uuid.UUID(bytes=hashlib.sha256(("wur-session|" + sys.argv[1]).encode()).digest()[:16],
                    version=4)))
PY
)"
  # The BARE version ("2.1.222"), not the full banner ("2.1.222 (Claude Code)").
  # preflight H12 compares `claude --version | split()[0]` against the canary's
  # `system/init.claude_code_version`, which is also bare; recording the banner
  # here would put a third spelling of the same fact into run_record.condition.
  # `< /dev/null`: without it every one-shot pays a 3 s stall (V19).
  CLI_VERSION="$(claude --version < /dev/null 2>/dev/null | head -1 | awk '{print $1}' || true)"
fi

if [[ "$EXPERIMENT" == "wur" ]]; then
  : "${GATE_TIMEOUT_MS:=300000}"
  : "${BUDGET_STEPS:=30}"
  : "${PROBE_LO:=1}"
  : "${PROBE_HI:=3}"
  : "${PROBE_MAX_PROBES:=24}"
  : "${PROBE_SALT:=wur-v1}"
  : "${PROBE_ENABLED:=true}"

  mkdir -p "$RUN_DIR/gate/req" "$RUN_DIR/gate/resp" "$RUN_DIR/watch/persisted"

  # V12: `claude --help` states that in --print mode "settings files that fail
  # validation are silently ignored" — a templating bug yields zero hooks, zero
  # barriers, zero probes and NO ERROR ANYWHERE. settings.py re-reads its own
  # output; we json.load it again from here because a good render someone edited
  # afterwards is a different failure from a bad render.
  SETTINGS_PATH="$(python3 "$LIB_DIR/wur/settings.py" --run-dir "$RUN_DIR" \
                     --mode barrier --timeout-ms "$GATE_TIMEOUT_MS")"
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$SETTINGS_PATH" \
    || { echo "ERROR: rendered settings.json is not valid JSON: $SETTINGS_PATH" >&2; exit 1; }
  echo "--> Settings rendered + validated: $SETTINGS_PATH"

  # The cadence is frozen BEFORE the child starts, so the schedule exists even if
  # the run dies and can be diffed across arms of the same (task, rep).
  python3 "$LIB_DIR/wur/cadence.py" --task-id "$TASK_ID" --rep "$REP" \
    --budget "$BUDGET_STEPS" --lo "$PROBE_LO" --hi "$PROBE_HI" \
    --max-probes "$PROBE_MAX_PROBES" --salt "$PROBE_SALT" > "$RUN_DIR/probe_plan.json.tmp"
  mv "$RUN_DIR/probe_plan.json.tmp" "$RUN_DIR/probe_plan.json"
  echo "--> Probe plan frozen: $RUN_DIR/probe_plan.json"
fi

# ── Run metadata (v2) ────────────────────────────────────────────────────────
TIMESTAMP_START=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# The child argv, hashed BEFORE the run so the stamp survives a dead run. The
# driver recomputes it from the argv it actually launched and teardown lifts that
# measured value over this one.
ARGV_JSON="$RUN_DIR/.print_argv.json"
if [[ "$EXPERIMENT" == "wur" && -n "$SESSION_UUID" ]]; then
  RUN_DIR="$RUN_DIR" TASK_PROMPT="${TASK_PROMPT:-preflight}" \
  python3 "$LIB_DIR/wur/driver.py" --run-dir "$RUN_DIR" --print-argv \
      --session-id "$SESSION_UUID" --run-id "$RUN_ID" \
      ${MODEL:+--model "$MODEL"} > "$ARGV_JSON" 2>/dev/null || rm -f "$ARGV_JSON"
fi

export ATLAS_LIB_DIR="$LIB_DIR"
export JOB_DIR RUN_DIR RUN_ID JOB_ID TASK_ID ENV_ID CONDITION_ID REP AGENT_ID
export EXPERIMENT OVERLAY_MODE BASELINE_SHA WORKSPACE TIMESTAMP_START SESSION_UUID
export CLI_VERSION ATTEMPT
export BASE_SHA="$SHA"
export MANIFEST_PATH="${MANIFEST_PATH:-}"
export OVERLAY_DIR="${OVERLAY_DIR:-}"
export ARGV_JSON
export REPO_URL="$(field repo.url)"
export REPO_PATH="$(field repo.path)"
export BACKEND="${BACKEND:-}" MODEL="${MODEL:-}"
export BUDGET_STEPS="${BUDGET_STEPS:-}" PROBE_ENABLED="${PROBE_ENABLED:-}"
export MATRIX_SEED="${MATRIX_SEED:-}" CELL_INDEX="${CELL_INDEX:-}"
export RUN_ORDER_INDEX="${RUN_ORDER_INDEX:-}" CONCURRENCY_AT_LAUNCH="${CONCURRENCY_AT_LAUNCH:-}"

python3 - <<'PY'
"""Assemble run_meta.json v2 — identity, the §7.1 factors, and the six stamps."""
import hashlib, json, os, subprocess, sys
from pathlib import Path

env = os.environ
lib = Path(env["ATLAS_LIB_DIR"])
sys.path.insert(0, str(lib))
sys.path.insert(0, str(lib / "wur"))
run_dir = Path(env["RUN_DIR"])
job_dir = Path(env["JOB_DIR"])
workspace = Path(env["WORKSPACE"])
experiment = env.get("EXPERIMENT", "ladder")
arm = env.get("CONDITION_ID") or env.get("ENV_ID") or "E0"


def _int(name):
    v = env.get(name, "")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _run(*args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=300)
        return p.stdout if p.returncode == 0 else ""
    except Exception:
        return ""


# workspace_sha256 — the content of the tree the model was handed. Hashing
# `git ls-tree -r` (mode, type, object id, path) is exactly "what is in this
# tree", is stable across checkouts, and costs one fork instead of a walk.
listing = _run("git", "-C", str(workspace), "ls-tree", "-r", env["BASELINE_SHA"])
workspace_sha256 = hashlib.sha256(listing.encode()).hexdigest() if listing else None

manifest = _json_file(env["MANIFEST_PATH"]) if env.get("MANIFEST_PATH") else None
overlay_sha256 = (manifest or {}).get("overlay_sha256")
if overlay_sha256 is None and env.get("OVERLAY_DIR") and Path(env["OVERLAY_DIR"]).is_dir():
    h = hashlib.sha256()
    root = Path(env["OVERLAY_DIR"])
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        h.update(str(p.relative_to(root)).encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    overlay_sha256 = h.hexdigest()

# The frozen protocol stamps belong to a wur record only. Writing them on a ladder
# run would put wur provenance into a ladder run_record.condition, and the two must
# never look poolable.
frozen = {}
if experiment == "wur":
    try:
        import protocol  # noqa: PLC0415  (lib/wur on sys.path)
        frozen = protocol.frozen_hashes()
    except Exception:
        frozen = {}

# init_sha256 / canary_sha256 are per (job, arm), not per run: they come from the
# canary that ran once for this arm. Absent canary -> null, never a fabricated hash.
canary = None
for cand in (job_dir / ".registry" / "conditions" / env.get("TASK_ID", "") / arm / "canary.json",
             job_dir / ".registry" / "conditions" / arm / "canary.json"):
    canary = _json_file(cand)
    if canary:
        break

argv_rec = _json_file(env["ARGV_JSON"]) if env.get("ARGV_JSON") else None

factors = None
if experiment == "wur":
    try:
        import jobspec  # noqa: PLC0415
        factors = jobspec.condition_factors(jobspec.load(job_dir), arm)
    except Exception:
        factors = None

stripped = []
sp = run_dir / ".stripped"
if sp.exists():
    stripped = sorted({line.strip() for line in sp.read_text().splitlines() if line.strip()})

probe_plan = _json_file(run_dir / "probe_plan.json") or {}

meta = {
    "schema_version": "2" if experiment == "wur" else "1",
    "run_id": env["RUN_ID"],
    "job_id": env["JOB_ID"],
    "task_id": env["TASK_ID"],
    "env_id": arm,
    "condition_id": arm,
    "replication": int(env.get("REP") or 1),
    "attempt": int(env.get("ATTEMPT") or 1),
    "timestamp_start": env["TIMESTAMP_START"],
    "agent_id": env["AGENT_ID"],
    "experiment": experiment,
    "overlay_mode": env.get("OVERLAY_MODE"),
    "base_repo_sha": env["BASE_SHA"],
    "baseline_sha": env["BASELINE_SHA"],
    "repo_url": env.get("REPO_URL") or None,
    "repo_path": env.get("REPO_PATH") or None,
    "workspace_path": str(workspace),
    "stripped": stripped,
}
for key, val in (("backend", env.get("BACKEND")), ("model", env.get("MODEL")),
                 ("cli_version", env.get("CLI_VERSION"))):
    if val:
        meta[key] = val
if factors:
    meta["factors"] = factors
if env.get("SESSION_UUID"):
    meta["session_id"] = env["SESSION_UUID"]
for key in ("MATRIX_SEED", "CELL_INDEX", "RUN_ORDER_INDEX", "CONCURRENCY_AT_LAUNCH"):
    v = _int(key)
    if v is not None:
        meta[key.lower()] = v
if probe_plan:
    meta["probe_seed"] = probe_plan.get("seed_int")
    meta["probe_seed_material"] = probe_plan.get("seed_material")

# The counter-prior gate result, carried onto every fact_trace row so no analysis
# has to go looking for it (STATUS §4.4 "gates and outcome"). trace.py reads both
# fields off run_meta; the gate itself ran once per task, at job setup, and its
# verdict is a property of the fact rather than of this run. Without this copy
# `prior_check_status` and `control_fire_rate` are null on every row and the
# weak-fact exclusion (D2) has nothing to key off.
_pc = _json_file(job_dir / ".registry" / "prior_check" / f"{env['TASK_ID']}.json")
if isinstance(_pc, dict):
    _row = _pc
    if isinstance(_pc.get("facts"), list) and _pc["facts"]:
        _row = _pc["facts"][0]
    status = _row.get("prior_check_status") or _row.get("status")
    if status:
        meta["prior_check_status"] = status
    if _row.get("control_fire_rate") is not None:
        meta["control_fire_rate"] = _row["control_fire_rate"]
if env.get("PROBE_ENABLED"):
    meta["probe_enabled"] = env["PROBE_ENABLED"].strip().lower() in ("1", "true", "yes", "on")
if env.get("BUDGET_STEPS"):
    meta["budget"] = {"steps": _int("BUDGET_STEPS")}
if overlay_sha256:
    meta["overlay_sha256"] = overlay_sha256
    meta["env_overlay_hash"] = overlay_sha256
if workspace_sha256:
    meta["workspace_sha256"] = workspace_sha256
if frozen.get("pacing_prompt_sha256"):
    meta["pacing_prompt_sha256"] = frozen["pacing_prompt_sha256"]
if frozen.get("probe_text_sha256"):
    meta["probe_text_sha256"] = frozen["probe_text_sha256"]
if frozen:
    meta["protocol"] = frozen
if argv_rec and argv_rec.get("cli_argv_sha256"):
    meta["cli_argv_sha256"] = argv_rec["cli_argv_sha256"]
if canary:
    if canary.get("init_sha256"):
        meta["init_sha256"] = canary["init_sha256"]
    if canary.get("canary_sha256"):
        meta["canary_sha256"] = canary["canary_sha256"]
if manifest:
    meta["plant"] = {
        "manifest_path": env.get("MANIFEST_PATH"),
        "fact_id": manifest.get("fact_id"),
        "fact_present": manifest.get("fact_present"),
        "nonce": manifest.get("nonce"),
        "notes_path": manifest.get("notes_path"),
        "claude_md_path": manifest.get("claude_md_path"),
        "exposure_basis": manifest.get("exposure_basis"),
    }

tmp = run_dir / "run_meta.json.tmp"
tmp.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
tmp.replace(run_dir / "run_meta.json")
print(f"--> run_meta.json written ({len(meta)} keys)")
PY

rm -f "$STRIPPED_LOG" "$ARGV_JSON"
echo "==> Run $RUN_ID ready. Workspace: $WORKSPACE"
