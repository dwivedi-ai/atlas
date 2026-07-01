#!/usr/bin/env python3
"""
jobspec.py — author, validate, read, and mutate exp-runner job specs.

A "job" is one repo + one task + one natural-language acceptance description +
one model. It lives at jobs/<job_id>/job.yaml. This module is the single source
of truth for that file; both the wizard and run.sh's flag mode go through it, and
the shell scripts read individual fields via the `field` subcommand.

Subcommands
  create  --repo URL | --path DIR [--ref R] [--task .. | --task-file F]
          [--accept .. | --accept-file F] [--model M] [--reps N]
          [--max-seconds S] [--build-stack S] [--build-cmd C] [--job-id ID]
              -> creates jobs/<id>/job.yaml, prints the job dir to stdout
  field   <job_dir> <dotted.key>     -> prints one field ("" if null/missing)
  set     <job_dir> <dotted.key> <value>   -> sets a field (value "null" -> null)
  validate <job_dir>                 -> exits non-zero with a message on failure
  show    <job_dir>                  -> prints the parsed spec as JSON
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCHEMA_PATH = ROOT / "schemas" / "job.schema.json"
JOBS_DIR = ROOT / "jobs"

DEFAULTS = {
    "schema_version": "1",
    "build": {"stack": "auto", "command": None, "detected": None},
    "reps": 1,
    "max_seconds": 0,
    "judge_votes": 1,
    "analyze": True,
    "environments": ["E0", "E1", "E2", "E3", "E4", "E5", "E6"],
}


# ── slug / id helpers ────────────────────────────────────────────────────────
def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return (s or "job")[:64].rstrip("-")


def default_job_id(spec: dict) -> str:
    repo = spec.get("repo", {})
    src = repo.get("url") or repo.get("path") or "job"
    base = Path(src.rstrip("/")).name
    base = re.sub(r"\.git$", "", base)
    return slugify(base or "job")


# ── load / validate / write ──────────────────────────────────────────────────
def _deep_fill(spec: dict) -> dict:
    for k, v in DEFAULTS.items():
        if isinstance(v, dict):
            node = spec.setdefault(k, {})
            for kk, vv in v.items():
                node.setdefault(kk, vv)
        else:
            spec.setdefault(k, v)
    spec.setdefault("repo", {}).setdefault("ref", "HEAD")
    spec["repo"].setdefault("pinned_sha", None)
    _normalize_tasks(spec)
    return spec


def _normalize_tasks(spec: dict) -> None:
    """Canonicalize to spec['tasks'] = [{id, task, accept}, ...].

    Single-task jobs (top-level `task` + `accept`) become one task with id 't1',
    so everything downstream iterates a uniform list.
    """
    tasks = spec.get("tasks")
    if not tasks:
        if (spec.get("task") or "").strip() and (spec.get("accept") or "").strip():
            tasks = [{"id": "t1", "task": spec["task"], "accept": spec["accept"]}]
        else:
            tasks = []
    for i, t in enumerate(tasks, 1):
        t.setdefault("id", f"t{i}")
        t["id"] = slugify(str(t["id"]))
    spec["tasks"] = tasks
    # keep top-level task/accept mirroring the first task (back-compat for any
    # single-task reader); harmless for multi-task.
    if tasks:
        spec.setdefault("task", tasks[0]["task"])
        spec.setdefault("accept", tasks[0]["accept"])


def validate(spec: dict) -> None:
    """Raise ValueError with a human message if the spec is invalid."""
    try:
        import jsonschema  # type: ignore

        schema = json.loads(SCHEMA_PATH.read_text())
        jsonschema.validate(spec, schema)
        return
    except ImportError:
        pass  # fall back to the manual checks below
    except Exception as e:  # jsonschema.ValidationError and friends
        raise ValueError(str(getattr(e, "message", e)))

    # ── manual fallback (jsonschema unavailable) ──
    if spec.get("schema_version") != "1":
        raise ValueError("schema_version must be \"1\"")
    jid = spec.get("job_id", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", jid):
        raise ValueError(f"invalid job_id: {jid!r}")
    repo = spec.get("repo") or {}
    if not (repo.get("url") or repo.get("path")):
        raise ValueError("repo must have a url or a path")
    tasks = spec.get("tasks") or []
    if not tasks:
        raise ValueError("a job needs at least one task (task+accept, or a tasks list)")
    ids = [t.get("id") for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate task ids: {ids}")
    for t in tasks:
        if not (t.get("task") or "").strip() or not (t.get("accept") or "").strip():
            raise ValueError(f"task {t.get('id')!r} needs non-empty task and accept text")
    if spec.get("model") not in ("codex", "claude", "claude-sonnet-4-6"):
        raise ValueError(f"model must be codex|claude, got {spec.get('model')!r}")
    if int(spec.get("reps", 1)) < 1:
        raise ValueError("reps must be >= 1")
    envs = spec.get("environments") or ["E0"]
    try:
        from ladder import VALID_ENVS
    except ImportError:
        VALID_ENVS = ["E0", "E1", "E2", "E3"]
    bad = [e for e in envs if e not in VALID_ENVS]
    if bad:
        raise ValueError(f"unknown environment(s): {bad} (valid: {VALID_ENVS})")


def load(job_dir: Path) -> dict:
    spec = yaml.safe_load((Path(job_dir) / "job.yaml").read_text())
    return _deep_fill(spec or {})


def write(spec: dict, job_dir: Path | None = None) -> Path:
    spec = _deep_fill(dict(spec))
    spec.setdefault("job_id", default_job_id(spec))
    validate(spec)
    job_dir = Path(job_dir) if job_dir else (JOBS_DIR / spec["job_id"])
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.yaml").write_text(
        yaml.safe_dump(spec, sort_keys=False, default_flow_style=False, allow_unicode=True)
    )
    return job_dir


# ── dotted-key get/set (for the shell scripts) ───────────────────────────────
def _get(spec: dict, dotted: str):
    cur = spec
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set(spec: dict, dotted: str, value):
    parts = dotted.split(".")
    cur = spec
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


# ── CLI ──────────────────────────────────────────────────────────────────────
def _read_or_inline(value: str | None, file_path: str | None) -> str | None:
    if file_path:
        return Path(file_path).read_text()
    return value


def cmd_create(args) -> None:
    spec = {
        "schema_version": "1",
        "repo": {"ref": args.ref or "HEAD", "pinned_sha": None},
        "build": {"stack": args.build_stack or "auto", "command": args.build_cmd, "detected": None},
        "task": (_read_or_inline(args.task, args.task_file) or "").strip(),
        "accept": (_read_or_inline(args.accept, args.accept_file) or "").strip(),
        "model": args.model or "codex",
        "reps": int(args.reps),
        "max_seconds": int(args.max_seconds),
        "judge_votes": int(args.judge_votes),
        "analyze": (not args.no_analyze),
    }
    # Environments: default = all 7 (E0..E6, like l1-doxed); --envs overrides.
    ALL7 = ["E0", "E1", "E2", "E3", "E4", "E5", "E6"]
    if args.envs:
        spec["environments"] = [e.strip() for e in args.envs.split(",") if e.strip()]
    else:
        spec["environments"] = ALL7
    # Self-analysis is ON by default for every job (analyze = not --no-analyze).
    # It adds one agent call per cell; pass --no-analyze to keep a big matrix lean.
    if args.tasks_file:
        loaded = yaml.safe_load(Path(args.tasks_file).read_text())
        if not isinstance(loaded, list) or not loaded:
            raise ValueError("--tasks-file must contain a non-empty YAML/JSON list of {task, accept}")
        spec["tasks"] = [
            {"id": t.get("id", f"t{i}"), "task": (t.get("task") or "").strip(),
             "accept": (t.get("accept") or "").strip()}
            for i, t in enumerate(loaded, 1)
        ]
        spec.pop("task", None)
        spec.pop("accept", None)
    if args.repo:
        spec["repo"]["url"] = args.repo
    if args.path:
        spec["repo"]["path"] = str(Path(args.path).expanduser().resolve())
    if args.job_id:
        spec["job_id"] = slugify(args.job_id)
    job_dir = write(spec)
    print(job_dir)


def cmd_field(args) -> None:
    spec = load(Path(args.job_dir))
    val = _get(spec, args.key)
    print("" if val is None else val)


def _find_task(spec: dict, task_id: str) -> dict:
    for t in spec.get("tasks", []):
        if t["id"] == task_id:
            return t
    raise ValueError(f"no such task id: {task_id}")


def cmd_tasks(args) -> None:
    spec = load(Path(args.job_dir))
    for t in spec.get("tasks", []):
        print(t["id"])


def cmd_envs(args) -> None:
    spec = load(Path(args.job_dir))
    for e in spec.get("environments", ["E0"]):
        print(e)


def cmd_task_text(args) -> None:
    spec = load(Path(args.job_dir))
    print(_find_task(spec, args.task_id)["task"].strip())


def cmd_accept_text(args) -> None:
    spec = load(Path(args.job_dir))
    print(_find_task(spec, args.task_id)["accept"].strip())


def cmd_set(args) -> None:
    job_dir = Path(args.job_dir)
    spec = load(job_dir)
    value = None if args.value == "null" else args.value
    _set(spec, args.key, value)
    write(spec, job_dir)


def cmd_validate(args) -> None:
    validate(load(Path(args.job_dir)))
    print("ok")


def cmd_show(args) -> None:
    print(json.dumps(load(Path(args.job_dir)), indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description="exp-runner job spec helper")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("--repo")
    c.add_argument("--path")
    c.add_argument("--ref")
    c.add_argument("--task")
    c.add_argument("--task-file")
    c.add_argument("--accept")
    c.add_argument("--accept-file")
    c.add_argument("--tasks-file", help="YAML/JSON list of {id?, task, accept} for multi-task jobs")
    c.add_argument("--model")
    c.add_argument("--reps", default=1)
    c.add_argument("--max-seconds", default=0)
    c.add_argument("--judge-votes", default=1)
    c.add_argument("--no-analyze", action="store_true", help="skip the self-analysis end step")
    c.add_argument("--envs", help="comma-separated environment ids, e.g. E0,E1,E2,E3")
    c.add_argument("--ladder", action="store_true", help="use the full E0..E3 context ladder")
    c.add_argument("--build-stack")
    c.add_argument("--build-cmd")
    c.add_argument("--job-id")
    c.set_defaults(func=cmd_create)

    f = sub.add_parser("field")
    f.add_argument("job_dir")
    f.add_argument("key")
    f.set_defaults(func=cmd_field)

    tl = sub.add_parser("tasks")
    tl.add_argument("job_dir")
    tl.set_defaults(func=cmd_tasks)

    el = sub.add_parser("envs")
    el.add_argument("job_dir")
    el.set_defaults(func=cmd_envs)

    tt = sub.add_parser("task-text")
    tt.add_argument("job_dir")
    tt.add_argument("task_id")
    tt.set_defaults(func=cmd_task_text)

    at = sub.add_parser("accept-text")
    at.add_argument("job_dir")
    at.add_argument("task_id")
    at.set_defaults(func=cmd_accept_text)

    s = sub.add_parser("set")
    s.add_argument("job_dir")
    s.add_argument("key")
    s.add_argument("value")
    s.set_defaults(func=cmd_set)

    v = sub.add_parser("validate")
    v.add_argument("job_dir")
    v.set_defaults(func=cmd_validate)

    sh = sub.add_parser("show")
    sh.add_argument("job_dir")
    sh.set_defaults(func=cmd_show)

    args = p.parse_args()
    try:
        args.func(args)
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
