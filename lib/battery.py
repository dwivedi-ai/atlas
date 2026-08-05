#!/usr/bin/env python3
"""
battery.py — run a synthesized mechanical test battery against a workspace.

RESPONSIBILITY
  Execute mechanical criteria — a shell command plus a `pass_condition` Python
  expression over (exit_code, stdout) — against one workspace, and report each
  verdict WITHOUT ever inventing one. This is also the execution engine for the
  WUR `used` detectors (IMPLEMENTATION.md §6.4): a fact detector IS a mechanical
  criterion, so no second engine exists.

INPUTS
  criteria  list of criterion dicts (grader/criteria.json, or synthesized by
            lib/wur/detect_use.py from the closed detector registry)
  workspace path to the tree to run them in; its ./venv/bin is prepended to PATH
            so `pytest`/`python` resolve to the hermetic env

OUTPUTS
  {id: {"passed": bool|None, "output": str, "exit_code": int|None,
        "command": str|None, "status": str, "error": str|None,
        "timed_out": bool, "duration_s": float, "kind": str}}

  `passed is None` means UNDECIDED, never "failed": the criterion was skipped
  (kind != mechanical, or no command), or the pass_condition itself blew up.

A criterion:
  {"id": "C1", "description": "...", "kind": "mechanical",
   "command": "<shell>", "pass_condition": "int(stdout.strip()) >= 1",
   "expect_on_base": "fail",
   "timeout": 300,          # optional per-criterion override of AC_TIMEOUT_DEFAULT
   "output_limit": 20000}   # optional per-criterion stdout cap (default 1000)

FIXES vs. the pre-WUR version (IMPLEMENTATION.md §8.1)
  1. A `pass_condition` that raises used to fall back to `exit_code == 0` — a
     VERIFIED SILENT FALSE PASS: a criterion whose expression had a typo scored
     PASS for every run whose command happened to exit 0. It now returns
     passed=None with status="error", which propagates as "undecided" and makes
     the floor-check mismatch loudly instead of publishing a wrong verdict.
  2. AC_TIMEOUT_DEFAULT 60 -> 120, with a per-criterion `timeout` override, so a
     real test suite is not scored as a failure for being slow.
  3. `exit_code` and `command` are returned, so a caller (detect_use.py) can
     read a detector's structured result off stdout and its fired/eligible
     verdict off the exit code.

  A TIMEOUT still returns passed=False (status="timeout"), deliberately: the
  existing ladder floor-check treats a timing-out new-behaviour criterion as
  "fails on base", which is correct, and turning it into None would flip every
  such floor-check to MISMATCH. `timed_out` is on the row for callers that need
  the distinction.
"""
from __future__ import annotations

import json as _json
import os
import re as _re
import subprocess
import time
from pathlib import Path

AC_TIMEOUT_DEFAULT = 120
AC_OUTPUT_LIMIT_DEFAULT = 1000

# status values, in one place so callers can compare against a name
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_SKIPPED = "skipped"

# LLM-authored pass_conditions use ordinary Python expressions, so expose a
# generous but SAFE builtin set (no import / open / eval / file access). This is
# what lets conditions like `all(...)`, `set(...)`, comprehensions and
# `'x' in stdout` evaluate instead of raising.
SAFE_BUILTINS = {
    "all": all, "any": any, "len": len, "sum": sum, "min": min, "max": max,
    "sorted": sorted, "set": set, "list": list, "dict": dict, "tuple": tuple,
    "map": map, "filter": filter, "range": range, "enumerate": enumerate,
    "zip": zip, "abs": abs, "round": round, "int": int, "str": str,
    "float": float, "bool": bool, "repr": repr, "isinstance": isinstance,
    # `re` and `json` are the two modules an output-parsing condition actually
    # needs, and a model authoring criteria reaches for them constantly — the
    # first synthesized battery in this repo produced
    # `int(__import__('re').search(r'(\d+) passed', stdout).group(1)) >= 156`,
    # which raised (no __import__ under an empty __builtins__), scored the
    # criterion `None`, and made the whole floor-check report floor_ok=False.
    # Both are pure parsers: no filesystem, no network, no process control, and
    # they cannot reach __import__/open/eval, which stay absent.
    "re": _re, "json": _json,
}


def _skipped(reason: str) -> dict:
    return {
        "passed": None, "output": reason, "exit_code": None, "command": None,
        "status": STATUS_SKIPPED, "error": None, "timed_out": False,
        "duration_s": 0.0, "kind": None,
    }


