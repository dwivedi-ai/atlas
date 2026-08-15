#!/usr/bin/env python3
"""
wizard.py — interactive job authoring. Asks for repo, task(s), acceptance, model,
environments, then writes a validated jobs/<id>/job.yaml.

IMPORTANT: run.sh captures this program's STDOUT via `$(...)` to learn the job dir,
so every user-facing prompt/message goes to STDERR and ONLY the final job-dir path
is printed to STDOUT. (If prompts went to stdout they'd be swallowed by the command
substitution and the wizard would look hung.)

Multi-line answers (task / acceptance): type the text, then a single line with just
"." to finish. Defaults are shown in [brackets]; press Enter to accept.
"""
from __future__ import annotations

import sys
from pathlib import Path

import jobspec


def _p(msg: str = "") -> None:
    print(msg, file=sys.stderr, flush=True)


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        sys.stderr.write(f"{prompt}{suffix}: "); sys.stderr.flush()
        try:
            val = input().strip()
        except EOFError:
            if default is not None:
                return default
            _p(""); sys.exit(1)
        if val:
            return val
        if default is not None:
            return default
        _p("  (required)")


def ask_multiline(prompt: str) -> str:
    _p(prompt)
    _p('  (enter text; finish with a single line containing only ".")')
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
        sys.stderr.write(f"{prompt} ({opts}) [{default}]: "); sys.stderr.flush()
        try:
            val = input().strip().lower()
        except EOFError:
            return default
        if not val:
            return default
        if val in choices:
            return val
        _p(f"  choose one of: {opts}")


USAGE = """\
wizard.py — interactive job authoring for exp-runner (ladder mode).

Asks for the repo, task(s), acceptance, model, environments and reps, then writes a
validated jobs/<id>/job.yaml and prints its path. Normally reached as `./run.sh` with
no source flags, which runs the job straight afterwards.

./run.sh                    author interactively, then brew + run
  python3 lib/wizard.py       author only; prints the job dir

Multi-line answers (task / acceptance): type the text, then a single line with just
"." to finish. Defaults are in [brackets]; press Enter to accept.

Every prompt goes to STDERR and only the job dir to STDOUT, so `JD=$(...)` works.

This wizard authors LADDER jobs. A `wur` job needs a fact registry, arm ids and a
matrix seed, which are not promptable — start from templates/job.wur.example.yaml,
or `run.sh --experiment wur` with flags.
"""


def main() -> None:
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(USAGE)
        return
    _p("── exp-runner: new job ──────────────────────────────────────────")
    src = ask("Repository (git URL or local path)")
    is_path = (Path(src).expanduser().exists() and not src.endswith(".git")) or src.startswith(("/", "./", "../", "~"))
    ref = ask("Ref (branch / tag / SHA)", "HEAD")
    _p()

    tasks = []
    while True:
        n = len(tasks) + 1
        task = ask_multiline(f"Task #{n} — what should the agent do?")
        if not task:
            _p("ERROR: a task is required"); sys.exit(1)
        _p()
        accept = ask_multiline(f"Acceptance #{n} — what does a correct solution look like? (graded, never shown to the agent)")
        if not accept:
            _p("ERROR: an acceptance is required"); sys.exit(1)
        tasks.append({"id": f"t{n}", "task": task, "accept": accept})
        _p()
        if ask_choice("Add another task to this job?", ["y", "n"], "n") == "n":
            break
        _p()

    # Default `claude`, not `codex`: it is what install.sh checks for as DEFAULT_BACKEND
    # and what lib/agent.py's registry resolves to, so hitting Enter used to author a
    # job that could not run on the machine the installer had just declared ready.
    model = ask_choice("Model", ["claude", "codex", "gemini", "agy"], "claude")
    _p()
    _p("Context environments — run the task across levels of project context?")
    _p("  E0 bare → E1 +README → E2 +AGENTS → E3 +PROJECT → E4 full-XO → E5 +memory → E6 DOX.")
    _p("  The runner sets up each environment for YOUR repo (agent-written docs), then runs the task in all of them.")
    all7 = ask_choice("Run across all 7 environments E0..E6? (else just E0, repo as-is)", ["y", "n"], "y") == "y"
    environments = ["E0", "E1", "E2", "E3", "E4", "E5", "E6"] if all7 else ["E0"]
    reps = ask("Reps (runs per task × environment)", "1")
    _p("  (self-analysis = one extra agent call per run that writes a written reflection;")
    _p("   turn it off to keep a large 7-env × many-rep matrix cheap.)")
    analyze = ask_choice("Have the agent write a self-analysis (text) after each run?", ["y", "n"], "y") == "y"
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
        _p(f"ERROR: {e}"); sys.exit(1)

    _p("")
    _p(f"✓ wrote {job_dir}/job.yaml")
    print(job_dir)   # <-- the ONLY thing on stdout (captured by run.sh)


if __name__ == "__main__":
    main()
