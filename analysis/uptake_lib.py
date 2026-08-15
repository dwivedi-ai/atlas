#!/usr/bin/env python3
"""
uptake_lib.py — every statistic the Workspace Uptake & Retention experiment reports.

RESPONSIBILITY
  Own the numbers. Each metric and each estimator is exactly one
  function here, importable and
  unit-testable outside Jupyter, so a result can be reproduced without a kernel.
  analysis/uptake.ipynb holds no logic: every cell is a call into this module.

  This module reads tables and returns numbers. It never reads a run dir, never
  parses a stream, and never writes into the repo — lib/wur/aggregate.py owns
  the shape of the input, lib/wur/trace.py owns the meaning of each field.

INPUTS
  <out>/fact_trace.parquet   one (run x fact) row — the headline table
  <out>/probes.parquet       one row per probe (slots flattened by aggregate.py)
  <out>/events.parquet       one row per event
  produced by `python3 lib/wur/aggregate.py --job-dir jobs/<id>`.

OUTPUTS
  tidy pandas DataFrames and small frozen result records (Estimate,
  ClusterResult, RetentionResult, ...), each carrying its own n, CI and method
  string so a figure caption can be written from the object alone.

THE FIVE COMMITMENTS THIS FILE ENCODES
  1. PRIMARY INFERENCE is the cluster-level paired t on per-task risk
     differences. `paired_cluster_t()`. n is the number of TASKS.
  2. CMH and within-task label permutation are SECONDARY and are SUPPRESSED
     when gamma-hat > 0.5: both are anti-conservative under task x arm
     heterogeneity (measured type-I 0.094 -> 0.188 as gamma goes 1.0 -> 2.0),
     which is the regime this design expects. `cmh_test()`,
     `permutation_test_within_task()` return suppressed=True and p=None there.
  3. TASK IS THE UNIT OF GENERALIZATION. Reps sample decoding noise, not
     population: n = 12, never 840. Every cluster function reports n_tasks.
  4. USE IS ALWAYS LIFT over the paired control. `use_lift()` is the reported
     quantity; `use_rate_table()` is the appendix, and it always reports
     use_rate_uncond and use_rate_cond together.
  5. RETENTION IS RMST over a common horizon J, never median half-life — the KM
     median is undefined whenever S(j) > 0.5, which is the expected case.

TWO EXCLUSION RULES THAT ARE NOT NEGOTIABLE AT ANALYSIS TIME
  - `read` is boolean-OR-NULL and null means UNKNOWN (a truncation or the 256 KB
    ceiling hit a call targeting the fact file). Unknown is NEVER coerced to
    false: wide searches truncate and deep facts are found by wide searches, so
    that coercion would make the bias run with the hypothesis. Unknown
    rows leave the denominator and are counted in `n_unknown`; the bounding pair
    rate_if_unknown_false / rate_if_unknown_true brackets what they could do.
  - The alarm metrics (confabulation, unexplained possession) are computed on a
    frame that KEEPS quarantined rows. A control row that names the nonce is the
    confabulation the gate counts; dropping it as "quarantined" would make the
    numerator structurally unmeasurable.

DEPENDENCIES
  pandas, numpy, scipy always; lifelines only inside the retention functions and
  statsmodels only inside the SECONDARY model fits, both imported lazily so a
  missing optional wheel costs one metric rather than the whole module.
  See requirements-analysis.txt.
"""
from __future__ import annotations

import json
import math
import warnings
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy import stats

UPTAKE_LIB_VERSION = "wur-uptake-lib-v1"

