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
  --grade        Run the battery against one run's workspace, LLM-adjudicate the
                 non-mechanical criteria, write the run's judge.json.

INPUTS   job.yaml (via jobspec), the pinned base SHA, a run's workspace + git.patch.
OUTPUTS  grader/<task>/criteria.json + manifest.json + floor.log; runs/<id>/judge.json.

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


def floor_check(job_dir: Path, task_id: str, criteria: list[dict],
                sha: str | None = None, write: bool = True) -> dict:
    """Run `criteria` against a PRISTINE base worktree and report whether each one
    discriminates. Importable on purpose — a hand-written battery (every WUR detector
    pack) must go through exactly this, and it is also what the mandate-detector gate
    reuses.

    A "new behavior" criterion (`expect_on_base: "fail"`) that PASSES on the untouched
    repo tests nothing: every run scores it 1 regardless of what the agent did. A
    criterion that cannot be EVALUATED on base (passed is None) is `skip`, which matches
    neither expectation and is therefore reported as non-discriminating rather than
    quietly treated as a fail.

    Returns the manifest {floor_ok, criteria:[{id, kind, expect_on_base, floor_passed,
    discriminating, output}], sha, n_mechanical, n_llm} and, with write=True, also
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
        manifest: dict = {"criteria": [], "floor_ok": True, "sha": sha}
        log_lines = [f"Floor-check (pristine base @ {(sha or 'HEAD')[:10]})", ""]
        for c in criteria:
            res = floor.get(c["id"], {"passed": None, "output": ""})
            passed = res.get("passed")
            exp = c.get("expect_on_base", "fail")
            if c.get("kind") != "mechanical":
                status, ok = "n/a (llm)", True
            else:
                actual = "pass" if passed else ("fail" if passed is False else "skip")
                ok = (actual == exp)
                status = f"base={actual} expect={exp} -> {'OK' if ok else 'MISMATCH'}"
                if not ok:
                    manifest["floor_ok"] = False
            manifest["criteria"].append({
                "id": c["id"], "kind": c.get("kind"), "expect_on_base": exp,
                "floor_passed": passed, "discriminating": ok,
                "output": (res.get("output") or "")[:300],
            })
            log_lines.append(f"[{c['id']}] {status}")
            log_lines.append(f"        {c.get('description', '')}")
            out = res.get("output") or ""
            if out:
                log_lines.append(f"        out: {out[:160].splitlines()[0]}")
        manifest["n_mechanical"] = sum(1 for c in criteria if c.get("kind") == "mechanical")
        manifest["n_llm"] = len(criteria) - manifest["n_mechanical"]
        if write:
            grader.mkdir(parents=True, exist_ok=True)
            (grader / "manifest.json").write_text(json.dumps(manifest, indent=2))
            (grader / "floor.log").write_text("\n".join(log_lines) + "\n")
        return manifest
    finally:
        _remove_worktree(job_dir, scratch)


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
            # config_dir: the judge runs under the same isolation as the task (§6.5).
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

    existing = _load_criteria_file(grader) if not force else None
    if existing and isinstance(existing.get("criteria"), list) and existing["criteria"]:
        print(f"[skip] criteria already present, skipping LLM synthesis: {grader/'criteria.json'}")
        criteria = _normalize_criteria(existing)
    else:
        criteria = _normalize_criteria(_synthesize_criteria(job_dir, spec, task, sha))
    (grader / "criteria.json").write_text(json.dumps(criteria, indent=2))

    if not criteria["criteria"]:
        print("[synth] no criteria — skipping floor-check", file=sys.stderr)
        return criteria

    manifest = floor_check(job_dir, task_id, criteria["criteria"], sha=sha)
    n_mech, n_llm = manifest["n_mechanical"], manifest["n_llm"]
    print(f"[synth] {len(criteria['criteria'])} criteria ({n_mech} mechanical, {n_llm} llm); "
          f"floor_ok={manifest['floor_ok']} -> {grader}")
    if not manifest["floor_ok"]:
        bad = [c["id"] for c in manifest["criteria"] if not c["discriminating"]]
        print(f"[synth] WARNING: {bad} do not discriminate on the base tree — "
              f"they score the same for every run and measure nothing.", file=sys.stderr)
    return criteria


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
    #    met is TRI-STATE: True / False / None. None means "could not be evaluated" — an
    #    eval error in the pass_condition, a timeout, a missing command, an adjudicator
    #    that did not answer. Coercing it to False (the old `bool(r["passed"])`) made a
    #    broken battery indistinguishable from a failed solution and silently dragged the
    #    score toward 0.
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
    judge = {
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
    out.write_text(json.dumps(judge, indent=2))
    score_s = f"{score:.3f}" if isinstance(score, float) else "None"
    print(f"[grade] {run_id}: verdict={verdict} score={score_s} "
          f"({met_count}/{len(graded)} graded, {len(errored)} unevaluable, {total} total)")
    return judge


def main() -> None:
    p = argparse.ArgumentParser(description="exp-runner judge")
    p.add_argument("--synthesize", action="store_true")
    p.add_argument("--grade", action="store_true")
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
    elif a.grade:
        if not a.run_id:
            p.error("--grade requires --run-id")
        grade(job_dir, a.run_id, task_id=a.task_id, agent_exit_code=a.agent_exit_code)
    else:
        p.error("specify --synthesize or --grade")


if __name__ == "__main__":
    main()