def run_one_full(
    crit: dict, workspace: Path, timeout: int = AC_TIMEOUT_DEFAULT
) -> dict:
    """Execute one criterion and return the full result row.

    `run_one` is the historical 2-tuple wrapper around this; nothing about the
    execution model changed, only what is reported back.
    """
    kind = crit.get("kind", "mechanical")
    if kind != "mechanical":
        row = _skipped("[llm-adjudicated criterion — not run by the battery]")
        row["kind"] = kind
        return row
    command = crit.get("command")
    pass_condition = crit.get("pass_condition")
    if not command or not pass_condition:
        row = _skipped("[criterion has no command/pass_condition]")
        row["kind"] = kind
        row["command"] = command
        return row

    # Per-criterion overrides. A detector criterion needs a bigger stdout budget
    # than a `wc -l`; a real pytest run needs more than 120 s.
    try:
        eff_timeout = int(crit.get("timeout") or timeout)
    except (TypeError, ValueError):
        eff_timeout = timeout
    eff_timeout = max(1, eff_timeout)
    try:
        limit = int(crit.get("output_limit") or AC_OUTPUT_LIMIT_DEFAULT)
    except (TypeError, ValueError):
        limit = AC_OUTPUT_LIMIT_DEFAULT

    # Resolve to absolute so the venv/bin PATH entry is correct regardless of the
    # caller's cwd (a relative workspace would resolve PATH against the subprocess
    # cwd and break `pytest`/`python` lookups).
    workspace = Path(workspace).resolve()
    venv_bin = str(workspace / "venv" / "bin")
    # PYTHONDONTWRITEBYTECODE: running the battery's own pytest/python must not
    # litter the workspace with __pycache__/*.pyc — otherwise a "no unrelated
    # files changed" criterion would flag the grader's own byte-cache. It also
    # keeps a WUR detector run, which happens BEFORE grading touches the tree,
    # from writing anything into the workspace at all.
    env = {
        **os.environ,
        "PATH": f"{venv_bin}:{os.environ.get('PATH', '')}",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    row = {
        "passed": None, "output": "", "exit_code": None, "command": command,
        "status": STATUS_OK, "error": None, "timed_out": False,
        "duration_s": 0.0, "kind": kind,
    }
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            command, shell=True, cwd=str(workspace),
            capture_output=True, text=True, timeout=eff_timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        row.update(
            passed=False, output=f"[timeout after {eff_timeout}s]",
            status=STATUS_TIMEOUT, timed_out=True,
            error=f"timeout after {eff_timeout}s",
            duration_s=round(time.monotonic() - t0, 3),
        )
        return row
    except Exception as e:  # noqa: BLE001
        # The command could not be launched at all. That is a harness fault, not
        # a verdict about the workspace — do not manufacture one.
        row.update(
            passed=None, output=f"[error: {e}]", status=STATUS_ERROR,
            error=f"{type(e).__name__}: {e}",
            duration_s=round(time.monotonic() - t0, 3),
        )
        return row

    stdout = (result.stdout + result.stderr).strip()
    exit_code = result.returncode
    local_ns = {"exit_code": exit_code, "stdout": stdout, **SAFE_BUILTINS}
    try:
        passed = bool(eval(pass_condition, {"__builtins__": {}}, local_ns))  # noqa: S307
    except Exception as e:  # noqa: BLE001
        # DO NOT fall back to `exit_code == 0`. That fallback was a verified
        # silent false PASS: a broken expression scored every exit-0 run as a
        # pass, with the only trace a line appended to output nobody reads.
        row.update(
            passed=None,
            output=(stdout + f"\n[pass_condition eval error: {e}]")[:limit],
            exit_code=exit_code, status=STATUS_ERROR,
            error=f"pass_condition eval error: {type(e).__name__}: {e}",
            duration_s=round(time.monotonic() - t0, 3),
        )
        return row
    row.update(
        passed=passed, output=stdout[:limit], exit_code=exit_code,
        duration_s=round(time.monotonic() - t0, 3),
    )
    return row


def run_one(
    crit: dict, workspace: Path, timeout: int = AC_TIMEOUT_DEFAULT
) -> tuple[bool | None, str]:
    """(passed, output) — the historical shape, preserved for existing callers."""
    row = run_one_full(crit, workspace, timeout)
    return row["passed"], row["output"]


def run(criteria: list[dict], workspace: Path, timeout: int = AC_TIMEOUT_DEFAULT) -> dict:
    """{criterion id: result row}. Rows keep `passed`/`output` first for callers
    written against the pre-WUR shape; the extra keys are additive."""
    workspace = Path(workspace)
    results: dict[str, dict] = {}
    for crit in criteria:
        cid = crit["id"]
        results[cid] = run_one_full(crit, workspace, timeout)
    return results


if __name__ == "__main__":
    import argparse
    import json
    p = argparse.ArgumentParser(description="Run a criteria battery against a workspace.")
    p.add_argument("--criteria", required=True, help="path to criteria.json")
    p.add_argument("--workspace", required=True)
    p.add_argument("--timeout", type=int, default=AC_TIMEOUT_DEFAULT)
    a = p.parse_args()
    crits = json.loads(Path(a.criteria).read_text())["criteria"]
    print(json.dumps(run(crits, Path(a.workspace), a.timeout), indent=2))
