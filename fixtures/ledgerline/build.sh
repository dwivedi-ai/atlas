#!/usr/bin/env bash
# build.sh — materialize fixtures/ledgerline/tree as a real git repository with a
# reproducible commit SHA.
#
# WHY THIS EXISTS
#   The fixture ships as a plain tree plus this builder, not as a nested git
#   repo: `git clone --bare` of a plain subdirectory fails, so lib/brew.sh needs
#   a real repository to clone from — and lib/wur/nonce.py mints every nonce as
#   a pure function of (salt, repo_sha, fact_id), so that SHA has to be stable
#   forever. A tree committed with the ambient user identity and the current
#   clock would produce a different SHA on every machine and every run, and the
#   whole registry would re-mint.
#
#   Everything that feeds the commit hash is therefore pinned here: the author
#   and committer identity, both timestamps, the commit message, the file modes,
#   and the line-ending configuration. What is NOT pinned is the output
#   directory, which is a per-invocation detail and does not reach the object
#   database.
#
# INPUTS
#   fixtures/ledgerline/tree/       the source tree (the only thing to edit)
#   fixtures/ledgerline/repo_sha.txt the expected commit SHA
#
# OUTPUTS
#   A git repository at --out (default $ATLAS_FIXTURE_OUT, else
#   ${TMPDIR:-/tmp}/atlas-fixtures/ledgerline) whose HEAD is repo_sha.txt.
#   The payload — the path, the SHA, or both as JSON — goes to STDOUT; every
#   progress line goes to STDERR, so `REPO="$(bash build.sh)"` works.
#
# USAGE
#   bash build.sh [--out DIR] [--print path|sha|json] [--check] [--write-sha] [--force]
#     --check      build into a temp dir, verify the SHA, remove it. Exit 1 on drift.
#     --write-sha  (re)write repo_sha.txt from the built commit. For the first
#                  build and for a deliberate fixture change; never in CI.
#     --force      replace a non-empty --out directory.
#   Exit 0 = built and verified, 1 = SHA drift or a build failure, 2 = usage.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TREE="$HERE/tree"
SHA_FILE="$HERE/repo_sha.txt"

# ── the pinned identity. Changing any line below changes the commit SHA. ──────
FIXTURE_NAME="ledgerline"
GIT_AUTHOR_NAME="ledgerline fixture"
GIT_AUTHOR_EMAIL="fixture@ledgerline.invalid"
GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
# Fixed instant, fixed offset. `date -d` is never consulted.
GIT_AUTHOR_DATE="2024-11-18T09:00:00+00:00"
GIT_COMMITTER_DATE="$GIT_AUTHOR_DATE"
COMMIT_MESSAGE="ledgerline 0.7.2 fixture tree"
DEFAULT_BRANCH="main"
export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL
export GIT_AUTHOR_DATE GIT_COMMITTER_DATE

# Paths that are executable in the committed tree. Everything else is 0644.
# Listed rather than inherited from the source tree so a checkout made with an
# odd umask, an archive extraction, or a copy through a filesystem without a
# permission bit still produces the same SHA.
EXECUTABLE_PATHS=(
  "scripts/check_ledger.sh"
  "scripts/regen_fixtures.sh"
  "scripts/gen_fixtures.py"
)

# Never copied into the repository: build detritus that would otherwise make the
# SHA depend on whether anyone had run the suite in the source tree.
EXCLUDES=(
  "--exclude=.git"
  "--exclude=__pycache__"
  "--exclude=*.pyc"
  "--exclude=*.pyo"
  "--exclude=.pytest_cache"
  "--exclude=.mypy_cache"
  "--exclude=.DS_Store"
)

log() { printf '%s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }

OUT=""; PRINT="path"; CHECK=0; WRITE_SHA=0; FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)       OUT="${2:?--out needs a directory}"; shift 2;;
    --print)     PRINT="${2:?--print needs path|sha|json}"; shift 2;;
    --check)     CHECK=1; shift;;
    --write-sha) WRITE_SHA=1; shift;;
    --force)     FORCE=1; shift;;
    -h|--help)   sed -n '2,36p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 0;;
    *)           die "unknown argument: $1" 2;;
  esac
done
case "$PRINT" in path|sha|json) ;; *) die "--print must be path, sha or json" 2;; esac

[[ -d "$TREE" ]] || die "no fixture tree at $TREE"
command -v git >/dev/null 2>&1 || die "git is not on PATH"

CLEANUP_DIR=""
# Written as an `if`, not as `[[ ... ]] && rm`: a false test would make the trap
# return 1, and a trap's status on EXIT becomes the script's exit status.
cleanup() {
  if [[ -n "$CLEANUP_DIR" ]]; then rm -rf "$CLEANUP_DIR"; fi
  return 0
}
trap cleanup EXIT

if [[ "$CHECK" == "1" ]]; then
  CLEANUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ledgerline-check-XXXXXX")"
  OUT="$CLEANUP_DIR/$FIXTURE_NAME"
elif [[ -z "$OUT" ]]; then
  OUT="${ATLAS_FIXTURE_OUT:-${TMPDIR:-/tmp}/atlas-fixtures/$FIXTURE_NAME}"