# ── the design, as data ─────────────────────────────
#: arm id -> factors. Used only to LABEL rows whose factor_* columns are null
#: (a hand-built frame, an old run); a real run carries its own factors.
ARMS: dict[str, dict[str, Any]] = {
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

#: The pulled-depth ladder. d0-push (pushed) and d1-ptr (pointer) vary a second
#: factor and are contrasted separately, never inside the depth slope.
DEPTH_LADDER = ("d1", "d2", "d3")
DEPTH_INDEX = {"d0": 0, "d1": 1, "d2": 2, "d3": 3}
FORMAT_ARMS = ("d2", "d2-check", "d2-table")
#: (probed, unprobed) pairs for the probe-reactivity contrast.
REACTIVITY_PAIRS = (("d1", "d1-np"), ("d3", "d3-np"))


@dataclass(frozen=True)
class AnalysisConfig:
    """Everything a metric needs to know that is not in the data.

    Pre-registered defaults. Changing one after seeing data is a protocol
    deviation and must be reported as such, which is why the whole config is
    stamped into every result record via `to_dict()`.
    """
    baseline_condition: str = "ctrl"          # D3 primary control: fact-free NOTES.md at d2
    secondary_control: str = "ctrl-nofile"    # D3 secondary: no NOTES.md at all
    read_field: str = "read"                  # PRIMARY (inbound U self_thinking)
    sensitivity_read_field: str = "read_inbound_only"  # mandatory sensitivity row
    exclude_weak_facts: bool = True           # D2: prior_check_status == "weak" out of primary
    exclude_nonanalyzable: bool = True        # analyzable == false is EXCLUDED, never a miss
    exclude_quarantined: bool = True          # ... except in the alarm metrics
    alpha: float = 0.05
    gamma_suppress: float = 0.5               #: suppress CMH/permutation above this
    horizon_at_risk_frac: float = 0.10        # J: >=10% of the arm still at risk
    retention_min_J: int = 3                  # J < 3 probes => descriptive only
    retention_pre_use_only: bool = True       #: strictly before first use
    retention_reference: str | None = None    # None -> shallowest pulled arm present
    min_tasks: int = 2                        # below this a cluster t has no dof
    bootstrap_draws: int = 2000
    rmst_bootstrap_draws: int = 1000
    permutation_draws: int = 2000
    random_seed: int = 20260805

    def with_read_field(self, field_name: str) -> "AnalysisConfig":
        return replace(self, read_field=field_name)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_CONFIG = AnalysisConfig()


# ── loading ──────────────────────────────────────────────────────────────────
@dataclass
class Tables:
    """The three aggregate tables, plus where they came from."""
    fact_trace: pd.DataFrame
    probes: pd.DataFrame
    events: pd.DataFrame
    source: str | None = None
    manifest: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:  # pragma: no cover - convenience
        return (f"Tables(fact_trace={len(self.fact_trace)}, probes={len(self.probes)}, "
                f"events={len(self.events)}, source={self.source!r})")

    @property
    def n_runs(self) -> int:
        return int(self.fact_trace["run_id"].nunique()) if len(self.fact_trace) else 0

    @property
    def n_tasks(self) -> int:
        return int(self.fact_trace["task_id"].nunique()) if len(self.fact_trace) else 0


def load_tables(path: str | Path) -> Tables:
    """Load the three tables from an aggregate output directory.

    Parquet first, CSV second: the CSV mirror exists so a table survives a
    pyarrow/pandas version fight, but it loses the Int64/boolean dtypes, so they
    are restored on load. Anything that reads `first_exposure_seq is <NA>` needs
    that, and the d0-push null assertion IS such a read.
    """
    d = Path(path)
    frames: dict[str, pd.DataFrame] = {}
    for name in ("fact_trace", "probes", "events"):
        pq, csv = d / f"{name}.parquet", d / f"{name}.csv"
        if pq.exists():
            frames[name] = pd.read_parquet(pq)
        elif csv.exists():
            frames[name] = _restore_dtypes(pd.read_csv(csv), name)
        else:
            frames[name] = pd.DataFrame()
    manifest = {}
    mpath = d / "aggregate_manifest.json"
    if mpath.exists():
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
    return Tables(frames["fact_trace"], frames["probes"], frames["events"],
                  source=str(d), manifest=manifest)


def _restore_dtypes(df: pd.DataFrame, table: str) -> pd.DataFrame:
    """CSV round-trip loses nullable dtypes; put them back from aggregate.py's declaration."""
    try:
        import sys as _sys

        lib = Path(__file__).resolve().parent.parent / "lib" / "wur"
        if str(lib) not in _sys.path:
            _sys.path.insert(0, str(lib))
        import aggregate as _agg  # type: ignore

        ints, bools = _agg.INT64_COLUMNS.get(table, ()), _agg.BOOL_COLUMNS.get(table, ())
    except Exception:  # noqa: BLE001 — analysis must not depend on lib/ being importable
        ints, bools = (), ()
    for col in ints:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    for col in bools:
        if col in df.columns:
            df[col] = df[col].map({True: True, False: False, "True": True, "False": False,
                                   1: True, 0: False}).astype("boolean")
    return df


# ── exclusions ───────────────────────────────────────────────────────────────
@dataclass
class ExclusionReport:
    """Why every dropped row was dropped. Printed above every table; a silent
    exclusion is how a funnel study lies to itself."""
    n_in: int = 0
    n_out: int = 0
    dropped: dict[str, int] = field(default_factory=dict)

    def add(self, reason: str, n: int) -> None:
        if n:
            self.dropped[reason] = self.dropped.get(reason, 0) + int(n)

    def to_frame(self) -> pd.DataFrame:
        rows = [{"reason": k, "n_dropped": v} for k, v in sorted(self.dropped.items())]
        rows.append({"reason": "_kept", "n_dropped": self.n_out})
        return pd.DataFrame(rows)

    def to_dict(self) -> dict[str, Any]:
        return {"n_in": self.n_in, "n_out": self.n_out, "dropped": dict(self.dropped)}


def analysis_frame(source: Tables | pd.DataFrame,
                   cfg: AnalysisConfig = DEFAULT_CONFIG) -> tuple[pd.DataFrame, ExclusionReport]:
    """The primary-analysis frame: analyzable, un-quarantined, weak facts removed.

    Pre-registered: facts firing 1/12 in control are ADMITTED to the suite
    and EXCLUDED from primary. The exclusion is decided before treatment data
    exists, which is what removes the forking-paths risk — so it lives in the
    default config, not in a notebook cell.
    """
    df = source.fact_trace if isinstance(source, Tables) else source
    df = df.copy()
    rep = ExclusionReport(n_in=len(df))
    if cfg.exclude_nonanalyzable and "analyzable" in df.columns:
        bad = df["analyzable"].fillna(False).astype(bool) == False  # noqa: E712
        rep.add("not_analyzable", int(bad.sum()))
        if "exclusion_reason" in df.columns:
            for reason, n in df.loc[bad, "exclusion_reason"].fillna("unspecified").value_counts().items():
                rep.add(f"not_analyzable:{reason}", int(n))
                rep.dropped["not_analyzable"] -= int(n)
            if rep.dropped.get("not_analyzable", 0) <= 0:
                rep.dropped.pop("not_analyzable", None)
        df = df[~bad]
    if cfg.exclude_quarantined and "quarantined" in df.columns:
        q = df["quarantined"].fillna(False).astype(bool)
        rep.add("quarantined", int(q.sum()))
        df = df[~q]
    if cfg.exclude_weak_facts and "prior_check_status" in df.columns:
        weak = df["prior_check_status"].astype("string").fillna("") == "weak"
        rep.add("weak_fact_prior_check", int(weak.sum()))
        df = df[~weak]
    rep.n_out = len(df)
    return df.reset_index(drop=True), rep


def alarm_frame(source: Tables | pd.DataFrame,
                cfg: AnalysisConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """The frame the ALARMS are computed on: analyzable rows, quarantine KEPT.

    A control run whose nonce shows up is quarantined by construction (the
    fact_trace schema will not accept it otherwise). It is also the entire
    numerator of confab_rate. Dropping it would make the pilot gate report 0.00
    on exactly the failure it exists to catch.
    """
    df = source.fact_trace if isinstance(source, Tables) else source
    df = df.copy()
    if cfg.exclude_nonanalyzable and "analyzable" in df.columns:
        df = df[df["analyzable"].fillna(False).astype(bool)]
    return df.reset_index(drop=True)


# ── small statistical primitives ─────────────────────────────────────────────
def wilson_ci(k: int, n: int, alpha: float = 0.05) -> tuple[float | None, float | None]:
    """Wilson score interval. Used everywhere a binomial proportion is reported —
    it is the interval the prior-check gate is stated in (0/12 -> upper 0.265)."""
    if n <= 0:
        return (None, None)
    z = float(stats.norm.ppf(1 - alpha / 2))
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(max(p * (1 - p) / n + z * z / (4 * n * n), 0.0)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class Estimate:
    """A number with everything needed to quote it honestly."""
    name: str
    value: float | None
    ci_low: float | None = None
    ci_high: float | None = None
    se: float | None = None
    n: int | None = None
    n_tasks: int | None = None
    p_value: float | None = None
    method: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __str__(self) -> str:  # pragma: no cover - convenience
        v = "None" if self.value is None else f"{self.value:.4f}"
        ci = "" if self.ci_low is None else f" [{self.ci_low:.4f}, {self.ci_high:.4f}]"
        p = "" if self.p_value is None else f" p={self.p_value:.4g}"
        return f"{self.name}={v}{ci}{p} (n_tasks={self.n_tasks}, {self.method})"


def _bool_series(df: pd.DataFrame, col: str) -> pd.Series:
    """A nullable boolean column as object-of-{True,False,None}. Missing column -> all null."""
    if col not in df.columns:
        return pd.Series([pd.NA] * len(df), index=df.index, dtype="boolean")
    return df[col].astype("boolean")


# ── rates ────────────────────────────────────────────────────────────────────
def rate_by_condition(df: pd.DataFrame, outcome: str,
                      cfg: AnalysisConfig = DEFAULT_CONFIG,
                      *, denominator: str | None = None,
                      group: str = "condition_id") -> pd.DataFrame:
    """P(outcome) per arm, with Wilson CIs and the unknown bracket.

    `denominator`, when given, is a boolean column that must be True for a row to
    enter the denominator (e.g. `eligible` for use_rate_cond, `read` for the
    mention rate). Null outcomes leave the denominator and are reported as
    n_unknown, plus the bracketing pair that says what they could have done.
    """
    rows: list[dict[str, Any]] = []
    if not len(df):
        return pd.DataFrame(columns=["condition_id", "n", "k", "rate", "ci_low", "ci_high",
                                     "n_unknown", "rate_if_unknown_false",
                                     "rate_if_unknown_true", "trust"])
    for arm, sub in df.groupby(group, dropna=False):
        if denominator:
            sub = sub[_bool_series(sub, denominator).fillna(False).astype(bool)]
        vals = _bool_series(sub, outcome)
        n_unknown = int(vals.isna().sum())
        known = vals.dropna()
        n, k = int(len(known)), int(known.sum())
        lo, hi = wilson_ci(k, n, cfg.alpha)
        n_all = n + n_unknown
        rows.append({
            group: arm,
            "n": n, "k": k,
            "rate": (k / n) if n else None,
            "ci_low": lo, "ci_high": hi,
            "n_unknown": n_unknown,
            "rate_if_unknown_false": (k / n_all) if n_all else None,
            "rate_if_unknown_true": ((k + n_unknown) / n_all) if n_all else None,
            "trust": _trust_of(sub),
        })
    return pd.DataFrame(rows).sort_values(group).reset_index(drop=True)


def _trust_of(sub: pd.DataFrame) -> str:
    """MEASURED vs ASSERTED. d0-push's exposure is asserted by the
    autoload canary because auto-loaded content appears in NO log; pooling it with
    measured exposure without saying so is the main way to draw a wrong conclusion."""
    if "exposure_basis" not in sub.columns or not len(sub):
        return "measured"
    basis = set(sub["exposure_basis"].dropna().astype(str))
    if basis == {"manifest_canary"}:
        return "asserted"
    if "manifest_canary" in basis:
        return "mixed"
    return "measured"


def per_task_rates(df: pd.DataFrame, outcome: str, *,
                   denominator: str | None = None,
                   group: str = "condition_id") -> pd.DataFrame:
    """Per (task, arm) rate — the input to every cluster-level statistic.

    Reps sample decoding noise; this is the step that turns 840 runs into 12
    independent units of generalization.
    """
    if not len(df):
        return pd.DataFrame(columns=["task_id", group, "n", "k", "rate", "n_unknown"])
    rows: list[dict[str, Any]] = []
    for (task, arm), sub in df.groupby(["task_id", group], dropna=False):
        if denominator:
            sub = sub[_bool_series(sub, denominator).fillna(False).astype(bool)]
        vals = _bool_series(sub, outcome)
        known = vals.dropna()
        n, k = int(len(known)), int(known.sum())
        rows.append({"task_id": task, group: arm, "n": n, "k": k,
                     "rate": (k / n) if n else np.nan,
                     "n_unknown": int(vals.isna().sum())})
    return pd.DataFrame(rows).sort_values(["task_id", group]).reset_index(drop=True)


# ── PRIMARY inference: cluster-level paired t ────────────────────────────────
@dataclass(frozen=True)
class ClusterResult:
    """The primary inferential object: a paired t over per-task risk differences."""
    arm: str
    control: str
    outcome: str
    n_tasks: int
    mean_diff: float | None
    se: float | None
    t_stat: float | None
    dof: int | None
    p_value: float | None
    ci_low: float | None
    ci_high: float | None
    per_task: pd.DataFrame = field(default_factory=pd.DataFrame, repr=False)
    method: str = "cluster-level paired t on per-task risk differences"
    note: str = ""

    def to_estimate(self) -> Estimate:
        return Estimate(name=f"lift[{self.outcome}] {self.arm}-{self.control}",
                        value=self.mean_diff, ci_low=self.ci_low, ci_high=self.ci_high,
                        se=self.se, n=int(self.per_task["n_arm"].sum()) if len(self.per_task) else None,
                        n_tasks=self.n_tasks, p_value=self.p_value, method=self.method,
                        detail={"t": self.t_stat, "dof": self.dof, "note": self.note})

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["per_task"] = self.per_task.to_dict(orient="records") if len(self.per_task) else []
        return d


def paired_cluster_t(df: pd.DataFrame, arm: str, control: str, outcome: str,
                     cfg: AnalysisConfig = DEFAULT_CONFIG, *,
                     denominator: str | None = None) -> ClusterResult:
    """THE PRIMARY TEST.

    lambda_{a,t} = mean_k outcome[a,t,k] - mean_k outcome[ctrl,t,k]
    lambda_a     = mean_t lambda_{a,t}
    tested by a one-sample t over the T task-level differences, so the standard
    error is estimated from between-task variation and nothing has to assume the
    reps are exchangeable across tasks. Only tasks present in BOTH arms count,
    which is what makes it paired.
    """
    rates = per_task_rates(df, outcome, denominator=denominator)
    a = rates[rates["condition_id"] == arm].set_index("task_id")
    c = rates[rates["condition_id"] == control].set_index("task_id")
    tasks = sorted(set(a.index) & set(c.index))
    tasks = [t for t in tasks if a.loc[t, "n"] > 0 and c.loc[t, "n"] > 0]
    per_task = pd.DataFrame({
        "task_id": tasks,
        "rate_arm": [float(a.loc[t, "rate"]) for t in tasks],
        "rate_ctrl": [float(c.loc[t, "rate"]) for t in tasks],
        "n_arm": [int(a.loc[t, "n"]) for t in tasks],
        "n_ctrl": [int(c.loc[t, "n"]) for t in tasks],
    })
    if len(per_task):
        per_task["diff"] = per_task["rate_arm"] - per_task["rate_ctrl"]
    n = len(per_task)
    if n < cfg.min_tasks:
        return ClusterResult(arm, control, outcome, n, None, None, None, None, None, None, None,
                             per_task, note=f"n_tasks={n} < min_tasks={cfg.min_tasks}")
    d = per_task["diff"].to_numpy(dtype=float)
    mean = float(np.mean(d))
    sd = float(np.std(d, ddof=1))
    se = sd / math.sqrt(n)
    dof = n - 1
    if se == 0:
        # Every task moved identically. A t is undefined; say so instead of
        # emitting p=0, which is the classic way a degenerate cell becomes a claim.
        return ClusterResult(arm, control, outcome, n, mean, 0.0, None, dof, None, mean, mean,
                             per_task, note="zero between-task variance; t undefined")
    t_stat = mean / se
    p = float(2 * stats.t.sf(abs(t_stat), dof))
    crit = float(stats.t.ppf(1 - cfg.alpha / 2, dof))
    return ClusterResult(arm, control, outcome, n, mean, se, t_stat, dof, p,
                         mean - crit * se, mean + crit * se, per_task)


def cluster_bootstrap_ci(values: Sequence[float], cfg: AnalysisConfig = DEFAULT_CONFIG,
                         *, statistic=np.mean) -> tuple[float | None, float | None]:
    """Percentile bootstrap resampling TASKS (never runs). Reported alongside the
    t interval wherever T is small enough that normality is a stretch."""
    v = np.asarray([x for x in values if x is not None and not (isinstance(x, float) and math.isnan(x))],
                   dtype=float)
    if v.size < 2:
        return (None, None)
    rng = np.random.default_rng(cfg.random_seed)
    draws = rng.integers(0, v.size, size=(cfg.bootstrap_draws, v.size))
    boot = statistic(v[draws], axis=1)
    return (float(np.quantile(boot, cfg.alpha / 2)), float(np.quantile(boot, 1 - cfg.alpha / 2)))



# ── heterogeneity, and the SECONDARY tests it suppresses ─────────────────────
def gamma_hat(df: pd.DataFrame, arm: str, control: str, outcome: str) -> Estimate:
    """Task x arm heterogeneity on the logit scale — the quantity that decides
    whether the secondary tests may be reported at all.

    DerSimonian-Laird moment estimator of tau over per-task log odds ratios with a
    0.5 continuity correction. gamma_hat = sqrt(tau^2). suppresses CMH and the
    permutation test above 0.5 because their type-I error was measured to run
    0.094 -> 0.188 as gamma goes 1.0 -> 2.0.
    """
    psi, var = [], []
    for task, sub in df.groupby("task_id", dropna=False):
        a_sub = sub[sub["condition_id"] == arm]
        c_sub = sub[sub["condition_id"] == control]
        av, cv = _bool_series(a_sub, outcome).dropna(), _bool_series(c_sub, outcome).dropna()
        if not len(av) or not len(cv):
            continue
        a, b = float(av.sum()) + 0.5, float(len(av) - av.sum()) + 0.5
        c, d = float(cv.sum()) + 0.5, float(len(cv) - cv.sum()) + 0.5
        psi.append(math.log((a * d) / (b * c)))
        var.append(1 / a + 1 / b + 1 / c + 1 / d)
    k = len(psi)
    if k < 2:
        return Estimate("gamma_hat", None, n_tasks=k, method="DerSimonian-Laird (insufficient tasks)")
    psi_a, var_a = np.asarray(psi), np.asarray(var)
    w = 1.0 / var_a
    psi_bar = float(np.sum(w * psi_a) / np.sum(w))
    Q = float(np.sum(w * (psi_a - psi_bar) ** 2))
    denom = float(np.sum(w) - np.sum(w ** 2) / np.sum(w))
    tau2 = max(0.0, (Q - (k - 1)) / denom) if denom > 0 else 0.0
    return Estimate("gamma_hat", math.sqrt(tau2), n_tasks=k,
                    method="DerSimonian-Laird tau over per-task log odds ratios",
                    detail={"Q": Q, "tau2": tau2, "psi_bar": psi_bar,
                            "I2": max(0.0, (Q - (k - 1)) / Q) if Q > 0 else 0.0})


@dataclass(frozen=True)
class SecondaryResult:
    name: str
    statistic: float | None
    p_value: float | None
    n_tasks: int
    suppressed: bool
    gamma_hat: float | None
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)




def _strata(df: pd.DataFrame, arm: str, control: str, outcome: str) -> list[tuple[int, int, int, int]]:
    """Per-task 2x2 (a=arm events, b=arm non-events, c=ctrl events, d=ctrl non-events)."""
    out = []
    for task, sub in df.groupby("task_id", dropna=False):
        av = _bool_series(sub[sub["condition_id"] == arm], outcome).dropna()
        cv = _bool_series(sub[sub["condition_id"] == control], outcome).dropna()
        if not len(av) or not len(cv):
            continue
        out.append((int(av.sum()), int(len(av) - av.sum()),
                    int(cv.sum()), int(len(cv) - cv.sum())))
    return out


def cmh_test(df: pd.DataFrame, arm: str, control: str, outcome: str,
             cfg: AnalysisConfig = DEFAULT_CONFIG, *,
             suppress: bool = True, correct: bool = True) -> SecondaryResult:
    """Cochran-Mantel-Haenszel over task strata. SECONDARY, and a sharp-null test.

    It conditions on the stratum margins, so it answers "is there ANY effect in
    ANY task", not "does the average task move" — and under task x arm
    heterogeneity it rejects far more often than nominal. Hence the suppression
    rule: above gamma_hat 0.5 the p-value is not reported at all, rather
    than being reported with a caveat nobody reads.
    """
    g = gamma_hat(df, arm, control, outcome)
    strata = _strata(df, arm, control, outcome)
    k = len(strata)
    if suppress and g.value is not None and g.value > cfg.gamma_suppress:
        return SecondaryResult("cmh", None, None, k, True, g.value,
                               reason=f"gamma_hat={g.value:.3f} > {cfg.gamma_suppress}; "
                                      "CMH is anti-conservative under task x arm heterogeneity")
    if k < 1:
        return SecondaryResult("cmh", None, None, 0, False, g.value, reason="no shared strata")
    num = den = 0.0
    or_num = or_den = 0.0
    for a, b, c, d in strata:
        n = a + b + c + d
        if n < 2:
            continue
        num += a - (a + b) * (a + c) / n
        den += (a + b) * (c + d) * (a + c) * (b + d) / (n * n * (n - 1))
        or_num += a * d / n
        or_den += b * c / n
    if den <= 0:
        return SecondaryResult("cmh", None, None, k, False, g.value,
                               reason="zero variance across strata")
    adj = 0.5 if correct else 0.0
    chi2 = (abs(num) - adj) ** 2 / den if abs(num) > adj else 0.0
    p = float(stats.chi2.sf(chi2, 1))
    return SecondaryResult("cmh", float(chi2), p, k, False, g.value,
                           detail={"mh_odds_ratio": (or_num / or_den) if or_den > 0 else None,
                                   "continuity_correction": correct})


def permutation_test_within_task(df: pd.DataFrame, arm: str, control: str, outcome: str,
                                 cfg: AnalysisConfig = DEFAULT_CONFIG, *,
                                 suppress: bool = True,
                                 draws: int | None = None) -> SecondaryResult:
    """Within-task label permutation. SECONDARY, same suppression rule as CMH.

    Permuting arm labels WITHIN a task holds the task fixed, which is exactly why
    it cannot see between-task variance in the effect: it tests the sharp null
    "this run's outcome would have been identical under the other label", and it
    is anti-conservative for the population claim the study actually makes.
    """
    g = gamma_hat(df, arm, control, outcome)
    B = int(draws if draws is not None else cfg.permutation_draws)
    obs = paired_cluster_t(df, arm, control, outcome, cfg)
    k = obs.n_tasks
    if suppress and g.value is not None and g.value > cfg.gamma_suppress:
        return SecondaryResult("permutation_within_task", obs.mean_diff, None, k, True, g.value,
                               reason=f"gamma_hat={g.value:.3f} > {cfg.gamma_suppress}; "
                                      "within-task permutation is anti-conservative here")
    if obs.mean_diff is None:
        return SecondaryResult("permutation_within_task", None, None, k, False, g.value,
                               reason=obs.note or "no estimate")
    pools: list[tuple[np.ndarray, int, int]] = []
    for task, sub in df.groupby("task_id", dropna=False):
        av = _bool_series(sub[sub["condition_id"] == arm], outcome).dropna()
        cv = _bool_series(sub[sub["condition_id"] == control], outcome).dropna()
        if not len(av) or not len(cv):
            continue
        pools.append((np.concatenate([av.to_numpy(dtype=float), cv.to_numpy(dtype=float)]),
                      len(av), len(cv)))
    if not pools:
        return SecondaryResult("permutation_within_task", obs.mean_diff, None, 0, False, g.value,
                               reason="no shared strata")
    rng = np.random.default_rng(cfg.random_seed)
    stat_obs = abs(float(obs.mean_diff))
    hits = 0
    for _ in range(B):
        diffs = []
        for pool, na, nc in pools:
            perm = rng.permutation(pool)
            diffs.append(perm[:na].mean() - perm[na:na + nc].mean())
        if abs(float(np.mean(diffs))) >= stat_obs - 1e-12:
            hits += 1
    p = (1.0 + hits) / (1.0 + B)
    return SecondaryResult("permutation_within_task", float(obs.mean_diff), float(p), k, False,
                           g.value, detail={"draws": B})


# ── metric 1: read rate (+ the mandatory sensitivity) ────────────────────────
def read_rate_table(source: Tables | pd.DataFrame,
                    cfg: AnalysisConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """P(nonce entered context) per arm, PRIMARY and sensitivity in one frame.

    definition="read"              PRIMARY (inbound channels U self_thinking)
    definition="read_inbound_only" the pre-D4 definition — a MANDATORY row in
                                   every table where read is a denominator, so it is emitted here rather than
                                   left to a caller to remember.
    """
    df, _ = analysis_frame(source, cfg) if not isinstance(source, pd.DataFrame) else (source, None)
    out = []
    for definition in (cfg.read_field, cfg.sensitivity_read_field):
        t = rate_by_condition(df, definition, cfg)
        t.insert(1, "definition", definition)
        out.append(t)
    return pd.concat(out, ignore_index=True)


def incidental_exposure(source: Tables | pd.DataFrame,
                        cfg: AnalysisConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """read - opened, per arm: did the agent FIND the fact, or stumble into it?

    Reported two ways because they answer different questions:
      incidental_rate  = P(read) - P(opened)            (the definition)
      incidental_share = P(not opened | read)           (of the runs that saw it,
                                                         how many never opened it)
    """
    df, _ = analysis_frame(source, cfg) if not isinstance(source, pd.DataFrame) else (source, None)
    rows = []
    for arm, sub in df.groupby("condition_id", dropna=False):
        r = _bool_series(sub, cfg.read_field)
        o = _bool_series(sub, "opened")
        n_r = int(r.notna().sum())
        n_o = int(o.notna().sum())
        p_r = float(r.dropna().mean()) if n_r else None
        p_o = float(o.dropna().mean()) if n_o else None
        both = sub[r.fillna(False).astype(bool)]
        ob = _bool_series(both, "opened")
        n_share = int(ob.notna().sum())
        k_share = int((ob.dropna() == False).sum())  # noqa: E712
        lo, hi = wilson_ci(k_share, n_share, cfg.alpha)
        rows.append({"condition_id": arm, "n": len(sub),
                     "read_rate": p_r, "opened_rate": p_o,
                     "incidental_rate": (p_r - p_o) if (p_r is not None and p_o is not None) else None,
                     "n_read": n_share, "k_read_not_opened": k_share,
                     "incidental_share": (k_share / n_share) if n_share else None,
                     "share_ci_low": lo, "share_ci_high": hi})
    return pd.DataFrame(rows).sort_values("condition_id").reset_index(drop=True)


# ── metric 2: mention rate ───────────────────────────────────────────────────
def mention_rate(source: Tables | pd.DataFrame,
                 cfg: AnalysisConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """P(named in >= 1 probe | exposed), per arm — with the unconditional rate beside it.

    The conditional form is the defined one, and the one that answers
    "does the agent hold it in reportable state"; the unconditional form is what a
    reader will otherwise compute wrongly from the funnel.
    """
    df, _ = analysis_frame(source, cfg) if not isinstance(source, pd.DataFrame) else (source, None)
    df = df[_bool_series(df, "factor_probe").fillna(True).astype(bool)]  # no-probe arms cannot mention
    cond = rate_by_condition(df, "ever_mention", cfg, denominator=cfg.read_field)
    cond = cond.rename(columns={"n": "n_exposed", "k": "k_mentioned", "rate": "mention_rate_cond",
                                "ci_low": "cond_ci_low", "ci_high": "cond_ci_high"})
    uncond = rate_by_condition(df, "ever_mention", cfg)[["condition_id", "n", "k", "rate"]]
    uncond = uncond.rename(columns={"n": "n_all", "k": "k_all", "rate": "mention_rate_uncond"})
    keep = ["condition_id", "n_exposed", "k_mentioned", "mention_rate_cond",
            "cond_ci_low", "cond_ci_high", "trust"]
    return cond[keep].merge(uncond, on="condition_id", how="outer")


# ── metric 3: use (lift over the paired control) ─────────────────────────────
def use_rate_table(source: Tables | pd.DataFrame,
                   cfg: AnalysisConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """APPENDIX ONLY: the raw use rate per arm.

    use_rate_uncond = fired / N and use_rate_cond = fired / eligible are emitted
    in one frame because requires them to be reported together — `eligible`
    is not decoration, and a run that never created the site the mandate applies
    to is CENSORED, not evidence of non-use.
    """
    df, _ = analysis_frame(source, cfg) if not isinstance(source, pd.DataFrame) else (source, None)
    uncond = rate_by_condition(df, "used", cfg).rename(
        columns={"n": "n_uncond", "k": "k_uncond", "rate": "use_rate_uncond",
                 "ci_low": "uncond_ci_low", "ci_high": "uncond_ci_high"})
    cond = rate_by_condition(df, "used", cfg, denominator="eligible").rename(
        columns={"n": "n_eligible", "k": "k_cond", "rate": "use_rate_cond",
                 "ci_low": "cond_ci_low", "ci_high": "cond_ci_high"})
    cols_u = ["condition_id", "n_uncond", "k_uncond", "use_rate_uncond",
              "uncond_ci_low", "uncond_ci_high"]
    cols_c = ["condition_id", "n_eligible", "k_cond", "use_rate_cond",
              "cond_ci_low", "cond_ci_high"]
    out = uncond[cols_u].merge(cond[cols_c], on="condition_id", how="outer")
    out["eligibility_rate"] = out["n_eligible"] / out["n_uncond"].replace(0, np.nan)
    out["appendix_only"] = True
    return out.sort_values("condition_id").reset_index(drop=True)


def use_lift(source: Tables | pd.DataFrame, cfg: AnalysisConfig = DEFAULT_CONFIG, *,
             arms: Sequence[str] | None = None, control: str | None = None,
             outcome: str = "used",
             conditional: bool = False) -> pd.DataFrame:
    """THE REPORTED USE QUANTITY: lift over the paired control.

    One row per arm: lambda_a with its cluster-level paired t, its bootstrap
    interval over tasks, and — because demands they travel together — the
    conditional version is available by passing conditional=True (denominator
    `eligible`). n is n_tasks, and it says so in the frame.
    """
    df, _ = analysis_frame(source, cfg) if not isinstance(source, pd.DataFrame) else (source, None)
    ctrl = control or cfg.baseline_condition
    all_arms = arms if arms is not None else [a for a in sorted(df["condition_id"].dropna().unique())
                                              if a != ctrl]
    denom = "eligible" if conditional else None
    rows = []
    for arm in all_arms:
        res = paired_cluster_t(df, arm, ctrl, outcome, cfg, denominator=denom)
        blo, bhi = cluster_bootstrap_ci(res.per_task["diff"].tolist() if len(res.per_task) else [], cfg)
        g = gamma_hat(df, arm, ctrl, outcome)
        rows.append({"condition_id": arm, "control": ctrl, "outcome": outcome,
                     "conditional_on_eligible": conditional,
                     "n_tasks": res.n_tasks, "lift": res.mean_diff, "se": res.se,
                     "t": res.t_stat, "dof": res.dof, "p_value": res.p_value,
                     "ci_low": res.ci_low, "ci_high": res.ci_high,
                     "boot_ci_low": blo, "boot_ci_high": bhi,
                     "gamma_hat": g.value, "note": res.note})
    return pd.DataFrame(rows)


# ── metric 4: retention as RMST over a common horizon ────────────────────────
@dataclass(frozen=True)
class RetentionResult:
    horizon_J: float | None
    per_arm: pd.DataFrame
    dataset: pd.DataFrame
    exploratory: bool
    note: str = ""
    method: str = "RMST over common horizon J (lifelines.utils.restricted_mean_survival_time)"

    def to_dict(self) -> dict[str, Any]:
        return {"horizon_J": self.horizon_J, "exploratory": self.exploratory,
                "note": self.note, "method": self.method,
                "per_arm": self.per_arm.to_dict(orient="records")}


def retention_dataset(source: Tables | pd.DataFrame,
                      cfg: AnalysisConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """The survival dataset, in PROBE INDEX units, never wall clock.

    Defined on {ever_mention = 1} only; a fact never mentioned is EXCLUDED and
    fully accounted for by the mention rate. Three censoring dispositions:
      administrative   the run completed while still mentioning -> censored at B_r
      truncated_run    the run timed out / errored -> censored at the last observed probe
      post_discharge   primary retention is restricted to probes STRICTLY BEFORE
                       first use; after the agent has discharged a fact, dropping
                       it is correct behaviour, not forgetting. Those rows are
                       censored at the pre-use horizon and the post-first-use
                       period is reported separately.
    """
    df, _ = analysis_frame(source, cfg) if not isinstance(source, pd.DataFrame) else (source, None)
    rows = []
    for _, r in df.iterrows():
        if not bool(_scalar_bool(r.get("ever_mention"))):
            continue
        B = _scalar_int(r.get("at_risk_horizon"))
        lapse = _scalar_int(r.get("lapse_probe_index"))
        i0 = _scalar_int(r.get("first_mention_probe"))
        first_use = _scalar_int(r.get("first_use_probe_index"))
        reason = r.get("censoring_reason")
        if lapse is not None:
            duration, event = float(lapse), 1
            reason = None
        elif B is not None:
            duration, event = float(B), 0
            reason = reason if isinstance(reason, str) and reason else "administrative"
        else:
            continue
        pre_use_horizon = None
        if cfg.retention_pre_use_only and first_use is not None and i0 is not None:
            pre_use_horizon = float(first_use - i0)
            if pre_use_horizon < 0:
                continue  # discharged before it was ever named: nothing at risk
            if duration > pre_use_horizon:
                duration, event, reason = pre_use_horizon, 0, "post_discharge"
        if duration < 0:
            continue
        rows.append({"run_id": r.get("run_id"), "task_id": r.get("task_id"),
                     "condition_id": r.get("condition_id"), "rep": r.get("rep"),
                     "duration": duration, "event": int(event),
                     "censoring_reason": reason,
                     "at_risk_horizon": B, "first_mention_probe": i0,
                     "first_use_probe_index": first_use,
                     "pre_use_horizon": pre_use_horizon,
                     "n_reinjections": _scalar_int(r.get("n_reinjections"))})
    return pd.DataFrame(rows)


def _scalar_bool(v: Any) -> bool | None:
    if v is None or v is pd.NA or (isinstance(v, float) and math.isnan(v)):
        return None
    return bool(v)


def _scalar_int(v: Any) -> int | None:
    if v is None or v is pd.NA or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def common_horizon(dataset: pd.DataFrame, cfg: AnalysisConfig = DEFAULT_CONFIG,
                   *, arms: Sequence[str] | None = None) -> tuple[float | None, pd.DataFrame]:
    """J = min over arms of the largest time at which >= 10% of that arm's retention
    subset is still at risk. Returns (J, per-arm detail)."""
    if not len(dataset):
        return None, pd.DataFrame(columns=["condition_id", "n", "J_arm"])
    use = dataset if arms is None else dataset[dataset["condition_id"].isin(list(arms))]
    rows = []
    for arm, sub in use.groupby("condition_id", dropna=False):
        n = len(sub)
        d = sub["duration"].to_numpy(dtype=float)
        grid = np.unique(np.concatenate([[0.0], d]))
        J_arm = 0.0
        for u in grid:
            at_risk = float((d >= u).sum())
            if n and at_risk / n >= cfg.horizon_at_risk_frac:
                J_arm = float(u)
        rows.append({"condition_id": arm, "n": n, "J_arm": J_arm})
    detail = pd.DataFrame(rows).sort_values("condition_id").reset_index(drop=True)
    J = float(detail["J_arm"].min()) if len(detail) else None
    return J, detail


def _km_rmst(durations: np.ndarray, events: np.ndarray, horizon: float) -> float:
    """Kaplan-Meier RMST in numpy: the same integral lifelines computes, fast enough
    to bootstrap. tests/test_wur_lib.py asserts it equals
    lifelines.utils.restricted_mean_survival_time to 1e-9 — lifelines owns the
    definition, this owns the resampling loop.
    """
    d = np.asarray(durations, dtype=float)
    e = np.asarray(events, dtype=int)
    if d.size == 0:
        return float("nan")
    times = np.unique(d[e == 1])
    times = times[times <= horizon]
    area, surv, prev = 0.0, 1.0, 0.0
    for t in times:
        at_risk = float((d >= t).sum())
        if at_risk <= 0:
            break
        area += surv * (t - prev)
        surv *= 1.0 - float(((d == t) & (e == 1)).sum()) / at_risk
        prev = float(t)
    area += surv * (horizon - prev)
    return float(area)


def rmst_by_condition(dataset: pd.DataFrame, horizon: float,
                      cfg: AnalysisConfig = DEFAULT_CONFIG, *,
                      bootstrap: bool = True) -> pd.DataFrame:
    """RMST_a(J) = integral_0^J S_a(u) du, per arm, via lifelines' KM fit.

    NOT the median. The KM median is undefined whenever S(j) > 0.5 across the
    observed support, which is the expected case here — asking for it is
    how a retention table ends up full of NaN and gets quietly dropped.

    THE INTERVAL IS A CLUSTER BOOTSTRAP OVER TASKS, NOT lifelines' variance.
    `restricted_mean_survival_time(..., return_variance=True)` returns
    E[T^2] - E[T]^2, the variance of the survival-time DISTRIBUTION, not the
    sampling variance of the estimator: measured here at sqrt(var) = 2.48 against
    a bootstrap SD of 0.16 on the same 200-observation fit. Using it as a standard
    error inflates every retention interval by ~15x. It is reported below as
    `sd_survival_time`, under its real name, and never as `se`.
    """
    from lifelines import KaplanMeierFitter
    from lifelines.utils import restricted_mean_survival_time

    rng = np.random.default_rng(cfg.random_seed)
    rows = []
    for arm, sub in dataset.groupby("condition_id", dropna=False):
        dur = sub["duration"].to_numpy(dtype=float)
        ev = sub["event"].to_numpy(dtype=int)
        kmf = KaplanMeierFitter()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            kmf.fit(dur, ev, label=str(arm))
            value = float(restricted_mean_survival_time(kmf, t=horizon))
            try:
                _, dist_var = restricted_mean_survival_time(kmf, t=horizon, return_variance=True)
            except Exception:  # noqa: BLE001 — signature drift across lifelines versions
                dist_var = float("nan")
        se = lo = hi = None
        if bootstrap:
            boot = _cluster_bootstrap_rmst(sub, horizon, cfg, rng)
            if boot.size:
                se = float(np.std(boot, ddof=1))
                lo = float(np.quantile(boot, cfg.alpha / 2))
                hi = float(np.quantile(boot, 1 - cfg.alpha / 2))
        rows.append({"condition_id": arm, "n": int(len(sub)),
                     "n_tasks": int(sub["task_id"].nunique()),
                     "n_events": int(ev.sum()),
                     "n_censored": int((ev == 0).sum()),
                     "rmst": value, "se": se, "ci_low": lo, "ci_high": hi,
                     "sd_survival_time": math.sqrt(dist_var) if dist_var == dist_var and dist_var >= 0 else None,
                     "median_probes_at_risk": float(np.median(dur))})
    return pd.DataFrame(rows).sort_values("condition_id").reset_index(drop=True)


def _cluster_bootstrap_rmst(sub: pd.DataFrame, horizon: float, cfg: AnalysisConfig,
                            rng: np.random.Generator) -> np.ndarray:
    """Resample TASKS with replacement, refit, recompute RMST. Task is the unit of
    generalization here exactly as it is everywhere else in this file."""
    groups = [g for _, g in sub.groupby("task_id", dropna=False)]
    if len(groups) < 2:
        return np.asarray([])
    out = np.empty(cfg.rmst_bootstrap_draws, dtype=float)
    for b in range(cfg.rmst_bootstrap_draws):
        pick = rng.integers(0, len(groups), len(groups))
        d = np.concatenate([groups[i]["duration"].to_numpy(dtype=float) for i in pick])
        e = np.concatenate([groups[i]["event"].to_numpy(dtype=int) for i in pick])
        out[b] = _km_rmst(d, e, horizon)
    return out[~np.isnan(out)]


def rmst_delta(dataset: pd.DataFrame, horizon: float, arm: str, reference: str,
               cfg: AnalysisConfig = DEFAULT_CONFIG) -> Estimate:
    """Delta-RMST(J) between two arms, with a PAIRED cluster bootstrap: the same
    resampled task list is used for both arms in every draw, so the between-task
    variance that is common to the pair cancels the way it does in the primary
    paired t."""
    a = dataset[dataset["condition_id"] == arm]
    r = dataset[dataset["condition_id"] == reference]
    if not len(a) or not len(r):
        return Estimate(f"delta_rmst[{arm}-{reference}]", None,
                        method="paired cluster bootstrap (missing arm)")
    point = _km_rmst(a["duration"].to_numpy(float), a["event"].to_numpy(int), horizon) - \
        _km_rmst(r["duration"].to_numpy(float), r["event"].to_numpy(int), horizon)
    tasks = sorted(set(a["task_id"]) & set(r["task_id"]))
    if len(tasks) < 2:
        return Estimate(f"delta_rmst[{arm}-{reference}]", float(point), n_tasks=len(tasks),
                        method="paired cluster bootstrap (too few shared tasks for an interval)")
    rng = np.random.default_rng(cfg.random_seed)
    ag = {t: a[a["task_id"] == t] for t in tasks}
    rg = {t: r[r["task_id"] == t] for t in tasks}
    boot = np.empty(cfg.rmst_bootstrap_draws, dtype=float)
    for b in range(cfg.rmst_bootstrap_draws):
        pick = [tasks[i] for i in rng.integers(0, len(tasks), len(tasks))]
        da = np.concatenate([ag[t]["duration"].to_numpy(float) for t in pick])
        ea = np.concatenate([ag[t]["event"].to_numpy(int) for t in pick])
        dr = np.concatenate([rg[t]["duration"].to_numpy(float) for t in pick])
        er = np.concatenate([rg[t]["event"].to_numpy(int) for t in pick])
        boot[b] = _km_rmst(da, ea, horizon) - _km_rmst(dr, er, horizon)
    boot = boot[~np.isnan(boot)]
    return Estimate(f"delta_rmst[{arm}-{reference}]", float(point),
                    float(np.quantile(boot, cfg.alpha / 2)),
                    float(np.quantile(boot, 1 - cfg.alpha / 2)),
                    float(np.std(boot, ddof=1)), n=int(len(a) + len(r)), n_tasks=len(tasks),
                    method="Delta-RMST(J), paired cluster bootstrap over tasks",
                    detail={"horizon_J": horizon, "arm": arm, "reference": reference})


def retention_table(source: Tables | pd.DataFrame,
                    cfg: AnalysisConfig = DEFAULT_CONFIG, *,
                    arms: Sequence[str] | None = None) -> RetentionResult:
    """The retention headline: J, RMST per arm, and Delta-RMST vs the control.

    Honest naming: this is SUSTAINED SELF-REPORT UNDER REPEATED
    ELICITATION, not memory decay — the probe re-injects the nonce roughly every
    two tool calls, and `n_reinjections` is carried as the dose covariate.
    """
    ds = retention_dataset(source, cfg)
    if not len(ds):
        return RetentionResult(None, pd.DataFrame(), ds, True, note="no rows with ever_mention=1")
    J, detail = common_horizon(ds, cfg, arms=arms)
    if J is None or J <= 0:
        return RetentionResult(J, pd.DataFrame(), ds, True,
                               note="common horizon J is 0: no arm keeps 10% at risk past time 0")
    use = ds if arms is None else ds[ds["condition_id"].isin(list(arms))]
    per_arm = rmst_by_condition(use, J, cfg)
    per_arm = per_arm.merge(detail[["condition_id", "J_arm"]], on="condition_id", how="left")
    ref = retention_reference_arm(use, cfg)
    notes = []
    if ref is None:
        per_arm["delta_rmst"] = np.nan
        per_arm["delta_ci_low"] = np.nan
        per_arm["delta_ci_high"] = np.nan
        notes.append("no reference arm with retention rows; RMST reported unc-ontrasted")
    else:
        deltas = {a: rmst_delta(use, J, a, ref, cfg) for a in per_arm["condition_id"]}
        per_arm["delta_reference"] = ref
        per_arm["delta_rmst"] = [deltas[a].value for a in per_arm["condition_id"]]
        per_arm["delta_ci_low"] = [deltas[a].ci_low for a in per_arm["condition_id"]]
        per_arm["delta_ci_high"] = [deltas[a].ci_high for a in per_arm["condition_id"]]
        if ref != cfg.baseline_condition:
            notes.append(
                f"reference arm is {ref!r}, not the {cfg.baseline_condition!r} control: the "
                "control is fact-free by construction, so it contributes ZERO retention rows "
                "(retention is defined on {ever_mention = 1}). Delta-RMST is a within-treatment "
                "contrast and is NOT a control-adjusted lift.")
    per_arm["horizon_J"] = J
    exploratory = J < cfg.retention_min_J
    if exploratory:
        notes.insert(0, "J < %d probes: retention is DESCRIPTIVE ONLY (KM curves); "
                        "Delta-RMST drops to exploratory" % cfg.retention_min_J)
    return RetentionResult(J, per_arm, ds, exploratory, note=" | ".join(notes))


def retention_reference_arm(dataset: pd.DataFrame,
                            cfg: AnalysisConfig = DEFAULT_CONFIG) -> str | None:
    """Which arm Delta-RMST is measured against.

    NOT the control. `ctrl` is fact-free, so it can never mention the fact and
    contributes no rows to a dataset defined on {ever_mention = 1}: a Delta-RMST
    against it would be NaN in every cell, forever. The default is the shallowest
    pulled arm that actually has rows (d1 -> d2 -> d3 -> whatever is present), and
    it is stated on the result so no reader mistakes it for a control contrast.
    """
    present = set(dataset["condition_id"].dropna().astype(str))
    if cfg.retention_reference and cfg.retention_reference in present:
        return cfg.retention_reference
    for arm in DEPTH_LADDER:
        if arm in present:
            return arm
    return sorted(present)[0] if present else None




def km_curves(dataset: pd.DataFrame, *, arms: Sequence[str] | None = None) -> dict[str, pd.DataFrame]:
    """Kaplan-Meier survival curves per arm, for plotting and for the descriptive
    fallback when J < 3."""
    from lifelines import KaplanMeierFitter

    out: dict[str, pd.DataFrame] = {}
    use = dataset if arms is None else dataset[dataset["condition_id"].isin(list(arms))]
    for arm, sub in use.groupby("condition_id", dropna=False):
        kmf = KaplanMeierFitter()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            kmf.fit(sub["duration"].to_numpy(dtype=float), sub["event"].to_numpy(dtype=int),
                    label=str(arm))
        out[str(arm)] = kmf.survival_function_.join(kmf.confidence_interval_)
    return out


def post_discharge_persistence(source: Tables | pd.DataFrame,
                               cfg: AnalysisConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """The complement of the primary retention window, reported separately.

    Rows whose lapse happened at or after first use — where dropping the fact is
    correct behaviour rather than forgetting.
    """
    df, _ = analysis_frame(source, cfg) if not isinstance(source, pd.DataFrame) else (source, None)
    unrestricted = retention_dataset(df, replace(cfg, retention_pre_use_only=False))
    restricted = retention_dataset(df, replace(cfg, retention_pre_use_only=True))
    keys = set(zip(restricted["run_id"], restricted["condition_id"])) if len(restricted) else set()
    rows = []
    for arm, sub in unrestricted.groupby("condition_id", dropna=False):
        in_primary = sum(1 for r in sub.itertuples() if (r.run_id, r.condition_id) in keys)
        rows.append({"condition_id": arm, "n_ever_mention": len(sub),
                     "n_in_primary_window": in_primary,
                     "n_post_discharge_only": len(sub) - in_primary,
                     "mean_duration_unrestricted": float(sub["duration"].mean()),
                     "event_rate_unrestricted": float(sub["event"].mean())})
    return pd.DataFrame(rows).sort_values("condition_id").reset_index(drop=True)


# ── metric 5: probe fidelity ─────────────────────────────────────────────────
def probe_fidelity(source: Tables, cfg: AnalysisConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """Agreement between the slot-level `affects_next_action` claim and the tool
    call the agent actually issued next.

    The only metric here that generalizes past workspace design: if it is high,
    cheap self-report is a legitimate monitoring instrument; if it is low,
    introspective agent telemetry is not trustworthy. Aggregated per arm at BOTH
    levels — per probe (many per run) and per task (the unit of generalization).
    """
    probes = source.probes if isinstance(source, Tables) else source
    if not len(probes) or "fidelity_agree" not in probes.columns:
        return pd.DataFrame(columns=["condition_id", "n_probes", "fidelity_probe_level",
                                     "n_tasks", "fidelity_task_mean", "ci_low", "ci_high"])
    p = probes[_bool_series(probes, "fidelity_agree").notna()].copy()
    p["agree"] = _bool_series(p, "fidelity_agree").astype(float)
    rows = []
    for arm, sub in p.groupby("condition_id", dropna=False):
        per_task = sub.groupby("task_id")["agree"].mean()
        n_t = int(per_task.notna().sum())
        lo = hi = None
        if n_t >= cfg.min_tasks:
            m, sd = float(per_task.mean()), float(per_task.std(ddof=1))
            se = sd / math.sqrt(n_t)
            crit = float(stats.t.ppf(1 - cfg.alpha / 2, n_t - 1))
            lo, hi = m - crit * se, m + crit * se
        k, n = int(sub["agree"].sum()), int(len(sub))
        wlo, whi = wilson_ci(k, n, cfg.alpha)
        rows.append({"condition_id": arm, "n_probes": n,
                     "fidelity_probe_level": (k / n) if n else None,
                     "probe_ci_low": wlo, "probe_ci_high": whi,
                     "n_tasks": n_t,
                     "fidelity_task_mean": float(per_task.mean()) if n_t else None,
                     "ci_low": lo, "ci_high": hi})
    return pd.DataFrame(rows).sort_values("condition_id").reset_index(drop=True)


def probe_quality(source: Tables, cfg: AnalysisConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """parse_ok / refusal / outcome census per arm — the instrument's own health.

    Pilot gates: parse_ok >= 0.90 and refused == 0. A refusal means the trusted
    stream-json user channel broke, which invalidates every probe-derived number
    in the run (V1/V2).
    """
    probes = source.probes if isinstance(source, Tables) else source
    if not len(probes):
        return pd.DataFrame(columns=["condition_id", "n_probes", "parse_ok_rate", "n_refused"])
    rows = []
    for arm, sub in probes.groupby("condition_id", dropna=False):
        n = len(sub)
        parse_ok = _bool_series(sub, "parse_ok").fillna(False).astype(bool)
        outcome = sub["outcome"].astype("string") if "outcome" in sub.columns else pd.Series(dtype="string")
        tiers = sub["parse_tier"].astype("string") if "parse_tier" in sub.columns else pd.Series(dtype="string")
        rows.append({"condition_id": arm, "n_probes": n,
                     "parse_ok_rate": float(parse_ok.mean()) if n else None,
                     "n_strict": int((tiers == "strict").sum()),
                     "n_lenient": int((tiers == "lenient").sum()),
                     "n_failed": int((tiers == "failed").sum()),
                     "n_answered": int((outcome == "answered").sum()),
                     "n_superseded": int((outcome == "superseded").sum()),
                     "n_unanswered": int((outcome == "unanswered").sum()),
                     "n_refused": int((outcome == "refused").sum()),
                     "retry_rate": float(_bool_series(sub, "retry_sent").fillna(False).mean()) if n else None})
    return pd.DataFrame(rows).sort_values("condition_id").reset_index(drop=True)


def slot_class_distribution(source: Tables) -> pd.DataFrame:
    """The filler distribution IS a finding: with one fact per task at least two
    slots are filler by construction, so what fills them is data."""
    probes = source.probes if isinstance(source, Tables) else source
    cols = [f"n_{c}_slots" for c in ("critical_fact", "task_restatement", "generic_workspace",
                                     "filler", "empty", "distrusted")]
    have = [c for c in cols if c in probes.columns]
    if not len(probes) or not have:
        return pd.DataFrame(columns=["condition_id"] + cols)
    g = probes.groupby("condition_id", dropna=False)[have].sum().reset_index()
    total = g[have].sum(axis=1).replace(0, np.nan)
    for c in have:
        g[c.replace("n_", "share_")] = g[c] / total
    g["n_probes"] = probes.groupby("condition_id", dropna=False).size().values
    return g


# ── metric 6: the alarms ─────────────────────────────────────────────────────
def confabulation_rate(source: Tables | pd.DataFrame,
                       cfg: AnalysisConfig = DEFAULT_CONFIG) -> Estimate:
    """P(mention | NOT read) — an ALARM, not a finding.

    A nonce cannot be guessed. A breach of the 0.05 gate means it leaked into a
    prompt, the tier-(b) paraphrase regex over-matches, or an exposure channel is
    missing from the scanner — and any of those invalidates every
    exposure-conditioned metric in the study.

    Computed on the ALARM frame (quarantined rows kept) and using strict
    read == False: unknown reads are not "not read". The gate form additionally
    drops rows where the model produced the nonce itself; fact_trace carries
    `echoed` as a run-level flag rather than an echo-before-mention ordering, so
    the gate figure below is the conservative approximation of
    P(mention | NOT read AND NOT echoed) and is labelled as such.
    """
    df = alarm_frame(source, cfg)
    read = _bool_series(df, cfg.read_field)
    not_read = df[read.fillna(True).astype(bool) == False]  # noqa: E712
    n = int(len(not_read))
    k = int(_bool_series(not_read, "ever_mention").fillna(False).sum())
    lo, hi = wilson_ci(k, n, cfg.alpha)
    ech = _bool_series(not_read, "echoed").fillna(False).astype(bool)
    gate_rows = not_read[~ech]
    n_g = int(len(gate_rows))
    k_g = int(_bool_series(gate_rows, "ever_mention").fillna(False).sum())
    glo, ghi = wilson_ci(k_g, n_g, cfg.alpha)
    return Estimate("confab_rate", (k / n) if n else None, lo, hi, n=n,
                    method="P(ever_mention | read == False), alarm frame (quarantine kept)",
                    detail={"k": k, "n": n, "threshold": 0.05,
                            "gate_rate": (k_g / n_g) if n_g else None,
                            "gate_k": k_g, "gate_n": n_g,
                            "gate_ci": [glo, ghi],
                            "gate_definition": "P(mention | NOT read AND NOT echoed) — "
                                               "approximates '¬echoed-before-mention'",
                            "breach": bool(n and (k / n) > 0.05)})


def unexplained_possession_rate(source: Tables | pd.DataFrame,
                                cfg: AnalysisConfig = DEFAULT_CONFIG) -> Estimate:
    """The D4 alarm: a self_thinking hit with no prior inbound hit.

    Folding thinking into `read` mutes confabulation detection; this restores it.
    Any true row is quarantined and hand-audited, and a pilot rate above 0.05 is a
    FIXTURE-WIDE FAILURE, not a data point.
    """
    df = alarm_frame(source, cfg)
    vals = _bool_series(df, "unexplained_possession")
    n, k = int(vals.notna().sum()), int(vals.fillna(False).sum())
    lo, hi = wilson_ci(k, n, cfg.alpha)
    flagged = df[vals.fillna(False).astype(bool)]
    return Estimate("unexplained_possession_rate", (k / n) if n else None, lo, hi, n=n,
                    method="mean(unexplained_possession) over analyzable rows, quarantine kept",
                    detail={"k": k, "n": n, "threshold": 0.05,
                            "breach": bool(n and (k / n) > 0.05),
                            "run_ids": [str(x) for x in flagged.get("run_id", pd.Series(dtype=str)).tolist()]})



def orthogonality_phi(source: Tables | pd.DataFrame,
                      cfg: AnalysisConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """phi(used, success) per task. |phi| > 0.8 means the fact is not orthogonal to
    acceptance — the battery is testing the mandate, success == used, and the funnel
    collapses to one measurement. Same disposition as a failed prior-check:
    discard the fact.
    """
    df, _ = analysis_frame(source, cfg) if not isinstance(source, pd.DataFrame) else (source, None)
    rows = []
    for task, sub in df.groupby("task_id", dropna=False):
        u = _bool_series(sub, "used").dropna()
        s = _bool_series(sub, "success").dropna()
        idx = u.index.intersection(s.index)
        uu = u.loc[idx].astype(float).to_numpy()
        ss = s.loc[idx].astype(float).to_numpy()
        phi = None
        if len(idx) >= 2 and uu.std() > 0 and ss.std() > 0:
            phi = float(np.corrcoef(uu, ss)[0, 1])
        rows.append({"task_id": task, "n": int(len(idx)), "phi": phi,
                     "breach": bool(phi is not None and abs(phi) > 0.8)})
    return pd.DataFrame(rows).sort_values("task_id").reset_index(drop=True)


# ── metric 7: depth, format, probe reactivity ────────────────────────────────
def _fit_glmm_logistic(frame: pd.DataFrame, outcome: str, fixed: str,
                       group: str = "task_id") -> dict[str, Any]:
    """SECONDARY model fit: logistic with a random intercept for task.

    Tries statsmodels' BinomialBayesMixedGLM (the literal "mixed-effects logistic,
    random intercept for task") and falls back to GEE with an
    exchangeable working correlation clustered on task, which estimates the same
    marginal slope with a cluster-robust SE. Whichever ran is reported in
    `method`; the primary claim never depends on either.
    """
    out: dict[str, Any] = {"method": None, "coef": None, "se": None, "p_value": None,
                           "n_obs": int(len(frame)), "n_groups": int(frame[group].nunique()),
                           "error": None}
    if len(frame) < 4 or frame[outcome].nunique() < 2:
        out["error"] = "degenerate outcome"
        return out
    try:
        import statsmodels.api as sm
        import statsmodels.formula.api as smf

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = smf.gee(f"{outcome} ~ {fixed}", groups=group, data=frame,
                            family=sm.families.Binomial(),
                            cov_struct=sm.cov_struct.Exchangeable())
            res = model.fit()
        out.update(method="GEE logistic, exchangeable, clustered on task",
                   coef=float(res.params.get(fixed, np.nan)),
                   se=float(res.bse.get(fixed, np.nan)),
                   p_value=float(res.pvalues.get(fixed, np.nan)))
    except Exception as exc:  # noqa: BLE001 — a secondary fit may not converge
        out["error"] = f"{type(exc).__name__}: {exc}"
    try:
        from statsmodels.genmod.bayes_mixed_glm import BinomialBayesMixedGLM

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            glmm = BinomialBayesMixedGLM.from_formula(
                f"{outcome} ~ {fixed}", {"task": f"0 + C({group})"}, frame)
            gres = glmm.fit_vb(verbose=False)
        names = list(gres.model.exog_names)
        if fixed in names:
            i = names.index(fixed)
            out["glmm_coef"] = float(gres.fe_mean[i])
            out["glmm_se"] = float(gres.fe_sd[i])
            out["glmm_method"] = "BinomialBayesMixedGLM (variational), random intercept for task"
    except Exception as exc:  # noqa: BLE001
        out["glmm_error"] = f"{type(exc).__name__}: {exc}"
    return out


def depth_sensitivity(source: Tables | pd.DataFrame, cfg: AnalysisConfig = DEFAULT_CONFIG, *,
                      outcome: str | None = None,
                      arms: Sequence[str] = DEPTH_LADDER) -> dict[str, Any]:
    """Does burying a fact cost you? Prices "how deep can documentation go".

    PRIMARY is cluster-level: one slope per task over the pulled ladder d1->d2->d3,
    then a one-sample t across tasks. Pairwise paired contrasts are reported
    beside it. The mixed-effects logistic is fitted too, and is
    SECONDARY — with T = 12 it is a 12-cluster GLMM and its asymptotics are a
    promise, not a fact.
    """
    outcome = outcome or cfg.read_field
    df, _ = analysis_frame(source, cfg) if not isinstance(source, pd.DataFrame) else (source, None)
    sub = df[df["condition_id"].isin(list(arms))].copy()
    rates = per_task_rates(sub, outcome)
    wide = rates.pivot_table(index="task_id", columns="condition_id", values="rate")
    depth_of = {a: DEPTH_INDEX.get(str(ARMS.get(a, {}).get("depth")), np.nan) for a in arms}
    slopes: list[float] = []
    for task, row in wide.iterrows():
        xs, ys = [], []
        for a in arms:
            if a in row.index and not pd.isna(row[a]) and not math.isnan(depth_of[a]):
                xs.append(depth_of[a])
                ys.append(float(row[a]))
        if len(xs) >= 2 and np.std(xs) > 0:
            slopes.append(float(np.polyfit(xs, ys, 1)[0]))
    slope_est = _one_sample_t(slopes, cfg, name=f"depth_slope[{outcome}]",
                              method="per-task OLS slope of rate on depth index, t across tasks")
    contrasts = []
    for i in range(len(arms)):
        for j in range(i + 1, len(arms)):
            r = paired_cluster_t(sub, arms[j], arms[i], outcome, cfg)
            contrasts.append({"contrast": f"{arms[j]}-{arms[i]}", "n_tasks": r.n_tasks,
                              "diff": r.mean_diff, "ci_low": r.ci_low, "ci_high": r.ci_high,
                              "p_value": r.p_value})
    model_frame = sub.copy()
    model_frame["y"] = _bool_series(model_frame, outcome).astype("float")
    model_frame = model_frame.dropna(subset=["y", "task_id"])
    model_frame["depth_idx"] = model_frame["condition_id"].map(
        lambda a: DEPTH_INDEX.get(str(ARMS.get(a, {}).get("depth")), np.nan))
    model_frame = model_frame.dropna(subset=["depth_idx"])
    glmm = _fit_glmm_logistic(model_frame, "y", "depth_idx")
    return {"outcome": outcome, "arms": list(arms),
            "per_task_rates": rates, "wide": wide.reset_index(),
            "slope": slope_est, "contrasts": pd.DataFrame(contrasts),
            "secondary_model": glmm,
            "rate_by_condition": rate_by_condition(sub, outcome, cfg)}


def format_sensitivity(source: Tables | pd.DataFrame, cfg: AnalysisConfig = DEFAULT_CONFIG, *,
                       outcome: str = "used",
                       arms: Sequence[str] = FORMAT_ARMS) -> dict[str, Any]:
    """Prose vs checklist vs table at fixed depth d2 — same fact, same length.

    If format dominates depth, workspaces should be structured for ROUTING rather
    than for human readability, which reorders the whole workspace-design backlog.
    Primary: paired cluster contrasts against prose. Omnibus: Friedman over the
    per-task rates (nonparametric, respects the pairing, does not assume normal).
    """
    df, _ = analysis_frame(source, cfg) if not isinstance(source, pd.DataFrame) else (source, None)
    sub = df[df["condition_id"].isin(list(arms))]
    rates = per_task_rates(sub, outcome)
    wide = rates.pivot_table(index="task_id", columns="condition_id", values="rate").dropna()
    contrasts = []
    ref = arms[0]
    for a in arms[1:]:
        r = paired_cluster_t(sub, a, ref, outcome, cfg)
        contrasts.append({"contrast": f"{a}-{ref}", "n_tasks": r.n_tasks, "diff": r.mean_diff,
                          "ci_low": r.ci_low, "ci_high": r.ci_high, "p_value": r.p_value})
    omnibus: dict[str, Any] = {"test": "friedman", "statistic": None, "p_value": None,
                               "n_tasks": int(len(wide))}
    cols = [a for a in arms if a in wide.columns]
    if len(wide) >= 3 and len(cols) >= 3:
        try:
            stat, p = stats.friedmanchisquare(*[wide[a].to_numpy() for a in cols])
            omnibus.update(statistic=float(stat), p_value=float(p))
        except ValueError as exc:
            omnibus["error"] = str(exc)
    return {"outcome": outcome, "arms": list(arms), "per_task_rates": rates,
            "wide": wide.reset_index(), "contrasts": pd.DataFrame(contrasts),
            "omnibus": omnibus, "rate_by_condition": rate_by_condition(sub, outcome, cfg)}


def probe_reactivity(source: Tables | pd.DataFrame, cfg: AnalysisConfig = DEFAULT_CONFIG, *,
                     outcomes: Sequence[str] = ("used", "read"),
                     pairs: Sequence[tuple[str, str]] = REACTIVITY_PAIRS) -> pd.DataFrame:
    """How much did asking change the answer? d1/d3 vs d1-np/d3-np.

    This BOUNDS every other number in the study: absolute uptake figures are
    conditional on the probe and on the pacing prompt, and without this contrast
    nothing here is quotable unprobed.
    """
    df, _ = analysis_frame(source, cfg) if not isinstance(source, pd.DataFrame) else (source, None)
    rows = []
    for probed, unprobed in pairs:
        for outcome in outcomes:
            oc = cfg.read_field if outcome == "read" else outcome
            r = paired_cluster_t(df, probed, unprobed, oc, cfg)
            rows.append({"probed_arm": probed, "unprobed_arm": unprobed, "outcome": oc,
                         "n_tasks": r.n_tasks, "diff": r.mean_diff, "se": r.se,
                         "ci_low": r.ci_low, "ci_high": r.ci_high, "p_value": r.p_value,
                         "note": r.note})
    return pd.DataFrame(rows)


def slot_precision_table(source: Tables | pd.DataFrame,
                         cfg: AnalysisConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """d2-dist only: did the right fact win against confusable distractors?
    Prices the cost of a cluttered workspace."""
    df, _ = analysis_frame(source, cfg) if not isinstance(source, pd.DataFrame) else (source, None)
    rows = []
    for arm, sub in df.groupby("condition_id", dropna=False):
        wrong = _bool_series(sub, "wrong_value_in_slot")
        prec = pd.to_numeric(sub.get("slot_precision"), errors="coerce") if "slot_precision" in sub.columns else pd.Series(dtype=float)
        n_w = int(wrong.notna().sum())
        rows.append({"condition_id": arm, "n": int(len(sub)),
                     "n_scored": n_w,
                     "wrong_value_rate": float(wrong.dropna().mean()) if n_w else None,
                     "slot_precision_mean": float(prec.dropna().mean()) if prec.notna().any() else None,
                     "distractors": int(sub["factor_distractors"].dropna().max()) if "factor_distractors" in sub.columns and sub["factor_distractors"].notna().any() else None})
    return pd.DataFrame(rows).sort_values("condition_id").reset_index(drop=True)


def _one_sample_t(values: Sequence[float], cfg: AnalysisConfig, *, name: str,
                  method: str) -> Estimate:
    v = np.asarray([x for x in values if x is not None and not (isinstance(x, float) and math.isnan(x))],
                   dtype=float)
    n = int(v.size)
    if n < cfg.min_tasks:
        return Estimate(name, None, n_tasks=n, method=method + " (insufficient tasks)")
    mean, sd = float(v.mean()), float(v.std(ddof=1))
    if sd == 0:
        return Estimate(name, mean, mean, mean, 0.0, n, n, None, method + " (zero variance)")
    se = sd / math.sqrt(n)
    t = mean / se
    p = float(2 * stats.t.sf(abs(t), n - 1))
    crit = float(stats.t.ppf(1 - cfg.alpha / 2, n - 1))
    boot_lo, boot_hi = cluster_bootstrap_ci(v.tolist(), cfg)
    return Estimate(name, mean, mean - crit * se, mean + crit * se, se, n, n, p, method,
                    detail={"t": t, "dof": n - 1, "boot_ci": [boot_lo, boot_hi]})


# ── the funnel, and the one-call summary ─────────────────────────────────────
def funnel_table(source: Tables | pd.DataFrame,
                 cfg: AnalysisConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """available -> read -> used -> retained, one row per arm.

    The four columns are three different failures with three different fixes:
    read but not used is decorative; used but not retained needs re-reading every
    turn; never read is invisible regardless of quality.
    """
    df, _ = analysis_frame(source, cfg) if not isinstance(source, pd.DataFrame) else (source, None)
    rows = []
    for arm, sub in df.groupby("condition_id", dropna=False):
        def r(col: str, denom: str | None = None) -> float | None:
            s = sub if denom is None else sub[_bool_series(sub, denom).fillna(False).astype(bool)]
            v = _bool_series(s, col).dropna()
            return float(v.mean()) if len(v) else None
        rows.append({
            "condition_id": arm, "n_runs": int(len(sub)),
            "available": r("available"),
            "read": r(cfg.read_field),
            "read_inbound_only": r(cfg.sensitivity_read_field),
            "n_read_unknown": int(_bool_series(sub, cfg.read_field).isna().sum()),
            "opened": r("opened"),
            "ever_mention": r("ever_mention"),
            "mention_given_read": r("ever_mention", cfg.read_field),
            "used": r("used"),
            "used_given_read": r("used", cfg.read_field),
            "used_given_eligible": r("used", "eligible"),
            "success": r("success"),
            "trust": _trust_of(sub),
        })
    return pd.DataFrame(rows).sort_values("condition_id").reset_index(drop=True)


def summarize(tables: Tables, cfg: AnalysisConfig = DEFAULT_CONFIG) -> dict[str, Any]:
    """Every headline number in one call — what the notebook's final cell prints and
    what analysis/REPORT.md is written from."""
    df, excl = analysis_frame(tables, cfg)
    out: dict[str, Any] = {
        "version": UPTAKE_LIB_VERSION,
        "config": cfg.to_dict(),
        "exclusions": excl.to_dict(),
        "n_runs": int(df["run_id"].nunique()) if len(df) else 0,
        "n_tasks": int(df["task_id"].nunique()) if len(df) else 0,
        "n_arms": int(df["condition_id"].nunique()) if len(df) else 0,
        "funnel": funnel_table(df, cfg),
        "read_rate": read_rate_table(df, cfg),
        "incidental_exposure": incidental_exposure(df, cfg),
        "mention_rate": mention_rate(df, cfg),
        "use_rate_appendix": use_rate_table(df, cfg),
        "use_lift": use_lift(df, cfg),
        "use_lift_conditional": use_lift(df, cfg, conditional=True),
        "confab_rate": confabulation_rate(tables, cfg),
        "unexplained_possession": unexplained_possession_rate(tables, cfg),
        "orthogonality_phi": orthogonality_phi(df, cfg),
        "probe_reactivity": probe_reactivity(df, cfg),
        "slot_precision": slot_precision_table(df, cfg),
    }
    try:
        out["retention"] = retention_table(df, cfg)
    except ImportError as exc:
        out["retention"] = f"lifelines unavailable: {exc}"
    try:
        out["depth"] = depth_sensitivity(df, cfg)
        out["format"] = format_sensitivity(df, cfg)
    except Exception as exc:  # noqa: BLE001 — a secondary fit must not kill the summary
        out["depth_format_error"] = f"{type(exc).__name__}: {exc}"
    if len(tables.probes):
        out["probe_fidelity"] = probe_fidelity(tables, cfg)
        out["probe_quality"] = probe_quality(tables, cfg)
        out["slot_classes"] = slot_class_distribution(tables)
    return out


# ── figures (thin; the notebook must contain no logic) ───────────────────────
def _axes(ax=None, figsize=(8, 4.5)):
    import matplotlib.pyplot as plt

    if ax is not None:
        return ax.figure, ax
    fig, ax = plt.subplots(figsize=figsize)
    return fig, ax


def plot_funnel(source: Tables | pd.DataFrame, cfg: AnalysisConfig = DEFAULT_CONFIG,
                *, arms: Sequence[str] | None = None, ax=None):
    """Stacked funnel per arm: available -> read -> mention -> used."""
    t = funnel_table(source, cfg)
    if arms:
        t = t[t["condition_id"].isin(list(arms))]
    fig, ax = _axes(ax, (9, 4.5))
    x = np.arange(len(t))
    for i, col in enumerate(("available", "read", "ever_mention", "used")):
        ax.bar(x + i * 0.2 - 0.3, t[col].astype(float).fillna(0), width=0.19, label=col)
    ax.set_xticks(x)
    ax.set_xticklabels(t["condition_id"], rotation=45, ha="right")
    ax.set_ylim(0, 1.02)
    ax.set_ylabel("rate")
    ax.set_title("Funnel: available -> read -> mention -> used")
    ax.legend(fontsize=8)
    fig.tight_layout()
    return fig




def plot_read_rate(source: Tables | pd.DataFrame, cfg: AnalysisConfig = DEFAULT_CONFIG, *, ax=None):
    """Read rate with Wilson CIs, PRIMARY and the mandatory inbound-only sensitivity."""
    t = read_rate_table(source, cfg)
    fig, ax = _axes(ax, (9, 4.5))
    arms = list(dict.fromkeys(t["condition_id"]))
    x = np.arange(len(arms))
    for i, definition in enumerate(sorted(t["definition"].unique())):
        sub = t[t["definition"] == definition].set_index("condition_id").reindex(arms)
        vals = sub["rate"].astype(float).to_numpy()
        lo = vals - sub["ci_low"].astype(float).to_numpy()
        hi = sub["ci_high"].astype(float).to_numpy() - vals
        ax.errorbar(x + (i - 0.5) * 0.12, vals, yerr=[np.nan_to_num(lo), np.nan_to_num(hi)],
                    fmt="o", capsize=3, label=definition)
    ax.set_xticks(x)
    ax.set_xticklabels(arms, rotation=45, ha="right")
    ax.set_ylim(-0.02, 1.02)
    ax.set_ylabel("P(nonce entered context)")
    ax.set_title("Read rate (primary = inbound U self_thinking, D4)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    return fig




def plot_lift(lift_frame: pd.DataFrame, *, title: str = "Use lift over paired control", ax=None):
    """Forest plot of lambda_a with its cluster-level interval. Zero line included,
    because the interesting cell is high read with zero lift."""
    fig, ax = _axes(ax, (8, 0.5 * max(len(lift_frame), 3) + 1.5))
    t = lift_frame.dropna(subset=["lift"]).reset_index(drop=True)
    y = np.arange(len(t))
    ax.errorbar(t["lift"].astype(float), y,
                xerr=[np.nan_to_num(t["lift"].astype(float) - t["ci_low"].astype(float)),
                      np.nan_to_num(t["ci_high"].astype(float) - t["lift"].astype(float))],
                fmt="o", capsize=3)
    ax.axvline(0.0, color="k", lw=1, ls="--")
    ax.set_yticks(y)
    ax.set_yticklabels(t["condition_id"])
    ax.set_xlabel("risk difference vs control (task-level mean)")
    ax.set_title(title)
    fig.tight_layout()
    return fig


def plot_km(dataset: pd.DataFrame, *, horizon: float | None = None,
            arms: Sequence[str] | None = None, ax=None):
    """KM curves of continued mention, in probe-index units. The honest label is
    'sustained self-report under repeated elicitation'."""
    curves = km_curves(dataset, arms=arms)
    fig, ax = _axes(ax, (8, 4.5))
    for arm, sf in curves.items():
        col = [c for c in sf.columns if not c.endswith("upper_0.95") and not c.endswith("lower_0.95")][0]
        ax.step(sf.index, sf[col], where="post", label=arm)
    if horizon:
        ax.axvline(horizon, color="k", ls=":", lw=1)
        ax.text(horizon, 1.01, " J", fontsize=8)
    ax.set_xlabel("probes since first mention")
    ax.set_ylabel("P(still naming the fact)")
    ax.set_ylim(0, 1.05)
    ax.set_title("Sustained self-report under repeated elicitation")
    ax.legend(fontsize=8)
    fig.tight_layout()
    return fig


def plot_depth(depth_result: Mapping[str, Any], *, ax=None):
    """Per-task read/use rate against depth, one line per task, mean overlaid."""
    wide = depth_result["wide"].set_index("task_id")
    arms = [a for a in depth_result["arms"] if a in wide.columns]
    xs = [DEPTH_INDEX.get(str(ARMS.get(a, {}).get("depth")), i) for i, a in enumerate(arms)]
    fig, ax = _axes(ax, (7, 4.5))
    for task, row in wide.iterrows():
        ax.plot(xs, [row[a] for a in arms], marker="o", alpha=0.35, lw=1)
    ax.plot(xs, [wide[a].mean() for a in arms], marker="s", lw=2.5, color="k", label="mean")
    ax.set_xticks(xs)
    ax.set_xticklabels(arms)
    ax.set_ylim(-0.02, 1.02)
    ax.set_ylabel(f"P({depth_result['outcome']})")
    ax.set_title("Depth sensitivity (one line per task)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    return fig


# ── synthetic data, for tests and for the notebook's dry run ─────────────────
def synthetic_tables(*, n_tasks: int = 12, n_reps: int = 5,
                     arms: Sequence[str] = ("ctrl", "d1", "d2", "d3"),
                     read_rate_by_arm: Mapping[str, float] | None = None,
                     use_rate_ctrl: float = 0.05,
                     use_lift_by_arm: Mapping[str, float] | None = None,
                     gamma: float = 0.0, tau: float = 0.5,
                     mention_given_read: float = 0.8,
                     n_probes: int = 10, lapse_hazard: float = 0.25,
                     seed: int = 7) -> Tables:
    """A fact_trace/probes frame with a KNOWN planted effect.

    Exists so the estimators can be tested against ground truth — the known-lift
    recovery test in tests/test_wur_lib.py and the Phase-8 gate both call it —
    and so uptake.ipynb can be executed end-to-end before a single real run
    exists. `gamma` is the SD of the per-task arm effect on the logit scale, the
    same parameter analysis/power.py sweeps.
    """
    rng = np.random.default_rng(seed)
    use_lift_by_arm = dict(use_lift_by_arm or {a: 0.0 for a in arms})
    read_rate_by_arm = dict(read_rate_by_arm or
                            {a: (0.0 if a.startswith("ctrl") else 0.7) for a in arms})
    ft_rows, probe_rows = [], []
    logit = lambda p: math.log(p / (1 - p)) if 0 < p < 1 else (-20.0 if p <= 0 else 20.0)
    expit = lambda x: 1.0 / (1.0 + math.exp(-x))
    for t in range(n_tasks):
        task = f"t{t:02d}"
        task_shift = float(rng.normal(0.0, tau))
        arm_shift = {a: float(rng.normal(0.0, gamma)) for a in arms}
        for arm in arms:
            present = not arm.startswith("ctrl")
            p_read = read_rate_by_arm.get(arm, 0.0)
            p_use = min(0.98, max(0.0, use_rate_ctrl + use_lift_by_arm.get(arm, 0.0)))
            p_use_t = expit(logit(max(p_use, 1e-6)) + task_shift + arm_shift[arm]) if p_use > 0 else 0.0
            for rep in range(n_reps):
                run_id = f"{task}-{arm}-r{rep}"
                did_read = bool(present and rng.random() < p_read)
                used = bool(rng.random() < p_use_t)
                mention = bool(did_read and rng.random() < mention_given_read)
                i0 = int(rng.integers(0, 3)) if mention else None
                lapse = None
                if mention:
                    for j in range(1, n_probes - i0):
                        if rng.random() < lapse_hazard:
                            lapse = j
                            break
                ft_rows.append({
                    "schema_version": "1", "run_id": run_id, "job_id": "synthetic",
                    "task_id": task, "condition_id": arm, "rep": rep,
                    "fact_id": f"{task}-f0", "tier": "A",
                    "factor_depth": ARMS.get(arm, {}).get("depth"),
                    "factor_format": ARMS.get(arm, {}).get("format"),
                    "factor_channel": ARMS.get(arm, {}).get("channel"),
                    "factor_distractors": ARMS.get(arm, {}).get("distractors"),
                    "factor_fact_present": present,
                    "factor_probe": ARMS.get(arm, {}).get("probe", True),
                    "factor_pointer_regime": ARMS.get(arm, {}).get("pointer_regime"),
                    "available": present,
                    "read": did_read, "read_status": "read" if did_read else "not_read",
                    "read_inbound_only": did_read, "unexplained_possession": False,
                    "opened": did_read and bool(rng.random() < 0.6),
                    "read_censored": False, "read_error": False,
                    "echoed": mention, "thinking_echo": False,
                    "used": used, "used_in_diff": used, "eligible": True,
                    "exposure_basis": "event_stream",
                    "first_exposure_seq": int(rng.integers(3, 40)) if did_read else None,
                    "exposure_channel": "tool_read" if did_read else None,
                    "first_used_seq": int(rng.integers(40, 80)) if used else None,
                    "first_mention_seq": int(rng.integers(40, 90)) if mention else None,
                    "ever_mention": mention,
                    "first_mention_probe": i0,
                    "last_mention_probe": (i0 + (lapse - 1 if lapse else n_probes - i0 - 1)) if mention else None,
                    "mention_run_length": (lapse if lapse else n_probes - (i0 or 0)) if mention else None,
                    "n_reinjections": n_probes if mention else None,
                    "first_use_probe_index": None,
                    "n_probes_observed": n_probes,
                    "at_risk_horizon": (n_probes - i0) if mention else None,
                    "lapse_probe_index": lapse,
                    "retention_censored": bool(mention and lapse is None),
                    "censoring_reason": ("administrative" if (mention and lapse is None)
                                         else (None if mention else "never_mentioned")),
                    "control_fire_rate": 0.0, "prior_check_status": "pass",
                    "success": bool(rng.random() < 0.6), "score_automated": None,
                    "analyzable": True, "exclusion_reason": None,
                    "quarantined": False, "probe_integrity": "ok",
                    "tool_calls_total": int(rng.integers(20, 40)),
                    "tool_calls_task": int(rng.integers(15, 35)),
                    "turns_total": int(rng.integers(20, 45)),
                    "tokens_accounting_version": "per_message_v2",
                })
                for k in range(n_probes):
                    named = bool(mention and i0 is not None and k >= i0 and
                                 (lapse is None or k < i0 + lapse))
                    probe_rows.append({
                        "schema_version": "1", "run_id": run_id, "job_id": "synthetic",
                        "task_id": task, "condition_id": arm, "rep": rep,
                        "probe_idx": k, "probe_id": f"WURP-{abs(hash(run_id)) % (16 ** 8):08x}-{k:03d}",
                        "outcome": "answered", "raw_response": "{}", "parse_ok": True,
                        "parse_tier": "strict", "retry_sent": False,
                        "answered_after_retry": False,
                        "mention_tier_a": named, "mention_tier_b": named,
                        "mention_primary": named, "mention_llm": None,
                        "n_slots": 3, "n_slots_matched": 1 if named else 0,
                        "n_critical_fact_slots": 1 if named else 0,
                        "n_filler_slots": 2 if named else 3,
                        "n_empty_slots": 0, "n_distrusted_slots": 0,
                        "n_task_restatement_slots": 0, "n_generic_workspace_slots": 0,
                        "fidelity_agree": bool(rng.random() < 0.7),
                        "sent_at_barrier": 2 * (k + 1),
                    })
    ft = pd.DataFrame(ft_rows)
    pr = pd.DataFrame(probe_rows)
    for col in ("read", "read_inbound_only", "opened", "used", "used_in_diff", "eligible",
                "ever_mention", "available", "analyzable", "quarantined", "success",
                "unexplained_possession", "echoed", "thinking_echo", "retention_censored",
                "read_censored", "read_error", "factor_fact_present", "factor_probe"):
        if col in ft.columns:
            ft[col] = ft[col].astype("boolean")
    for col in ("first_exposure_seq", "first_used_seq", "first_mention_seq",
                "first_mention_probe", "last_mention_probe", "mention_run_length",
                "n_reinjections", "first_use_probe_index", "n_probes_observed",
                "at_risk_horizon", "lapse_probe_index", "rep", "factor_distractors"):
        if col in ft.columns:
            ft[col] = pd.to_numeric(ft[col], errors="coerce").astype("Int64")
    for col in ("mention_tier_a", "mention_tier_b", "mention_primary", "mention_llm",
                "parse_ok", "retry_sent", "answered_after_retry", "fidelity_agree"):
        if col in pr.columns:
            pr[col] = pr[col].astype("boolean")
    return Tables(ft, pr, pd.DataFrame(), source="synthetic")


__all__ = [
    "UPTAKE_LIB_VERSION", "ARMS", "DEPTH_LADDER", "FORMAT_ARMS", "REACTIVITY_PAIRS",
    "AnalysisConfig", "DEFAULT_CONFIG", "Tables", "load_tables",
    "ExclusionReport", "analysis_frame", "alarm_frame",
    "wilson_ci", "Estimate", "rate_by_condition", "per_task_rates",
    "ClusterResult", "paired_cluster_t", "cluster_bootstrap_ci",
    "gamma_hat", "SecondaryResult", "cmh_test", "permutation_test_within_task",
    "read_rate_table", "incidental_exposure", "mention_rate",
    "use_rate_table", "use_lift",
    "RetentionResult", "retention_dataset", "common_horizon", "rmst_by_condition",
    "rmst_delta", "retention_reference_arm", "retention_table", "km_curves",
    "post_discharge_persistence",
    "probe_fidelity", "probe_quality", "slot_class_distribution",
    "confabulation_rate", "unexplained_possession_rate", "orthogonality_phi",
    "depth_sensitivity", "format_sensitivity", "probe_reactivity", "slot_precision_table",
    "funnel_table", "summarize",
    "plot_funnel", "plot_read_rate", "plot_lift", "plot_km", "plot_depth",
    "synthetic_tables",
]
