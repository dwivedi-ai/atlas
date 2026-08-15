#!/usr/bin/env python3
"""
judge.py — the NL-acceptance grader.

RESPONSIBILITY
  Turn a job's plain-English `accept` text into a mechanical battery, prove that
  battery DISCRIMINATES, and grade one run against it.

  --synthesize   Author criteria from `accept` (the agent only ever sees the acceptance,
                 never any solution), then FLOOR-CHECK them against the pristine base
                 worktree: new-behavior criteria must FAIL on base or they measure
                 nothing. Writes grader/<task>/{criteria.json, manifest.json, floor.log}.
  --floor-check  Re-run the floor check (and the two-sided proof) over the criteria
                 already on disk, without spending an LLM call re-authoring them.
  --grade        Run the battery against one run's workspace, LLM-adjudicate the
                 non-mechanical criteria, write the run's judge.json.
  --regrade      Grade a FINISHED run by rebuilding its tree from
                 refs/atlas/baseline-run/<RUN_ID> + git.patch, because teardown
                 deletes the workspace. Mechanical only; writes judge.regrade.json
                 (or judge.json with --in-place).

INPUTS   job.yaml (via jobspec), the pinned base SHA, a run's workspace + git.patch.
OUTPUTS  grader/<task>/criteria.json + manifest.json + floor.log; runs/<id>/judge.json.

THE FLOOR CHECK IS ONE-SIDED UNLESS YOU GIVE IT REFERENCE SOLUTIONS
  Running the battery against the pristine base establishes "fails before" and
  silently ASSUMES "passes after". A criterion shaped `exit_code == 0 and <broken
  expression>` short-circuits on the base tree — the command exits non-zero, the
  broken half is never evaluated — so it looks cleanly discriminating and breaks
  only once a correct solution makes the command succeed. The one-sided check
  cannot see this by construction, and it was measured in the field: three of seven
  criteria scored "undecided" on a verified-correct solution, which grades a
  completed run as not-completed.
  Put `reference_patches: [...]` on a task — independent known-correct solutions —
  and the proof becomes two-sided: base must FAIL every new-behaviour criterion and
  every reference must PASS all of them. Absent references, the manifest records
  `proof.two_sided: false` rather than implying a proof it did not run.

TWO DEFECTS THIS FILE USED TO HAVE
  1. `synthesize` returned EARLY when criteria.json already existed, so a HAND-WRITTEN
     criteria pack — the normal way a WUR detector battery arrives — was never floor
     checked. An unverified battery that passes on the base tree measures nothing and
     reports 1.00. Now the early return skips only the LLM SYNTHESIS; the floor check
     always runs, and floor_check() is importable so other callers reuse it.
  2. `met = bool(r["passed"])` silently turned "the criterion could not be evaluated"
     (passed is None — an eval error, a timeout, a missing command) into a hard FAIL,
     which is indistinguishable in judge.json from a criterion the candidate actually
     failed. An unevaluable criterion is now `met: None` with an `error`, and the score
     is computed over the criteria that were actually GRADED. A run whose battery blew
     up scores None, not 0.0.

The judge uses the SAME model the user chose for the job (one logged-in CLI), via
lib/agent.py, under a per-call CLAUDE_CONFIG_DIR so it carries no more ambient context
than the task run does. Synthesis/adjudication are separate invocations from the task run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))

import agent          # noqa: E402
import battery        # noqa: E402
import jobspec        # noqa: E402

CRITERIA_SCHEMA_HINT = """\
Each criterion is one JSON object with these fields:
  "id":            short unique id, e.g. "C1"
  "description":   one plain sentence describing exactly what is checked
  "kind":          "mechanical" (a shell command decides it) or "llm" (needs judgement)
  "command":       for mechanical: a shell command run from the repo root; else null
  "pass_condition":for mechanical: a Python expression over `exit_code` (int) and
                   `stdout` (str, stdout+stderr combined, stripped). Examples:
                   "exit_code == 0", "int(stdout.strip()) >= 1", "len(stdout) > 0".
                   For llm: null

                   IT IS EVALUATED IN A SANDBOX WITH NO __builtins__. Available:
                   all any len sum min max sorted set list dict tuple map filter
                   range enumerate zip abs round int str float bool repr
                   isinstance, plus the modules `re` and `json` ALREADY BOUND.
                   Write `re.search(...)` and `json.loads(stdout)` directly.
                   `__import__`, `open`, `eval` and `exec` are ABSENT — a
                   condition using them raises, scores the criterion None, and
                   fails the floor-check.

                   DO NOT PIPE the command (`cmd | tail -5`, `cmd | head`): the
                   shell reports the LAST stage's status, so `exit_code` becomes
                   tail's 0 and stops meaning anything. If you only need the tail
                   of the output, keep the command unpiped and slice in the
                   condition instead (`stdout` is already captured whole).

                   PUT THE EXIT-CODE GUARD AT THE TOP LEVEL of the expression.
                   Python evaluates a call's ARGUMENTS before its body, so this
                   is BROKEN — the parse runs before the guard, raises whenever
                   the command failed, and scores the criterion None:
                       (lambda d: exit_code == 0 and d['x'])(json.loads(stdout))
                   Write one of these instead:
                       exit_code == 0 and json.loads(stdout)['x']
                       exit_code == 0 and (lambda d: d['x'])(json.loads(stdout))
  "expect_on_base":"fail" if this checks NEW behavior the task is supposed to add
                   (so it should FAIL on the unmodified repo), or "pass" if it is an
                   invariant that should already hold (e.g. "existing tests pass")."""


# ── prompt builders ──────────────────────────────────────────────────────────
def _repo_tree(workspace: Path, limit: int = 200) -> str:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=str(workspace), capture_output=True, text=True, timeout=30
        ).stdout
    except Exception:  # noqa: BLE001
        out = ""
    files = [f for f in out.splitlines() if f]
    shown = files[:limit]
    extra = f"\n... (+{len(files) - limit} more)" if len(files) > limit else ""
    return "\n".join(shown) + extra


def _synthesis_prompt(task: str, accept: str, tree: str) -> str:
    return f"""\
