#!/usr/bin/env bash
# brew.sh — the "brew" step: make a job hermetic and ready to run.
#
# Generalized from experiments/l4/harness/scripts/bootstrap.sh. Idempotent.
# Given a job folder it produces, inside that folder:
#   repo.git/   — a self-contained bare clone of the user's repo (source of every
#                 run's worktree). Cloned once; the user's repo is never touched again.
#   .venv/      — a hermetic Python env (deps from the repo's requirements.txt /
#                 pyproject), used by the grader/battery to run tests.
#   .brew_done  — sentinel written on success.
# It also resolves repo.ref -> a concrete SHA and pins it back into job.yaml, and
# records the detected stack into build.detected.
#
# Input (environment):
#   JOB_DIR   — path to jobs/<job_id> (required)
#   REBUILD_VENV=1 — rebuild .venv from scratch (optional)
#
# All output is tee'd to $JOB_DIR/brew.log.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${JOB_DIR:?JOB_DIR is required (path to jobs/<job_id>)}"
JOB_DIR="$(cd "$JOB_DIR" && pwd)"
JOB_YAML="$JOB_DIR/job.yaml"
[[ -f "$JOB_YAML" ]] || { echo "ERROR: no job.yaml in $JOB_DIR" >&2; exit 1; }

BARE="$JOB_DIR/repo.git"
VENV="$JOB_DIR/.venv"
LOCK="$JOB_DIR/.worktree.lock"
BREW_LOG="$JOB_DIR/brew.log"

# Mirror all stdout/stderr into brew.log (append; keep history across re-brews).
exec > >(tee -a "$BREW_LOG") 2>&1
echo "================================================================"
echo " brew  $(date -u +%Y-%m-%dT%H:%M:%SZ)   job_dir=$JOB_DIR"
echo "================================================================"

field() { python3 "$LIB_DIR/jobspec.py" field "$JOB_DIR" "$1"; }
setfield() { python3 "$LIB_DIR/jobspec.py" set "$JOB_DIR" "$1" "$2"; }

REPO_URL="$(field repo.url)"
REPO_PATH="$(field repo.path)"
REF="$(field repo.ref)";        [[ -z "$REF" ]] && REF="HEAD"
BUILD_STACK="$(field build.stack)"; [[ -z "$BUILD_STACK" ]] && BUILD_STACK="auto"
BUILD_CMD="$(field build.command)"

# ── 1. Bare clone (idempotent) ───────────────────────────────────────────────
if [[ -d "$BARE" ]]; then
  echo "[ok]   bare repo already present: repo.git"
else
  if [[ -n "$REPO_PATH" ]]; then
    SRC="$REPO_PATH"
    echo "[..]   cloning bare from local path: $SRC"
  elif [[ -n "$REPO_URL" ]]; then
    SRC="$REPO_URL"
    echo "[..]   cloning bare from url: $SRC"
  else
    echo "ERROR: job has neither repo.url nor repo.path" >&2; exit 1
  fi
  git clone --bare "$SRC" "$BARE"
  # Drop alternates so the bare repo is fully self-contained / portable.
  rm -f "$BARE/objects/info/alternates"
  echo "[ok]   bare repo created and de-aliased"
fi

# ── 2. Resolve ref -> SHA and pin it ──────────────────────────────────────────
SHA="$(git --git-dir="$BARE" rev-parse --verify -q "${REF}^{commit}" 2>/dev/null || true)"
if [[ -z "$SHA" ]]; then
  SHA="$(git --git-dir="$BARE" rev-parse --verify -q "$REF" 2>/dev/null || true)"
fi
if [[ -z "$SHA" ]] || [[ "$(git --git-dir="$BARE" cat-file -t "$SHA" 2>/dev/null)" != "commit" ]]; then
  echo "ERROR: could not resolve ref '$REF' to a commit in the repo." >&2
  echo "       Check the branch/tag/SHA exists." >&2
  exit 1
fi
echo "[ok]   pinned ref '$REF' -> $SHA"
setfield repo.pinned_sha "$SHA"

