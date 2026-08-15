#!/usr/bin/env python3
"""
detect_use.py — run the closed detector registry over one tree and write use_detect.json.

RESPONSIBILITY
  Turn (a run, or any tree-shaped corpus) into the `used` half of the funnel.
  It assembles the three MEASURED sources a detector is allowed to see — the
  final workspace, the unified diff against $BASELINE_SHA, and the ordered Bash
  commands recorded at the PreToolUse barrier — compiles each (fact -> detector,
  params) binding into a mechanical criterion, and executes it through
  lib/battery.py. A fact detector IS a mechanical criterion, so no second
  execution engine is written here.

  ORDERING IS LOAD-BEARING: this runs BEFORE
  anything else touches the tree. Grading mutates the workspace — it installs,
  it runs pytest, it can create files — so a detector that ran after the judge
  would be reading the grader's own footprints. Nothing in this module writes
  inside a `run` or `workspace` scope tree; the venv symlink is created only for
  the temp trees this module materializes itself.

INPUTS
  --scope run        $RUN_DIR (workspace/, git.patch, gate/tool_calls.jsonl, run_meta.json)
  --scope workspace  a finished workspace directory (Gate 1b cross-task corpus)
  --scope tree       a plain directory, copied out and committed (Gate 1: pristine base)
  --scope patch      a base tree + one patch/overlay, materialized and committed
  bindings from the fact registry (.registry/facts.yaml) or given inline

OUTPUTS
  use_detect.json — {schema_version, scope, context, detector_registry_sha256,
  facts:[{fact_id, detector, params, params_sha256, used, used_in_diff,
  eligible, evidence, detail, error, battery{...}}]}. `used` is null on a
  detector error: that is an unmeasured run (exclusion_reason
  "detector_error"), never a non-fire.

CLI
  python3 lib/wur/detect_use.py --scope run --run-dir DIR [--facts F] [--out P]
  python3 lib/wur/detect_use.py --scope tree --tree DIR --detector NAME --params-json '{}'
  python3 lib/wur/detect_use.py --scope patch --tree BASE --patch P [--bash-json '[]']
  exit 0 when the detectors ran (a non-fire is a measurement, never a failure);
  exit 2 on a usage/scope error; exit 1 only with --fail-on-error and a detector
  that errored. A run with zero bindings still writes a valid, empty
  use_detect.json so trace.py never has to special-case a missing file.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

HERE = Path(__file__).resolve().parent
LIB = HERE.parent
for _p in (str(LIB), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import battery  # noqa: E402  (lib/battery.py — the one execution engine)

import detectors  # noqa: E402  (flat import: HERE is on sys.path)
from detectors import (  # noqa: E402
    DetectorContext,
    registry_sha256,
    validate_params,
)

SCHEMA_VERSION = "1"
SCOPES = ("run", "workspace", "tree", "patch")

# A detector is a pure read of the tree; 120 s is already generous. The output
# budget must hold the whole result JSON — battery truncates at output_limit and
# a truncated JSON is an unparseable one.
DETECT_TIMEOUT_S = 180
DETECT_OUTPUT_LIMIT = 262_144

# Copied out of a fixture / base tree; never carried into a materialized scope.
_COPY_SKIP = {".git", "venv", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache",
              "node_modules", ".claude", ".cursor"}



_GIT_ID = ["-c", "user.email=wur@local", "-c", "user.name=wur-detect", "-c", "commit.gpgsign=false"]


# ── bindings ─────────────────────────────────────────────────────────────────
@dataclass
class Binding:
    """One (fact -> detector, params) pair. Exactly one detector per fact."""

    fact_id: str
    detector: str
    params: dict = field(default_factory=dict)
    task_id: str | None = None
    planted_paths: list[str] = field(default_factory=list)
    label: str = ""

    @property
    def criterion_id(self) -> str:
        return f"use::{self.fact_id}"

    def to_dict(self) -> dict:
        return {"fact_id": self.fact_id, "detector": self.detector, "params": self.params,
                "task_id": self.task_id, "planted_paths": list(self.planted_paths)}


def _load_structured(path: Path) -> Any:
    """Load .json or .yaml. YAML needs PyYAML; the system python3 may not have it."""
    text = Path(path).read_text()
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # noqa: PLC0415
        except ImportError as e:  # pragma: no cover - environment dependent
            raise RuntimeError(
                f"{path} is YAML but PyYAML is not importable ({e}). Run install.sh, "
                "or hand this module a .json registry."
            ) from e
        return yaml.safe_load(text)
    return json.loads(text)


def _iter_fact_entries(doc: Any) -> Iterator[dict]:
    """Yield fact entries from any of the shapes a registry plausibly uses.

    Deliberately permissive on SHAPE and strict on CONTENT: lib/wur/facts.py is
    written by another component and its exact nesting is not this module's to
    fix, but an entry that names a detector outside the closed registry, or a
    parameter that is not in that detector's spec, is rejected by
    validate_params() before anything runs.
    """
    if isinstance(doc, list):
        for e in doc:
            if isinstance(e, dict):
                yield e
        return
    if not isinstance(doc, dict):
        return
    facts = doc.get("facts")
    if isinstance(facts, list):
        for e in facts:
            if isinstance(e, dict):
                yield e
        return
    if isinstance(facts, dict):
        for k, v in facts.items():
            if isinstance(v, dict):
                yield {"fact_id": v.get("fact_id", k), **v}
        return
    for k, v in doc.items():
        if isinstance(v, dict) and ("detector" in v or "detectors" in v):
            yield {"fact_id": v.get("fact_id", k), **v}


def _planted_of(entry: dict) -> list[str]:
    out: list[str] = []
    for key in ("planted_paths", "plant_paths", "carrier_paths"):
        v = entry.get(key)
        if isinstance(v, (list, tuple)):
            out.extend(str(x) for x in v)
    for key in ("source_path", "planted_path", "path", "carrier"):
        v = entry.get(key)
        if isinstance(v, str) and v:
            out.append(v)
    plant = entry.get("plant")
    if isinstance(plant, dict):
        for key in ("path", "source_path"):
            v = plant.get(key)
            if isinstance(v, str) and v:
                out.append(v)
    return sorted(set(out))


def _sub_nonce(value: Any, nonce: Any) -> Any:
    """Deep-substitute `{nonce}` in strings. Identity when no nonce is minted yet.

    Mirrors facts.FactSpec._sub_deep, which does the same for the plant's rendered
    statement — the two halves of one fact must agree on what the token is.
    """
    if not nonce or not isinstance(nonce, str):
        return value
    if isinstance(value, str):
        return value.replace("{nonce}", nonce)
    if isinstance(value, dict):
        return {k: _sub_nonce(v, nonce) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sub_nonce(v, nonce) for v in value]
    return value


def binding_from_entry(entry: dict) -> Binding:
    """One registry entry -> a Binding. Raises ValueError on an unusable entry."""
    fact_id = str(entry.get("fact_id") or entry.get("id") or "").strip()
    if not fact_id:
        raise ValueError("fact entry has no fact_id")
    det = entry.get("detector")
    if det is None:
        dets = entry.get("detectors")
        if isinstance(dets, list):
            if len(dets) != 1:
                raise ValueError(
                    f"{fact_id}: exactly one detector per fact (one fact per task, "
                    f"one predicate per fact); got {len(dets)}"
                )
            det = dets[0]
    params: dict = {}
    if isinstance(det, str):
        name = det
        params = entry.get("params") or entry.get("detector_params") or {}
    elif isinstance(det, dict):
        name = det.get("name") or det.get("detector") or ""
        params = det.get("params") or {}
    else:
        raise ValueError(f"{fact_id}: no detector declared")
    name = str(name).strip()
    if not name:
        raise ValueError(f"{fact_id}: detector has no name")
    if not isinstance(params, dict):
        raise ValueError(f"{fact_id}: detector params must be an object")
    # `{nonce}` IN A DETECTOR PARAM IS SUBSTITUTED HERE, from the entry's own minted
    # nonce. Without this the placeholder reached the predicate verbatim and it
    # searched for the six characters "{nonce}" — so a pack whose mandate is "stamp
    # this exact token" had to hardcode the minted value in the param instead, which
    # pins the nonce: `mint --force` re-mints the registry, the param keeps the old
    # literal, and the predicate silently measures a token nobody planted. It also put
    # an answer key in a tracked file. The installed registry is per job and mode 600,
    # so this is the right place for the real value to appear.
    params = _sub_nonce(params, entry.get("nonce"))
    problems = validate_params(name, params)
    if problems:
        raise ValueError(f"{fact_id}: " + "; ".join(problems))
    return Binding(fact_id=fact_id,
        detector=name,
        params=params,
        task_id=(entry.get("task_id") or entry.get("task") or None),
        planted_paths=_planted_of(entry),
        label=str(entry.get("label") or entry.get("description") or entry.get("gist") or ""),
                )


def load_bindings(facts_path: Path, *, task_id: str | None = None, fact_id: str | None = None
) -> list[Binding]:
    """Every runnable binding in a registry, optionally filtered to one task/fact."""
    doc = _load_structured(Path(facts_path))
    out: list[Binding] = []
    for entry in _iter_fact_entries(doc):
        try:
            b = binding_from_entry(entry)
        except ValueError:
            if entry.get("detector") or entry.get("detectors"):
                raise
            continue
        if fact_id and b.fact_id != fact_id:
            continue
        if task_id and b.task_id and b.task_id != task_id:
            continue
        out.append(b)
    return out


# ── run-scope artifact readers ───────────────────────────────────────────────
def _git(args: Sequence[str], cwd: Path) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                           text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"
    return r.returncode, (r.stdout or "")






def resolve_baseline_sha(workspace: Path, run_dir: Path | None, explicit: str | None) -> str | None:
    """$BASELINE_SHA — the POST-overlay commit, so the plant never appears in the diff.

    setup_run.sh points refs/atlas/baseline at it; run_meta.json records it. The
    fallback to HEAD is last-resort and is recorded in the output, because a diff
    against HEAD is a diff against the agent's own last commit if it made one.
    """
    if explicit:
        return explicit
    rc, out = _git(["rev-parse", "--verify", "refs/atlas/baseline"], workspace)
    if rc == 0 and out.strip():
        return out.strip()
    if run_dir:
        for name in ("run_meta.json", "run_record.json"):
            p = run_dir / name
            if not p.exists():
                continue
            try:
                doc = json.loads(p.read_text())
            except Exception:  # noqa: BLE001
                continue
            for key in ("baseline_sha", "baseline"):
                v = doc.get(key) or (doc.get("run") or {}).get(key) or (doc.get("condition") or {}).get(key)
                if isinstance(v, str) and v:
                    return v
    return None


def compute_diff(workspace: Path, baseline_sha: str | None) -> str:
    """`git diff $BASELINE_SHA` plus one --no-index diff per untracked file."""
    ref = baseline_sha or "HEAD"
    rc, tracked = _git(["diff", ref], workspace)
    parts = [tracked] if rc == 0 else []
    rc2, others = _git(["ls-files", "--others", "--exclude-standard",
         "-x", "venv", "-x", "venv/**", "-x", "__pycache__", "-x", "**/__pycache__/**",
         "-x", "*.pyc"],
        workspace,
                )
    if rc2 == 0:
        for rel in [l for l in others.splitlines() if l.strip()]:
            _rc3, one = _git(["diff", "--no-index", "--", "/dev/null", rel], workspace)
            if one:
                parts.append(one)
    return "".join(parts)


def collect_bash_commands(run_dir: Path | None) -> tuple[list[str], str]:
    """Ordered Bash commands, from gate/tool_calls.jsonl first, stream.jsonl second.

    Deduplicated by tool_use_id: a denied call costs TWO barrier fires, so
    gate ordinals are NOT tool-call ordinals. First occurrence wins, preserving
    issue order.
    """
    if run_dir is None:
        return [], "none"
    gate = run_dir / "gate" / "tool_calls.jsonl"
    if gate.exists():
        cmds, seen = [], set()
        for line in gate.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue  # V8: a >PIPE_BUF interleaved append can corrupt a line
            if (row.get("tool_name") or row.get("tool")) != "Bash":
                continue
            tid = row.get("tool_use_id") or row.get("tid")
            if tid and tid in seen:
                continue
            if tid:
                seen.add(tid)
            ti = row.get("tool_input") or row.get("input") or {}
            cmd = ti.get("command") if isinstance(ti, dict) else None
            if isinstance(cmd, str) and cmd.strip():
                cmds.append(cmd)
        if cmds:
            return cmds, "gate"
    for name in ("stream.jsonl", "transcript.jsonl"):
        src = run_dir / name
        if not src.exists():
            continue
        cmds, seen = [], set()
        for line in src.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            msg = row.get("message") if isinstance(row.get("message"), dict) else row
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for blk in content:
                if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                    continue
                if blk.get("name") != "Bash":
                    continue
                bid = blk.get("id")
                if bid and bid in seen:
                    continue
                if bid:
                    seen.add(bid)
                cmd = (blk.get("input") or {}).get("command")
                if isinstance(cmd, str) and cmd.strip():
                    cmds.append(cmd)
        if cmds:
            return cmds, name.split(".")[0]
    return [], "none"


def collect_planted_paths(run_dir: Path | None, extra: Iterable[str] = ()) -> list[str]:
    """Carrier paths to exclude from content scans — the plant is not use.

    Reads whatever plant.py left behind; the constant floor in detectors.py
    (DEFAULT_PLANTED_GLOBS) applies regardless, so a missing manifest cannot
    turn the planted NOTES.md into evidence that the fact was used.
    """
    out: list[str] = [str(x) for x in extra if x]
    if run_dir is not None:
        for name in ("plant_manifest.json", "run_meta.json", "manifest.json"):
            p = run_dir / name
            if not p.exists():
                continue
            try:
                doc = json.loads(p.read_text())
            except Exception:  # noqa: BLE001
                continue
            out.extend(_paths_in_manifest(doc))
    return sorted({o for o in out if o})


def _paths_in_manifest(doc: Any, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    found: list[str] = []
    if isinstance(doc, dict):
        for key in ("planted_paths", "plant_paths", "carrier_paths", "files"):
            v = doc.get(key)
            if isinstance(v, list):
                for x in v:
                    if isinstance(x, str):
                        found.append(x)
                    elif isinstance(x, dict):
                        for kk in ("path", "dest", "target", "relpath"):
                            if isinstance(x.get(kk), str):
                                found.append(x[kk])
        for key in ("planted_path", "source_path", "carrier"):
            if isinstance(doc.get(key), str):
                found.append(doc[key])
        for v in doc.values():
            if isinstance(v, (dict, list)):
                found.extend(_paths_in_manifest(v, depth + 1))
    elif isinstance(doc, list):
        for v in doc:
            if isinstance(v, (dict, list)):
                found.extend(_paths_in_manifest(v, depth + 1))
    return found


# ── materialization (non-run scopes) ─────────────────────────────────────────
def link_venv(workspace: Path, venv: Path | None) -> str | None:
    """Symlink the hermetic venv in as ./venv — non-worktree scopes only.

    battery.py prepends `<workspace>/venv/bin` to PATH so `python`/`pytest`
    resolve hermetically. A run-scope worktree already has this link from
    setup_run.sh; a tree we materialized ourselves does not, and this module
    must never create a file inside a real run's workspace before grading.
    """
    if not venv:
        return None
    venv = Path(venv)
    if not (venv / "bin").is_dir():
        return None
    dest = workspace / "venv"
    if dest.exists() or dest.is_symlink():
        return str(dest)
    try:
        dest.symlink_to(venv, target_is_directory=True)
    except OSError:
        return None
    return str(dest)


def _copy_tree(src: Path, dest: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n in _COPY_SKIP}

    shutil.copytree(src, dest, ignore=ignore, symlinks=False, dirs_exist_ok=True)


def materialize_base(src: Path, dest: Path, venv: Path | None = None) -> str:
    """Copy a plain tree into `dest`, git-init it, commit -> the baseline sha."""
    dest.mkdir(parents=True, exist_ok=True)
    _copy_tree(Path(src), dest)
    link_venv(dest, venv)
    _git(["init", "-q"], dest)
    exclude = dest / ".git" / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a") as fh:
            fh.write("/venv\n")
    except OSError:
        pass
    _git(["add", "-A"], dest)
    _git([*_GIT_ID, "commit", "-q", "-m", "wur-detect-baseline", "--allow-empty"], dest)
    rc, out = _git(["rev-parse", "HEAD"], dest)
    return out.strip() if rc == 0 else ""


def apply_case(workspace: Path, patch: Path | None, nonce: str | None = None) -> str | None:
    """Apply one reference / near-miss case. Returns an error string, or None.

    A case is either a unified diff (applied with `git apply`) or a directory
    overlay (copied over the tree). The overlay form exists because authoring
    ten near-miss patches by hand is how a truth table stops getting written.

    `{nonce}` IN A PATCH IS SUBSTITUTED HERE. Without it a pack whose mandate is
    "stamp this exact token" had to hardcode the minted value in every reference
    patch, which pinned the nonce for that fact: `mint --force` then re-minted the
    registry while the patches kept the old literal, so the references stopped
    demonstrating compliance and Gate 1b flipped to reject — and the answer key sat
    in a tracked file. Substitution is byte-safe for a unified diff because a nonce
    is a single token: hunk headers count LINES, and replacing a token inside a line
    changes no line count.
    """
    if patch is None:
        return None
    patch = Path(patch)
    if not patch.exists():
        return f"case path does not exist: {patch}"
    if patch.is_dir():
        _copy_tree(patch, workspace)
        return None

    target = patch.resolve()
    tmp_patch: Path | None = None
    if nonce:
        try:
            text = patch.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"could not read case patch: {e}"
        if "{nonce}" in text:
            tmp_patch = Path(tempfile.mkdtemp(prefix="wur-case-")) / patch.name
            tmp_patch.write_text(text.replace("{nonce}", nonce), encoding="utf-8")
            target = tmp_patch.resolve()
    try:
        rc, out = _git(["apply", "--whitespace=nowarn", str(target)], workspace)
        if rc != 0:
            rc2, out2 = _git(["apply", "-p0", "--whitespace=nowarn", str(target)], workspace)
            if rc2 != 0:
                return f"git apply failed: {out.strip() or out2.strip()}"
        return None
    finally:
        if tmp_patch is not None:
            shutil.rmtree(tmp_patch.parent, ignore_errors=True)


# ── context assembly ─────────────────────────────────────────────────────────
@dataclass
class ScopeResult:
    ctx: DetectorContext
    workspace: Path
    tmpdir: Path | None
    diff_source: str
    bash_source: str
    baseline_sha: str | None

    def cleanup(self) -> None:
        if self.tmpdir and self.tmpdir.exists():
            shutil.rmtree(self.tmpdir, ignore_errors=True)


def build_scope(scope: str,
    *,
    run_dir: Path | None = None,
    workspace: Path | None = None,
    tree: Path | None = None,
    patch: Path | None = None,
    baseline_sha: str | None = None,
    bash_commands: Sequence[str] | None = None,
    planted_paths: Sequence[str] = (),
    venv: Path | None = None,
    workdir: Path | None = None,
    prefer_git_patch: bool = True,
    #: Minted nonce, substituted for `{nonce}` in a case patch so a pack whose
    #: mandate is "stamp this exact token" need not hardcode the answer key.
    nonce: str | None = None,
) -> ScopeResult:
    """Assemble the DetectorContext for one scope. The one place scopes differ."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; expected one of {list(SCOPES)}")
    tmpdir: Path | None = None
    diff_source = "none"

    if scope in ("tree", "patch"):
        src = Path(tree) if tree else None
        if src is None or not src.is_dir():
            raise ValueError(f"--scope {scope} needs --tree pointing at a directory")
        tmpdir = Path(tempfile.mkdtemp(prefix="wur-detect-", dir=str(workdir) if workdir else None))
        ws = tmpdir / "workspace"
        base = materialize_base(src, ws, venv)
        baseline_sha = base or None
        if scope == "patch":
            err = apply_case(ws, Path(patch) if patch else None, nonce=nonce)
            if err:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise RuntimeError(err)
        diff_text = compute_diff(ws, baseline_sha)
        diff_source = "computed"
        cmds = list(bash_commands or ())
        bash_source = "explicit" if cmds else "none"
    else:
        if scope == "run":
            if run_dir is None:
                raise ValueError("--scope run needs --run-dir")
            run_dir = Path(run_dir).resolve()
            ws = Path(workspace) if workspace else run_dir / "workspace"
        else:
            if workspace is None:
                raise ValueError("--scope workspace needs --workspace")
            ws = Path(workspace).resolve()
        if not ws.is_dir():
            raise ValueError(f"workspace does not exist: {ws}")
        baseline_sha = resolve_baseline_sha(ws, run_dir, baseline_sha)
        patch_file = (run_dir / "git.patch") if run_dir else None
        if baseline_sha:
            diff_text = compute_diff(ws, baseline_sha)
            diff_source = "computed"
        elif prefer_git_patch and patch_file and patch_file.exists():
            diff_text = patch_file.read_text(errors="replace")
            diff_source = "git.patch"
        else:
            diff_text = compute_diff(ws, None)
            diff_source = "computed_vs_head"
        if bash_commands is not None:
            cmds, bash_source = list(bash_commands), "explicit"
        else:
            cmds, bash_source = collect_bash_commands(run_dir)

    planted = collect_planted_paths(run_dir if scope == "run" else None, planted_paths)
    ctx = DetectorContext(workspace=Path(ws).resolve(),
        diff_text=diff_text,
        bash_commands=cmds,
        planted_paths=planted,
        baseline_sha=baseline_sha,
        scope=scope,
                )
    return ScopeResult(ctx=ctx, workspace=Path(ws).resolve(), tmpdir=tmpdir,
                       diff_source=diff_source, bash_source=bash_source,
                       baseline_sha=baseline_sha)


