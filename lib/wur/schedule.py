#!/usr/bin/env python3
"""
schedule.py — blocked randomisation of the run matrix, frozen to schedule.json.

RESPONSIBILITY
  Turn a job's (tasks x conditions x reps) matrix into ONE ordered list of cells
  and write it to $JOB_DIR/schedule.json before a single agent is launched.
  run_job.sh then walks that list instead of the nested
  `for task; for env; for rep` loops it used to walk (run_job.sh:61-64).

  That nesting is the whole reason this module exists. It ran every cell of arm
  `d0-push` before any cell of `d1`, so ARM WAS PERFECTLY CONFOUNDED WITH WALL
  CLOCK: a model deployment, a rate-limit slowdown, or a machine getting busier
  over an 36-hour matrix lands entirely on the arms that happen to run late, and
  no amount of downstream analysis can separate that from the effect being
  measured. Blocked randomisation breaks the confound by construction.

INPUTS
  --job-dir  (tasks / conditions / reps / matrix_seed read via lib/jobspec.py),
  or the same four given explicitly on the command line so this module can be
  unit-tested and re-derived without a job on disk.

OUTPUTS
  $JOB_DIR/schedule.json — {schema_version, design_version, job_id, experiment,
  matrix_seed, salt, block_key, tasks, conditions, reps, n_blocks, n_cells,
  design_sha256, order_sha256, generated_at, cells[]}. Each cell carries
  {cell_index, run_order_index, block, block_index, within_block_index, task_id,
  condition, rep} and, when --agent-dash is given, `run_id`.

  `design_sha256` hashes the DESIGN (sorted tasks, sorted conditions, reps,
  block key, seed, salt) and not the realized order, so it answers "is this the
  matrix that was pre-registered?" A silent edit — one more arm, one more rep —
  changes it loudly. `order_sha256` hashes the realized order, so a re-derivation
  proves the shuffle is reproducible.

THE RANDOMISATION (a randomized complete block design)
  block           = (task_id, rep). Every condition appears exactly once in it.
  within a block  the conditions are shuffled.
  across blocks   the block order is shuffled.
  Both draws are seeded from sha256 material that includes the block identity, so
  the order inside block (t7, rep 3) does not change when an unrelated task is
  added to the job. Re-running an interrupted matrix with the same matrix_seed
  therefore reproduces the same order for every block that had not run yet.

BANNED HERE, exactly as in cadence.py
  time-seeded RNG, module-global random.*, $RANDOM, uuid4 as measured
  randomness. Every Random instance below is local to the call and seeded from a
  recorded string. `matrix_seed: null` in job.yaml means "seed 0", not "seed from
  the clock" — an unseeded shuffle is not a design, it is an anecdote.

CLI
  python3 lib/wur/schedule.py --job-dir DIR [--out PATH] [--agent-dash D]
  python3 lib/wur/schedule.py --job-dir DIR --check       # re-derive, compare, exit 1 on drift
  python3 lib/wur/schedule.py --tasks t1,t2 --conditions d1,d2 --reps 3 --print
  Payload (the schedule path, or the JSON with --print) goes to stdout; every
  human-facing message goes to stderr — run.sh reads this by command substitution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

HERE = Path(__file__).resolve().parent
LIB = HERE.parent

SCHEDULE_BASENAME = "schedule.json"
ACTUAL_BASENAME = "schedule_actual.jsonl"
DESIGN_VERSION = "wur-schedule-v1"
DEFAULT_SALT = "wur-v1"
#: (task_id, rep) — the block within which conditions are compared. Recorded in
#: schedule.json so a reader never has to infer it from the cell list.
BLOCK_KEY = ("task_id", "rep")


class ScheduleError(RuntimeError):
    """The matrix cannot be built, or an on-disk schedule disagrees with the job."""


# ── seeding ──────────────────────────────────────────────────────────────────
def seed_int(material: str) -> int:
    """The 64-bit seed for one named draw. Same construction as cadence.py."""
    return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big")


def _rng(*parts: Any) -> tuple[random.Random, str]:
    material = "|".join(str(p) for p in parts)
    return random.Random(seed_int(material)), material


def sha256_json(obj: Any) -> str:
    """sha256 of a canonical JSON encoding (sorted keys, no incidental spacing)."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# ── the design ───────────────────────────────────────────────────────────────
