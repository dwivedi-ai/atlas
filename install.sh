#!/usr/bin/env bash
# install.sh — set up exp-runner on this machine for a teammate.
#
#   git clone <repo> && cd exp-runner && ./install.sh
#
# What it does (idempotent):
#   1. checks prerequisites (git, python3 >= 3.9; the DEFAULT BACKEND IS CLAUDE, so
#      a missing `claude` is called out first — you must be logged in to run jobs);
#   2. makes the runtime Python deps (PyYAML, jsonschema, matplotlib, numpy)
#      importable — using whatever the machine already has, else `pip install
#      --user`, else a private .runner-venv;
#   3. bootstraps .venv-analysis, the SEPARATE environment the offline analysis
#      chain needs (pyarrow, pandas, lifelines). It is separate on purpose: the
#      runner must stay installable on a machine that will never open a notebook,
#      and pyarrow alone is a ~100 MB wheel;
#   4. drops an `exp-runner` command on your PATH (a thin wrapper around run.sh).
#
# Env overrides:
#   BINDIR=/somewhere/bin   where to put the `exp-runner` wrapper (default ~/.local/bin)
#   SKIP_ANALYSIS_VENV=1    do not build .venv-analysis (step 3)
#   DEFAULT_BACKEND=codex   check for a different agent CLI first
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BINDIR="${BINDIR:-$HOME/.local/bin}"
VENV="$HERE/.runner-venv"
ANALYSIS_VENV="$HERE/.venv-analysis"
# The default backend is claude: lib/agent.py's registry defaults to
# claude-sonnet-5, and the WUR instrument only runs against Claude Code (it is the
# only backend with a stream-json user channel, a PreToolUse barrier, and
# full-fidelity token accounting). codex/gemini/agy remain supported for ladder jobs.
DEFAULT_BACKEND="${DEFAULT_BACKEND:-claude}"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '  \033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

echo "exp-runner installer  ($HERE)"
echo ""

# ── 1. Prerequisites ─────────────────────────────────────────────────────────
echo "Checking prerequisites…"
command -v git >/dev/null     || die "git not found — install git first."
command -v python3 >/dev/null || die "python3 not found — install Python 3.9+ first."
ok "git: $(git --version | awk '{print $3}')"

PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' \
  || die "python3 is $PYV; exp-runner needs >= 3.9."
ok "python3: $PYV"

# Agent CLIs are needed at RUN time (you log in separately). Warn, don't fail.
HAVE_AGENT=0
HAVE_DEFAULT=0
for cli in claude codex gemini agy; do
  if command -v "$cli" >/dev/null; then
    ok "$cli CLI found$( [[ "$cli" == "$DEFAULT_BACKEND" ]] && printf '  (default backend)' )"
    HAVE_AGENT=1
    [[ "$cli" == "$DEFAULT_BACKEND" ]] && HAVE_DEFAULT=1
  fi
done
if [[ "$HAVE_DEFAULT" == 0 ]]; then
  warn "the default backend '$DEFAULT_BACKEND' is NOT on PATH."
  warn "    claude login       # https://docs.claude.com/claude-code"
  warn "The WUR experiment (--experiment wur) runs on claude only."
fi
if [[ "$HAVE_AGENT" == 0 ]]; then
  warn "no agent CLI at all is on PATH — install one and log in before running jobs:"
  warn "    codex login        # https://developers.openai.com/codex"
fi

# ── 2. Python dependencies (PyYAML, jsonschema) ──────────────────────────────
echo ""
echo "Ensuring Python deps (PyYAML, jsonschema, matplotlib, numpy)…"
DEPS_CHECK='import yaml, jsonschema, matplotlib, numpy'
WRAP_PATH_LINE=""   # set if we fall back to a private venv
if python3 -c "$DEPS_CHECK" 2>/dev/null; then
  ok "already importable by python3"
elif python3 -m pip install --user -q -r "$HERE/requirements.txt" 2>/dev/null \
     && python3 -c "$DEPS_CHECK" 2>/dev/null; then
  ok "installed with pip --user"