You are setting up an automated GRADER for a coding task. You will NOT see any
candidate solution — only the task and what an accepted solution looks like. Your
job is to turn the acceptance description into a small battery of concrete,
checkable criteria a machine can run against a candidate's working directory.

The candidate's changes will be UNCOMMITTED in the working tree at grade time, so
mechanical commands can inspect them with e.g. `git diff HEAD` / `git status` or by
running the code/tests. The hermetic interpreter is at ./venv/bin (so use
`./venv/bin/python` and `./venv/bin/pytest`, or just `python`/`pytest` — PATH is set).

--- TASK (what the candidate was asked to do) ---
{task}

--- ACCEPTANCE (what a correct solution looks like) ---
{accept}

--- REPO FILES (tracked, at base) ---
{tree}

Produce 3-7 criteria that together capture the acceptance. Prefer "mechanical"
criteria (deterministic shell commands). Use "llm" only for genuinely subjective
checks (e.g. "no unrelated files changed", "code is reasonable"). Keep each
criterion focused on ONE thing. Make mechanical commands robust (don't assume a
server is running — import the app / use a test client / grep the diff instead).

IMPORTANT calibration rules (avoid false negatives on a correct solution):
- Concrete values in the acceptance (e.g. "multiply(3,4)==12") are ILLUSTRATIVE
  examples. Test the GENERAL behavior (e.g. that multiply returns a*b for a few
  inputs you choose), NOT one hard-coded literal — unless the acceptance clearly
  mandates an exact value. Do not require the candidate to use your specific test
  inputs.
- For any "no unrelated files changed" / file-scope criterion, IGNORE noise that
  is not a source change: `__pycache__/`, `*.pyc`, `.pytest_cache/`, and `venv`.
  Compare only real tracked/untracked source files against what the task allows.
- NEVER grep file text for a word to decide whether something was added. A word
  that occurs anywhere for an unrelated reason satisfies the grep on the UNMODIFIED
  repo, and the criterion then scores 1 for every candidate including one that did
  nothing. To check "was a test added?", ask the test runner what it COLLECTED —
  `python3 -m pytest --collect-only`, then count lines containing `::` — and
  compare against the count on the unmodified repo.
- If the repo's `pytest.ini`/`setup.cfg` already sets `-q` in `addopts`, do NOT add
  another `-q`: two of them raise the quiet level and suppress the very summary line
  and node-id listing your condition parses.
- If the TASK asks for something the ACCEPTANCE does not mention (a common one:
  "add tests"), grade the acceptance and say nothing about the rest. Do not invent
  a criterion for it. The mismatch is a problem with the two documents, not
  something to paper over here.

{CRITERIA_SCHEMA_HINT}

Write your answer as a JSON file named `criteria.json` in the current directory,
with exactly this shape:
{{"criteria": [ {{ ...criterion... }}, ... ]}}
Write ONLY that file. Do not modify anything else."""


def _adjudication_prompt(diff: str, llm_criteria: list[dict]) -> str:
    crit_lines = "\n".join(f'  - id={c["id"]}: {c["description"]}' for c in llm_criteria)
    diff_trunc = diff[:12000] + ("\n...[diff truncated]..." if len(diff) > 12000 else "")
    return f"""\
You are grading a candidate's code change against specific criteria. Below is the
unified diff of everything the candidate changed, followed by the criteria you must
judge. For each criterion decide if it is MET (true) or NOT met (false), with a one
line evidence note. Be strict and literal.

--- CANDIDATE DIFF ---
{diff_trunc}

--- CRITERIA TO JUDGE ---
{crit_lines}