# ── the battery bridge ───────────────────────────────────────────────────────
def _criterion(binding: Binding, ctx_path: Path, *, diff_only: bool) -> dict:
    cmd = " ".join(shlex.quote(x)
        for x in (sys.executable,
            str(HERE / "detectors.py"),
            "eval",
            "--detector", binding.detector,
            "--params-b64", base64.b64encode(
                json.dumps(binding.params, sort_keys=True).encode("utf-8")
            ).decode("ascii"),
            "--context", str(ctx_path),
                )
                )
    if diff_only:
        cmd += " --diff-only"
    suffix = "::diff" if diff_only else ""
    return {
        "id": binding.criterion_id + suffix,
        "kind": "mechanical",
        "description": f"used[{binding.fact_id}] via {binding.detector}"
                       + (" (diff only)" if diff_only else ""),
        "command": cmd,
        # exit 0 = fired. 3 (not fired) and 4 (not eligible) are both "not a
        # pass"; the JSON on stdout is what tells them apart, and `eligible` is
        # what makes fired=0 censorable rather than a miss.
        "pass_condition": "exit_code == 0",
        "timeout": DETECT_TIMEOUT_S,
        "output_limit": DETECT_OUTPUT_LIMIT,
    }


def _parse_result(output: str) -> dict | None:
    """Pull the detector's JSON object out of a battery row's combined output."""
    if not output:
        return None
    start = output.find("{")
    while start >= 0:
        try:
            obj, _end = json.JSONDecoder().raw_decode(output[start:])
        except ValueError:
            start = output.find("{", start + 1)
            continue
        if isinstance(obj, dict) and "eligible" in obj:
            return obj
        start = output.find("{", start + 1)
    return None


