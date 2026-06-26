#!/usr/bin/env python3
"""
judge.py — the NL-acceptance grader. Two phases:

  --synthesize   Author a mechanical test battery from the job's plain-English
                 `accept` text (the agent only ever sees the acceptance, never any
                 solution), then FLOOR-CHECK it against the pristine base worktree
                 (new-behavior criteria must fail on base). Writes:
                   grader/criteria.json   the decomposed, checkable criteria
                   grader/manifest.json   floor-check results per criterion
                   grader/floor.log       human-readable floor-check log

  --grade        Run the battery against one run's workspace, LLM-adjudicate any
                 non-mechanical ("llm") criteria, and write the run's judge.json
                 (verdict + score + per-criterion evidence).

The judge uses the SAME model the user chose for the job (one logged-in CLI), via
lib/agent.py. Synthesis/adjudication are separate invocations from the task run.
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


def synthesize(job_dir: Path, task_id: str | None = None, force: bool = False) -> dict:
    job_dir = Path(job_dir).resolve()
    spec = jobspec.load(job_dir)
    task = _resolve_task(spec, task_id)
    task_id = task["id"]
    grader = job_dir / "grader" / task_id
    if (grader / "criteria.json").exists() and not force:
        print(f"[skip] grader already synthesized: {grader/'criteria.json'}")
        return json.loads((grader / "criteria.json").read_text())

    sha = spec["repo"]["pinned_sha"]
    model = spec["model"]
    max_seconds = int(spec.get("max_seconds") or 0)
    grader.mkdir(parents=True, exist_ok=True)

    scratch = job_dir / f".synth-src-{task_id}"
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
            text, code = agent.run_text(model, prompt, scratch, max_seconds)
            data = _load_criteria_file(scratch) or _extract_json(text)
            if data and isinstance(data.get("criteria"), list) and data["criteria"]:
                criteria = data
                break
            print(f"[synth] attempt {attempt} produced no valid criteria"
                  + (" — retrying" if attempt < attempts else ""), file=sys.stderr)
        if not criteria:
            criteria = {"criteria": []}

        # normalize/validate fields
        for i, c in enumerate(criteria["criteria"], 1):
            c.setdefault("id", f"C{i}")
            c.setdefault("kind", "mechanical" if c.get("command") else "llm")
            c.setdefault("expect_on_base", "fail")
            c.setdefault("description", c.get("criterion", c["id"]))
        (grader / "criteria.json").write_text(json.dumps(criteria, indent=2))

        # ── floor-check against the PRISTINE base worktree ──
        # The synthesis agent authored criteria.json (and possibly scratch files)
        # inside `scratch`. Restore it to an exact base checkout first — otherwise
        # "no unrelated files" style invariants see the synthesis litter and a
        # new-behavior check could accidentally pass. Keep only the venv symlink.
        subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=str(scratch),
                       capture_output=True, text=True)
        subprocess.run(["git", "clean", "-fdx", "-e", "venv"], cwd=str(scratch),
                       capture_output=True, text=True)
        floor = battery.run(criteria["criteria"], scratch)
        manifest = {"criteria": [], "floor_ok": True}
        log_lines = [f"Floor-check (pristine base @ {sha[:10]})", ""]
        for c in criteria["criteria"]:
            res = floor.get(c["id"], {"passed": None, "output": ""})
            passed = res["passed"]
            exp = c.get("expect_on_base", "fail")
            if c.get("kind") != "mechanical":
                status = "n/a (llm)"
                ok = True
            else:
                actual = "pass" if passed else ("fail" if passed is False else "skip")
                ok = (actual == exp)
                status = f"base={actual} expect={exp} -> {'OK' if ok else 'MISMATCH'}"
                if not ok:
                    manifest["floor_ok"] = False
            manifest["criteria"].append({
                "id": c["id"], "kind": c.get("kind"), "expect_on_base": exp,
                "floor_passed": passed, "discriminating": ok,
            })
            log_lines.append(f"[{c['id']}] {status}")
            log_lines.append(f"        {c['description']}")
            if res.get("output"):
                log_lines.append(f"        out: {res['output'][:160].splitlines()[0] if res['output'] else ''}")
        (grader / "manifest.json").write_text(json.dumps(manifest, indent=2))
        (grader / "floor.log").write_text("\n".join(log_lines) + "\n")

        n_mech = sum(1 for c in criteria["criteria"] if c.get("kind") == "mechanical")
        n_llm = len(criteria["criteria"]) - n_mech
        print(f"[synth] {len(criteria['criteria'])} criteria ({n_mech} mechanical, {n_llm} llm); "
              f"floor_ok={manifest['floor_ok']} -> {grader}")
        return criteria
    finally:
        _remove_worktree(job_dir, scratch)


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
        for v in range(votes):
            with tempfile.TemporaryDirectory() as tmp:
                text, _ = agent.run_text(spec["model"], _adjudication_prompt(diff, llm_criteria),
                                         tmp, int(spec.get("max_seconds") or 0))
            data = _extract_json(text) or {}
            for item in data.get("verdicts", []):
                cid = item.get("id")
                if cid in tally:
                    tally[cid].append(bool(item.get("met")))
                    evidence[cid] = item.get("evidence", "")
        for cid, mets in tally.items():
            met = (sum(mets) > len(mets) / 2) if mets else False
            llm_verdicts[cid] = {"met": met, "evidence": evidence.get(cid, "(no response)")}

    # 3) assemble per-criterion verdicts
    crit_out = []
    met_count = 0
    for c in criteria:
        cid = c["id"]
        if c.get("kind") == "mechanical":
            r = mech_results.get(cid, {"passed": None, "output": ""})
            met = bool(r["passed"])
            src, ev = "mechanical", (r["output"] or "")[:300]
        else:
            r = llm_verdicts.get(cid, {"met": False, "evidence": ""})
            met = bool(r["met"])
            src, ev = "llm", r["evidence"]
        met_count += 1 if met else 0
        crit_out.append({"id": cid, "criterion": c["description"], "met": met,
                         "source": src, "evidence": ev})

    total = len(criteria)
    score = met_count / total if total else None
    if agent_exit_code == 124:
        verdict = "timeout"
    elif score == 1.0:
        verdict = "accepted"
    elif score == 0.0:
        verdict = "rejected"
    else:
        verdict = "partial"

    n_mech = sum(1 for c in criteria if c.get("kind") == "mechanical")
    mech_passed = sum(1 for c in crit_out if c["source"] == "mechanical" and c["met"])
    judge = {
        "run_id": run_id,
        "verdict": verdict,
        "score": score,
        "criteria": crit_out,
        "battery": {"total": n_mech, "passed": mech_passed},
        "reasoning": f"{met_count}/{total} criteria met",
    }
    out.write_text(json.dumps(judge, indent=2))
    print(f"[grade] {run_id}: verdict={verdict} score={score} ({met_count}/{total})")
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