Respond with ONLY a JSON object (no prose) of this shape:
{{"verdicts": [ {{"id": "C?", "met": true, "evidence": "..."}}, ... ]}}"""


# ── json extraction helpers ──────────────────────────────────────────────────
def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    # Prefer a fenced ```json block, else the outermost {...}.
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = m.group(1) if m else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        candidate = text[start:end + 1] if start != -1 and end > start else None
    if candidate is None:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


# ── criteria linter ──────────────────────────────────────────────────────────
# Names battery.py's sandbox does NOT provide. A pass_condition using one raises,
# scores the criterion None ("undecided"), and drags the whole floor check to
# floor_ok=false — via a NameError buried in a 300-char output field. A shipped
# battery in this repo once used `__import__('json')` in four of seven criteria
# although the synthesis prompt already documents it as absent, and the one-sided
# floor check could not see it. Naming the violation is cheaper than deducing it.
SANDBOX_ABSENT = ("__import__", "open", "eval", "exec", "compile", "globals",
                  "locals", "vars", "getattr", "setattr", "delattr", "input",
                  "__builtins__", "__class__", "__subclasses__")
_QUOTED_RE = re.compile(r"'[^']*'" + r'|"[^"]*"')
_SHORT_CIRCUIT_RE = re.compile(r"\bexit_code\s*==\s*0\s+and\b")
# A lambda that takes the exit-code guard INSIDE its body while the parse sits in its
# ARGUMENT: `(lambda d: exit_code == 0 and...)(json.loads(stdout))`. Python evaluates
# a call's arguments before its body, so the parse runs unguarded, raises on the base
# tree, and scores the criterion None. Measured: 3 of 7 criteria of one battery.
_GUARD_INSIDE_LAMBDA_RE = re.compile(r"lambda\b[^:]*:[^)]*\bexit_code\b.*?\)\s*\(\s*(?:json\.loads|int|float)\b", re.DOTALL)
# A criterion that greps the WORKING TREE for a word, rather than asking a tool what
# actually happened. The fixture contains `assets.cashflow` inside an unrelated
# assertion, which satisfied a "was a cashflow test added?" grep on the pristine tree.
# Grepping a DIFF is a different and legitimate thing — it is about what CHANGED, and
# the synthesis prompt recommends it for file-scope criteria — so a command that also
# names git diff/show/status is left alone.
_TREE_GREP_RE = re.compile(r"\bgrep\b")
_DIFF_SCOPED_RE = re.compile(r"\bgit\s+(diff|show|status|ls-files)\b")
# `pytest -q` when pytest.ini already sets `-q`: the second one raises the quiet level
# and suppresses both the summary line and the node-id listing a condition parses.
_PYTEST_Q_RE = re.compile(r"\bpytest\b[^\n]*(?<![\w-])-q(?![\w-])")


def lint_criteria(criteria: list[dict], two_sided: bool = False) -> list[dict]:
    """Advisory static checks over a criteria pack. Returns one row per finding.

    Nothing here can decide whether a criterion is correct — that is what the
    two-sided proof is for. These are the failure shapes that have actually shipped,
    each of which reads as a legitimate verdict downstream.

    `two_sided` suppresses the `short_circuit` finding, whose own remedy is "give the
    task reference_patches so the proof is two-sided". Once that is done the finding
    is answered, and a linter that keeps crying about it trains people to skip the
    output — which is where the `sandbox` findings are.
    """
    findings: list[dict] = []
    for c in criteria:
        cid = c.get("id", "?")
        if c.get("kind") != "mechanical":
            continue
        cond = c.get("pass_condition") or ""
        cmd = c.get("command") or ""
        if _GUARD_INSIDE_LAMBDA_RE.search(cond):
            findings.append({
                "id": cid, "kind": "guard_inside_lambda",
                "detail": "the exit-code guard is INSIDE a lambda whose argument does the "
                          "parsing. Python evaluates a call's arguments before its body, so "
                          "the parse runs BEFORE the guard, raises on any tree where the "
                          "command failed, and scores this criterion None (undecided) "
                          "instead of False. Move the guard to the top level: "
                          "`exit_code == 0 and (lambda d:...)(json.loads(stdout))`.",
            })
        if _TREE_GREP_RE.search(cmd) and not _DIFF_SCOPED_RE.search(cmd):
            findings.append({
                "id": cid, "kind": "tree_grep",
                "detail": "the command greps the WORKING TREE for a word. A word that "
                          "appears anywhere for an unrelated reason satisfies it on the "
                          "pristine tree, and the criterion then scores 1 for every run "
                          "including the do-nothing run. To ask 'was a test added?', count "
                          "what the test runner COLLECTED (`pytest --collect-only`); to ask "
                          "'what changed?', grep a `git diff` instead.",
            })
        if _PYTEST_Q_RE.search(cmd):
            findings.append({
                "id": cid, "kind": "double_quiet",
                "detail": "`pytest -q` — pytest.ini already sets `-q` for this fixture, so "
                          "a second one raises the quiet level and suppresses both the "
                          "summary line and the node-id listing a condition parses. Invoke "
                          "pytest with no `-q` of your own.",
            })
        for name in SANDBOX_ABSENT:
            if re.search(rf"(?<![\w.]){re.escape(name)}\b", cond):
                findings.append({
                    "id": cid, "kind": "sandbox",
                    "detail": f"pass_condition uses `{name}`, which battery.py's sandbox "
                              f"does not provide — it will raise and score this criterion "
                              f"None (undecided), not False",
                })
        if not two_sided and _SHORT_CIRCUIT_RE.search(cond):
            findings.append({
                "id": cid, "kind": "short_circuit",
                "detail": "pass_condition is `exit_code == 0 and...`: on a base tree "
                          "where the command fails, the right-hand side is NEVER "
                          "evaluated, so the base-only floor check cannot tell a sound "
                          "expression from a broken one. Give the task "
                          "`reference_patches` so the proof is two-sided.",
            })
        bare = _QUOTED_RE.sub("", cmd)
        if re.search(r"(?<!\|)\|(?!\|)", bare):
            findings.append({
                "id": cid, "kind": "pipe",
                "detail": "command is piped: the shell reports the LAST stage's status, "
                          "so `exit_code` becomes the tail command's 0 and stops meaning "
                          "anything. Keep the command unpiped and slice `stdout` in the "
                          "condition instead.",
            })
    return findings


def _load_criteria_file(d: Path) -> dict | None:
    f = d / "criteria.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except json.JSONDecodeError:
            return None
    return None


# ── worktree helpers (shell out to keep flock semantics simple) ──────────────
def _exclude_venv(worktree: Path) -> None:
    """Mirror setup_run.sh: hide the ./venv symlink from git so file-scope checks
    behave identically here (floor-check) and at grade time (run workspace)."""
    try:
        gp = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"],
                            cwd=str(worktree), capture_output=True, text=True, check=True).stdout.strip()
        excl = (worktree / gp) if not Path(gp).is_absolute() else Path(gp)
        excl.parent.mkdir(parents=True, exist_ok=True)
        existing = excl.read_text() if excl.exists() else ""
        if "/venv" not in existing.split():
            excl.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + "/venv\n")
    except Exception:  # noqa: BLE001
        pass


def _remove_worktree(job_dir: Path, dest: Path) -> None:
    _flocked_worktree(job_dir, ["remove", "--force", str(dest)], ignore_error=True)
    if dest.exists():
        subprocess.run(["rm", "-rf", str(dest)])


def _flocked_worktree(job_dir: Path, args: list[str], ignore_error: bool = False) -> None:
    bare = job_dir / "repo.git"
    lock = job_dir / ".worktree.lock"
    quoted = " ".join(f'"{a}"' if " " in a or "/" in a else a for a in args)
    script = f'flock 200; git --git-dir="{bare}" worktree {quoted}'
    cmd = ["bash", "-c", f"exec 200>\"{lock}\"; {script}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 and not ignore_error:
        raise RuntimeError(f"worktree {args}: {r.stderr.strip()}")


# ── synthesize ───────────────────────────────────────────────────────────────
def _resolve_task(spec: dict, task_id: str | None) -> dict:
    tasks = spec.get("tasks") or []
    if not tasks:
        raise RuntimeError("job has no tasks")
    if task_id is None:
        return tasks[0]
    for t in tasks:
        if t["id"] == task_id:
            return t
    raise RuntimeError(f"no such task id: {task_id}")


def _normalize_criteria(criteria: dict) -> dict:
    """Fill the fields the battery and the floor check require. Idempotent, so it is safe
    on a hand-written pack as well as on an LLM-authored one."""
    for i, c in enumerate(criteria.get("criteria") or [], 1):
        c.setdefault("id", f"C{i}")
        c.setdefault("kind", "mechanical" if c.get("command") else "llm")
        c.setdefault("expect_on_base", "fail")
        c.setdefault("description", c.get("criterion", c["id"]))
    return criteria


def _resolve_path(job_dir: Path, raw: str) -> Path | None:
    """A pack-relative path (reference patch, frozen criteria file): absolute, or
    relative to the job dir, or relative to the runner's repo root."""
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p if p.is_file() else None
    for base in (job_dir, LIB.parent):
        cand = (base / p).resolve()
        if cand.is_file():
            return cand
    return None


