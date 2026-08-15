#!/usr/bin/env python3
"""
test_uptake_lib.py — the analysis layer's own tests, runnable without pytest.

RESPONSIBILITY
  Prove that the estimators recover what they claim to recover, on data whose
  truth is known by construction. Three of these are named gates in
   the analysis-layer companion to tests/test_wur_lib.py:
  known-lift recovery, censoring, and type-I.

  It also covers the exclusion rules that are easy to break silently and
  impossible to notice afterwards: unknown reads must not be coerced to false,
  quarantined control rows must stay in the confabulation numerator, weak facts
  must leave the primary frame, and nullable integer columns must survive the
  parquet round-trip as Int64 rather than as float64 NaN.

INPUTS   none — every fixture is synthetic and seeded
OUTPUTS  pass/fail counts on stdout; exit 1 if anything fails

RUN
  .venv-analysis/bin/python analysis/test_uptake_lib.py        # standalone
  .venv-analysis/bin/python -m pytest analysis/test_uptake_lib.py -q
"""
from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lib" / "wur"))

import aggregate as agg  # noqa: E402
import pilot_triage as triage  # noqa: E402
import power as P  # noqa: E402
import uptake_lib as U  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────
def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for c in ("read", "read_inbound_only", "used", "eligible", "ever_mention", "available",
              "analyzable", "quarantined", "success", "unexplained_possession", "echoed",
              "opened"):
        if c in df.columns:
            df[c] = df[c].astype("boolean")
    for c in ("rep", "lapse_probe_index", "at_risk_horizon", "first_mention_probe",
              "first_use_probe_index", "n_probes_observed"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    return df


def _binary_frame(per_task_arm: dict[str, dict[str, list[bool]]], outcome: str = "used"
                  ) -> pd.DataFrame:
    rows = []
    for task, arms in per_task_arm.items():
        for arm, vals in arms.items():
            for i, v in enumerate(vals):
                rows.append({"run_id": f"{task}-{arm}-{i}", "task_id": task,
                             "condition_id": arm, "rep": i, outcome: v,
                             "analyzable": True, "quarantined": False,
                             "prior_check_status": "pass"})
    return _frame(rows)


# ── 1. known-lift recovery ───────────────────────────────────────────────────
def test_known_lift_recovery_single_frame():
    """A planted lift of 0.30 must come back as 0.30 on a large exact-DGP frame."""
    t = U.synthetic_tables(n_tasks=40, n_reps=40, arms=("ctrl", "d2"),
                           use_rate_ctrl=0.20, use_lift_by_arm={"d2": 0.30},
                           tau=0.0, gamma=0.0, seed=101)
    res = U.paired_cluster_t(t.fact_trace, "d2", "ctrl", "used")
    assert res.n_tasks == 40, res.n_tasks
    # Judged against the estimator's OWN standard error, not a hand-picked epsilon:
    # a fixed tolerance either passes by luck or fails by luck.
    assert abs(res.mean_diff - 0.30) < 3 * res.se, (res.mean_diff, res.se)
    assert res.ci_low <= 0.30 <= res.ci_high, (res.ci_low, res.ci_high)
    assert res.p_value < 1e-6, res.p_value
    return (f"lift={res.mean_diff:.4f} (truth 0.30, se={res.se:.4f}, "
            f"95% CI [{res.ci_low:.4f}, {res.ci_high:.4f}] covers it), "
            f"p={res.p_value:.2e}, n_tasks={res.n_tasks}")


def test_known_lift_recovery_unbiased_and_covering():
    """Over 300 replicate experiments at the DESIGN size (12 tasks x 5 reps): the
    estimator is unbiased for the planted lift and its 95% interval covers."""
    truth = 0.25
    ests, covers = [], []
    for seed in range(300):
        t = U.synthetic_tables(n_tasks=12, n_reps=5, arms=("ctrl", "d2"),
                               use_rate_ctrl=0.10, use_lift_by_arm={"d2": truth},
                               tau=0.0, gamma=0.0, seed=1000 + seed)
        r = U.paired_cluster_t(t.fact_trace, "d2", "ctrl", "used")
        if r.mean_diff is None:
            continue
        ests.append(r.mean_diff)
        if r.ci_low is not None:
            covers.append(bool(r.ci_low <= truth <= r.ci_high))
    bias = float(np.mean(ests)) - truth
    mcse = float(np.std(ests, ddof=1)) / math.sqrt(len(ests))
    cov = float(np.mean(covers))
    assert abs(bias) < 3 * mcse, (bias, mcse)
    assert 0.90 <= cov <= 0.99, cov
    return (f"mean_est={np.mean(ests):.4f} (truth {truth}), bias={bias:+.4f} "
            f"(3*MCSE={3 * mcse:.4f}), coverage={cov:.3f}, n_sims={len(ests)}")


def test_lift_recovery_survives_heterogeneity():
    """gamma > 0 widens the interval but must not bias the point estimate."""
    out = {}
    for gamma in (0.0, 1.0):
        ests = []
        for seed in range(200):
            t = U.synthetic_tables(n_tasks=12, n_reps=5, arms=("ctrl", "d2"),
                                   use_rate_ctrl=0.20, use_lift_by_arm={"d2": 0.20},
                                   tau=0.5, gamma=gamma, seed=5000 + seed)
            r = U.paired_cluster_t(t.fact_trace, "d2", "ctrl", "used")
            if r.mean_diff is not None:
                ests.append(r.mean_diff)
        out[gamma] = (float(np.mean(ests)), float(np.std(ests, ddof=1)))
    assert out[1.0][1] > out[0.0][1], out
    return f"gamma=0: mean={out[0.0][0]:.4f} sd={out[0.0][1]:.4f} | gamma=1: mean={out[1.0][0]:.4f} sd={out[1.0][1]:.4f}"




# ── 2. censoring ─────────────────────────────────────────────────────────────
def test_censoring_dispositions():
    """All three dispositions land where they should, and never_mentioned is
    excluded from the dataset rather than censored at 0."""
    rows = [
        # lapsed at probe 3 after i0=1 -> event
        dict(run_id="a", task_id="t0", condition_id="d2", ever_mention=True,
             first_mention_probe=1, at_risk_horizon=9, lapse_probe_index=3,
             first_use_probe_index=None, analyzable=True, quarantined=False),
        # still mentioning at run end -> administrative censoring at B
        dict(run_id="b", task_id="t0", condition_id="d2", ever_mention=True,
             first_mention_probe=0, at_risk_horizon=7, lapse_probe_index=None,
             censoring_reason="administrative", first_use_probe_index=None,
             analyzable=True, quarantined=False),
        # run died mid-flight -> censored at the last observed probe
        dict(run_id="c", task_id="t0", condition_id="d2", ever_mention=True,
             first_mention_probe=2, at_risk_horizon=4, lapse_probe_index=None,
             censoring_reason="truncated_run", first_use_probe_index=None,
             analyzable=True, quarantined=False),
        # lapse AFTER first use -> post_discharge, censored at the pre-use horizon
        dict(run_id="d", task_id="t0", condition_id="d2", ever_mention=True,
             first_mention_probe=1, at_risk_horizon=9, lapse_probe_index=6,
             first_use_probe_index=3, analyzable=True, quarantined=False),
        # never mentioned -> EXCLUDED entirely
        dict(run_id="e", task_id="t0", condition_id="d2", ever_mention=False,
             first_mention_probe=None, at_risk_horizon=None, lapse_probe_index=None,
             censoring_reason="never_mentioned", first_use_probe_index=None,
             analyzable=True, quarantined=False),
    ]
    ds = U.retention_dataset(_frame(rows)).set_index("run_id")
    assert "e" not in ds.index, "never_mentioned must be excluded, not censored"
    assert (ds.loc["a", "duration"], ds.loc["a", "event"]) == (3.0, 1)
    assert (ds.loc["b", "duration"], ds.loc["b", "event"]) == (7.0, 0)
    assert ds.loc["b", "censoring_reason"] == "administrative"
    assert (ds.loc["c", "duration"], ds.loc["c", "event"]) == (4.0, 0)
    assert ds.loc["c", "censoring_reason"] == "truncated_run"
    assert (ds.loc["d", "duration"], ds.loc["d", "event"]) == (2.0, 0), ds.loc["d"].to_dict()
    assert ds.loc["d", "censoring_reason"] == "post_discharge"
    return ("event/administrative/truncated_run/post_discharge all correct; "
            "never_mentioned excluded (4 rows from 5)")


def test_censoring_rmst_unbiased():
    """RMST from censored data recovers the truth; the naive mean of observed
    durations does not. This is the whole reason for the survival machinery."""
    rng = np.random.default_rng(7)
    p, J, n = 0.25, 5, 6000
    true_rmst = sum((1 - p) ** u for u in range(J))  # sum_{u=0}^{J-1} S(u)
    T = rng.geometric(p, size=n).astype(float)       # lapse at 1,2,3,...
    # Random administrative censoring: a run ends when it ends, so the horizon
    # varies run to run and a third of runs stop before the RMST horizon.
    C = rng.integers(2, 11, size=n).astype(float)
    obs = np.minimum(T, C)
    ev = (T <= C).astype(int)
    rows = []
    for i, (d, e) in enumerate(zip(obs, ev)):
        rows.append(dict(run_id=f"r{i}", task_id=f"t{i % 12}", condition_id="d2",
                         ever_mention=True, first_mention_probe=0,
                         at_risk_horizon=int(d), lapse_probe_index=(int(d) if e else None),
                         first_use_probe_index=None, analyzable=True, quarantined=False))
    ds = U.retention_dataset(_frame(rows))
    km = U._km_rmst(ds["duration"].to_numpy(float), ds["event"].to_numpy(int), J)
    naive = float(np.minimum(ds["duration"], J).mean())
    assert abs(km - true_rmst) < 0.10, (km, true_rmst)
    assert naive < true_rmst - 0.05, (naive, true_rmst)
    return (f"KM RMST={km:.4f} vs truth {true_rmst:.4f} (|err|={abs(km - true_rmst):.4f}); "
            f"naive mean-of-observed={naive:.4f} is biased low as expected")


def test_rmst_matches_lifelines():
    """The numpy RMST used inside the bootstrap must equal lifelines' to 1e-9."""
    from lifelines import KaplanMeierFitter
    from lifelines.utils import restricted_mean_survival_time as rmst

    rng = np.random.default_rng(13)
    worst = 0.0
    for _ in range(8):
        T = rng.geometric(0.3, size=120).astype(float)
        E = (rng.random(120) < 0.75).astype(int)
        k = KaplanMeierFitter().fit(T, E)
        worst = max(worst, abs(float(rmst(k, t=6.0)) - U._km_rmst(T, E, 6.0)))
    assert worst < 1e-9, worst
    return f"max |numpy - lifelines| = {worst:.2e} over 8 fits"


def test_common_horizon_and_exploratory_flag():
    """J is the min over arms of the last time >=10% is still at risk, and J < 3
    flips the result to exploratory."""
    rows = []
    for i in range(20):
        rows.append(dict(run_id=f"a{i}", task_id=f"t{i % 5}", condition_id="d1",
                         ever_mention=True, first_mention_probe=0, at_risk_horizon=10,
                         lapse_probe_index=8, first_use_probe_index=None,
                         analyzable=True, quarantined=False))
        rows.append(dict(run_id=f"b{i}", task_id=f"t{i % 5}", condition_id="d3",
                         ever_mention=True, first_mention_probe=0, at_risk_horizon=10,
                         lapse_probe_index=2, first_use_probe_index=None,
                         analyzable=True, quarantined=False))
    res = U.retention_table(_frame(rows))
    assert res.horizon_J == 2.0, res.horizon_J
    assert res.exploratory is True
    assert "DESCRIPTIVE ONLY" in res.note
    return f"J={res.horizon_J} (min of 8 and 2), exploratory=True, note fired"


def test_retention_reference_is_not_the_control():
    """The fact-free control contributes zero retention rows by construction, so
    Delta-RMST must not be silently NaN against it."""
    t = U.synthetic_tables(n_tasks=8, n_reps=6, arms=("ctrl", "d1", "d3"),
                           read_rate_by_arm={"ctrl": 0.0, "d1": 0.9, "d3": 0.6},
                           tau=0.0, gamma=0.0, seed=31)
    res = U.retention_table(t)
    assert "ctrl" not in set(res.dataset["condition_id"])
    assert res.per_arm["delta_reference"].iloc[0] == "d1"
    assert res.per_arm["delta_rmst"].notna().all()
    assert "NOT a control-adjusted lift" in res.note
    return f"reference={res.per_arm['delta_reference'].iloc[0]}, J={res.horizon_J}, note states the caveat"




# ── 3. type-I ────────────────────────────────────────────────────────────────
def test_type_i_primary_is_nominal():
    """The PRIMARY cluster-level paired t holds ~0.05 across the heterogeneity
    range it was chosen for."""
    out = {}
    for gamma in (0.0, 0.5, 1.0):
        pt = P.power_at(0.0, p0=0.20, tau=0.5, gamma=gamma, tasks=12, reps=5,
                        nsim=4000, seed=424242)
        out[gamma] = (pt.power_primary, pt.coverage_primary)
        assert 0.03 <= pt.power_primary <= 0.075, (gamma, pt.power_primary)
        assert 0.92 <= pt.coverage_primary <= 0.98, (gamma, pt.coverage_primary)
    return " | ".join(f"gamma={g}: type-I={v[0]:.3f}, coverage={v[1]:.3f}" for g, v in out.items())




def test_secondary_tests_inflate_with_heterogeneity():
    """CMH and the within-task permutation inflate as gamma grows — the measured
    justification for demoting them."""
    lo = P.power_at(0.0, p0=0.50, tau=0.5, gamma=0.0, tasks=12, reps=5, nsim=3000,
                    seed=99, secondary=True, perm_draws=300)
    hi = P.power_at(0.0, p0=0.50, tau=0.5, gamma=2.0, tasks=12, reps=5, nsim=3000,
                    seed=99, secondary=True, perm_draws=300)
    assert hi.power_cmh > lo.power_cmh, (lo.power_cmh, hi.power_cmh)
    assert hi.power_perm > lo.power_perm, (lo.power_perm, hi.power_perm)
    assert hi.power_cmh > 0.08, hi.power_cmh
    assert abs(hi.power_primary - 0.05) < 0.02, hi.power_primary
    return (f"CMH {lo.power_cmh:.3f}->{hi.power_cmh:.3f}, "
            f"permutation {lo.power_perm:.3f}->{hi.power_perm:.3f}, "
            f"primary {lo.power_primary:.3f}->{hi.power_primary:.3f} as gamma 0->2")


def test_secondary_suppressed_above_gamma_half():
    """'s rule is executable: above gamma_hat 0.5 no secondary p-value is emitted."""
    rng = np.random.default_rng(3)
    tasks = {}
    for i in range(12):
        # alternating strong positive / strong negative task effects: mean ~0, gamma large
        if i % 2:
            arm, ctrl = [True] * 5, [False] * 5
        else:
            arm, ctrl = [False] * 5, [True] * 5
        tasks[f"t{i}"] = {"d2": arm, "ctrl": ctrl}
    df = _binary_frame(tasks)
    g = U.gamma_hat(df, "d2", "ctrl", "used")
    cmh = U.cmh_test(df, "d2", "ctrl", "used")
    perm = U.permutation_test_within_task(df, "d2", "ctrl", "used", draws=200)
    assert g.value > 0.5, g.value
    assert cmh.suppressed and cmh.p_value is None
    assert perm.suppressed and perm.p_value is None
    forced = U.cmh_test(df, "d2", "ctrl", "used", suppress=False)
    assert forced.p_value is not None
    return (f"gamma_hat={g.value:.3f} > 0.5 -> CMH and permutation suppressed "
            f"(forced CMH p={forced.p_value:.3g} still available for audit)")


# ── 4. exclusion rules that fail silently if broken ──────────────────────────
def test_unknown_read_is_not_false():
    """A null `read` leaves the denominator and shows up in the bracket."""
    rows = [dict(run_id=f"r{i}", task_id="t0", condition_id="d2", read=v,
                 analyzable=True, quarantined=False, prior_check_status="pass")
            for i, v in enumerate([True, True, False, None, None])]
    t = U.rate_by_condition(_frame(rows), "read")
    r = t.iloc[0]
    assert r["n"] == 3 and r["k"] == 2 and r["n_unknown"] == 2
    assert abs(r["rate"] - 2 / 3) < 1e-12
    assert abs(r["rate_if_unknown_false"] - 0.4) < 1e-12
    assert abs(r["rate_if_unknown_true"] - 0.8) < 1e-12
    return "rate=0.667 on n=3 known, 2 unknown bracketed [0.400, 0.800]"


def test_confab_keeps_quarantined_rows():
    """A control row that names the nonce is quarantined BY SCHEMA and is the entire
    confab numerator; the alarm frame must keep it."""
    rows = [
        dict(run_id="q", task_id="t0", condition_id="ctrl", available=False, read=False,
             ever_mention=True, echoed=False, unexplained_possession=False,
             analyzable=True, quarantined=True, prior_check_status="pass"),
        *[dict(run_id=f"c{i}", task_id="t0", condition_id="ctrl", available=False,
               read=False, ever_mention=False, echoed=False, unexplained_possession=False,
               analyzable=True, quarantined=False, prior_check_status="pass")
          for i in range(19)],
    ]
    df = _frame(rows)
    est = U.confabulation_rate(df)
    primary, rep = U.analysis_frame(df)
    assert est.detail["k"] == 1 and est.n == 20, est.to_dict()
    assert abs(est.value - 0.05) < 1e-12
    assert len(primary) == 19, len(primary)
    assert rep.dropped.get("quarantined") == 1
    return (f"confab numerator kept: {est.detail['k']}/{est.n} = {est.value:.3f}; "
            "the same row is dropped from the primary frame (19 of 20 kept)")


def test_weak_facts_excluded_from_primary():
    rows = [dict(run_id=f"r{i}", task_id="t0", condition_id="d2", used=True,
                 analyzable=True, quarantined=False,
                 prior_check_status=("weak" if i < 4 else "pass")) for i in range(10)]
    df = _frame(rows)
    kept, rep = U.analysis_frame(df)
    assert len(kept) == 6 and rep.dropped["weak_fact_prior_check"] == 4
    kept2, _ = U.analysis_frame(df, U.AnalysisConfig(exclude_weak_facts=False))
    assert len(kept2) == 10
    return "4 weak rows excluded from primary (10 -> 6); sensitivity config keeps all 10"


def test_nonanalyzable_excluded_with_reason():
    rows = [dict(run_id="a", task_id="t0", condition_id="d2", used=True, analyzable=False,
                 exclusion_reason="plant_missing", quarantined=False,
                 prior_check_status="pass"),
            dict(run_id="b", task_id="t0", condition_id="d2", used=True, analyzable=True,
                 exclusion_reason=None, quarantined=False, prior_check_status="pass")]
    kept, rep = U.analysis_frame(_frame(rows))
    assert len(kept) == 1
    assert rep.dropped.get("not_analyzable:plant_missing") == 1
    return "plant_missing exclusion recorded by reason, not as a silent drop"


def test_read_rate_table_carries_sensitivity_row():
    t = U.synthetic_tables(n_tasks=4, n_reps=4, arms=("ctrl", "d2"), tau=0.0, seed=5)
    tab = U.read_rate_table(t)
    assert set(tab["definition"]) == {"read", "read_inbound_only"}
    return "read_rate_table emits both the primary and the mandatory inbound-only row"


def test_use_rate_reports_cond_and_uncond():
    rows = []
    for i in range(10):
        rows.append(dict(run_id=f"r{i}", task_id="t0", condition_id="d2",
                         used=(i < 3), eligible=(i < 5), analyzable=True,
                         quarantined=False, prior_check_status="pass"))
    t = U.use_rate_table(_frame(rows))
    r = t.iloc[0]
    assert abs(r["use_rate_uncond"] - 0.3) < 1e-12
    assert abs(r["use_rate_cond"] - 0.6) < 1e-12
    assert bool(r["appendix_only"]) is True
    return "use_rate_uncond=0.300 and use_rate_cond=0.600 emitted together, flagged appendix_only"




def test_gamma_hat_zero_when_homogeneous():
    tasks = {f"t{i}": {"d2": [True] * 4 + [False] * 1, "ctrl": [True] * 1 + [False] * 4}
             for i in range(10)}
    g = U.gamma_hat(_binary_frame(tasks), "d2", "ctrl", "used")
    assert g.value is not None and g.value < 0.35, g.value
    return f"gamma_hat={g.value:.4f} on an exactly homogeneous design"


# ── 5. aggregate: shape, dtypes, round-trip ──────────────────────────────────
def _write_job(tmp: Path, tables: U.Tables, *, n_runs_override: int | None = None) -> Path:
    """Explode synthetic Tables back into per-run dirs, the way the harness writes them."""
    job = tmp / "jobs" / "synthjob"
    (job).mkdir(parents=True, exist_ok=True)
    (job / "job.yaml").write_text("job_id: synthjob\nexperiment: wur\n", encoding="utf-8")
    runs_root = tmp / "runs"
    (runs_root / "synthjob").mkdir(parents=True, exist_ok=True)
    ft = tables.fact_trace
    pr = tables.probes
    for run_id, sub in ft.groupby("run_id"):
        rd = runs_root / "synthjob" / str(run_id)
        rd.mkdir(parents=True, exist_ok=True)
        rows = json.loads(sub.to_json(orient="records"))
        for r in rows:
            r["factors"] = {k: r.pop(f"factor_{k}", None) for k in agg.FACTOR_KEYS}
        (rd / "fact_trace.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        prows = json.loads(pr[pr["run_id"] == run_id].to_json(orient="records"))
        for r in prows:
            r["slots"] = [{"slot_idx": i, "fact": "x", "source": "s",
                           "affects_next_action": True, "slot_class": "filler",
                           "match_nonce": bool(r.get("mention_tier_a")) and i == 0,
                           "match_regex": False, "match_llm": None,
                           "source_verified": None} for i in range(3)]
        (rd / "probes.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in prows), encoding="utf-8")
        (rd / "events.jsonl").write_text(json.dumps({
            "schema_version": "1", "run_id": run_id, "seq": 0, "type": "assistant",
            "is_probe_turn": False, "message_id": "m0", "nonce_hits": [],
            "tokens_in": 10, "tokens_out": 5}) + "\n", encoding="utf-8")
        first = rows[0]
        (rd / "run_meta.json").write_text(json.dumps({
            "run_id": run_id, "job_id": "synthjob", "task_id": first["task_id"],
            "condition_id": first["condition_id"], "rep": first.get("rep")}),
            encoding="utf-8")
        (rd / "run_record.json").write_text(json.dumps({
            "schema_version": "2",
            "run": {"run_id": run_id, "experiment_id": "synthjob", "replication": 1,
                    "timestamp_start": "2026-08-05T00:00:00Z",
                    "timestamp_end": "2026-08-05T00:10:00Z", "experiment": "wur"},
            "condition": {"task_id": first["task_id"], "env_id": first["condition_id"],
                          "agent_id": "claude-sonnet-5", "agent_provider": "anthropic",
                          "base_repo_sha": "0" * 40},
            "outcome": {"terminal_state": "completed", "score_automated": 1.0},
            "tokens": {"total_input": 100, "total_output": 10, "cache_read": 0,
                       "cache_write": 0, "context_acquisition": 0,
                       "accounting_version": "per_message_v2", "result_input": 100,
                       "result_output": 10, "dedupe_delta_input": 0,
                       "dedupe_delta_output": 0},
            "timing": {"wall_clock_seconds": 600, "time_to_first_edit_seconds": 30},
            "navigation": {"file_reads_total": 3, "file_reads_unique": 3, "nav_entropy": 1.0,
                           "nav_entropy_pre_edit": 1.0, "files_read_sequence": [],
                           "files_edited": []},
            "operations": {"tool_calls_total": 20, "bash_calls": 1, "search_calls": 1,
                           "git_calls": 0, "web_searches": 0, "agent_spawns": 0,
                           "replanning_events": 0, "message_count": 40,
                           "turns_total": 20, "max_tool_uses_per_message": 1},
            "raw": {"transcript_path": "transcript.jsonl", "patch_path": "git.patch",
                    "grade_path": "judge.json"}}), encoding="utf-8")
        (rd / "hygiene.json").write_text(json.dumps({"ok": True, "failed": [],
                                                     "ambient_memory": []}), encoding="utf-8")
        (rd / "reconcile.json").write_text(json.dumps(
            {"gates": {"join_coverage": {"value": 1.0, "threshold": 0.99, "pass": True}}}),
            encoding="utf-8")
    return job


def test_aggregate_roundtrip_preserves_int64_nulls():
    """The Phase-0 trap: a null first_exposure_seq must come back as <NA>, not NaN.
    float64 round-trip defeats trace.py's own d0-push null assertion."""
    tmp = Path(tempfile.mkdtemp(prefix="wur-agg-"))
    try:
        t = U.synthetic_tables(n_tasks=3, n_reps=2, arms=("ctrl", "d2"), tau=0.0, seed=17)
        job = _write_job(tmp, t)
        man = agg.aggregate(job, runs_root=tmp / "runs")
        assert man["tables"]["fact_trace"]["rows"] == len(t.fact_trace), man["tables"]
        assert man["n_run_dirs"] == t.fact_trace["run_id"].nunique()
        back = U.load_tables(Path(man["out_dir"]))
        ft = back.fact_trace
        assert str(ft["first_exposure_seq"].dtype) == "Int64", ft["first_exposure_seq"].dtype
        assert str(ft["read"].dtype) == "boolean", ft["read"].dtype
        nulls = ft.loc[ft["condition_id"] == "ctrl", "first_exposure_seq"]
        assert nulls.isna().all()
        assert nulls.iloc[0] is pd.NA, type(nulls.iloc[0])
        assert set(ft["factor_depth"].dropna()) <= {"d0", "d1", "d2", "d3"}
        # csv mirror exists and reloads
        csv = pd.read_csv(Path(man["out_dir"]) / "fact_trace.csv")
        assert len(csv) == len(ft)
        return (f"{man['tables']['fact_trace']['rows']} fact_trace rows, "
                f"{man['tables']['probes']['rows']} probe rows; first_exposure_seq dtype=Int64, "
                "null round-trips as pd.NA (not NaN); csv mirror matches")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_aggregate_flattens_slots_and_mention_tiers():
    tmp = Path(tempfile.mkdtemp(prefix="wur-agg2-"))
    try:
        t = U.synthetic_tables(n_tasks=2, n_reps=2, arms=("d2",), tau=0.0, seed=23)
        job = _write_job(tmp, t)
        man = agg.aggregate(job, runs_root=tmp / "runs")
        pr = U.load_tables(Path(man["out_dir"])).probes
        for c in ("slot0_fact", "slot2_slot_class", "mention_primary", "mention_tier_a",
                  "n_slots", "n_filler_slots"):
            assert c in pr.columns, c
        assert (pr["n_slots"] == 3).all()
        assert (pr["mention_primary"].fillna(False) == pr["mention_tier_a"].fillna(False)).all()
        return f"{len(pr)} probe rows flattened: slot0..slot2 columns + folded mention tiers"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 6. pilot triage ──────────────────────────────────────────────────────────
def test_pilot_triage_all_gates_pass_on_clean_job():
    tmp = Path(tempfile.mkdtemp(prefix="wur-triage-"))
    try:
        t = U.synthetic_tables(n_tasks=4, n_reps=3, arms=("ctrl", "d1", "d3"),
                               read_rate_by_arm={"ctrl": 0.0, "d1": 0.8, "d3": 0.4},
                               tau=0.0, gamma=0.0, seed=41)
        job = _write_job(tmp, t)
        runs = triage.load_runs(job, tmp / "runs")
        res = triage.triage(runs)
        failed = [g for g in res["gates"] if g["status"] == "fail"]
        assert not failed, [(g["id"], g["value"]) for g in failed]
        assert res["blocked"] is False
        assert res["n_unevaluable"] <= 1, res["unevaluable"]
        return (f"{res['n_runs']} runs: {res['n_passed']} gates pass, "
                f"{res['n_failed']} fail, {res['n_unevaluable']} unevaluable, blocked=False")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pilot_triage_catches_a_breach():
    """Break the pacing field and a probe refusal; the triage must block."""
    tmp = Path(tempfile.mkdtemp(prefix="wur-triage2-"))
    try:
        t = U.synthetic_tables(n_tasks=4, n_reps=3, arms=("ctrl", "d1", "d3"),
                               read_rate_by_arm={"ctrl": 0.0, "d1": 0.8, "d3": 0.4},
                               tau=0.0, seed=43)
        job = _write_job(tmp, t)
        run_dirs = sorted((tmp / "runs" / "synthjob").iterdir())
        for rd in run_dirs[:6]:
            rec = json.loads((rd / "run_record.json").read_text())
            rec["operations"]["max_tool_uses_per_message"] = 4
            (rd / "run_record.json").write_text(json.dumps(rec))
        probes = [json.loads(l) for l in (run_dirs[0] / "probes.jsonl").read_text().splitlines()]
        probes[0]["outcome"] = "refused"
        (run_dirs[0] / "probes.jsonl").write_text(
            "".join(json.dumps(p) + "\n" for p in probes))
        res = triage.triage(triage.load_runs(job, tmp / "runs"))
        assert res["blocked"] is True
        assert "G7" in res["failed"] and "G8" in res["failed"], res["failed"]
        return f"blocked=True, failed gates {res['failed']} (refusal + pacing)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pilot_triage_unevaluable_does_not_block_by_default():
    tmp = Path(tempfile.mkdtemp(prefix="wur-triage3-"))
    try:
        t = U.synthetic_tables(n_tasks=2, n_reps=2, arms=("ctrl", "d1"), tau=0.0, seed=47)
        job = _write_job(tmp, t)
        for rd in (tmp / "runs" / "synthjob").iterdir():
            (rd / "hygiene.json").unlink()
            (rd / "reconcile.json").unlink()
        res = triage.triage(triage.load_runs(job, tmp / "runs"))
        assert res["blocked"] is False
        assert "G12" in res["unevaluable"] and "G13" in res["unevaluable"]
        strict = triage.triage(triage.load_runs(job, tmp / "runs"), require_all=True)
        assert strict["blocked"] is True
        return (f"unevaluable {res['unevaluable']} -> blocked=False by default, "
                "blocked=True under --require-all")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 7. power module sanity ───────────────────────────────────────────────────
def test_power_solves_the_null_exactly():
    """target_rd = 0 must produce an average risk difference of exactly 0, even at
    gamma > 0 where a mean-zero logit shift does NOT."""
    m = P.solve_m(0.05, 0.5)
    d = P.solve_delta(m, 0.5, 1.0, 0.0)
    assert abs(P.mean_risk_difference(m, 0.5, d, 1.0)) < 1e-10
    naive = P.mean_risk_difference(m, 0.5, 0.0, 1.0)
    assert naive > 1e-3, naive
    assert abs(P.marginal_rate(m, 0.5) - 0.05) < 1e-10
    return (f"delta_logit={d:+.4f} gives E[RD]=0 exactly; delta=0 would give "
            f"E[RD]={naive:+.4f} (the convexity trap)")


def test_power_mdd_increases_with_heterogeneity():
    a = P.mdd(p0=0.20, tau=0.5, gamma=0.0, nsim=1500, seed=8)
    b = P.mdd(p0=0.20, tau=0.5, gamma=1.0, nsim=1500, seed=8)
    assert a["mdd"] < b["mdd"], (a["mdd"], b["mdd"])
    return f"MDD {a['mdd']:.3f} (gamma 0) -> {b['mdd']:.3f} (gamma 1) at p0=0.20"


def test_power_fast_path_equals_uptake_lib():
    res = P.check_agreement(n_frames=4, tasks=8, reps=5)
    assert res["ok"], res
    return f"max abs diff {json.dumps(res['max_abs_diff'])}"


# ── runner ───────────────────────────────────────────────────────────────────
def _all_tests():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


def main() -> int:
    passed = failed = 0
    for name, fn in _all_tests():
        try:
            detail = fn()
            passed += 1
            print(f"PASS {name}\n     {detail if detail else ''}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}\n     {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}\n     {type(exc).__name__}: {exc}")
    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
