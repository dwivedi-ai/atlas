#!/usr/bin/env python3
"""
battery.py — run a synthesized mechanical test battery against a workspace.

Ported from experiments/l4/harness/extract/grade_run.py's `run_ac`: each
mechanical criterion is a shell command + a `pass_condition` expression evaluated
over (exit_code, stdout). The workspace's ./venv/bin is prepended to PATH so
`pytest`/`python` resolve to the hermetic env.

A criterion (from grader/criteria.json):
  {"id": "C1", "description": "...", "kind": "mechanical",
   "command": "<shell>", "pass_condition": "int(stdout.strip()) >= 1",
   "expect_on_base": "fail"}

Criteria with kind != "mechanical" (e.g. "llm") are skipped here (the judge
adjudicates those separately) and reported as passed=None.

API:
  run(criteria, workspace, timeout=60) -> {id: {"passed": bool|None, "output": str}}
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

AC_TIMEOUT_DEFAULT = 60


def run_one(crit: dict, workspace: Path, timeout: int = AC_TIMEOUT_DEFAULT) -> tuple[bool | None, str]:
    if crit.get("kind", "mechanical") != "mechanical":
        return None, "[llm-adjudicated criterion — not run by the battery]"
    command = crit.get("command")
    pass_condition = crit.get("pass_condition")
    if not command or not pass_condition:
        return None, "[criterion has no command/pass_condition]"

    # Resolve to absolute so the venv/bin PATH entry is correct regardless of the
    # caller's cwd (a relative workspace would resolve PATH against the subprocess
    # cwd and break `pytest`/`python` lookups).
    workspace = Path(workspace).resolve()
    venv_bin = str(workspace / "venv" / "bin")
    # PYTHONDONTWRITEBYTECODE: running the battery's own pytest/python must not
    # litter the workspace with __pycache__/*.pyc — otherwise a "no unrelated
    # files changed" criterion would flag the grader's own byte-cache.
    env = {
        **os.environ,
        "PATH": f"{venv_bin}:{os.environ.get('PATH', '')}",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        result = subprocess.run(
            command, shell=True, cwd=str(workspace),
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"[timeout after {timeout}s]"
    except Exception as e:  # noqa: BLE001
        return False, f"[error: {e}]"

    stdout = (result.stdout + result.stderr).strip()
    exit_code = result.returncode
    # LLM-authored pass_conditions use ordinary Python expressions, so expose a
    # generous but SAFE builtin set (no import / open / eval / file access). This
    # is what lets conditions like `all(... )`, `set(...)`, comprehensions, and
    # `'x' in stdout` evaluate instead of silently falling back to exit_code==0.
    safe_builtins = {
        "all": all, "any": any, "len": len, "sum": sum, "min": min, "max": max,
        "sorted": sorted, "set": set, "list": list, "dict": dict, "tuple": tuple,
        "map": map, "filter": filter, "range": range, "enumerate": enumerate,
        "zip": zip, "abs": abs, "round": round, "int": int, "str": str,
        "float": float, "bool": bool, "repr": repr, "isinstance": isinstance,
    }
    local_ns = {"exit_code": exit_code, "stdout": stdout, **safe_builtins}
    try:
        passed = bool(eval(pass_condition, {"__builtins__": {}}, local_ns))  # noqa: S307
    except Exception as e:  # noqa: BLE001
        passed = exit_code == 0
        stdout += f"\n[pass_condition eval error: {e}]"
    return passed, stdout[:1000]


def run(criteria: list[dict], workspace: Path, timeout: int = AC_TIMEOUT_DEFAULT) -> dict:
    workspace = Path(workspace)
    results: dict[str, dict] = {}
    for crit in criteria:
        cid = crit["id"]
        passed, output = run_one(crit, workspace, timeout)
        results[cid] = {"passed": passed, "output": output}
    return results


if __name__ == "__main__":
    import argparse
    import json
    p = argparse.ArgumentParser(description="Run a criteria battery against a workspace.")
    p.add_argument("--criteria", required=True, help="path to criteria.json")
    p.add_argument("--workspace", required=True)
    a = p.parse_args()
    crits = json.loads(Path(a.criteria).read_text())["criteria"]
    print(json.dumps(run(crits, Path(a.workspace)), indent=2))
