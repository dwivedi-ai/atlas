#!/usr/bin/env python3
"""
aggregate.py — the rollup: many run dirs in, three analysis tables out.

RESPONSIBILITY
  Walk every finished run of one job, read the four per-run derived tables that
  reconcile.py wrote, flatten them into rectangles a statistics library can use,
  and materialize them as parquet with .csv mirrors. It computes no statistic:
  every number in analysis/ is produced by analysis/uptake_lib.py from these
  files, so a scanner bugfix means "re-run reconcile.py, re-run aggregate.py",
  never "re-derive a metric by hand".

  Two things it DOES decide, because both are shape rather than inference:
    - nested JSON becomes columns (`factors{}` -> factor_*, `slots[3]` -> slot0_*,
      lists -> a *_json column plus a count), because parquet-of-structs is not
      readable from a CSV mirror and the CSV mirror is what survives a pandas
      major version;
    - per-probe mention tiers (§4.5 (a) exact nonce, (b) frozen paraphrase regex,
      primary = a OR b) are folded up from the three slots, because that fold is
      a definition in the spec and not a modelling choice.

INPUTS
  $JOB_DIR/job.yaml                         job_id, for locating the runs root
  <run>/fact_trace.jsonl                    one (run x fact) row — the headline table
  <run>/probes.jsonl                        one row per probe
  <run>/events.jsonl                        one row per event
  <run>/run_record.json                     identity fallback only (never a metric)
  Run dirs are discovered under $JOB_DIR/runs/ AND
  $ATLAS_RUNS_ROOT/<job_id>/ (default /tmp/atlas-runs) — WUR moves run roots out
  of the job dir so no secret is an ancestor of a workspace (V9).

OUTPUTS
  <out>/fact_trace.parquet + .csv
  <out>/probes.parquet     + .csv
  <out>/events.parquet     + .csv
  <out>/aggregate_manifest.json   row counts, run counts, the accounting-version
                                  census, and every run that was skipped and why
  Default <out> is $JOB_DIR/analysis (see PATH NOTE).

PARQUET DTYPES ARE NOT COSMETIC
  Every nullable seq / probe-index / count column is written as pandas Int64, not
  the default float64. A d0-push row's null `first_exposure_seq` round-trips as
  NaN under float64, and NaN != None defeats trace.py's own null assertion — the
  Phase-0 spike hit exactly this. The declaration lives in INT64_COLUMNS below
  and is asserted by tests/test_wur_lib.py.

PATH NOTE (departure, deliberate)
  IMPLEMENTATION.md §5.2 and STATUS.md §6 write the destination as a bare
  `analysis/*.parquet`. Taken literally, two jobs aggregated in a row overwrite
  each other's tables with no warning. Default output is therefore job-scoped,
  `$JOB_DIR/analysis/`; `--out-dir analysis` reproduces the document's literal
  path when that is what you want.

CLI
  python3 lib/wur/aggregate.py --job-dir jobs/<id> [--out-dir DIR]
                               [--runs-root DIR] [--no-csv] [--no-parquet]
                               [--strict] [--quiet]
    exit 1 when no run produced a fact_trace row, or (with --strict) when a run
    is unreadable or the job mixes tokens.accounting_version generations.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

AGGREGATE_VERSION = "wur-aggregate-v1"

#: The three tables, in the order the manifest reports them.
TABLE_NAMES = ("fact_trace", "probes", "events")

#: Per-run source file for each table.
SOURCE_FILES = {
    "fact_trace": "fact_trace.jsonl",
    "probes": "probes.jsonl",
    "events": "events.jsonl",
}

#: Columns that MUST be pandas Int64 (nullable integer), never float64.
#: Anything that can be null and is compared against None belongs here.
INT64_COLUMNS: dict[str, tuple[str, ...]] = {
    "fact_trace": (
        "rep", "factor_distractors",
        "first_exposure_seq", "first_exposure_bytes_before", "first_used_seq",
        "first_mention_seq", "first_mention_probe", "last_mention_probe",
        "mention_run_length", "n_reinjections", "first_use_probe_index",
        "n_probes_observed", "at_risk_horizon", "lapse_probe_index",
        "tool_calls_total", "tool_calls_task", "turns_total", "n_probes_sent",
        "tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_write",
        "tokens_effective",
    ),
    "probes": (
        "rep", "probe_idx", "sent_at_barrier", "planned_at_barrier",
        "sampled_interval", "sent_seq", "answer_seq", "tokens_out",
        "n_parse_errors", "n_slots", "n_slots_matched",
        "n_critical_fact_slots", "n_filler_slots", "n_distrusted_slots",
        "n_empty_slots", "n_task_restatement_slots", "n_generic_workspace_slots",
        "n_probe_turn_seq",
    ),
    "events": (
        "seq", "turn_idx", "barrier", "result_bytes", "tokens_in", "tokens_out",
        "n_nonce_hits", "n_inbound_hits",
    ),
}

#: Columns that MUST be pandas `boolean` (nullable bool). `read` is the
#: load-bearing one: null means UNKNOWN (a truncated or errored read on the fact
#: file) and coercing it to False would make the bias run with the hypothesis.
BOOL_COLUMNS: dict[str, tuple[str, ...]] = {
    "fact_trace": (
        "available", "read", "read_inbound_only", "unexplained_possession",
        "opened", "read_censored", "read_error", "echoed", "thinking_echo",
        "used", "used_in_diff", "eligible", "ever_mention", "retention_censored",
        "wrong_value_in_slot", "success", "analyzable", "quarantined",
        "factor_fact_present", "factor_probe",
    ),
    "probes": (
        "parse_ok", "retry_sent", "answered_after_retry", "probe_id_match",
        "fidelity_agree", "mention_tier_a", "mention_tier_b", "mention_primary",
        "mention_llm",
    ),
    "events": ("is_probe_turn", "truncated_by_cli", "is_error"),
}

#: fact_trace.factors{} -> flat columns. Same seven keys as
#: run_record.schema.json condition.factors.
FACTOR_KEYS = ("depth", "format", "channel", "distractors", "fact_present",
               "probe", "pointer_regime")

#: probes.slots[i] -> slot<i>_* columns.
SLOT_KEYS = ("fact", "source", "affects_next_action", "slot_class", "match_nonce",
             "match_regex", "match_llm", "match_form", "matched_regex",
             "source_verified", "wrong_value")

SLOT_CLASSES = ("critical_fact", "task_restatement", "generic_workspace",
                "filler", "empty", "distrusted")

#: Channels that count as INBOUND exposure. self_thinking is model-visible but
#: NOT inbound — that is the D4 carve-out that sets `read` while leaving
#: `read_inbound_only` alone (§4.2.1). Kept here only to count hits per event;
#: the funnel decision itself is trace.py's, never this module's.
INBOUND_CHANNELS = frozenset({
    "autoload_claude_md", "tool_read", "tool_grep_content", "tool_glob_filenames",
    "bash_stdout", "bash_unattributed", "tool_write_echo",
})

DEFAULT_RUNS_ROOT = "/tmp/atlas-runs"


# ── reading ──────────────────────────────────────────────────────────────────
def read_jsonl(path: str | os.PathLike) -> list[dict]:
    """Every parseable object in a .jsonl. A truncated final line is skipped, not fatal:
    a run killed mid-write must not take the whole rollup down with it."""
    rows: list[dict] = []
    p = Path(path)
    if not p.exists():
        return rows
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def read_json(path: str | os.PathLike, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return default


def job_id_of(job_dir: str | os.PathLike) -> str:
    """job.yaml's job_id, without requiring PyYAML — the runner venv has it, a bare
    system python3 does not, and this module must run under either."""
    jd = Path(job_dir)
    spec = jd / "job.yaml"
    if spec.exists():
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(spec.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict) and data.get("job_id"):
                return str(data["job_id"])
        except Exception:  # noqa: BLE001 — fall through to the line scan
            pass
        for line in spec.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^job_id\s*:\s*(.+?)\s*$", line)
            if m:
                return m.group(1).strip().strip("\"'")
    return jd.name


def iter_run_dirs(job_dir: str | os.PathLike,
                  runs_root: str | os.PathLike | None = None) -> list[Path]:
    """Every directory that looks like a finished run of this job.

    Two roots, because WUR moved run roots out of the job dir (V9: under
    bypassPermissions the agent read a registry sitting three `..` hops above the
    workspace) while ladder runs still live in $JOB_DIR/runs/.
    """
    jd = Path(job_dir).resolve()
    jid = job_id_of(jd)
    roots: list[Path] = [jd / "runs"]
    if runs_root:
        rr = Path(runs_root)
        roots.append(rr / jid)
        roots.append(rr)
    else:
        rr = Path(os.environ.get("ATLAS_RUNS_ROOT", DEFAULT_RUNS_ROOT))
        roots.append(rr / jid)

    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            rp = child.resolve()
            if rp in seen:
                continue
            if not _looks_like_run(child):
                continue
            seen.add(rp)
            out.append(child)
    return out


def _looks_like_run(d: Path) -> bool:
    if any((d / f).exists() for f in SOURCE_FILES.values()):
        return True
    return (d / "run_record.json").exists() or (d / "run_meta.json").exists()


def _identity(run_dir: Path) -> dict[str, Any]:
    """run_id / job_id / task_id / condition_id / rep for rows that omit them.

    Read from run_meta.json first (written before the child starts, so it exists
    even for a run that died) and from run_record.json second.
    """
    meta = read_json(run_dir / "run_meta.json", {}) or {}
    rec = read_json(run_dir / "run_record.json", {}) or {}
    run = rec.get("run") if isinstance(rec.get("run"), dict) else {}
    cond = rec.get("condition") if isinstance(rec.get("condition"), dict) else {}
    meta_run = meta.get("run") if isinstance(meta.get("run"), dict) else {}
    meta_cond = meta.get("condition") if isinstance(meta.get("condition"), dict) else {}

    def pick(*keys: str) -> Any:
        for k in keys:
            for src in (meta, meta_run, meta_cond, run, cond):
                if isinstance(src, dict) and src.get(k) not in (None, ""):
                    return src[k]
        return None

    return {
        "run_id": pick("run_id") or run_dir.name,
        "job_id": pick("job_id", "experiment_id"),
        "task_id": pick("task_id", "task"),
        "condition_id": pick("condition_id", "condition", "arm", "env_id"),
        "rep": pick("rep", "replicate", "replication"),
    }


# ── flattening ───────────────────────────────────────────────────────────────
def _as_json(value: Any) -> str | None:
    if value in (None, [], {}):
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _any_true(values: Iterable[Any]) -> bool | None:
    """True if any value is true; False if any is false and none true; None if all null.
    Used for the mention tiers, where "no slot was adjudicated" is not "no match"."""
    saw_false = False
    for v in values:
        if v is True:
            return True
        if v is False:
            saw_false = True
    return False if saw_false else None


def flatten_fact_trace(row: dict, ident: dict, run_dir: Path) -> dict:
    out = {k: v for k, v in row.items() if k not in ("factors", "extra")}
    for key in ("run_id", "job_id", "task_id", "condition_id", "rep"):
        if out.get(key) in (None, ""):
            out[key] = ident.get(key)
    factors = row.get("factors") if isinstance(row.get("factors"), dict) else {}
    for k in FACTOR_KEYS:
        out[f"factor_{k}"] = factors.get(k)
    out["extra_json"] = _as_json(row.get("extra"))
    out["run_dir"] = str(run_dir)
    return out


def flatten_probe(row: dict, ident: dict, run_dir: Path) -> dict:
    out = {k: v for k, v in row.items()
           if k not in ("slots", "parse_errors", "refusal_markers",
                        "is_probe_turn_seq", "extra")}
    for key in ("run_id", "job_id", "task_id", "condition_id", "rep"):
        if out.get(key) in (None, ""):
            out[key] = ident.get(key)

    slots = row.get("slots") if isinstance(row.get("slots"), list) else []
    for i in range(3):
        slot = slots[i] if i < len(slots) and isinstance(slots[i], dict) else {}
        for k in SLOT_KEYS:
            out[f"slot{i}_{k}"] = slot.get(k)
    out["n_slots"] = len(slots)

    # §4.5 mention ladder, folded from the three slots. PRIMARY = (a) OR (b).
    out["mention_tier_a"] = _any_true(s.get("match_nonce") for s in slots if isinstance(s, dict))
    out["mention_tier_b"] = _any_true(s.get("match_regex") for s in slots if isinstance(s, dict))
    out["mention_llm"] = _any_true(s.get("match_llm") for s in slots if isinstance(s, dict))
    primary = _any_true([out["mention_tier_a"], out["mention_tier_b"]])
    out["mention_primary"] = primary
    out["n_slots_matched"] = sum(
        1 for s in slots
        if isinstance(s, dict) and (s.get("match_nonce") is True or s.get("match_regex") is True)
    )

    classes = [s.get("slot_class") for s in slots if isinstance(s, dict)]
    for cls in SLOT_CLASSES:
        out[f"n_{cls}_slots"] = sum(1 for c in classes if c == cls)

    errs = row.get("parse_errors") if isinstance(row.get("parse_errors"), list) else []
    out["parse_errors_json"] = _as_json(errs)
    out["n_parse_errors"] = len(errs)
    marks = row.get("refusal_markers") if isinstance(row.get("refusal_markers"), list) else []
    out["refusal_markers_json"] = _as_json(marks)
    seqs = row.get("is_probe_turn_seq") if isinstance(row.get("is_probe_turn_seq"), list) else []
    out["probe_turn_seq_json"] = _as_json(seqs)
    out["n_probe_turn_seq"] = len(seqs)
    out["extra_json"] = _as_json(row.get("extra"))
    out["run_dir"] = str(run_dir)
    return out


def flatten_event(row: dict, ident: dict, run_dir: Path) -> dict:
    out = {k: v for k, v in row.items() if k not in ("nonce_hits", "tool_input", "extra")}
    if out.get("run_id") in (None, ""):
        out["run_id"] = ident.get("run_id")
    for key in ("job_id", "task_id", "condition_id", "rep"):
        out.setdefault(key, ident.get(key))

    hits = row.get("nonce_hits") if isinstance(row.get("nonce_hits"), list) else []
    out["nonce_hits_json"] = _as_json(hits)
    out["n_nonce_hits"] = len(hits)
    out["n_inbound_hits"] = sum(
        1 for h in hits
        if isinstance(h, dict) and (
            h.get("inbound") is True
            or (h.get("inbound") is None and h.get("channel") in INBOUND_CHANNELS)
        )
    )
    out["nonce_fact_ids"] = "|".join(
        sorted({str(h.get("fact_id")) for h in hits if isinstance(h, dict) and h.get("fact_id")})
    ) or None
    out["nonce_channels"] = "|".join(
        sorted({str(h.get("channel")) for h in hits if isinstance(h, dict) and h.get("channel")})
    ) or None
    out["tool_input_json"] = _as_json(row.get("tool_input"))
    out["extra_json"] = _as_json(row.get("extra"))
    out["run_dir"] = str(run_dir)
    return out


FLATTENERS = {
    "fact_trace": flatten_fact_trace,
    "probes": flatten_probe,
    "events": flatten_event,
}


# ── collection ───────────────────────────────────────────────────────────────
def collect(job_dir: str | os.PathLike,
            runs_root: str | os.PathLike | None = None,
            run_dirs: Sequence[Path] | None = None,
            tables: Sequence[str] = TABLE_NAMES) -> dict[str, Any]:
    """Read + flatten every table of every run. Pure stdlib, so it is unit-testable
    without the analysis venv."""
    jd = Path(job_dir).resolve()
    dirs = list(run_dirs) if run_dirs is not None else iter_run_dirs(jd, runs_root)
    out: dict[str, list[dict]] = {name: [] for name in tables}
    per_run: list[dict] = []
    skipped: list[dict] = []

    for rd in dirs:
        ident = _identity(rd)
        counts: dict[str, int] = {}
        missing: list[str] = []
        for name in tables:
            src = rd / SOURCE_FILES[name]
            if not src.exists():
                missing.append(name)
                counts[name] = 0
                continue
            rows = read_jsonl(src)
            flat = FLATTENERS[name]
            out[name].extend(flat(r, ident, rd) for r in rows)
            counts[name] = len(rows)
        per_run.append({"run_dir": str(rd), "run_id": ident.get("run_id"),
                        "task_id": ident.get("task_id"),
                        "condition_id": ident.get("condition_id"),
                        "rows": counts, "missing_tables": missing})
        if counts.get("fact_trace", 0) == 0:
            skipped.append({"run_dir": str(rd), "run_id": ident.get("run_id"),
                            "reason": "no fact_trace row"})

    return {
        "job_dir": str(jd),
        "job_id": job_id_of(jd),
        "n_run_dirs": len(dirs),
        "tables": out,
        "per_run": per_run,
        "skipped": skipped,
    }


def accounting_census(fact_trace_rows: Sequence[dict]) -> dict[str, int]:
    """How many rows carry each tokens_accounting_version. More than one generation
    in a job is a POOLING HAZARD: per_line_v1 input is inflated 1.00x-4.90x
    (median 1.50x) against per_message_v2 (V7), so the two must never be summed."""
    census: dict[str, int] = {}
    for r in fact_trace_rows:
        key = r.get("tokens_accounting_version") or "unset"
        census[str(key)] = census.get(str(key), 0) + 1
    return census


# ── writing ──────────────────────────────────────────────────────────────────
def _require_pandas():
    try:
        import pandas as pd  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "aggregate.py needs pandas + pyarrow: they live in requirements-analysis.txt, "
            "not requirements.txt, because the runner must never depend on them.\n"
            "  python3 -m venv .venv-analysis && "
            ".venv-analysis/bin/pip install -r requirements-analysis.txt"
        ) from exc
    import pandas as pd

    return pd


def to_dataframe(rows: Sequence[dict], table: str):
    """A DataFrame with the declared Int64/boolean dtypes applied.

    Doing this at write time rather than at read time is what makes the parquet
    self-describing: `first_exposure_seq` comes back as <NA>, not NaN, so
    `row.first_exposure_seq is pd.NA` still means "d0-push, asserted not scanned".
    """
    pd = _require_pandas()
    df = pd.DataFrame(list(rows))
    if df.empty:
        # Still give the caller the declared columns so downstream `df[col]`
        # does not KeyError on an empty job.
        for col in INT64_COLUMNS.get(table, ()):
            df[col] = pd.Series(dtype="Int64")
        for col in BOOL_COLUMNS.get(table, ()):
            df[col] = pd.Series(dtype="boolean")
        return df
    for col in INT64_COLUMNS.get(table, ()):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    for col in BOOL_COLUMNS.get(table, ()):
        if col in df.columns:
            df[col] = df[col].astype("boolean")
    return df


def write_tables(tables: dict[str, Sequence[dict]], out_dir: str | os.PathLike,
                 *, parquet: bool = True, csv: bool = True) -> dict[str, Any]:
    """Materialize the flattened tables. Returns per-table {rows, cols, files}."""
    od = Path(out_dir)
    od.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    for name in TABLE_NAMES:
        rows = tables.get(name, [])
        df = to_dataframe(rows, name)
        files: list[str] = []
        if parquet:
            target = od / f"{name}.parquet"
            df.to_parquet(target, index=False)
            files.append(str(target))
        if csv:
            target = od / f"{name}.csv"
            df.to_csv(target, index=False)
            files.append(str(target))
        report[name] = {"rows": int(len(df)), "cols": int(len(df.columns)), "files": files}
    return report


def aggregate(job_dir: str | os.PathLike, out_dir: str | os.PathLike | None = None,
              runs_root: str | os.PathLike | None = None, *,
              parquet: bool = True, csv: bool = True) -> dict[str, Any]:
    """Collect + write + manifest. The one call the pipeline and the tests share."""
    jd = Path(job_dir).resolve()
    od = Path(out_dir) if out_dir else jd / "analysis"
    collected = collect(jd, runs_root)
    written = write_tables(collected["tables"], od, parquet=parquet, csv=csv)
    census = accounting_census(collected["tables"].get("fact_trace", []))
    manifest = {
        "aggregate_version": AGGREGATE_VERSION,
        "job_dir": collected["job_dir"],
        "job_id": collected["job_id"],
        "out_dir": str(od),
        "n_run_dirs": collected["n_run_dirs"],
        "n_runs_with_fact_trace": collected["n_run_dirs"] - len(collected["skipped"]),
        "tables": written,
        "tokens_accounting_census": census,
        "mixed_accounting_versions": len([k for k in census if k != "unset"]) > 1,
        "skipped_runs": collected["skipped"],
        "per_run": collected["per_run"],
    }
    (od / "aggregate_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="roll one job's runs up into analysis tables")
    p.add_argument("--job-dir", required=True)
    p.add_argument("--out-dir", default=None,
                   help="default $JOB_DIR/analysis (job-scoped so two jobs cannot "
                        "silently overwrite each other)")
    p.add_argument("--runs-root", default=None,
                   help="default $ATLAS_RUNS_ROOT or /tmp/atlas-runs")
    p.add_argument("--no-parquet", action="store_true")
    p.add_argument("--no-csv", action="store_true")
    p.add_argument("--strict", action="store_true",
                   help="exit 1 on a run with no fact_trace row, or on mixed "
                        "tokens.accounting_version generations")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)

    manifest = aggregate(a.job_dir, a.out_dir, a.runs_root,
                         parquet=not a.no_parquet, csv=not a.no_csv)
    if not a.quiet:
        print(json.dumps(manifest["tables"], indent=2))
        print(f"out_dir: {manifest['out_dir']}", file=sys.stderr)
        print(f"runs: {manifest['n_runs_with_fact_trace']}/{manifest['n_run_dirs']} "
              f"produced a fact_trace row", file=sys.stderr)
    if manifest["mixed_accounting_versions"]:
        print("WARNING: this job mixes tokens.accounting_version generations "
              f"{manifest['tokens_accounting_census']} — per_line_v1 input is inflated "
              "1.00x-4.90x against per_message_v2 (V7). Do not pool token figures.",
              file=sys.stderr)
    rc = 0
    if manifest["tables"]["fact_trace"]["rows"] == 0:
        print("ERROR: no fact_trace rows found — nothing to analyse.", file=sys.stderr)
        rc = 1
    if a.strict and (manifest["skipped_runs"] or manifest["mixed_accounting_versions"]):
        rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