fi

if [[ -e "$OUT" ]]; then
  if [[ "$FORCE" == "1" || "$CHECK" == "1" ]]; then
    rm -rf "$OUT"
  elif [[ -d "$OUT/.git" ]]; then
    # An existing build is only reusable if it is already the right commit.
    EXISTING="$(git -C "$OUT" rev-parse HEAD 2>/dev/null || true)"
    if [[ -f "$SHA_FILE" && "$EXISTING" == "$(tr -d '[:space:]' < "$SHA_FILE")" ]]; then
      log "[ok]   $OUT already at $EXISTING"
      case "$PRINT" in
        path) printf '%s\n' "$OUT";;
        sha)  printf '%s\n' "$EXISTING";;
        json) printf '{"path":"%s","sha":"%s","reused":true}\n' "$OUT" "$EXISTING";;
      esac
      exit 0
    fi
    rm -rf "$OUT"
  else
    die "$OUT exists and is not a ledgerline build; pass --force to replace it"
  fi
fi

log "[..]   copying $TREE -> $OUT"
mkdir -p "$(dirname "$OUT")"
mkdir -p "$OUT"
# A trailing slash on the source copies the CONTENTS, which is what makes the
# committed paths relative to the repository root rather than to tree/.
if command -v rsync >/dev/null 2>&1; then
  rsync -a "${EXCLUDES[@]}" "$TREE/" "$OUT/"
else
  ( cd "$TREE" && tar -cf - \
      --exclude=.git --exclude=__pycache__ --exclude='*.pyc' --exclude='*.pyo' \
      --exclude=.pytest_cache --exclude=.mypy_cache --exclude=.DS_Store . \
  ) | ( cd "$OUT" && tar -xf - )
fi

log "[..]   normalizing modes"
find "$OUT" -type d -exec chmod 755 {} +
find "$OUT" -type f -exec chmod 644 {} +
for rel in "${EXECUTABLE_PATHS[@]}"; do
  [[ -f "$OUT/$rel" ]] || die "EXECUTABLE_PATHS names a missing file: $rel"
  chmod 755 "$OUT/$rel"
done

log "[..]   git init + commit (pinned identity, pinned dates)"
GIT_CFG=(
  -c "core.autocrlf=false"
  -c "core.eol=lf"
  -c "core.fileMode=true"
  -c "core.symlinks=false"
  -c "commit.gpgsign=false"
  -c "gc.auto=0"
  -c "init.defaultBranch=$DEFAULT_BRANCH"
  -c "user.name=$GIT_AUTHOR_NAME"
  -c "user.email=$GIT_AUTHOR_EMAIL"
)
git "${GIT_CFG[@]}" -C "$OUT" init -q
git "${GIT_CFG[@]}" -C "$OUT" add -A
git "${GIT_CFG[@]}" -C "$OUT" commit -q -m "$COMMIT_MESSAGE" --no-verify

BUILT_SHA="$(git -C "$OUT" rev-parse HEAD)"
TREE_SHA="$(git -C "$OUT" rev-parse 'HEAD^{tree}')"
N_FILES="$(git -C "$OUT" ls-tree -r --name-only HEAD | wc -l | tr -d ' ')"
log "[ok]   commit $BUILT_SHA  tree $TREE_SHA  ($N_FILES files)"

if [[ "$WRITE_SHA" == "1" ]]; then
  printf '%s\n' "$BUILT_SHA" > "$SHA_FILE"
  log "[ok]   wrote $SHA_FILE"
elif [[ -f "$SHA_FILE" ]]; then
  EXPECTED="$(tr -d '[:space:]' < "$SHA_FILE")"
  if [[ "$BUILT_SHA" != "$EXPECTED" ]]; then
    log ""
    log "SHA DRIFT"
    log "  expected (repo_sha.txt): $EXPECTED"
    log "  built:                   $BUILT_SHA"
    log ""
    log "  Every nonce in .registry/facts.yaml is minted from this SHA, so a"
    log "  change here re-mints the whole registry and makes old runs"
    log "  incomparable. If the tree change was deliberate, re-run with"
    log "  --write-sha and re-mint; otherwise find what changed:"
    log "      git -C $OUT ls-tree -r HEAD"
    exit 1
  fi
  log "[ok]   SHA matches repo_sha.txt"
else
  die "no $SHA_FILE — bootstrap it with: bash build.sh --write-sha"
fi

if [[ "$CHECK" == "1" ]]; then
  log "[ok]   check passed"
  case "$PRINT" in
    path) printf '%s\n' "$TREE";;
    sha)  printf '%s\n' "$BUILT_SHA";;
    json) printf '{"path":"%s","sha":"%s","checked":true}\n' "$TREE" "$BUILT_SHA";;
  esac
  exit 0
fi

case "$PRINT" in
  path) printf '%s\n' "$OUT";;
  sha)  printf '%s\n' "$BUILT_SHA";;
  json) printf '{"path":"%s","sha":"%s","tree":"%s","files":%s}\n' \
          "$OUT" "$BUILT_SHA" "$TREE_SHA" "$N_FILES";;
esac