def _apply_patch(worktree: Path, patch: Path) -> tuple[bool, str]:
    """Apply an absolute patch path inside `worktree`. (False, why) if it will not apply.

    ABSOLUTE on purpose: `git -C <dir> apply <relpath>` chdirs BEFORE resolving the
    patch path, so a relative path silently becomes "can't open patch". Never
    treated as success — a reference solution that does not apply is a broken
    proof, not a passing one.
    """
    last = ""
    for extra in ([], ["--3way"]):
        r = subprocess.run(["git", "-C", str(worktree), "apply", "--whitespace=nowarn", *extra, str(patch)],
            capture_output=True, text=True,
)
        if r.returncode == 0:
            return True, ""
        last = (r.stderr or r.stdout or "").strip()
    return False, last[:400]


def reference_check(job_dir: Path, task_id: str, criteria: list[dict],
                    references: list[str], sha: str | None = None) -> dict:
    """Run the battery against each reference solution — the OTHER half of the proof.

    Returns {"references": [{path, applied, error, results:{cid: passed}}],
             "per_criterion": {cid: True|False|None}}, where per_criterion is True
    only when the criterion PASSED on every reference that applied. None means
    "no reference established this" (no references, or none applied).
    """
    job_dir = Path(job_dir).resolve()
    out: dict = {"references": [], "per_criterion": {}}
    tallies: dict[str, list] = {c["id"]: [] for c in criteria}
    venv = job_dir / ".venv"
    for i, raw in enumerate(references):
        row = {"path": raw, "resolved": None, "applied": False, "error": None, "results": {}}
        patch = _resolve_path(job_dir, raw)
        if patch is None:
            row["error"] = "reference patch not found"
            out["references"].append(row)
            continue
        row["resolved"] = str(patch)
        scratch = job_dir / f".ref-{task_id}-{i}"
        _remove_worktree(job_dir, scratch)
        _flocked_worktree(job_dir, ["add", "--detach", str(scratch)] + ([sha] if sha else []))
        try:
            if venv.exists() and not (scratch / "venv").exists():
                (scratch / "venv").symlink_to(venv)
            _exclude_venv(scratch)
            ok, why = _apply_patch(scratch, patch)
            row["applied"] = ok
            if not ok:
                row["error"] = f"patch did not apply: {why}"
                out["references"].append(row)
                continue
            res = battery.run(criteria, scratch)
            for c in criteria:
                cid = c["id"]
                passed = res.get(cid, {}).get("passed")
                row["results"][cid] = passed
                if c.get("kind") == "mechanical":
                    tallies[cid].append(passed)
        finally:
            _remove_worktree(job_dir, scratch)
        out["references"].append(row)
    for cid, seen in tallies.items():
        out["per_criterion"][cid] = (all(v is True for v in seen) if seen else None)
    return out