def design_core(
    *,
    job_id: str,
    experiment: str,
    tasks: Sequence[str],
    conditions: Sequence[str],
    reps: int,
    matrix_seed: int,
    salt: str = DEFAULT_SALT,
) -> dict[str, Any]:
    """The canonical, ORDER-FREE description of the matrix. This is what is hashed.

    Sorted, so listing the same arms in a different order in job.yaml does not
    look like a different experiment; and complete, so adding an arm or a rep
    does.
    """
    return {
        "design_version": DESIGN_VERSION,
        "job_id": job_id,
        "experiment": experiment,
        "block_key": list(BLOCK_KEY),
        "tasks": sorted(tasks),
        "conditions": sorted(conditions),
        "reps": int(reps),
        "matrix_seed": int(matrix_seed),
        "salt": salt,
    }


def design_sha256(core: dict[str, Any]) -> str:
    return sha256_json(core)


def _check_ids(name: str, values: Sequence[str]) -> list[str]:
    out = [str(v).strip() for v in values if str(v).strip()]
    if not out:
        raise ScheduleError(f"{name} is empty — there is no matrix to schedule")
    if len(out) != len(set(out)):
        dupes = sorted({v for v in out if out.count(v) > 1})
        raise ScheduleError(f"duplicate {name}: {dupes}")
    return out


def build(
    *,
    job_id: str,
    tasks: Sequence[str],
    conditions: Sequence[str],
    reps: int = 1,
    matrix_seed: int | None = 0,
    experiment: str = "wur",
    salt: str = DEFAULT_SALT,
    agent_dash: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the whole schedule. Pure: touches no file, draws no unseeded number.

    `matrix_seed=None` is normalized to 0 rather than to a clock reading, so a
    job.yaml that never set one still produces a reproducible order.
    """
    tasks = _check_ids("tasks", tasks)
    conditions = _check_ids("conditions", conditions)
    # NOT `int(reps or 1)`: that silently turns an explicit reps=0 into 1, which is
    # the difference between "this job is misconfigured" and "this job quietly ran
    # a third of the matrix you thought you asked for".
    reps = 1 if reps is None else int(reps)
    if reps < 1:
        raise ScheduleError(f"reps must be >= 1, got {reps}")
    seed = 0 if matrix_seed is None else int(matrix_seed)

    core = design_core(job_id=job_id, experiment=experiment, tasks=tasks,
                       conditions=conditions, reps=reps, matrix_seed=seed, salt=salt)
    d_sha = design_sha256(core)

    # cell_index is the position in the CANONICAL enumeration — a stable identity
    # for "which cell", independent of when it happens to run. run_order_index is
    # the position in the shuffled order. run_record carries both because the
    # whole point of this module is that they are different numbers.
    canonical: list[tuple[str, str, int]] = []
    for t in tasks:
        for c in conditions:
            for r in range(1, reps + 1):
                canonical.append((t, c, r))
    cell_index = {key: i for i, key in enumerate(canonical)}

    blocks: list[tuple[str, int]] = [(t, r) for t in tasks for r in range(1, reps + 1)]
    block_rng, block_material = _rng(salt, "blocks", job_id, seed)
    block_rng.shuffle(blocks)

    cells: list[dict[str, Any]] = []
    for b_idx, (task_id, rep) in enumerate(blocks):
        arms = list(conditions)
        # Seeded by the block identity, so adding a task never re-randomizes an
        # unrelated block — an interrupted matrix resumes in the same order.
        arm_rng, _ = _rng(salt, "within", job_id, seed, task_id, rep)
        arm_rng.shuffle(arms)
        for w_idx, cond in enumerate(arms):
            key = (task_id, cond, rep)
            cell = {
                "cell_index": cell_index[key],
                "run_order_index": len(cells),
                "block": f"{task_id}|{rep}",
                "block_index": b_idx,
                "within_block_index": w_idx,
                "task_id": task_id,
                "condition": cond,
                "rep": rep,
            }
            if agent_dash:
                cell["run_id"] = run_id_for(job_id, task_id, cond, agent_dash, rep)
            cells.append(cell)

    order_sha = sha256_json([[c["task_id"], c["condition"], c["rep"]] for c in cells])
    return {
        "schema_version": "1",
        **core,
        "block_material": block_material,
        "n_blocks": len(blocks),
        "n_cells": len(cells),
        "design_sha256": d_sha,
        "order_sha256": order_sha,
        "generated_at": generated_at,
        "agent_dash": agent_dash,
        "cells": cells,
    }


def run_id_for(job_id: str, task_id: str, condition: str, agent_dash: str, rep: int) -> str:
    """The run id run_job.sh composes. Kept here verbatim so schedule.json can
    carry it, but run_job.sh still composes its own — a schedule must not become
    the only place a run id is defined, or a schedule written for one backend
    would silently rename another backend's runs."""
    return f"{job_id}-{task_id}-{condition}-{agent_dash}-r{int(rep):03d}"


# ── job.yaml integration ─────────────────────────────────────────────────────
def _jobspec():
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))
    import jobspec  # noqa: PLC0415
    return jobspec


