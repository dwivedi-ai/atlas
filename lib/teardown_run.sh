#!/usr/bin/env bash
# teardown_run.sh — collect artifacts after the agent exits, then clean up.
#
# ORDER IS THE POINT OF THIS FILE. Every step reads a tree that the next step is
# allowed to mutate, so a step that runs late reads someone else's footprints:
#
#   1   git.patch      diff against $BASELINE_SHA — the post-overlay commit, so
#                      injected context never appears in the captured solution
#   1b  use_detect     the detectors run BEFORE anything else touches the tree.
#                      Grading installs packages, runs pytest and creates files;
#                      a detector that ran after the judge would be measuring the
#                      grader, not the agent.
#   2   transcript     copied BY --session-id, not by newest mtime
#   3   judge          success, deliberately independent of `used`
#   4   reconcile      the offline derivation chain (needs no workspace, but must
#                      precede the gzip and the claude_home deletion)
#   5   telemetry      run_record.json v2 + event_log.jsonl
#   6   gzip / persisted outputs / git status --ignored
#   7   worktree removed, claude_home DELETED and asserted gone, .run_done
#
# Input (environment):
#   JOB_DIR   — path to jobs/<job_id>   (required)
#   RUN_ID    — the run id              (required)
#   AGENT_ID  — the resolved agent id   (required)
#   RUN_DIR   — the run directory       (default $JOB_DIR/runs/$RUN_ID)
#   TASK_ID / EXPERIMENT / CONDITION_ID (optional)
#   ATLAS_KEEP_WORKSPACE=1 — keep the worktree instead of removing it (step 7)
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${JOB_DIR:?JOB_DIR is required}"
: "${RUN_ID:?RUN_ID is required}"
: "${AGENT_ID:?AGENT_ID is required}"
: "${TASK_ID:=t1}"
: "${EXPERIMENT:=}"

JOB_DIR="$(cd "$JOB_DIR" && pwd)"
BARE="$JOB_DIR/repo.git"
LOCK="$JOB_DIR/.worktree.lock"
: "${RUN_DIR:=$JOB_DIR/runs/$RUN_ID}"
# Physical path: a wur run dir is reached through a symlink at the legacy
# location, and `git worktree remove` compares real paths.
RUN_DIR="$(cd "$RUN_DIR" && pwd -P)"
WORKSPACE="$RUN_DIR/workspace"
AGENT_EXIT_CODE="$(cat "$RUN_DIR/agent_exit_code" 2>/dev/null || echo 0)"