# ── 3. Detect stack via a temporary worktree at the pinned SHA ───────────────
TMP_WS="$JOB_DIR/.brew-src"
cleanup() {
  ( flock 200; git --git-dir="$BARE" worktree remove --force "$TMP_WS" 2>/dev/null || rm -rf "$TMP_WS"; ) 200>"$LOCK" || true
}
trap cleanup EXIT
rm -rf "$TMP_WS"
( flock 200; git --git-dir="$BARE" worktree add --detach "$TMP_WS" "$SHA"; ) 200>"$LOCK"

DETECT_JSON="$(python3 "$LIB_DIR/detect_stack.py" "$TMP_WS")"
DETECTED_STACK="$(printf '%s' "$DETECT_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["stack"])')"
PY_INSTALL="$(printf '%s' "$DETECT_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("py_install") or "")')"
echo "[ok]   detected stack: $DETECTED_STACK  ($DETECT_JSON)"
setfield build.detected "$DETECTED_STACK"

EFFECTIVE_STACK="$BUILD_STACK"
[[ "$BUILD_STACK" == "auto" ]] && EFFECTIVE_STACK="$DETECTED_STACK"
echo "[ok]   effective stack: $EFFECTIVE_STACK"

# ── 4. Build the env ─────────────────────────────────────────────────────────
if [[ "${REBUILD_VENV:-0}" == "1" && -d "$VENV" ]]; then
  echo "[..]   REBUILD_VENV=1 -> removing existing .venv"; rm -rf "$VENV"
fi

build_python_venv() {
  if [[ -x "$VENV/bin/pytest" ]]; then
    echo "[ok]   .venv already present with pytest"
    return 0
  fi
  echo "[..]   building hermetic .venv"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  if [[ "$PY_INSTALL" == "requirements" && -f "$TMP_WS/requirements.txt" ]]; then
    echo "[..]   pip install -r requirements.txt"
    "$VENV/bin/pip" install -q -r "$TMP_WS/requirements.txt" || {
      echo "WARN: requirements install reported errors; continuing" >&2; }
  elif [[ "$PY_INSTALL" == "project" ]]; then
    echo "[..]   pip install . (from pyproject/setup)"
    "$VENV/bin/pip" install -q "$TMP_WS" || {
      echo "WARN: project install reported errors; continuing" >&2; }
  fi
  # The grader runs the battery + the repo's tests; pytest must be present.
  "$VENV/bin/pip" install -q pytest
  # Optional user-supplied extra build step (e.g. dev deps), run with venv on PATH.
  if [[ -n "$BUILD_CMD" ]]; then
    echo "[..]   running build.command: $BUILD_CMD"
    ( cd "$TMP_WS"; PATH="$VENV/bin:$PATH" bash -lc "$BUILD_CMD" ) || {
      echo "WARN: build.command reported errors; continuing" >&2; }
  fi
  echo "[ok]   .venv built: $("$VENV/bin/python" --version 2>&1), $("$VENV/bin/pytest" --version 2>&1 | head -1)"
}

case "$EFFECTIVE_STACK" in
  python)
    build_python_venv
    ;;
  node|other|none)
    echo "[..]   stack '$EFFECTIVE_STACK': no Python venv auto-build (Python-first v1)."
    if [[ -n "$BUILD_CMD" ]]; then
      echo "[..]   running build.command in a checkout: $BUILD_CMD"
      ( cd "$TMP_WS"; bash -lc "$BUILD_CMD" ) || {
        echo "WARN: build.command reported errors; continuing" >&2; }
      echo "NOTE: non-Python builds run per-checkout; deps are not shared across reps in v1." >&2
    else
      echo "NOTE: no build.command given for a non-Python repo — runs will use the repo as-is." >&2
    fi
    # Still provide an empty venv so the grader has pytest available for shell ACs.
    if [[ ! -x "$VENV/bin/python" ]]; then
      python3 -m venv "$VENV"; "$VENV/bin/pip" install -q --upgrade pip pytest || true
    fi
    ;;
  *)
    echo "ERROR: unknown stack '$EFFECTIVE_STACK'" >&2; exit 1
    ;;
esac

# ── 5. Done ──────────────────────────────────────────────────────────────────
date -u +%Y-%m-%dT%H:%M:%SZ > "$JOB_DIR/.brew_done"
echo ""
echo "[ok]   brew complete."
echo "       bare repo : $BARE"
echo "       venv      : $VENV"
echo "       pinned_sha: $SHA"
echo "       stack     : $EFFECTIVE_STACK (detected: $DETECTED_STACK)"