def floor_check(job_dir: Path, task_id: str, criteria: list[dict],
                sha: str | None = None, write: bool = True,
                references: list[str] | None = None) -> dict:
    """Run `criteria` against a PRISTINE base worktree and report whether each one
    discriminates. Importable on purpose — a hand-written battery (every WUR detector
    pack) must go through exactly this, and it is also what the mandate-detector gate
    reuses.

    A "new behavior" criterion (`expect_on_base: "fail"`) that PASSES on the untouched
    repo tests nothing: every run scores it 1 regardless of what the agent did. A
    criterion that cannot be EVALUATED on base (passed is None) is `skip`, which matches
    neither expectation and is therefore reported as non-discriminating rather than
    quietly treated as a fail.

    `references` are known-correct solutions (patch paths). With them the check is
    TWO-SIDED — base must fail and every reference must pass — which is the only way
    to catch a criterion whose expression is broken behind a short-circuit. Without
    them the manifest records `proof.two_sided: false` rather than implying a proof
    that never ran.

    Returns the manifest {floor_ok, proof_ok, proof, lint, criteria:[{id, kind,
    expect_on_base, floor_passed, passes_on_references, discriminating,
    two_sided_ok, output}], sha, n_mechanical, n_llm} and, with write=True, also
    writes grader/<task>/manifest.json + floor.log.
    """
    job_dir = Path(job_dir).resolve()
    grader = job_dir / "grader" / task_id
    scratch = job_dir / f".floor-{task_id}"
    # A FRESH worktree is pristine by construction. The old code reused the synthesis
    # worktree and scrubbed it with reset --hard + clean -fdx, which had to be exactly
    # right or the synthesis agent's own litter would satisfy a "no unrelated files"
    # invariant. Checking out again is cheaper than being sure.
    _remove_worktree(job_dir, scratch)
    _flocked_worktree(job_dir, ["add", "--detach", str(scratch)] + ([sha] if sha else []))
    venv = job_dir / ".venv"
    if venv.exists() and not (scratch / "venv").exists():
        (scratch / "venv").symlink_to(venv)
    _exclude_venv(scratch)
    try:
        floor = battery.run(criteria, scratch)
    finally:
        _remove_worktree(job_dir, scratch)

    refs = list(references or [])
    ref_out = (reference_check(job_dir, task_id, criteria, refs, sha=sha)
               if refs else {"references": [], "per_criterion": {}})
    ref_pass = ref_out["per_criterion"]
    refs_all_applied = bool(refs) and all(r["applied"] for r in ref_out["references"])

    manifest: dict = {"criteria": [], "floor_ok": True, "sha": sha}
    log_lines = [f"Floor-check (pristine base @ {(sha or 'HEAD')[:10]})", ""]
    proof_ok = refs_all_applied
    for c in criteria:
        res = floor.get(c["id"], {"passed": None, "output": ""})
        passed = res.get("passed")
        exp = c.get("expect_on_base", "fail")
        on_refs = ref_pass.get(c["id"])
        if c.get("kind") != "mechanical":
            status, ok = "n/a (llm)", True
            two_sided_ok = None
        else:
            actual = "pass" if passed else ("fail" if passed is False else "skip")
            ok = (actual == exp)
            status = f"base={actual} expect={exp} -> {'OK' if ok else 'MISMATCH'}"
            if not ok:
                manifest["floor_ok"] = False
            # A criterion earns its place only if it fails before AND passes after.
            # `on_refs is None` = no reference established the second half, so the
            # verdict stays None: unknown, never a silent pass.
            two_sided_ok = None if on_refs is None else bool(ok and on_refs)
            if two_sided_ok is False:
                proof_ok = False
            if refs:
                status += f"  refs={'pass' if on_refs else ('FAIL' if on_refs is False else '?')}"
        row = {
            "id": c["id"], "kind": c.get("kind"), "expect_on_base": exp,
            "floor_passed": passed, "discriminating": ok,
            "output": (res.get("output") or "")[:300],
        }
        if refs:
            row["passes_on_references"] = on_refs
            row["two_sided_ok"] = two_sided_ok
        manifest["criteria"].append(row)
        log_lines.append(f"[{c['id']}] {status}")
        log_lines.append(f"        {c.get('description', '')}")
        out = res.get("output") or ""
        if out:
            log_lines.append(f"        out: {out[:160].splitlines()[0]}")

    manifest["n_mechanical"] = sum(1 for c in criteria if c.get("kind") == "mechanical")
    manifest["n_llm"] = len(criteria) - manifest["n_mechanical"]
    manifest["proof"] = {
        "two_sided": bool(refs),
        "n_references": len(refs),
        "references": ref_out["references"],
    }
    manifest["proof_ok"] = bool(refs) and proof_ok
    manifest["lint"] = lint_criteria(criteria, two_sided=bool(refs))

    log_lines.append("")
    if refs:
        log_lines.append(f"Two-sided proof over {len(refs)} reference solution(s):")
        for r in ref_out["references"]:
            state = "applied" if r["applied"] else f"NOT APPLIED — {r.get('error')}"
            log_lines.append(f"  - {r['path']}: {state}")
        log_lines.append(f"  proof_ok={manifest['proof_ok']}")
    else:
        log_lines.append("Two-sided proof: NOT RUN — this task declares no "
                         "`reference_patches`, so 'passes after a correct solution' is "
                         "ASSUMED, not shown. A criterion broken behind a short-circuit "
                         "is invisible to a base-only check.")
    if manifest["lint"]:
        log_lines.append("")
        log_lines.append("Lint:")
        for f in manifest["lint"]:
            log_lines.append(f"  [{f['id']}] {f['kind']}: {f['detail']}")

    if write:
        grader.mkdir(parents=True, exist_ok=True)
        (grader / "manifest.json").write_text(json.dumps(manifest, indent=2))
        (grader / "floor.log").write_text("\n".join(log_lines) + "\n")
    return manifest


def _synthesize_criteria(job_dir: Path, spec: dict, task: dict, sha: str) -> dict:
    """The LLM half: author criteria.json in a scratch worktree. Returns the pack."""
    model = jobspec._model_of(spec)
    max_seconds = int(spec.get("max_seconds") or 0)
    scratch = job_dir / f".synth-src-{task['id']}"
    _remove_worktree(job_dir, scratch)
    _flocked_worktree(job_dir, ["add", "--detach", str(scratch), sha])
    venv = job_dir / ".venv"
    if venv.exists() and not (scratch / "venv").exists():
        (scratch / "venv").symlink_to(venv)
    _exclude_venv(scratch)
    try:
        tree = _repo_tree(scratch)
        prompt = _synthesis_prompt(task["task"], task["accept"], tree)
        criteria = None
        attempts = 3
        for attempt in range(1, attempts + 1):
            print(f"[synth] authoring criteria (attempt {attempt}/{attempts}) with {model} ...")
            # a fresh attempt must not inherit a previous attempt's partial file
            (scratch / "criteria.json").unlink(missing_ok=True)
            # config_dir: the judge runs under the same isolation as the task.
            text, _code = agent.run_text(model, prompt, scratch, max_seconds,
                                         config_dir=str(scratch / ".judge_home"))
            data = _load_criteria_file(scratch) or _extract_json(text)
            if data and isinstance(data.get("criteria"), list) and data["criteria"]:
                criteria = data
                break
            print(f"[synth] attempt {attempt} produced no valid criteria"
                  + (" — retrying" if attempt < attempts else ""), file=sys.stderr)
        return criteria or {"criteria": []}
    finally:
        _remove_worktree(job_dir, scratch)


