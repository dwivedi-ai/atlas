#!/usr/bin/env python3
"""
jobspec.py — author, validate, read, and mutate exp-runner job specs.

RESPONSIBILITY
  The single source of truth for jobs/<job_id>/job.yaml. Both the wizard and run.sh's
  flag mode go through it, and the shell scripts read individual fields via `field`.
  A job is one repo + one or more tasks + a natural-language acceptance description +
  an agent, run either as the 7-rung context LADDER or as the WUR arm matrix.

INPUTS   job.yaml (v1 ladder or v2 wur), schemas/job.schema.json, CLI arguments.
OUTPUTS  a validated spec dict; job.yaml on disk; one field per line on stdout for the
         shell scripts (payload to stdout, errors to stderr).

VALIDATION IS TWO LAYERS, AND BOTH ALWAYS RUN
  Before this rewrite, validate() `return`ed the moment jsonschema succeeded, so the
  manual block underneath was DEAD on every properly-installed machine — the model
  check, the duplicate-task-id check and the environment check had not run in
  production since jsonschema was added. Now:
    1. SHAPE   — jsonschema against schemas/job.schema.json, or a hand-rolled subset
                 when jsonschema is not importable (a stock python3 has neither yaml
                 nor jsonschema until install.sh has run).
    2. SEMANTICS — _semantic_checks(), ALWAYS, on every path.
  _semantic_checks is strict about what is PRESENT and lenient about what is ABSENT,
  because `set` re-validates after every single key write while run.sh builds a spec
  up one key at a time. Completeness is checked by `validate --strict`, which run.sh
  calls once before launching a matrix.

Subcommands
  create  --repo URL | --path DIR [--ref R] [--task .. | --task-file F]
          [--accept .. | --accept-file F] [--model M] [--backend B] [--reps N]
          [--experiment ladder|wur] [--conditions a,b,c] [--facts-file F]
          [--max-seconds S] [--build-stack S] [--build-cmd C] [--job-id ID]
              -> creates jobs/<id>/job.yaml, prints the job dir to stdout
  field   <job_dir> <dotted.key>            -> prints one field ("" if null/missing)
  set     <job_dir> <dotted.key> <value>    -> sets a field (value "null" -> null)
  tasks   <job_dir>                         -> one task id per line
  envs    <job_dir>                         -> one ladder environment per line
  conditions <job_dir>                      -> one ARM per line, whichever mode this is
  condition-field <job_dir> <arm> <key>     -> one design factor of one arm
  validate <job_dir> [--strict]             -> exits non-zero with a message on failure
  show    <job_dir>                         -> prints the parsed spec as JSON
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

LADDER_ENVS = ["E0", "E1", "E2", "E3", "E4", "E5", "E6"]

DEFAULTS = {
    "schema_version": "1",
    "build": {"stack": "auto", "command": None, "detected": None},
    "reps": 1,
    "max_seconds": 0,
    "judge_votes": 1,
    "analyze": True,
}

# Filled only when experiment == "ladder" (the default). A wur spec has no ladder rungs,
# and writing environments into it would make `envs` and `conditions` disagree about what
# the matrix is.
LADDER_DEFAULTS = {
    "environments": list(LADDER_ENVS),
}

# Filled only when experiment == "wur". The probe TEXT is not configurable — it lives in
# lib/wur/protocol.py and is sha256-stamped into run_meta.json; these are its knobs.
WUR_DEFAULTS = {
    "tier": "A",
    "pointer_regime": "none",
    "probe": {
        "enabled": True, "lo": 1, "hi": 3, "max_probes": 24, "salt": "wur-v1",
        "max_retries": 1, "max_resumes": 3, "gate_timeout_ms": 300000,
    },
}

# ── the §7.1 arm table ───────────────────────────────────────────────────────
# The design factors of each WUR arm, decomposed so figures, parquet and the shell
# scripts never have to parse an arm id. This is exactly what run_record
# condition.factors carries. Deliberately NOT here: the planted file's PATH and its
# rendered content — those belong to lib/wur/plant.py, which owns the overlay.
#
# `ctrl-np` is the §7.2 pilot's 13th arm (the difficulty band is defined on ctrl + no
# probe), which is why conditions[] is a pattern in the schema and not a closed enum.
ARM_FACTORS: dict[str, dict] = {
    "d0-push":     {"depth": "d0", "format": "prose",     "channel": "push",    "distractors": 0, "fact_present": True,  "probe": True,  "pointer_regime": "import"},
    "d1-ptr":      {"depth": "d1", "format": "prose",     "channel": "pointer", "distractors": 0, "fact_present": True,  "probe": True,  "pointer_regime": "prose"},
    "d1":          {"depth": "d1", "format": "prose",     "channel": "pull",    "distractors": 0, "fact_present": True,  "probe": True,  "pointer_regime": "none"},
    "d2":          {"depth": "d2", "format": "prose",     "channel": "pull",    "distractors": 0, "fact_present": True,  "probe": True,  "pointer_regime": "none"},
    "d3":          {"depth": "d3", "format": "prose",     "channel": "pull",    "distractors": 0, "fact_present": True,  "probe": True,  "pointer_regime": "none"},
    "d2-check":    {"depth": "d2", "format": "checklist", "channel": "pull",    "distractors": 0, "fact_present": True,  "probe": True,  "pointer_regime": "none"},
    "d2-table":    {"depth": "d2", "format": "table",     "channel": "pull",    "distractors": 0, "fact_present": True,  "probe": True,  "pointer_regime": "none"},
    "d2-dist":     {"depth": "d2", "format": "prose",     "channel": "pull",    "distractors": 3, "fact_present": True,  "probe": True,  "pointer_regime": "none"},
    "ctrl":        {"depth": "d2", "format": "prose",     "channel": "pull",    "distractors": 0, "fact_present": False, "probe": True,  "pointer_regime": "none"},
    "ctrl-nofile": {"depth": None, "format": None,        "channel": None,      "distractors": 0, "fact_present": False, "probe": True,  "pointer_regime": "none"},
    "ctrl-np":     {"depth": "d2", "format": "prose",     "channel": "pull",    "distractors": 0, "fact_present": False, "probe": False, "pointer_regime": "none"},
    "d1-np":       {"depth": "d1", "format": "prose",     "channel": "pull",    "distractors": 0, "fact_present": True,  "probe": False, "pointer_regime": "none"},
    "d3-np":       {"depth": "d3", "format": "prose",     "channel": "pull",    "distractors": 0, "fact_present": True,  "probe": False, "pointer_regime": "none"},
}
FACTOR_KEYS = ("depth", "format", "channel", "distractors", "fact_present", "probe",
               "pointer_regime")
CONDITION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


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


# ── mode / model accessors ───────────────────────────────────────────────────
def experiment_of(spec: dict) -> str:
    """`ladder` (the default when absent, so nothing that works today changes) or `wur`."""
    return (spec.get("experiment") or "ladder").strip() or "ladder"


def _model_of(spec: dict) -> str | None:
    """The name to hand lib/agent.py, from a v1 or a v2 spec.

    v2 split the single `model:` string into agent{backend, model, effort}: `backend`
    picks the CLI adapter, `model` the concrete id. `model:` survives as a deprecated
    free string so every existing job.yaml keeps working. Precedence is
    agent.model -> agent.backend -> model, and None means "the spec has not said yet",
    which is a legal intermediate state while run.sh builds a spec key by key.
    """
    agent = spec.get("agent")
    if isinstance(agent, dict):
        if (agent.get("model") or "").strip():
            return agent["model"].strip()
        if (agent.get("backend") or "").strip():
            return agent["backend"].strip()
    m = spec.get("model")
    return m.strip() if isinstance(m, str) and m.strip() else None


def conditions_of(spec: dict) -> list[str]:
    """The arms of this job's matrix, whichever mode it is in.

    WUR jobs list them in `conditions`; ladder jobs list them in `environments`. One
    accessor so run_job.sh iterates a matrix without branching on the experiment.
    """
    if experiment_of(spec) == "wur":
        return list(spec.get("conditions") or [])
    return list(spec.get("environments") or ["E0"])


def baseline_of(spec: dict) -> str | None:
    """The arm every other arm is differenced against.

    Explicit `baseline_condition` wins. Otherwise E0 for a ladder and ctrl for wur, each
    falling back to the first listed arm — because a figure normalized against an arm
    that has no runs renders 0.00x for every other arm under a label claiming 1.00x.
    """
    b = spec.get("baseline_condition")
    if isinstance(b, str) and b.strip():
        return b.strip()
    arms = conditions_of(spec)
    preferred = "ctrl" if experiment_of(spec) == "wur" else "E0"
    if preferred in arms:
        return preferred
    return arms[0] if arms else None


def condition_factors(spec: dict, arm: str) -> dict:
    """The §7.1 design factors of one arm, with the job-level defaults applied.

    Unknown arm ids are not an error: a job may define an arm this table has never heard
    of, and the honest answer is "no factor is known", not a crash in a shell script.
    """
    base = dict(ARM_FACTORS.get(arm) or {k: None for k in FACTOR_KEYS})
    base.setdefault("distractors", 0)
    for k in FACTOR_KEYS:
        base.setdefault(k, None)
    if base.get("pointer_regime") in (None, "none"):
        jr = spec.get("pointer_regime")
        if isinstance(jr, str) and jr:
            base["pointer_regime"] = jr
    probe_cfg = spec.get("probe")
    if base.get("probe") is None and isinstance(probe_cfg, dict):
        base["probe"] = bool(probe_cfg.get("enabled", True))
    if base.get("probe") is True and isinstance(probe_cfg, dict) \
            and probe_cfg.get("enabled") is False:
        # a job-level probe:{enabled:false} turns the whole matrix into no-probe arms
        base["probe"] = False
    return base


# ── load / validate / write ──────────────────────────────────────────────────
def _deep_fill(spec: dict) -> dict:
    for k, v in DEFAULTS.items():
        if isinstance(v, dict):
            node = spec.setdefault(k, {})
            for kk, vv in v.items():
                node.setdefault(kk, vv)
        else:
            spec.setdefault(k, v)

    # Mode-conditional defaults. `environments` is a LADDER key: filling it on a wur spec
    # would make `envs` and `conditions` disagree about what the matrix is, and would put
    # a ladder rung id in front of context_gen.py's _compose_env, which raises KeyError on
    # a non-ladder arm and rmtree's a hand-authored overlay on a ladder-named one.
    if experiment_of(spec) == "wur":
        spec.setdefault("schema_version", "2")
        spec["schema_version"] = "2"
        for k, v in WUR_DEFAULTS.items():
            if isinstance(v, dict):
                node = spec.setdefault(k, {})
                if isinstance(node, dict):
                    for kk, vv in v.items():
                        node.setdefault(kk, vv)
            else:
                spec.setdefault(k, v)
    else:
        for k, v in LADDER_DEFAULTS.items():
            spec.setdefault(k, list(v) if isinstance(v, list) else v)

    b = baseline_of(spec)
    if b is not None:
        spec.setdefault("baseline_condition", b)

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
    # Mirror the first task into top-level task/accept ONLY for a genuinely
    # single-task job (back-compat for readers that predate `tasks`). For a
    # multi-task job the mirror is actively misleading: it silently republishes
    # task 1 as if it were the job's task, and it defeated cmd_create's
    # `spec.pop("task")` — every --tasks-file spec came back out carrying a
    # duplicate of its first task. A multi-task spec now has `tasks` and nothing
    # else, which is also what schemas/job.schema.json describes.
    if len(tasks) == 1:
        spec.setdefault("task", tasks[0]["task"])
        spec.setdefault("accept", tasks[0]["accept"])
    elif len(tasks) > 1:
        spec.pop("task", None)
        spec.pop("accept", None)


def _manual_shape_checks(spec: dict) -> None:
    """The subset of the JSON schema that matters, for a machine without jsonschema."""
    if spec.get("schema_version") not in ("1", "2"):
        raise ValueError('schema_version must be "1" or "2"')
    jid = spec.get("job_id", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", jid or ""):
        raise ValueError(f"invalid job_id: {jid!r}")
    repo = spec.get("repo") or {}
    if not (repo.get("url") or repo.get("path")):
        raise ValueError("repo must have a url or a path")
    for key in ("task", "accept"):
        if not (spec.get(key) or "").strip():
            raise ValueError(f"{key} must be a non-empty string")
    if experiment_of(spec) not in ("ladder", "wur"):
        raise ValueError(f"experiment must be ladder|wur, got {spec.get('experiment')!r}")
    agent = spec.get("agent")
    if agent is not None:
        if not isinstance(agent, dict) or not (agent.get("backend") or "").strip():
            raise ValueError("agent must be an object with a non-empty backend")


def _semantic_checks(spec: dict, *, strict: bool = False) -> None:
    """Cross-key rules a JSON schema cannot express. ALWAYS runs — this is the block
    that was dead code before, sitting behind an early `return` on jsonschema success.

    Strict about what is PRESENT, lenient about what is ABSENT: `set` re-validates the
    spec after every single key write, so a rule demanding a COMPLETE wur spec would
    reject every intermediate state while run.sh builds one up. `strict=True` adds the
    completeness rules and is what run.sh calls once, before launching a matrix.
    """
    tasks = spec.get("tasks") or []
    if not tasks:
        raise ValueError("a job needs at least one task (task+accept, or a tasks list)")
    ids = [t.get("id") for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate task ids: {ids}")
    for t in tasks:
        if not (t.get("task") or "").strip() or not (t.get("accept") or "").strip():
            raise ValueError(f"task {t.get('id')!r} needs non-empty task and accept text")

    if int(spec.get("reps", 1) or 1) < 1:
        raise ValueError("reps must be >= 1")

    # The model check that has not actually run since jsonschema was installed.
    model = _model_of(spec)
    if model is not None:
        try:
            sys.path.insert(0, str(HERE))
            import agent as _agent  # noqa: PLC0415
            backend = (spec.get("agent") or {}).get("backend") if isinstance(spec.get("agent"), dict) else None
            effort = (spec.get("agent") or {}).get("effort") if isinstance(spec.get("agent"), dict) else None
            _agent.resolve_agent(model, backend=backend, effort=effort)
        except ImportError:
            pass
        except ValueError as e:
            raise ValueError(f"agent/model: {e}")
    elif strict:
        raise ValueError("no model: set agent.backend (v2) or model (v1)")

    exp = experiment_of(spec)
    if exp == "ladder":
        envs = spec.get("environments")
        if envs is not None:
            if not isinstance(envs, list) or not envs:
                raise ValueError("environments must be a non-empty list")
            bad = [e for e in envs if e not in LADDER_ENVS]
            if bad:
                raise ValueError(f"unknown environment(s): {bad} (valid: {LADDER_ENVS})")
        elif strict:
            raise ValueError("ladder mode needs environments[]")
        if spec.get("conditions"):
            raise ValueError("conditions[] is a wur key; a ladder job uses environments[]")
    else:
        conds = spec.get("conditions")
        if conds is not None:
            if not isinstance(conds, list) or not conds:
                raise ValueError("conditions must be a non-empty list")
            bad = [c for c in conds if not (isinstance(c, str) and CONDITION_ID_RE.fullmatch(c))]
            if bad:
                raise ValueError(f"invalid condition id(s): {bad}")
            if len(conds) != len(set(conds)):
                raise ValueError(f"duplicate condition ids: {conds}")
        elif strict:
            raise ValueError("wur mode needs conditions[]")
        if spec.get("environments"):
            raise ValueError(
                "environments[] is a ladder key; a wur job uses conditions[]. "
                "context_gen.py raises KeyError on a non-ladder arm and rmtree's a "
                "hand-authored overlay on a ladder-named one, so the two must not mix.")
        if strict and not (spec.get("facts_file") or "").strip():
            raise ValueError("wur mode needs facts_file (the frozen fact registry)")

    arms = conditions_of(spec)
    base = spec.get("baseline_condition")
    if isinstance(base, str) and base.strip() and arms and base.strip() not in arms:
        raise ValueError(
            f"baseline_condition {base!r} is not one of the arms {arms}. "
            f"A baseline with no runs normalizes every other arm to 0.00x under a label "
            f"claiming 1.00x — figures.py refuses to plot it rather than plotting zeros.")

    probe = spec.get("probe")
    if isinstance(probe, dict):
        lo, hi = probe.get("lo", 1), probe.get("hi", 3)
        if isinstance(lo, int) and isinstance(hi, int) and lo > hi:
            raise ValueError(f"probe.lo ({lo}) must be <= probe.hi ({hi})")
        if isinstance(probe.get("max_probes"), int) and probe["max_probes"] < 1:
            raise ValueError("probe.max_probes must be >= 1")

    budget = spec.get("budget")
    if isinstance(budget, dict) and isinstance(budget.get("steps"), int) and budget["steps"] < 1:
        raise ValueError("budget.steps must be >= 1")


def validate(spec: dict, *, strict: bool = False) -> None:
    """Raise ValueError with a human message if the spec is invalid.

    Both layers always run. jsonschema is optional (a stock python3 has neither yaml nor
    jsonschema until install.sh has run); its absence downgrades layer 1 to a hand-rolled
    subset but never skips layer 2.
    """
    try:
        import jsonschema  # type: ignore  # noqa: PLC0415
    except ImportError:
        _manual_shape_checks(spec)
    else:
        schema = json.loads(SCHEMA_PATH.read_text())
        try:
            jsonschema.validate(spec, schema)
        except jsonschema.ValidationError as e:
            where = ".".join(str(p) for p in e.absolute_path)
            raise ValueError(f"{where + ': ' if where else ''}{e.message}")
    _semantic_checks(spec, strict=strict)


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


# `set` receives a string from the shell but the schema declares types, so a typed key
# must be converted or the write fails validation — `set probe.enabled false` would
# otherwise store the STRING "false", which is truthy everywhere: a no-probe arm that
# probes. The table is keyed EXPLICITLY rather than sniffed, because sniffing would turn
# an all-digit `repo.pinned_sha` into an int and any task text containing a comma into a
# list. Anything not listed here is stored verbatim as a string.
_BOOL_KEYS = {"analyze", "probe.enabled"}
_INT_KEYS = {
    "reps", "max_seconds", "judge_votes", "matrix_seed",
    "probe.lo", "probe.hi", "probe.max_probes", "probe.max_retries",
    "probe.max_resumes", "probe.gate_timeout_ms",
    "budget.steps", "budget.max_seconds",
    "parallelism.jobs", "parallelism.per_backend.claude", "parallelism.per_backend.codex",
    "parallelism.per_backend.gemini", "parallelism.per_backend.agy",
}
_FLOAT_KEYS = {"budget.max_usd"}
_LIST_KEYS = {"environments", "conditions"}


def _coerce(key: str, value: str):
    """Type a `set` value according to the key it is being written to."""
    if value == "null":
        return None
    if key in _BOOL_KEYS:
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"{key} must be a boolean, got {value!r}")
    if key in _INT_KEYS:
        try:
            return int(value.strip())
        except ValueError:
            raise ValueError(f"{key} must be an integer, got {value!r}")
    if key in _FLOAT_KEYS:
        try:
            return float(value.strip())
        except ValueError:
            raise ValueError(f"{key} must be a number, got {value!r}")
    if key in _LIST_KEYS:
        return [p.strip() for p in value.split(",") if p.strip()]
    return value


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
    experiment = (args.experiment or "ladder").strip()
    spec = {
        "schema_version": "2" if experiment == "wur" else "1",
        "repo": {"ref": args.ref or "HEAD", "pinned_sha": None},
        "build": {"stack": args.build_stack or "auto", "command": args.build_cmd, "detected": None},
        "task": (_read_or_inline(args.task, args.task_file) or "").strip(),
        "accept": (_read_or_inline(args.accept, args.accept_file) or "").strip(),
        "reps": int(args.reps),
        "max_seconds": int(args.max_seconds),
        "judge_votes": int(args.judge_votes),
        "analyze": (not args.no_analyze),
        "experiment": experiment,
    }

    # Two-level model selection (§6.5). --backend/--model land in agent{}; --model alone
    # still populates the deprecated top-level `model` so a v1 reader keeps working.
    if args.backend or args.effort:
        ident = None
        try:
            sys.path.insert(0, str(HERE))
            import agent as _agent  # noqa: PLC0415
            ident = _agent.resolve_agent(args.model, backend=args.backend, effort=args.effort)
        except ImportError:
            pass
        if ident:
            spec["agent"] = {"backend": ident["backend"], "model": ident["model"],
                             "effort": ident["effort"]}
        else:
            spec["agent"] = {"backend": args.backend, "model": args.model,
                             "effort": args.effort}
    else:
        spec["model"] = args.model or "codex"

    if experiment == "wur":
        arms = ([c.strip() for c in args.conditions.split(",") if c.strip()]
                if args.conditions else list(ARM_FACTORS))
        spec["conditions"] = arms
        spec["baseline_condition"] = args.baseline or ("ctrl" if "ctrl" in arms else arms[0])
        if args.facts_file:
            spec["facts_file"] = args.facts_file
        if args.matrix_seed is not None:
            spec["matrix_seed"] = int(args.matrix_seed)
        if args.pointer_regime:
            spec["pointer_regime"] = args.pointer_regime
    else:
        # Environments: default = all 7 (E0..E6, like l1-doxed); --envs overrides.
        if args.envs:
            spec["environments"] = [e.strip() for e in args.envs.split(",") if e.strip()]
        else:
            spec["environments"] = list(LADDER_ENVS)
        spec["baseline_condition"] = args.baseline or spec["environments"][0]

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
    # `field model` on a v2 spec, where selection moved into agent{}, answers from
    # _model_of rather than printing an empty string that a shell script would carry
    # forward as an unset MODEL.
    if val is None and args.key == "model":
        val = _model_of(spec)
    if isinstance(val, bool):
        print("true" if val else "false")
    elif isinstance(val, (list, dict)):
        print(json.dumps(val))
    else:
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


def cmd_conditions(args) -> None:
    """One arm per line, whichever mode the job is in — so run_job.sh does not branch."""
    spec = load(Path(args.job_dir))
    for c in conditions_of(spec):
        print(c)


def cmd_condition_field(args) -> None:
    """One §7.1 design factor of one arm. `--all` prints the whole factor object as JSON."""
    spec = load(Path(args.job_dir))
    factors = condition_factors(spec, args.condition)
    if args.all or args.key in (None, ""):
        print(json.dumps(factors))
        return
    if args.key not in factors:
        raise ValueError(f"no such condition field: {args.key!r} (have {', '.join(FACTOR_KEYS)})")
    v = factors[args.key]
    if isinstance(v, bool):
        print("true" if v else "false")
    else:
        print("" if v is None else v)


def cmd_task_text(args) -> None:
    spec = load(Path(args.job_dir))
    print(_find_task(spec, args.task_id)["task"].strip())


def cmd_accept_text(args) -> None:
    spec = load(Path(args.job_dir))
    print(_find_task(spec, args.task_id)["accept"].strip())


def cmd_set(args) -> None:
    job_dir = Path(args.job_dir)
    spec = load(job_dir)
    _set(spec, args.key, args.value if args.raw else _coerce(args.key, args.value))
    write(spec, job_dir)


def cmd_validate(args) -> None:
    validate(load(Path(args.job_dir)), strict=args.strict)
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
    c.add_argument("--backend", help="claude|codex|gemini|agy — writes agent{} instead of model:")
    c.add_argument("--effort")
    c.add_argument("--reps", default=1)
    c.add_argument("--max-seconds", default=0)
    c.add_argument("--judge-votes", default=1)
    c.add_argument("--no-analyze", action="store_true", help="skip the self-analysis end step")
    c.add_argument("--envs", help="comma-separated environment ids, e.g. E0,E1,E2,E3")
    c.add_argument("--ladder", action="store_true", help="use the full E0..E6 context ladder")
    c.add_argument("--experiment", choices=["ladder", "wur"], default="ladder")
    c.add_argument("--conditions", help="comma-separated wur arm ids (default: all 13)")
    c.add_argument("--baseline", help="the arm every other arm is differenced against")
    c.add_argument("--facts-file")
    c.add_argument("--matrix-seed", type=int)
    c.add_argument("--pointer-regime", choices=["none", "prose", "import"])
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

    cl = sub.add_parser("conditions", help="one arm per line (conditions[] or environments[])")
    cl.add_argument("job_dir")
    cl.set_defaults(func=cmd_conditions)

    cf = sub.add_parser("condition-field", help="one §7.1 design factor of one arm")
    cf.add_argument("job_dir")
    cf.add_argument("condition")
    cf.add_argument("key", nargs="?")
    cf.add_argument("--all", action="store_true", help="print every factor as JSON")
    cf.set_defaults(func=cmd_condition_field)

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
    s.add_argument("--raw", action="store_true", help="store the value as a string verbatim")
    s.set_defaults(func=cmd_set)

    v = sub.add_parser("validate")
    v.add_argument("job_dir")
    v.add_argument("--strict", action="store_true",
                   help="also require completeness (run.sh calls this before a matrix)")
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