def detect(bindings: Sequence[Binding],
    scope_res: ScopeResult,
    *,
    with_diff_only: bool = True,
    workdir: Path | None = None,
) -> dict:
    """Run every binding through battery.run and assemble the use_detect payload."""
    ctx = scope_res.ctx
    tmp = Path(tempfile.mkdtemp(prefix="wur-ctx-", dir=str(workdir) if workdir else None))
    try:
        per_fact: list[dict] = []
        errors: list[str] = []
        for b in bindings:
            fact_ctx = ctx
            if b.planted_paths:
                fact_ctx = DetectorContext(workspace=ctx.workspace,
                    diff_text=ctx.diff_text,
                    bash_commands=list(ctx.bash_commands),
                    planted_paths=sorted(set(list(ctx.planted_paths) + list(b.planted_paths))),
                    baseline_sha=ctx.baseline_sha,
                    scope=ctx.scope,
                )
            ctx_path = tmp / f"ctx-{b.fact_id}.json"
            ctx_path.write_text(json.dumps(fact_ctx.to_dict()))

            crits = [_criterion(b, ctx_path, diff_only=False)]
            if with_diff_only:
                crits.append(_criterion(b, ctx_path, diff_only=True))
            rows = battery.run(crits, ctx.workspace)

            main_row = rows[b.criterion_id]
            main_res = _parse_result(main_row.get("output", ""))
            diff_row = rows.get(b.criterion_id + "::diff")
            diff_res = _parse_result(diff_row.get("output", "")) if diff_row else None

            err = None
            if main_res is None:
                err = (f"detector produced no parseable result "
                    f"(status={main_row.get('status')}, exit={main_row.get('exit_code')})"
                )
            elif main_res.get("error"):
                err = main_res["error"]
            elif main_row.get("status") == battery.STATUS_ERROR:
                err = main_row.get("error")

            used = None if err else bool(main_res["fired"])
            eligible = None if main_res is None else bool(main_res["eligible"])
            if diff_res is None or (diff_res.get("error") if diff_res else None):
                used_in_diff = used if (err is None and _is_diff_native(b)) else None
            else:
                used_in_diff = bool(diff_res["fired"])

            row = {
                "fact_id": b.fact_id,
                "task_id": b.task_id,
                "label": b.label,
                "detector": b.detector,
                "bucket": detectors.REGISTRY[b.detector].bucket,
                "params": (main_res or {}).get("params", b.params),
                "params_sha256": (main_res or {}).get("params_sha256"),
                "used": used,
                "used_in_diff": used_in_diff,
                "eligible": eligible,
                "evidence": (main_res or {}).get("evidence", []),
                "detail": (main_res or {}).get("detail", {}),
                "error": err,
                "planted_paths": sorted(set(list(ctx.planted_paths) + list(b.planted_paths))),
                "battery": {
                    "criterion_id": b.criterion_id,
                    "passed": main_row.get("passed"),
                    "exit_code": main_row.get("exit_code"),
                    "status": main_row.get("status"),
                    "timed_out": main_row.get("timed_out"),
                    "duration_s": main_row.get("duration_s"),
                    "command": main_row.get("command"),
                    "diff_exit_code": (diff_row or {}).get("exit_code"),
                },
            }
            if err:
                errors.append(f"{b.fact_id}: {err}")
            per_fact.append(row)

        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scope": ctx.scope,
            "detector_registry_version": detectors.REGISTRY_VERSION,
            "detector_registry_sha256": registry_sha256(),
            "detectors_module_sha256": detectors.module_sha256(),
            "diff_source": scope_res.diff_source,
            "bash_source": scope_res.bash_source,
            "context": ctx.summary(),
            "facts": per_fact,
            "errors": errors,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _is_diff_native(b: Binding) -> bool:
    return detectors.REGISTRY[b.detector].diff_native