def synthesize(job_dir: Path, task_id: str | None = None, force: bool = False) -> dict:
    """Author (or reuse) the criteria pack, then ALWAYS floor-check it.

    `force` and an existing criteria.json control the LLM SYNTHESIS ONLY. The floor check
    is not optional: a hand-written pack is exactly the case where nobody has ever proved
    the criteria fail on the base tree.
    """
    job_dir = Path(job_dir).resolve()
    spec = jobspec.load(job_dir)
    task = _resolve_task(spec, task_id)
    task_id = task["id"]
    grader = job_dir / "grader" / task_id
    grader.mkdir(parents=True, exist_ok=True)
    sha = spec["repo"]["pinned_sha"]

    # A FROZEN pack wins over everything, including --force. Synthesis is one unseeded
    # LLM call that produces a different battery each time — two jobs that author their
    # own graders are not comparable, and the base-only floor check cannot tell a sound
    # battery from a broken one. A task that ships `criteria_file` has already been
    # proven; installing it verbatim is the point.
    frozen_ref = task.get("criteria_file")
    frozen = _resolve_path(job_dir, frozen_ref) if frozen_ref else None
    if frozen_ref and frozen is None:
        raise RuntimeError(f"task '{task_id}' declares criteria_file '{frozen_ref}' but it does not exist. "
            f"Refusing to fall back to LLM synthesis: a job that silently authors its own "
            f"battery is not comparable to one that installed the frozen pack.")

    existing = _load_criteria_file(grader) if not force else None
    if frozen is not None:
        try:
            pack = json.loads(frozen.read_text())
        except json.JSONDecodeError as e:
            raise RuntimeError(f"criteria_file '{frozen}' is not valid JSON: {e}") from e
        if not isinstance(pack.get("criteria"), list) or not pack["criteria"]:
            raise RuntimeError(f"criteria_file '{frozen}' carries no criteria")
        criteria = _normalize_criteria(pack)
        print(f"[frozen] installed {len(criteria['criteria'])} criteria from {frozen} "
              f"(no LLM synthesis)")
    elif existing and isinstance(existing.get("criteria"), list) and existing["criteria"]:
        print(f"[skip] criteria already present, skipping LLM synthesis: {grader/'criteria.json'}")
        criteria = _normalize_criteria(existing)
    else:
        criteria = _normalize_criteria(_synthesize_criteria(job_dir, spec, task, sha))
    (grader / "criteria.json").write_text(json.dumps(criteria, indent=2))

    if not criteria["criteria"]:
        print("[synth] no criteria — skipping floor-check", file=sys.stderr)
        return criteria

    manifest = floor_check(job_dir, task_id, criteria["criteria"], sha=sha,
                           references=task.get("reference_patches") or [])
    n_mech, n_llm = manifest["n_mechanical"], manifest["n_llm"]
    print(f"[synth] {len(criteria['criteria'])} criteria ({n_mech} mechanical, {n_llm} llm); "
          f"floor_ok={manifest['floor_ok']} -> {grader}")
    _report_proof(manifest)
    return criteria


def _report_proof(manifest: dict, tag: str = "synth") -> None:
    """Report BOTH halves of the proof separately, plus the lint findings.

    The two halves fail for different reasons and need different fixes, so they are
    never collapsed into one message: a criterion can pass on every reference and
    still not establish "fails before" (the base run scores it `None`, undecided,
    because the expression raised before its own exit-code guard ran), and that is
    a different defect from one that fails on a known-correct solution.
    """
    def _w(msg: str) -> None:
        print(f"[{tag}] {msg}", file=sys.stderr)

    base_bad = [c["id"] for c in manifest["criteria"] if not c["discriminating"]]
    if base_bad:
        _w(f"WARNING: {base_bad} do not establish 'fails before' — they do not "
           f"discriminate on the base tree, so they score the same for every run and "
           f"measure nothing. (A `floor_passed: null` here means the expression "
           f"RAISED on base, which is undecided, not a failure.)")

    proof = manifest.get("proof") or {}
    if not proof.get("two_sided"):
        _w("NOTE: the floor check is ONE-SIDED — it proves 'fails before' and assumes "
           "'passes after'. Add `reference_patches` to the task to prove both.")
    else:
        unapplied = [r["path"] for r in proof.get("references", []) if not r["applied"]]
        if unapplied:
            _w(f"WARNING: reference solution(s) did not apply: {unapplied} — the proof "
               f"did not run. A reference that will not apply is a broken proof, not a "
               f"passing one.")
        ref_bad = [c["id"] for c in manifest["criteria"]
                   if c.get("passes_on_references") is False]
        if ref_bad:
            _w(f"WARNING: {ref_bad} FAIL on a known-correct solution — a completed run "
               f"will be graded not-completed. Fix these before trusting a verdict.")
        n = proof["n_references"]
        if manifest.get("proof_ok"):
            print(f"[{tag}] two-sided proof OK over {n} reference solution(s).")
        elif not unapplied and not ref_bad:
            # Say so explicitly. Otherwise a reader sees only the base-half warning
            # and cannot tell whether the reference half ran at all.
            _w(f"NOTE: the reference half PASSED — every criterion holds on all {n} "
               f"known-correct solution(s). proof_ok is false only because of the base "
               f"half above.")
    for f in manifest.get("lint") or []:
        _w(f"LINT [{f['id']}] {f['kind']}: {f['detail']}")