meta_field() { python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
cur = d
for part in sys.argv[2].split("."):
    if not isinstance(cur, dict) or part not in cur:
        sys.exit(0)
    cur = cur[part]
print("" if cur is None else cur)
' "$RUN_DIR/run_meta.json" "$1" 2>/dev/null || true; }

[[ -n "$EXPERIMENT" ]] || EXPERIMENT="$(meta_field experiment)"
[[ -n "$EXPERIMENT" ]] || EXPERIMENT="ladder"
CONDITION_ID="${CONDITION_ID:-$(meta_field condition_id)}"
BASELINE_SHA="$(meta_field baseline_sha)"
SESSION_ID="$(meta_field session_id)"

echo "==> Tearing down run: $RUN_ID  (experiment=$EXPERIMENT)"

# ── 1. Capture git diff (tracked changes + new files; exclude the venv symlink) ──
# `git diff $BASELINE_SHA`, not `git diff HEAD`: the baseline is the POST-overlay
# commit, which is what HEAD happens to be today — but only because nothing has
# committed since. Naming the SHA makes the patch's reference point a recorded
# fact instead of an assumption about the agent's git habits.
echo "--> Capturing git patch"
if [[ -z "$BASELINE_SHA" ]]; then
  BASELINE_SHA="$(git -C "$WORKSPACE" rev-parse HEAD 2>/dev/null || echo HEAD)"
  echo "--> WARN: run_meta.json carried no baseline_sha; falling back to $BASELINE_SHA" >&2
fi
(cd "$WORKSPACE"
  git diff "$BASELINE_SHA" > "$RUN_DIR/git.patch" 2>/dev/null || true
  git ls-files --others --exclude-standard \
      -x venv -x 'venv/**' -x '__pycache__' -x '**/__pycache__/**' -x '*.pyc' \
    | xargs -I{} git diff --no-index /dev/null {} >> "$RUN_DIR/git.patch" 2>/dev/null || true
)

# ── 1b. Detect USE — before anything else touches the tree ────────────────────
if [[ "$EXPERIMENT" == "wur" && -f "$LIB_DIR/wur/detect_use.py" ]]; then
  echo "--> Detecting use (detectors over workspace + diff + ordered Bash)"
  DETECT_ARGS=(--scope run --run-dir "$RUN_DIR")
  [[ -n "$TASK_ID" ]] && DETECT_ARGS+=(--task-id "$TASK_ID")
  [[ -f "$JOB_DIR/.registry/facts.yaml" ]] && DETECT_ARGS+=(--facts "$JOB_DIR/.registry/facts.yaml")
  [[ -n "$BASELINE_SHA" ]] && DETECT_ARGS+=(--baseline-sha "$BASELINE_SHA")
  [[ -x "$JOB_DIR/.venv/bin/python" ]] && DETECT_ARGS+=(--venv "$JOB_DIR/.venv")
  python3 "$LIB_DIR/wur/detect_use.py" "${DETECT_ARGS[@]}" >/dev/null \
    || echo "WARN: use detection failed" >&2
fi

# ── 2. Locate / verify the transcript ────────────────────────────────────────
echo "--> Locating transcript"
if [[ "$AGENT_ID" == claude-* ]]; then
  [[ -n "$SESSION_ID" ]] || SESSION_ID="$(python3 - "$RUN_ID" <<'PY' 2>/dev/null || true
import hashlib, sys, uuid
print(str(uuid.UUID(bytes=hashlib.sha256(("wur-session|" + sys.argv[1]).encode()).digest()[:16],
                    version=4)))
PY
)"
  # BY SESSION ID. The old newest-mtime glob picked whichever file was touched
  # last in a projects/ directory — correct only while claude was clamped to one
  # concurrent run. It no longer is.
  FOUND=""
  if [[ -n "$SESSION_ID" ]]; then
    for ROOT in "$RUN_DIR/claude_home/projects" "$HOME/.claude/projects"; do
      [[ -d "$ROOT" ]] || continue
      FOUND="$(find "$ROOT" -maxdepth 2 -type f -name "$SESSION_ID.jsonl" 2>/dev/null | head -1)"
      [[ -n "$FOUND" ]] && break
    done
  fi
  if [[ -n "$FOUND" ]]; then
    cp "$FOUND" "$RUN_DIR/transcript.jsonl"
    # Assert the file we copied really is this run's session. A same-named file in
    # another home would be a silent cross-run contamination.
    if ! python3 - "$RUN_DIR/transcript.jsonl" "$SESSION_ID" <<'PY'
import json, sys
path, want = sys.argv[1], sys.argv[2]
seen = set()
with open(path, encoding="utf-8", errors="replace") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = obj.get("sessionId") or obj.get("session_id")
        if sid:
            seen.add(sid)
        if len(seen) > 1:
            break
