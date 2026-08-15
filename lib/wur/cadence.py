#!/usr/bin/env python3
"""
cadence.py — the probe schedule, and the ONLY randomness in the probe subsystem.

RESPONSIBILITY
  Turn (task_id, rep) into a frozen list of barrier indices at which the driver
  fires probes. Nothing else in lib/wur/ may draw a random number.

INPUTS
  task_id, rep, budget (the step budget in tool calls), and the cadence bounds
  lo/hi/max_probes/salt taken from job.yaml `probe{}` / `budget.steps`.

OUTPUTS
  A plain dict, serialized verbatim to $RUN_DIR/probe_plan.json BEFORE the child
  starts: {seed_material, seed_int, lo, hi, max_probes, intervals, fire_at}.

WHY IT IS SEEDED THE WAY IT IS
  The seed is (salt, task_id, rep) — deliberately NOT the arm. Every arm of the
  same (task, rep) therefore fires its k-th probe at the same barrier index, and
  probe timing becomes a matched covariate rather than a nuisance one. Rollup
  asserts that the realized `sent_at_barrier` sequences of all arms of a
  (task_id, rep) agree on their common prefix, else the run is stamped
  probe_integrity: "schedule_divergence".

  max_probes = 24 is a cost bound: unbounded ~U{1,2,3} intervals over the step
  budget imply ~60 probes/run at ~350 output tokens each, ~20k tokens of pure
  instrument.

BANNED EVERYWHERE IN THIS SUBSYSTEM
  Math.random, $RANDOM, module-global random.*, time-seeded RNG, and uuid4 as
  measured randomness. The Random instance below is local to the call.

CLI
  python3 lib/wur/cadence.py --task-id t1 --rep 1 --budget 30   -> the plan as JSON
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from typing import Any

# Defaults are the values; job.yaml `probe{}` may override lo/hi/max_probes/salt.
DEFAULT_LO = 1
DEFAULT_HI = 3
DEFAULT_MAX_PROBES = 24
DEFAULT_SALT = "wur-v1"


def schedule(
    task_id: str,
    rep: int,
    budget: int,
    lo: int = DEFAULT_LO,
    hi: int = DEFAULT_HI,
    max_probes: int = DEFAULT_MAX_PROBES,
    salt: str = DEFAULT_SALT,
) -> dict[str, Any]:
    """The probe cadence for one (task_id, rep), verbatim from 

    `fire_at[k]` is the barrier ordinal (1-based, counting tool calls) at which
    probe k+1 is injected; `intervals[k]` is the gap that produced it. The body
    of this function is frozen — changing the order or count of rng draws
    silently re-randomizes every future run relative to every past one.
    """
    material = f"{salt}|{task_id}|{rep}"
    seed = int.from_bytes(hashlib.sha256(material.encode()).digest()[:8], "big")
    rng = random.Random(seed)
    intervals: list[int] = []
    fire_at: list[int] = []
    n = 0
    while n < budget and len(fire_at) < max_probes:
        iv = rng.randint(lo, hi)
        n += iv
        intervals.append(iv)
        fire_at.append(n)
    return {
        "seed_material": material,
        "seed_int": seed,
        "lo": lo,
        "hi": hi,
        "max_probes": max_probes,
        "intervals": intervals,
        "fire_at": fire_at,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="seeded WUR probe cadence")
    p.add_argument("--task-id", required=True)
    p.add_argument("--rep", type=int, required=True)
    p.add_argument("--budget", type=int, required=True, help="step budget, in tool calls")
    p.add_argument("--lo", type=int, default=DEFAULT_LO)
    p.add_argument("--hi", type=int, default=DEFAULT_HI)
    p.add_argument("--max-probes", type=int, default=DEFAULT_MAX_PROBES)
    p.add_argument("--salt", default=DEFAULT_SALT)
    a = p.parse_args(argv)
    print(
        json.dumps(
            schedule(a.task_id, a.rep, a.budget, a.lo, a.hi, a.max_probes, a.salt),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