# ── grade ────────────────────────────────────────────────────────────────────
def grade(job_dir: Path, run_id: str, task_id: str | None = None, agent_exit_code: int = 0) -> dict:
    job_dir = Path(job_dir).resolve()
    spec = jobspec.load(job_dir)
    task = _resolve_task(spec, task_id)
    grader = job_dir / "grader" / task["id"]
    run_dir = job_dir / "runs" / run_id
    workspace = run_dir / "workspace"

    criteria = (_load_criteria_file(grader) or {}).get("criteria", [])
    out = run_dir / "judge.json"

    if not criteria:
        judge = {"run_id": run_id, "verdict": "error", "score": None, "criteria": [],
                 "battery": {"total": 0, "passed": 0}, "reasoning": "no criteria synthesized"}
        out.write_text(json.dumps(judge, indent=2))
        return judge
    if not workspace.exists():
        judge = {"run_id": run_id, "verdict": "error", "score": None, "criteria": [],
                 "battery": {"total": 0, "passed": 0}, "reasoning": "workspace missing at grade time"}
        out.write_text(json.dumps(judge, indent=2))
        return judge

    # 1) mechanical battery
    mech_results = battery.run(criteria, workspace)

    # 2) llm adjudication for non-mechanical criteria
    llm_criteria = [c for c in criteria if c.get("kind") != "mechanical"]
    llm_verdicts: dict[str, dict] = {}
    if llm_criteria:
        diff = (run_dir / "git.patch").read_text(errors="replace") if (run_dir / "git.patch").exists() else ""
        votes = max(1, int(spec.get("judge_votes") or 1))
        tally: dict[str, list[bool]] = {c["id"]: [] for c in llm_criteria}
        evidence: dict[str, str] = {}
        model = jobspec._model_of(spec)
        for _v in range(votes):
            with tempfile.TemporaryDirectory() as tmp:
                text, _ = agent.run_text(model, _adjudication_prompt(diff, llm_criteria),
                                         tmp, int(spec.get("max_seconds") or 0),
                                         config_dir=str(Path(tmp) / ".judge_home"))
            data = _extract_json(text) or {}
            for item in data.get("verdicts", []):
                cid = item.get("id")
                if cid in tally:
                    tally[cid].append(bool(item.get("met")))
                    evidence[cid] = item.get("evidence", "")
        for cid, mets in tally.items():
            # No vote at all is NOT a "no": the adjudicator failed to answer, which is an
            # unevaluated criterion, not a failed one.
            met = (sum(mets) > len(mets) / 2) if mets else None
            llm_verdicts[cid] = {
                "met": met,
                "evidence": evidence.get(cid, "(no response)"),
                "error": None if mets else "adjudicator returned no verdict for this criterion",
            }

    # 3) assemble per-criterion verdicts.
    judge = _assemble_judge(run_id, criteria, mech_results, llm_verdicts, agent_exit_code)
    # WHICH battery produced this verdict. A grader patched mid-collection leaves a
    # dataset that was not scored by one instrument, and without a stamp on the row
    # that is undetectable after the fact — it happened, and it cost a published
    # claim. Comparing this hash across a job's runs is the cheapest possible check.
    judge["grader"] = {
        "criteria_sha256": _sha256_file(grader / "criteria.json"),
        "criteria_path": str(grader / "criteria.json"),
    }
    out.write_text(json.dumps(judge, indent=2))
    score = judge["score"]
    score_s = f"{score:.3f}" if isinstance(score, float) else "None"
    print(f"[grade] {run_id}: verdict={judge['verdict']} score={score_s} "
          f"({judge['criteria_graded']} graded, {judge['criteria_errored']} unevaluable, "
          f"{judge['criteria_total']} total)")
    return judge


def _assemble_judge(run_id: str, criteria: list[dict], mech_results: dict,
                    llm_verdicts: dict, agent_exit_code: int = 0) -> dict:
    """Turn battery rows + adjudicator verdicts into the judge record.

    `met` is TRI-STATE: True / False / None. None means "could not be evaluated" — an
    eval error in the pass_condition, a timeout, a missing command, an adjudicator
    that did not answer. Coercing it to False (the old `bool(r["passed"])`) made a
    broken battery indistinguishable from a failed solution and silently dragged the
    score toward 0.
    """
    crit_out = []
    for c in criteria:
        cid = c["id"]
        if c.get("kind") == "mechanical":
            r = mech_results.get(cid, {"passed": None, "output": "",
                                       "error": "criterion produced no battery result"})
            passed = r.get("passed")
            met = None if passed is None else bool(passed)
            src, ev = "mechanical", (r.get("output") or "")[:300]
            err = r.get("error") or (None if met is not None else
                                     "criterion could not be evaluated (see evidence)")
        else:
            r = llm_verdicts.get(cid, {"met": None, "evidence": "",
                                       "error": "criterion was never adjudicated"})
            met = r.get("met")
            met = None if met is None else bool(met)
            src, ev = "llm", r.get("evidence", "")
            err = r.get("error")
        entry = {"id": cid, "criterion": c["description"], "met": met,
                 "source": src, "evidence": ev}
        if met is None:
            entry["error"] = err or "not evaluated"
        crit_out.append(entry)

    total = len(criteria)
    graded = [c for c in crit_out if c["met"] is not None]
    errored = [c for c in crit_out if c["met"] is None]
    met_count = sum(1 for c in graded if c["met"])
    # Score over the criteria that were actually GRADED. A battery that blew up scores
    # None (verdict "error"), never 0.0 — those are different facts about a run.
    score = (met_count / len(graded)) if graded else None
    if agent_exit_code == 124:
        verdict = "timeout"
    elif score is None:
        verdict = "error"
    elif score == 1.0 and not errored:
        verdict = "accepted"
    elif score == 0.0 and not errored:
        verdict = "rejected"
    else:
        verdict = "partial"

    n_mech = sum(1 for c in criteria if c.get("kind") == "mechanical")
    mech_passed = sum(1 for c in crit_out if c["source"] == "mechanical" and c["met"] is True)
    mech_errored = sum(1 for c in crit_out if c["source"] == "mechanical" and c["met"] is None)
    reasoning = f"{met_count}/{len(graded)} graded criteria met"
    if errored:
        reasoning += (f"; {len(errored)}/{total} could not be evaluated "
                      f"({', '.join(c['id'] for c in errored)})")
    return {
        "run_id": run_id,
        "verdict": verdict,
        "score": score,
        "criteria": crit_out,
        "battery": {"total": n_mech, "passed": mech_passed, "errored": mech_errored},
        "criteria_total": total,
        "criteria_graded": len(graded),
        "criteria_errored": len(errored),
        "reasoning": reasoning,
    }