sys.exit(0 if (not seen or seen == {want}) else 1)
PY
    then
      echo "WARN: transcript sessionId does not match --session-id $SESSION_ID" >&2
    fi
    echo "--> Transcript (by session-id): $FOUND"
  else
    # Fallback: the pre-session-id location logic, kept so a run started by an
    # older run_agent.sh (no --session-id) still yields a transcript.
    WORKSPACE_SLUG=$(printf '%s' "$WORKSPACE" | sed 's/[^a-zA-Z0-9]/-/g')
    TRANSCRIPT_DIR="$RUN_DIR/claude_home/projects/$WORKSPACE_SLUG"
    [[ -d "$TRANSCRIPT_DIR" ]] || TRANSCRIPT_DIR="$HOME/.claude/projects/$WORKSPACE_SLUG"
    if [[ ! -d "$TRANSCRIPT_DIR" ]]; then
      ALT=$(ls -td "$HOME/.claude/projects/"*-workspace 2>/dev/null | head -1 || true)
      [[ -n "$ALT" ]] && TRANSCRIPT_DIR="$ALT"
    fi
    LATEST=""
    [[ -d "$TRANSCRIPT_DIR" ]] && LATEST=$(ls -t "$TRANSCRIPT_DIR"/*.jsonl 2>/dev/null | head -1 || true)
    if [[ -n "$LATEST" ]]; then
      cp "$LATEST" "$RUN_DIR/transcript.jsonl"
      echo "--> Transcript (mtime fallback): $LATEST"
    else
      echo '{"type":"error","message":"no transcript found"}' > "$RUN_DIR/transcript.jsonl"
    fi
  fi
elif [[ "$AGENT_ID" == "codex" ]]; then
  if [[ ! -s "$RUN_DIR/transcript.jsonl" ]]; then
    echo '{"type":"error","message":"codex produced no output"}' > "$RUN_DIR/transcript.jsonl"
  else
    echo "--> Codex transcript: $(wc -l < "$RUN_DIR/transcript.jsonl") events"
  fi
elif [[ "$AGENT_ID" == gemini-* ]]; then
  # Gemini writes ~/.gemini/tmp/<projectName>/chats/session-<ts>-<first8ofUUID>.jsonl
  # (dir is a NAME not a hash; filename holds only the first 8 of the UUID). Locate by
  # the session_id echoed in the -o json stdout; fall back to newest (safe: gemini is
  # serial, so only one session is in flight).
  SID="$(python3 -c "import json;print(json.load(open('$RUN_DIR/agent_stdout.json')).get('session_id',''))" 2>/dev/null || true)"
  GEM_TMP="$HOME/.gemini/tmp"; FOUND=""
  if [[ -n "$SID" ]]; then
    FOUND="$(ls -t "$GEM_TMP"/*/chats/session-*-"${SID:0:8}".jsonl 2>/dev/null | head -1 || true)"
  fi
  [[ -z "$FOUND" ]] && FOUND="$(ls -t "$GEM_TMP"/*/chats/session-*.jsonl 2>/dev/null | head -1 || true)"
  if [[ -n "$FOUND" ]]; then
    cp "$FOUND" "$RUN_DIR/transcript.jsonl"
    echo "--> Gemini transcript: $FOUND"
  else
    echo '{"type":"error","message":"no gemini transcript found"}' > "$RUN_DIR/transcript.jsonl"
  fi
elif [[ "$AGENT_ID" == agy* ]]; then
  # agy: recover the conversation id (from the --log-file, else last_conversations.json keyed
  # by the workspace cwd), copy transcript_full.jsonl + the conversation .db (tokens live there).
  AGY_HOME="$RUN_DIR/agy_home"
  GD=(); [[ -d "$AGY_HOME/antigravity-cli" ]] && GD=(--gemini-dir "$AGY_HOME")
  T="$(python3 "$LIB_DIR/agy.py" locate --log "$RUN_DIR/agy.log" --cwd "$WORKSPACE" "${GD[@]}" --what transcript 2>/dev/null || true)"
  DB="$(python3 "$LIB_DIR/agy.py" locate --log "$RUN_DIR/agy.log" --cwd "$WORKSPACE" "${GD[@]}" --what db 2>/dev/null || true)"
  if [[ -n "$T" && -f "$T" ]]; then
    cp "$T" "$RUN_DIR/transcript.jsonl"
    echo "--> agy transcript: $T"
  else
    echo '{"type":"error","message":"no agy transcript found"}' > "$RUN_DIR/transcript.jsonl"
  fi
  # WAL-safe snapshot (a plain cp loses gen_metadata rows still in the -wal).
  [[ -n "$DB" && -f "$DB" ]] && python3 "$LIB_DIR/agy.py" snapshot "$DB" "$RUN_DIR/agy_conversation.db" \
    && echo "--> agy conversation db: $DB"
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
# Skipped in wur: it is a second, unmeasured agent invocation inside the very
# workspace whose contents are the independent variable. has no such step.
ANALYZE="$(python3 "$LIB_DIR/jobspec.py" field "$JOB_DIR" analyze)"
if [[ "$EXPERIMENT" == "wur" ]]; then
  echo "--> Self-analysis skipped (wur: no unmeasured agent call in a measured workspace)"
elif [[ "$ANALYZE" == "False" ]]; then
  echo "--> Self-analysis disabled (analyze: false)"
