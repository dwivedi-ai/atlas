#!/usr/bin/env python3
"""
wizard.py — interactive job authoring. Asks the four questions (repo, task,
acceptance, model) plus a couple of optional knobs, then writes a validated
jobs/<id>/job.yaml and prints the job dir to stdout.

Multi-line answers (task / acceptance): type the text, then a single line with
just "." to finish. Defaults are shown in [brackets]; press Enter to accept.
"""
from __future__ import annotations

import sys
from pathlib import Path

import jobspec


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{prompt}{suffix}: ").strip()
        if val:
            return val
        if default is not None:
            return default
        print("  (required)")


def ask_multiline(prompt: str) -> str:
    print(f"{prompt}")
    print('  (enter text; finish with a single line containing only ".")')
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == ".":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def ask_choice(prompt: str, choices: list[str], default: str) -> str:
    opts = "/".join(choices)
    while True:
        val = input(f"{prompt} ({opts}) [{default}]: ").strip().lower()
        if not val:
            return default
        if val in choices:
            return val
        print(f"  choose one of: {opts}")


def main() -> None:
    print("── exp-runner: new job ──────────────────────────────────────────")
    src = ask("Repository (git URL or local path)")
    is_path = Path(src).expanduser().exists() and not src.endswith(".git") or src.startswith(("/", "./", "../", "~"))
    ref = ask("Ref (branch / tag / SHA)", "HEAD")
    print()

    tasks = []
    while True:
        n = len(tasks) + 1
        task = ask_multiline(f"Task #{n} — what should the agent do?")
        if not task:
            print("ERROR: a task is required", file=sys.stderr); sys.exit(1)
        print()
        accept = ask_multiline(f"Acceptance #{n} — what does a correct solution look like? (graded, never shown to the agent)")
        if not accept:
            print("ERROR: an acceptance is required", file=sys.stderr); sys.exit(1)
        tasks.append({"id": f"t{n}", "task": task, "accept": accept})
        print()
        if ask_choice("Add another task to this job?", ["y", "n"], "n") == "n":
            break
        print()

    model = ask_choice("Model", ["codex", "claude"], "codex")
    print()
    print("Context environments — compare the task across levels of project context?")
    print("  E0 bare → E1 +README → E2 +AGENTS → E3 +full scaffold (agent-written for your repo).")
    ladder = ask_choice("Run the E0→E3 context ladder? (else just E0, repo as-is)", ["y", "n"], "n") == "y"
    environments = ["E0", "E1", "E2", "E3"] if ladder else ["E0"]
    reps = ask("Reps (runs per task × environment)", "3" if ladder else "1")
    # A context experiment is about cost across environments — skip the per-run
    # self-analysis by default to keep the (larger) matrix lean.
    if ladder:
        analyze = False
    else:
        analyze = ask_choice("Have the agent write a self-analysis after grading?", ["y", "n"], "y") == "y"
    job_id_default = jobspec.slugify(Path(src.rstrip("/")).name.replace(".git", ""))
    job_id = ask("Job id (folder name)", job_id_default)

    spec = {
        "schema_version": "1",
        "repo": {"ref": ref, "pinned_sha": None},
        "build": {"stack": "auto", "command": None, "detected": None},
        "tasks": tasks,
        "model": model,
        "reps": int(reps),
        "max_seconds": 0,
        "judge_votes": 1,
        "analyze": analyze,
        "environments": environments,
        "job_id": jobspec.slugify(job_id),
    }
    if is_path:
        spec["repo"]["path"] = str(Path(src).expanduser().resolve())
    else:
        spec["repo"]["url"] = src

    try:
        job_dir = jobspec.write(spec)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr); sys.exit(1)

    print()
    print(f"✓ wrote {job_dir}/job.yaml")
    print(job_dir)


if __name__ == "__main__":
    main()