# ── regrade ──────────────────────────────────────────────────────────────────
def regrade(job_dir: Path, run_id: str, task_id: str | None = None,
            in_place: bool = False) -> dict:
    """Grade a FINISHED run by rebuilding its tree from archived artifacts.

    teardown_run.sh removes the worktree unconditionally, so `--grade` can only ever
    run once, at teardown, against a workspace that no longer exists afterwards. That
    makes the harness's own verdict unreproducible: fix a grader defect and the runs
    already collected keep the old answer, and a defect in the grader silently defines
    the truth it is measured against.

    What survives teardown is enough to rebuild the exact tree: the per-run ref
    `refs/atlas/baseline-run/<RUN_ID>` (the POST-overlay baseline commit) plus
    `git.patch` (the diff against it). This checks that ref out and applies the patch.

    Mechanical criteria only. LLM adjudication is deliberately NOT re-run: it is
    non-deterministic and costs an agent call, and the point of this path is a
    reproducible re-derivation. `llm` criteria come back `met: null` with a reason.
    """
    job_dir = Path(job_dir).resolve()
    spec = jobspec.load(job_dir)
    run_dir = (job_dir / "runs" / run_id).resolve()
    if not run_dir.is_dir():
        raise RuntimeError(f"no such run: {run_dir}")

    meta = {}
    try:
        meta = json.loads((run_dir / "run_meta.json").read_text())
    except Exception:  # noqa: BLE001
        pass
    task = _resolve_task(spec, task_id or meta.get("task_id"))
    grader = job_dir / "grader" / task["id"]
    criteria = (_load_criteria_file(grader) or {}).get("criteria", [])
    if not criteria:
        raise RuntimeError(f"no criteria at {grader/'criteria.json'} — nothing to grade against")

    patch = run_dir / "git.patch"
    if not patch.is_file():
        raise RuntimeError(f"no git.patch at {patch} — the solution was never captured")

    # The per-run ref first; run_meta's baseline_sha is the fallback for a run whose
    # ref was pruned. An EMPTY patch is legitimate (the agent changed nothing), an
    # unresolvable baseline is not — fail closed rather than grade the wrong tree.
    bare = job_dir / "repo.git"
    baseline = None
    for cand in (f"refs/atlas/baseline-run/{run_id}", meta.get("baseline_sha") or ""):
        if not cand:
            continue
        r = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "--verify", f"{cand}^{{commit}}"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            baseline = r.stdout.strip()
            break
    if not baseline:
        raise RuntimeError(f"cannot resolve a baseline for {run_id}: neither refs/atlas/baseline-run/{run_id} "
            f"nor run_meta.baseline_sha is reachable in {bare}")

    scratch = job_dir / f".regrade-{run_id}"
    _remove_worktree(job_dir, scratch)
    _flocked_worktree(job_dir, ["add", "--detach", str(scratch), baseline])
    try:
        venv = job_dir / ".venv"
        if venv.exists() and not (scratch / "venv").exists():
            (scratch / "venv").symlink_to(venv)
        _exclude_venv(scratch)
        applied, why = (True, "")
        if patch.stat().st_size > 0:
            applied, why = _apply_patch(scratch, patch.resolve())
        if not applied:
            raise RuntimeError(f"git.patch did not apply onto {baseline[:12]}: {why}")
        mech_results = battery.run(criteria, scratch)
    finally:
        _remove_worktree(job_dir, scratch)

    llm_verdicts = {c["id"]: {"met": None, "evidence": "",
                              "error": "llm criteria are not re-adjudicated by --regrade"}
                    for c in criteria if c.get("kind") != "mechanical"}
    try:
        agent_exit_code = int((run_dir / "agent_exit_code").read_text().strip())
    except Exception:  # noqa: BLE001
        agent_exit_code = 0
    judge = _assemble_judge(run_id, criteria, mech_results, llm_verdicts, agent_exit_code)
    judge["grader"] = {
        "criteria_sha256": _sha256_file(grader / "criteria.json"),
        "criteria_path": str(grader / "criteria.json"),
    }
    judge["regraded"] = {
        "baseline": baseline,
        "patch_bytes": patch.stat().st_size,
        "criteria_sha256": judge["grader"]["criteria_sha256"],
        "llm_adjudicated": False,
    }
    out = run_dir / ("judge.json" if in_place else "judge.regrade.json")
    out.write_text(json.dumps(judge, indent=2))
    score = judge["score"]
    score_s = f"{score:.3f}" if isinstance(score, float) else "None"
    print(f"[regrade] {run_id}: verdict={judge['verdict']} score={score_s} "
          f"(baseline {baseline[:12]}) -> {out}")
    return judge


def _sha256_file(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    p = argparse.ArgumentParser(description="exp-runner judge")
    p.add_argument("--synthesize", action="store_true")
    p.add_argument("--floor-check", action="store_true",
                   help="re-run the floor check + two-sided proof over the criteria "
                        "already on disk (no LLM call)")
    p.add_argument("--grade", action="store_true")
    p.add_argument("--regrade", action="store_true",
                   help="grade a finished run by rebuilding its tree from "
                        "refs/atlas/baseline-run/<RUN_ID> + git.patch")
    p.add_argument("--in-place", action="store_true",
                   help="--regrade: overwrite judge.json instead of writing judge.regrade.json")
    p.add_argument("--job-dir", required=True)
    p.add_argument("--run-id")
    p.add_argument("--task-id", help="which task in the job (default: first)")
    p.add_argument("--agent-exit-code", type=int, default=0)
    p.add_argument("--force", action="store_true", help="re-synthesize even if grader exists")
    a = p.parse_args()
    job_dir = Path(a.job_dir)
    if a.synthesize:
        result = synthesize(job_dir, task_id=a.task_id, force=a.force)
        if not result.get("criteria"):
            print("ERROR: synthesis produced no criteria — refusing to grade against an "
                  "empty battery. Re-run, or check the agent CLI / acceptance text.", file=sys.stderr)
            sys.exit(4)
    elif a.floor_check:
        spec = jobspec.load(job_dir)
        task = _resolve_task(spec, a.task_id)
        grader = job_dir.resolve() / "grader" / task["id"]
        pack = _load_criteria_file(grader) or {}
        crits = _normalize_criteria(pack).get("criteria") or []
        if not crits:
            print(f"ERROR: no criteria at {grader/'criteria.json'} — run --synthesize first.",
                  file=sys.stderr)
            sys.exit(4)
        manifest = floor_check(job_dir, task["id"], crits,
                               sha=spec["repo"]["pinned_sha"],
                               references=task.get("reference_patches") or [])
        print(f"[floor] floor_ok={manifest['floor_ok']} proof_ok={manifest['proof_ok']} "
              f"-> {grader}")
        _report_proof(manifest, tag="floor")
    elif a.grade:
        if not a.run_id:
            p.error("--grade requires --run-id")
        grade(job_dir, a.run_id, task_id=a.task_id, agent_exit_code=a.agent_exit_code)
    elif a.regrade:
        if not a.run_id:
            p.error("--regrade requires --run-id")
        try:
            regrade(job_dir, a.run_id, task_id=a.task_id, in_place=a.in_place)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(5)
    else:
        p.error("specify --synthesize, --floor-check, --grade or --regrade")


if __name__ == "__main__":
    main()