else
  MAX_SECONDS="$(python3 "$LIB_DIR/jobspec.py" field "$JOB_DIR" max_seconds)"; [[ -n "$MAX_SECONDS" ]] || MAX_SECONDS=0
  JOB_DIR="$JOB_DIR" RUN_ID="$RUN_ID" RUN_DIR="$RUN_DIR" AGENT_ID="$AGENT_ID" \
    MAX_SECONDS="$MAX_SECONDS" TASK_ID="$TASK_ID" TASK_PROMPT="${TASK_PROMPT:-}" \
    bash "$LIB_DIR/self_analysis.sh" || echo "WARN: self-analysis failed" >&2
fi

# ── 4. Reconcile — the offline derivation chain ──────────────────────────────
if [[ "$EXPERIMENT" == "wur" && -f "$LIB_DIR/wur/reconcile.py" ]]; then
  echo "--> Reconciling (regions → exposure → events → probes → trace)"
  # The fact cards MUST be passed in: the registry is deliberately not an ancestor
  # of the run dir, so reconcile cannot find them by walking up. Without them
  # exposure.jsonl and fact_trace.jsonl are empty BY CONSTRUCTION — a silent zero
  # read-rate rather than an error — so both candidates are tried and their absence
  # is called out rather than left to a generic "reconcile failed".
  RECON_ARGS=(--run-dir "$RUN_DIR")
  RECON_FACTS=""
  for cand in "$JOB_DIR/.registry/_index/probe_key.json" "$JOB_DIR/.registry/facts.yaml"; do
    [[ -f "$cand" ]] && { RECON_FACTS="$cand"; break; }
  done
  if [[ -n "$RECON_FACTS" ]]; then
    RECON_ARGS+=(--facts "$RECON_FACTS")
  else
    echo "WARN: no fact registry under $JOB_DIR/.registry — exposure.jsonl and" >&2
    echo "      fact_trace.jsonl will be empty by construction, not by measurement." >&2
  fi
  python3 "$LIB_DIR/wur/reconcile.py" "${RECON_ARGS[@]}" >/dev/null \
    || echo "WARN: reconcile failed" >&2
fi

# ── 5. Telemetry ─────────────────────────────────────────────────────────────
echo "--> Extracting telemetry"
TIMESTAMP_END=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
python3 - "$RUN_DIR" "$TIMESTAMP_END" <<'PY' || true
"""Close out run_meta.json: the end timestamp, plus the two hashes only the
driver could measure. Pre-computed values written at setup are kept
when the driver produced none — a dead run still carries what was knowable."""
import json, sys
from pathlib import Path

run_dir = Path(sys.argv[1])
meta_path = run_dir / "run_meta.json"
try:
    meta = json.loads(meta_path.read_text())
except Exception:
    sys.exit(0)
meta["timestamp_end"] = sys.argv[2]

summary_path = run_dir / "driver_summary.json"
if summary_path.exists():
    try:
        s = json.loads(summary_path.read_text())
    except Exception:
        s = {}
    child = s.get("child") or {}
    if child.get("cli_argv_sha256"):
        meta["cli_argv_sha256"] = child["cli_argv_sha256"]
    init = s.get("init") or {}
    if init.get("sha256"):
        meta["init_sha256"] = init["sha256"]
    frozen = s.get("frozen") or {}
    for key in ("pacing_prompt_sha256", "probe_text_sha256"):
        if frozen.get(key):
            meta[key] = frozen[key]
    integrity = s.get("integrity") or {}
    if integrity:
        meta["integrity"] = integrity
meta_path.write_text(json.dumps(meta, indent=2) + "\n")
PY
python3 "$LIB_DIR/telemetry.py" --run-dir "$RUN_DIR" || echo "WARN: telemetry extraction failed" >&2

# ── 5b. Per-run report ───────────────────────────────────────────────────────
if [[ -f "$LIB_DIR/report.py" ]]; then
  python3 "$LIB_DIR/report.py" --run-dir "$RUN_DIR" || echo "WARN: report failed" >&2
fi