else
  warn "couldn't install into the system/user python (PEP 668?) — using a private venv"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r "$HERE/requirements.txt"
  "$VENV/bin/python" -c "$DEPS_CHECK" || die "venv deps failed to import"
  WRAP_PATH_LINE="export PATH=\"$VENV/bin:\$PATH\"   # exp-runner's private deps"
  ok "built $VENV"
fi

# ── 3. The analysis environment (.venv-analysis) ─────────────────────────────
# Kept OUT of requirements.txt and out of the runner's own interpreter. pyarrow +
# pandas + lifelines are ~200 MB and are needed only by lib/wur/aggregate.py and
# analysis/, which run offline, after the matrix, often on a different machine.
echo ""
if [[ "${SKIP_ANALYSIS_VENV:-0}" == "1" ]]; then
  warn "skipping .venv-analysis (SKIP_ANALYSIS_VENV=1)"
elif [[ -x "$ANALYSIS_VENV/bin/python" ]] \
     && "$ANALYSIS_VENV/bin/python" -c 'import pyarrow, pandas' 2>/dev/null; then
  ok "analysis venv already present: $ANALYSIS_VENV"
else
  echo "Bootstrapping the analysis environment ($ANALYSIS_VENV)…"
  ANALYSIS_REQ="$HERE/requirements-analysis.txt"
  if python3 -m venv "$ANALYSIS_VENV" 2>/dev/null; then
    "$ANALYSIS_VENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
    if [[ -f "$ANALYSIS_REQ" ]]; then
      ANALYSIS_OK=0
      "$ANALYSIS_VENV/bin/pip" install -q -r "$ANALYSIS_REQ" && ANALYSIS_OK=1
    else
      # requirements-analysis.txt is referenced by the build docs but may not have
      # landed yet. Install the pinned set it declares rather than silently
      # producing an empty venv that fails at the first `import pyarrow`.
      warn "no requirements-analysis.txt — installing the declared set directly"
      ANALYSIS_OK=0
      "$ANALYSIS_VENV/bin/pip" install -q pyarrow pandas 'lifelines>=0.30.3' && ANALYSIS_OK=1
    fi
    if [[ "${ANALYSIS_OK:-0}" == 1 ]] \
       && "$ANALYSIS_VENV/bin/python" -c 'import pyarrow, pandas' 2>/dev/null; then
      ok "built $ANALYSIS_VENV"
      echo "      use it with:  $ANALYSIS_VENV/bin/python lib/wur/aggregate.py --job-dir jobs/<id>"
    else
      warn "analysis deps failed to install — parquet rollup and the notebook will not run."
      warn "retry later with:  python3 -m venv $ANALYSIS_VENV && $ANALYSIS_VENV/bin/pip install pyarrow pandas 'lifelines>=0.30.3'"
    fi
  else
    warn "could not create $ANALYSIS_VENV — the runner itself still works."
  fi
fi

# ── 4. Put `exp-runner` on PATH ──────────────────────────────────────────────
echo ""
echo "Installing the exp-runner command into $BINDIR…"
mkdir -p "$BINDIR"
WRAPPER="$BINDIR/exp-runner"
cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
# exp-runner — installed wrapper (generated by install.sh). Runs the real entry
# point with an absolute path so it works from any directory. Jobs are written
# under the cloned repo at $HERE/jobs/.
$WRAP_PATH_LINE
exec "$HERE/run.sh" "\$@"
EOF
chmod +x "$WRAPPER"
ok "wrote $WRAPPER"

echo ""
if [[ ":$PATH:" == *":$BINDIR:"* ]]; then
  ok "$BINDIR is on your PATH — you're ready."
  echo ""
  echo "Try:  exp-runner            # interactive wizard"
  echo "      exp-runner --help"
else
  warn "$BINDIR is NOT on your PATH. Add this to your shell rc, then restart your shell:"
  echo ""
  echo "      export PATH=\"$BINDIR:\$PATH\""
  echo ""
  echo "Until then, run it directly:  $HERE/run.sh"
fi
echo ""
echo "Prerequisite reminder: log in to your agent CLI before running jobs (claude login / codex login)."
echo "Default backend: $DEFAULT_BACKEND"