# ── CLI ──────────────────────────────────────────────────────────────────────
def _inline_bindings(a: argparse.Namespace) -> list[Binding]:
    if not a.detector:
        return []
    params = json.loads(a.params_json or "{}")
    problems = validate_params(a.detector, params)
    if problems:
        print("detector params rejected: " + "; ".join(problems), file=sys.stderr)
        raise SystemExit(2)
    return [Binding(fact_id=a.fact_id or "inline", detector=a.detector, params=params,
                    task_id=a.task_id, planted_paths=list(a.planted_path or ()))]


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="run the closed `used` detector registry")
    p.add_argument("--scope", choices=SCOPES, default="run")
    p.add_argument("--run-dir")
    p.add_argument("--workspace")
    p.add_argument("--tree")
    p.add_argument("--patch")
    p.add_argument("--baseline-sha")
    p.add_argument("--facts", help="path to facts.yaml / facts.json")
    p.add_argument("--task-id")
    p.add_argument("--fact-id")
    p.add_argument("--detector", help="run one detector inline instead of a registry")
    p.add_argument("--params-json", default="{}")
    p.add_argument("--planted-path", action="append", default=[])
    p.add_argument("--bash-json", help="JSON array of Bash commands (tree/patch scopes)")
    p.add_argument("--venv", help="hermetic venv to symlink into materialized trees")
    p.add_argument("--workdir", help="where temp trees go (default: system temp)")
    p.add_argument("--out", help="output path (default: <run-dir>/use_detect.json)")
    p.add_argument("--no-diff-only", action="store_true",
                   help="skip the second, diff-restricted pass (used_in_diff)")
    p.add_argument("--fail-on-error", action="store_true",
                   help="exit 1 if any detector errored (for gates, not for runs)")
    a = p.parse_args(argv)

    bindings = _inline_bindings(a)
    if not bindings:
        if not a.facts:
            print("need --facts or --detector", file=sys.stderr)
            return 2
        bindings = load_bindings(Path(a.facts), task_id=a.task_id, fact_id=a.fact_id)

    try:
        scope_res = build_scope(a.scope,
            run_dir=Path(a.run_dir) if a.run_dir else None,
            workspace=Path(a.workspace) if a.workspace else None,
            tree=Path(a.tree) if a.tree else None,
            patch=Path(a.patch) if a.patch else None,
            baseline_sha=a.baseline_sha,
            bash_commands=json.loads(a.bash_json) if a.bash_json else None,
            planted_paths=a.planted_path,
            venv=Path(a.venv) if a.venv else None,
            workdir=Path(a.workdir) if a.workdir else None,
                )
    except (ValueError, RuntimeError) as e:
        print(f"scope error: {e}", file=sys.stderr)
        return 2

    try:
        payload = detect(bindings, scope_res, with_diff_only=not a.no_diff_only,
                         workdir=Path(a.workdir) if a.workdir else None)
        payload["run_id"] = Path(a.run_dir).name if a.run_dir else None
        payload["task_id"] = a.task_id
        out_path = Path(a.out) if a.out else (
            Path(a.run_dir) / "use_detect.json" if a.run_dir else None
                )
        blob = json.dumps(payload, indent=2, sort_keys=True)
        if out_path:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = out_path.with_suffix(out_path.suffix + ".tmp")
            tmp.write_text(blob)
            os.replace(tmp, out_path)  # raw before derived, atomically
            summary = ", ".join(f"{f['fact_id']}: used={f['used']} eligible={f['eligible']}"
                for f in payload["facts"]
            ) or "[no bindings]"
            print(f"[detect_use] {out_path} — {summary}", file=sys.stderr)
        else:
            print(blob)
    finally:
        scope_res.cleanup()

    if a.fail_on_error and payload.get("errors"):
        for e in payload["errors"]:
            print(f"detector error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