# ── 6. Compress the raw stream, harvest what the model did NOT see, snapshot state ──
if [[ -f "$RUN_DIR/stream.jsonl" ]]; then
  gzip -f "$RUN_DIR/stream.jsonl" && echo "--> Compressed stream.jsonl → stream.jsonl.gz"
fi
if [[ "$EXPERIMENT" == "wur" ]]; then
  # persistedOutputPath files: a 926 KB Bash stdout reached the model as a
  # 2,299-char stub. These copies are what the model did NOT see. They are
  # kept for deferred-exposure analysis and are NEVER counted as exposure.
  python3 - "$RUN_DIR" <<'PY' || true
import gzip, json, re, shutil, sys
from pathlib import Path

run_dir = Path(sys.argv[1])
dest = run_dir / "watch" / "persisted"
dest.mkdir(parents=True, exist_ok=True)
PATH_RE = re.compile(r'"persistedOutputPath"\s*:\s*"([^"]+)"')
sources = [run_dir / "transcript.jsonl", run_dir / "stream.jsonl", run_dir / "stream.jsonl.gz"]
seen, copied = set(), 0
for src in sources:
    if not src.exists():
        continue
    opener = gzip.open if src.suffix == ".gz" else open
    try:
        with opener(src, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                for hit in PATH_RE.findall(line):
                    seen.add(hit)
    except Exception:
        continue
for raw in sorted(seen):
    p = Path(raw)
    if not p.is_file():
        continue
    try:
        shutil.copy(p, dest / p.name)
        copied += 1
    except Exception:
        pass
(dest / "index.json").write_text(json.dumps({"referenced": sorted(seen), "copied": copied}, indent=2) + "\n")
print(f"--> Persisted outputs: {copied} copied of {len(seen)} referenced")
PY
fi
if [[ -d "$WORKSPACE" ]]; then
  git -C "$WORKSPACE" status --ignored --porcelain > "$RUN_DIR/workspace_status.txt" 2>/dev/null || true
fi

# ── 7. Restore Gemini settings; drop the worktree; DELETE the credential copy ──
# There is no Claude settings restore any more: nothing global was ever modified.
# The per-run CLAUDE_CONFIG_DIR that replaced it holds a copy of the real
# credentials file, so deleting it is not housekeeping — it is the reason a
# credential copy never outlives the run that needed it.
GEM_SETTINGS_BACKUP="$RUN_DIR/gemini_settings_backup.json"
if [[ -f "$GEM_SETTINGS_BACKUP" ]]; then
  cp "$GEM_SETTINGS_BACKUP" "$HOME/.gemini/settings.json"
  echo "--> Restored Gemini settings"
fi

# ATLAS_KEEP_WORKSPACE=1 keeps the worktree. The default is still to remove it — a
# 7-env × multi-rep matrix would otherwise keep one full checkout per cell — but
# "removed unconditionally, no flag" is what made a verdict unreproducible: grade
# once, at teardown, against a tree that no longer exists. The reproducible path is
# `judge.py --regrade`, which rebuilds from refs/atlas/baseline-run/<RUN_ID> +
# git.patch and needs no workspace at all; this flag is for the case where you want
# to open the tree by hand. The credential copy is deleted either way, below.
if [[ "${ATLAS_KEEP_WORKSPACE:-0}" == "1" ]]; then
  echo "--> Keeping worktree (ATLAS_KEEP_WORKSPACE=1): $WORKSPACE"
  echo "    remove it later with: git --git-dir=$BARE worktree remove --force $WORKSPACE"
else
echo "--> Removing worktree"
(flock 200
  git --git-dir="$BARE" worktree remove --force "$WORKSPACE" 2>/dev/null || rm -rf "$WORKSPACE"
) 200>"$LOCK"
fi

if [[ -e "$RUN_DIR/claude_home" ]]; then
  rm -rf "$RUN_DIR/claude_home"
  if [[ -e "$RUN_DIR/claude_home" ]]; then
    echo "ERROR: claude_home still present after removal: $RUN_DIR/claude_home" >&2
    exit 1
  fi
  echo "--> claude_home deleted (credential copy gone — asserted)"
fi
rm -f "$RUN_DIR/task_prompt.txt"

date -u +%Y-%m-%dT%H:%M:%SZ > "$RUN_DIR/.run_done"
echo "==> Teardown complete: $RUN_DIR"