def from_job(job_dir: str | Path, *, agent_dash: str | None = None,
             matrix_seed: int | None = None, salt: str = DEFAULT_SALT,
             generated_at: str | None = None) -> dict[str, Any]:
    """Read tasks / conditions / reps / matrix_seed out of job.yaml and build."""
    jobspec = _jobspec()
    job_dir = Path(job_dir)
    spec = jobspec.load(job_dir)
    tasks = [t["id"] for t in spec.get("tasks") or []]
    conditions = jobspec.conditions_of(spec)
    seed = matrix_seed if matrix_seed is not None else spec.get("matrix_seed")
    return build(
        job_id=spec.get("job_id") or job_dir.name,
        tasks=tasks,
        conditions=conditions,
        reps=int(spec.get("reps") or 1),
        matrix_seed=seed,
        experiment=jobspec.experiment_of(spec),
        salt=salt,
        agent_dash=agent_dash,
        generated_at=generated_at,
    )


def schedule_path(job_dir: str | Path) -> Path:
    return Path(job_dir) / SCHEDULE_BASENAME


def write(sched: dict[str, Any], out: str | Path) -> Path:
    """Atomic write — a half-written schedule is a half-run matrix."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(sched, indent=2) + "\n", encoding="utf-8")
    tmp.replace(out)
    return out


def load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check(job_dir: str | Path, *, agent_dash: str | None = None) -> list[str]:
    """Re-derive the schedule from job.yaml and compare it to the one on disk.

    Returns a list of problems; empty means the frozen schedule still describes
    the job it was frozen for.
    """
    path = schedule_path(job_dir)
    if not path.exists():
        return [f"no schedule at {path}"]
    on_disk = load(path)
    fresh = from_job(job_dir, agent_dash=agent_dash or on_disk.get("agent_dash"),
                     matrix_seed=on_disk.get("matrix_seed"),
                     salt=on_disk.get("salt", DEFAULT_SALT))
    problems: list[str] = []
    for key in ("design_sha256", "order_sha256", "n_cells", "n_blocks"):
        if on_disk.get(key) != fresh.get(key):
            problems.append(f"{key}: on-disk {on_disk.get(key)!r} != re-derived {fresh.get(key)!r}")
    return problems


# ── schedule_actual.jsonl ────────────────────────────────────────────────────
def actual_path(job_dir: str | Path) -> Path:
    return Path(job_dir) / ACTUAL_BASENAME


def actual_row(cell: dict[str, Any], *, run_id: str, event: str,
               concurrency_at_launch: int | None = None, ts: str | None = None,
               **extra: Any) -> dict[str, Any]:
    """One realized-order row. run_job.sh appends these under flock (one writer
    per file, V8: unlocked appends past PIPE_BUF corrupt)."""
    row = {
        "ts": ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        "run_id": run_id,
        "cell_index": cell.get("cell_index"),
        "run_order_index": cell.get("run_order_index"),
        "block": cell.get("block"),
        "task_id": cell.get("task_id"),
        "condition": cell.get("condition"),
        "rep": cell.get("rep"),
        "concurrency_at_launch": concurrency_at_launch,
    }
    row.update(extra)
    return row


# ── CLI ──────────────────────────────────────────────────────────────────────
def _split(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _emit_cells(sched: dict[str, Any], fmt: str) -> None:
    """The line format run_job.sh reads: one cell per line, pipe-separated."""
    for c in sched["cells"]:
        if fmt == "tsv":
            print(f"{c['task_id']}\t{c['condition']}\t{c['rep']}\t{c['cell_index']}\t{c['run_order_index']}")
        else:
            print(f"{c['task_id']}|{c['condition']}|{c['rep']}|{c['cell_index']}|{c['run_order_index']}")


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="blocked randomisation of the run matrix")
    p.add_argument("--job-dir")
    p.add_argument("--job-id")
    p.add_argument("--tasks", help="comma-separated task ids (default: from job.yaml)")
    p.add_argument("--conditions", help="comma-separated arm ids (default: from job.yaml)")
    p.add_argument("--reps", type=int)
    p.add_argument("--matrix-seed", type=int)
    p.add_argument("--experiment", default=None)
    p.add_argument("--salt", default=DEFAULT_SALT)
    p.add_argument("--agent-dash", default=None,
                   help="stamp each cell with the run id run_job.sh will compose")
    p.add_argument("--out", default=None, help="default: <job-dir>/schedule.json")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing schedule.json (default: keep it, resume-safe)")
    p.add_argument("--check", action="store_true",
                   help="re-derive from job.yaml and compare; exit 1 on drift")
    p.add_argument("--print", dest="print_json", action="store_true",
                   help="print the schedule JSON on stdout instead of its path")
    p.add_argument("--cells", choices=["pipe", "tsv"], default=None,
                   help="print one cell per line and write nothing")
    a = p.parse_args(argv)

    try:
        if a.check:
            if not a.job_dir:
                print("ERROR: --check needs --job-dir", file=sys.stderr)
                return 2
            problems = check(a.job_dir, agent_dash=a.agent_dash)
            for prob in problems:
                print(f"SCHEDULE DRIFT: {prob}", file=sys.stderr)
            print("ok" if not problems else "drift")
            return 1 if problems else 0

        explicit = _split(a.tasks) and _split(a.conditions)
        if explicit:
            sched = build(
                job_id=a.job_id or (Path(a.job_dir).name if a.job_dir else "job"),
                tasks=_split(a.tasks),
                conditions=_split(a.conditions),
                reps=a.reps if a.reps is not None else 1,
                matrix_seed=a.matrix_seed,
                experiment=a.experiment or "wur",
                salt=a.salt,
                agent_dash=a.agent_dash,
                generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        else:
            if not a.job_dir:
                print("ERROR: give --job-dir, or both --tasks and --conditions", file=sys.stderr)
                return 2
            sched = from_job(a.job_dir, agent_dash=a.agent_dash, matrix_seed=a.matrix_seed,
                             salt=a.salt,
                             generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    except (ScheduleError, ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if a.cells:
        _emit_cells(sched, a.cells)
        return 0
    if a.print_json:
        print(json.dumps(sched, indent=2))
        return 0

    out = Path(a.out) if a.out else (schedule_path(a.job_dir) if a.job_dir else None)
    if out is None:
        print(json.dumps(sched, indent=2))
        return 0
    if out.exists() and not a.force:
        # Resume-safe by default: a matrix that is half-run must not be reshuffled
        # underneath itself. Re-deriving would give the same order anyway, but only
        # if job.yaml has not been touched — and if it has, silently reordering the
        # remaining cells is exactly the confound this module removes.
        existing = load(out)
        if existing.get("design_sha256") != sched.get("design_sha256"):
            print(
                f"ERROR: {out} exists with design_sha256 "
                f"{existing.get('design_sha256')} but the job now hashes to "
                f"{sched.get('design_sha256')} — the matrix changed under a "
                f"running schedule. Re-run with --force to reshuffle, and treat "
                f"the two halves as different experiments.",
                file=sys.stderr,
            )
            return 1
        print(f"--> schedule kept (unchanged design): {out}", file=sys.stderr)
        print(out)
        return 0
    write(sched, out)
    print(f"--> schedule: {sched['n_cells']} cells in {sched['n_blocks']} blocks "
          f"(design {sched['design_sha256'][:12]})", file=sys.stderr)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
