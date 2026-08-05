#!/usr/bin/env python3
"""
power.py — what this design can actually detect, by simulation rather than by assertion.

RESPONSIBILITY
  Replace the MDD headline with a number. IMPLEMENTATION.md §11 S8 says the
  minimum detectable difference "is a guess" and that the pre-registration cannot
  be frozen without it; this file computes it for the 12 x 12 x 5 design under the
  PRIMARY estimator (the cluster-level paired t on per-task risk differences),
  publishes the realized CI coverage next to it, and measures the type-I error of
  the two SECONDARY tests across the heterogeneity range that motivated demoting
  them.

  It also re-runs against real data: `--pilot-dir` reads the pilot's aggregate
  tables, estimates (p0, tau, gamma) from them, and recomputes every number with
  the design's own measurements instead of the design's assumptions.

THE DATA-GENERATING PROCESS (and why it is parameterized this way)
  task t:      a_t = m + tau * Z1          baseline log-odds, Z1 ~ N(0,1)
  arm effect:  b_t = delta + gamma * Z2    log-odds shift, Z2 ~ N(0,1)
  outcomes:    k_ctrl,t ~ Binom(R, expit(a_t)),  k_arm,t ~ Binom(R, expit(a_t + b_t))

  gamma is TASK x ARM HETEROGENEITY on the logit scale — the exact quantity
  uptake_lib.gamma_hat() estimates from data and the exact quantity §12 uses to
  decide whether the secondary tests may be reported at all.

  m and delta are never quoted directly. m is solved so the MARGINAL control rate
  equals the requested p0, and delta is solved so the TRUE AVERAGE RISK
  DIFFERENCE E_t[p_arm,t - p_ctrl,t] equals the requested target. Without that
  second solve the null is not where you think it is: expit is convex below 0.5,
  so a mean-zero logit shift produces a POSITIVE average risk difference, and a
  "type-I" simulation run that way is measuring power against a small true effect
  and calling it error. Both solves use Gauss-Hermite quadrature, so they are
  deterministic.

WHAT IS PRIMARY HERE
  primary      cluster-level paired t on the T per-task risk differences (n = T)
  secondary    CMH across task strata; within-task label permutation
  Both secondaries are simulated WITHOUT the §12 suppression rule, because the
  point is to measure the error rate the rule exists to prevent.

INPUTS   none (self-contained), or --pilot-dir <aggregate output dir>
OUTPUTS  a markdown report on stdout; --json writes the same numbers as a dict

CLI
  python3 analysis/power.py                       # the headline tables
  python3 analysis/power.py --json analysis/power_results.json
  python3 analysis/power.py --pilot-dir jobs/<id>/analysis --outcome used \\
                            --arm d2 --control ctrl
  python3 analysis/power.py --check              # vectorized fast path == uptake_lib
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from scipy import stats
from scipy.optimize import brentq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import uptake_lib as U  # noqa: E402

POWER_VERSION = "wur-power-v1"

#: The design of IMPLEMENTATION.md §7.2: 12 tasks, 12 arms, 5 reps = 720 main runs.
#: Task is the unit of generalization, so the n that drives every interval is 12.
DESIGN_TASKS = 12
DESIGN_REPS = 5
DESIGN_ARMS = 12
#: 9 treatment arms are contrasted against `ctrl` (12 arms - ctrl - ctrl-nofile - ...).
#: Used only for the Bonferroni column; the pre-registered PRIMARY contrast is one.
DESIGN_FAMILY = 9

GH_NODES = 64


def _gh():
    x, w = np.polynomial.hermite.hermgauss(GH_NODES)
    return x, w / math.sqrt(math.pi)


_GH_X, _GH_W = _gh()


def expit(x):
    return 1.0 / (1.0 + np.exp(-x))


def logit(p: float) -> float:
    return math.log(p / (1.0 - p))


# ── solving the DGP so the reported quantities mean what they say ────────────
def marginal_rate(m: float, tau: float) -> float:
    """E_t[expit(a_t)] under a_t = m + tau*Z."""
    return float(np.sum(_GH_W * expit(m + math.sqrt(2.0) * tau * _GH_X)))


def solve_m(p0: float, tau: float) -> float:
    """The task-mean log-odds whose MARGINAL rate is p0. With tau > 0 the naive
    logit(p0) is not it, and the whole MDD table is quoted in marginal units."""
    if tau <= 0:
        return logit(p0)
    return float(brentq(lambda m: marginal_rate(m, tau) - p0, -30.0, 30.0, xtol=1e-12))


def mean_risk_difference(m: float, tau: float, delta: float, gamma: float) -> float:
    """E_t[expit(a_t + b_t) - expit(a_t)] — the estimand of the primary test."""
    a = m + math.sqrt(2.0) * tau * _GH_X                      # (n,)
    b = delta + math.sqrt(2.0) * gamma * _GH_X                # (n,)
    arm = np.sum(_GH_W[None, :] * expit(a[:, None] + b[None, :]), axis=1)
    return float(np.sum(_GH_W * (arm - expit(a))))


def solve_delta(m: float, tau: float, gamma: float, target_rd: float) -> float:
    """The log-odds shift whose average risk difference is exactly `target_rd`.

    target_rd = 0 does NOT give delta = 0 when gamma > 0: expit is convex below
    0.5, so a mean-zero logit shift lifts the average rate. Getting this wrong
    turns a type-I simulation into a small-effect power simulation.
    """
    f = lambda d: mean_risk_difference(m, tau, d, gamma) - target_rd  # noqa: E731
    lo, hi = -30.0, 30.0
    if f(lo) > 0 or f(hi) < 0:
        raise ValueError(f"target_rd={target_rd} unreachable at p0-margin m={m:.3f}, gamma={gamma}")
    return float(brentq(f, lo, hi, xtol=1e-12))


# ── the vectorized estimators (checked against uptake_lib by --check) ────────
def draw(T: int, R: int, m: float, tau: float, delta: float, gamma: float,
         nsim: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """(k_arm, k_ctrl), each (nsim, T) counts of successes out of R."""
    a = m + tau * rng.standard_normal((nsim, T))
    b = delta + gamma * rng.standard_normal((nsim, T))
    p_c = expit(a)
    p_a = expit(a + b)
    return rng.binomial(R, p_a), rng.binomial(R, p_c)


def primary_paired_t(k_arm: np.ndarray, k_ctrl: np.ndarray, R: int,
                     alpha: float = 0.05) -> dict[str, np.ndarray]:
    """PRIMARY: one-sample t over the T per-task risk differences, vectorized.

    Identical arithmetic to uptake_lib.paired_cluster_t; --check asserts they
    agree to 1e-12 on real frames, so the fast path can never drift from the
    estimator the paper reports.
    """
    d = (k_arm - k_ctrl) / float(R)
    T = d.shape[1]
    mean = d.mean(axis=1)
    sd = d.std(axis=1, ddof=1)
    se = sd / math.sqrt(T)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(se > 0, mean / se, np.where(mean == 0, 0.0, np.inf * np.sign(mean)))
    p = 2 * stats.t.sf(np.abs(t), T - 1)
    crit = float(stats.t.ppf(1 - alpha / 2, T - 1))
    return {"estimate": mean, "se": se, "t": t, "p": p,
            "ci_low": mean - crit * se, "ci_high": mean + crit * se,
            "reject": p < alpha}


def cmh_vec(k_arm: np.ndarray, k_ctrl: np.ndarray, R: int, alpha: float = 0.05,
            correct: bool = True) -> dict[str, np.ndarray]:
    """SECONDARY: Cochran-Mantel-Haenszel across task strata, vectorized."""
    a = k_arm.astype(float)
    b = R - a
    c = k_ctrl.astype(float)
    d = R - c
    n = float(2 * R)
    num = (a - (a + b) * (a + c) / n).sum(axis=1)
    den = ((a + b) * (c + d) * (a + c) * (b + d) / (n * n * (n - 1))).sum(axis=1)
    adj = 0.5 if correct else 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        chi2 = np.where(den > 0, np.maximum(np.abs(num) - adj, 0.0) ** 2 / den, 0.0)
    p = stats.chi2.sf(chi2, 1)
    return {"chi2": chi2, "p": p, "reject": p < alpha}


def permutation_vec(k_arm: np.ndarray, k_ctrl: np.ndarray, R: int, alpha: float = 0.05,
                    draws: int = 400, rng: np.random.Generator | None = None) -> dict[str, np.ndarray]:
    """SECONDARY: within-task label permutation, drawn EXACTLY.

    Permuting arm/control labels within a task and recounting successes is, for
    binary outcomes, a hypergeometric draw: k_arm* ~ Hypergeom(2R, k_total, R).
    So the permutation null is sampled directly instead of by shuffling arrays —
    same distribution, ~100x cheaper, which is what makes 2,000 simulations of a
    permutation test affordable.
    """
    rng = rng or np.random.default_rng(0)
    nsim, T = k_arm.shape
    obs = np.abs(((k_arm - k_ctrl) / float(R)).mean(axis=1))
    total = k_arm + k_ctrl
    ngood = total
    nbad = 2 * R - total
    hits = np.zeros(nsim, dtype=int)
    for _ in range(draws):
        kp = rng.hypergeometric(ngood, nbad, R)
        stat = np.abs(((kp - (total - kp)) / float(R)).mean(axis=1))
        hits += (stat >= obs - 1e-12)
    p = (1.0 + hits) / (1.0 + draws)
    return {"p": p, "reject": p < alpha}


# ── power / type-I / coverage ────────────────────────────────────────────────
@dataclass
class PowerPoint:
    p0: float
    tau: float
    gamma: float
    target_rd: float
    tasks: int
    reps: int
    alpha: float
    nsim: int
    power_primary: float
    power_cmh: float | None
    power_cmh_uncorrected: float | None
    power_perm: float | None
    coverage_primary: float
    mean_estimate: float
    mean_ci_width: float
    delta_logit: float
    m: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def power_at(target_rd: float, *, p0: float = 0.05, tau: float = 0.5, gamma: float = 0.0,
             tasks: int = DESIGN_TASKS, reps: int = DESIGN_REPS, alpha: float = 0.05,
             nsim: int = 4000, seed: int = 20260805,
             secondary: bool = False, perm_draws: int = 400) -> PowerPoint:
    """Power, and the realized coverage of the primary 95% interval, at one point."""
    rng = np.random.default_rng(seed)
    m = solve_m(p0, tau)
    delta = solve_delta(m, tau, gamma, target_rd)
    k_a, k_c = draw(tasks, reps, m, tau, delta, gamma, nsim, rng)
    pri = primary_paired_t(k_a, k_c, reps, alpha)
    cov = float(np.mean((pri["ci_low"] <= target_rd) & (target_rd <= pri["ci_high"])))
    p_cmh = p_cmh_unc = p_perm = None
    if secondary:
        p_cmh = float(np.mean(cmh_vec(k_a, k_c, reps, alpha, correct=True)["reject"]))
        p_cmh_unc = float(np.mean(cmh_vec(k_a, k_c, reps, alpha, correct=False)["reject"]))
        p_perm = float(np.mean(permutation_vec(k_a, k_c, reps, alpha,
                                               draws=perm_draws, rng=rng)["reject"]))
    return PowerPoint(p0=p0, tau=tau, gamma=gamma, target_rd=target_rd, tasks=tasks,
                      reps=reps, alpha=alpha, nsim=nsim,
                      power_primary=float(np.mean(pri["reject"])),
                      power_cmh=p_cmh, power_cmh_uncorrected=p_cmh_unc, power_perm=p_perm,
                      coverage_primary=cov,
                      mean_estimate=float(np.mean(pri["estimate"])),
                      mean_ci_width=float(np.mean(pri["ci_high"] - pri["ci_low"])),
                      delta_logit=delta, m=m)


def mdd(*, p0: float = 0.05, tau: float = 0.5, gamma: float = 0.0,
        tasks: int = DESIGN_TASKS, reps: int = DESIGN_REPS, alpha: float = 0.05,
        target_power: float = 0.80, nsim: int = 4000, seed: int = 20260805,
        lo: float = 0.001, hi: float | None = None, iters: int = 22) -> dict[str, Any]:
    """Minimum detectable difference: the smallest TRUE AVERAGE RISK DIFFERENCE the
    primary test rejects at `target_power`.

    Bisection on the risk-difference scale (not the logit scale) because that is
    the scale the finding is quoted on: "a fact at d3 is used X percentage points
    less often than at d2".
    """
    hi = hi if hi is not None else min(0.95, 1.0 - p0 - 1e-3)
    f_lo = power_at(lo, p0=p0, tau=tau, gamma=gamma, tasks=tasks, reps=reps,
                    alpha=alpha, nsim=nsim, seed=seed)
    if f_lo.power_primary >= target_power:
        return {"mdd": lo, "power": f_lo.power_primary, "bracketed": False,
                "note": "power exceeds target at the smallest evaluated effect",
                "point": f_lo.to_dict()}
    f_hi = power_at(hi, p0=p0, tau=tau, gamma=gamma, tasks=tasks, reps=reps,
                    alpha=alpha, nsim=nsim, seed=seed)
    if f_hi.power_primary < target_power:
        return {"mdd": None, "power": f_hi.power_primary, "bracketed": False,
                "note": f"target power {target_power} unreachable up to RD={hi:.3f}",
                "point": f_hi.to_dict()}
    a, b = lo, hi
    point = f_hi
    for _ in range(iters):
        mid = 0.5 * (a + b)
        pt = power_at(mid, p0=p0, tau=tau, gamma=gamma, tasks=tasks, reps=reps,
                      alpha=alpha, nsim=nsim, seed=seed)
        if pt.power_primary >= target_power:
            b, point = mid, pt
        else:
            a = mid
        if b - a < 0.002:
            break
    return {"mdd": b, "power": point.power_primary, "bracketed": True,
            "coverage": point.coverage_primary, "mean_ci_width": point.mean_ci_width,
            "point": point.to_dict()}


def type_i_table(*, p0s: Sequence[float] = (0.05, 0.20, 0.50), tau: float = 0.5,
                 gammas: Sequence[float] = (0.0, 0.5, 1.0, 2.0),
                 tasks: int = DESIGN_TASKS, reps: int = DESIGN_REPS,
                 alpha: float = 0.05, nsim: int = 4000, seed: int = 20260805,
                 perm_draws: int = 400) -> list[dict[str, Any]]:
    """Type-I error of all three tests under the TRUE null (average RD exactly 0),
    across the heterogeneity range AND the base-rate range.

    This is the table that demotes CMH and the permutation test. Both condition
    on the stratum margins, so they test the SHARP null "this run's outcome would
    be identical under the other label" — which gamma > 0 makes false even when
    the population effect is exactly zero.

    p0 is a loop dimension because it changes the answer completely. At p0 = 0.05
    with R = 5 reps almost every stratum is 0-or-1 successes out of 5 and the
    discreteness makes every test conservative — the inflation is real but it
    hides behind the granularity. It shows up plainly at p0 >= 0.2, which is the
    regime the read-rate contrasts live in.
    """
    rows = []
    for p0 in p0s:
        for g in gammas:
            pt = power_at(0.0, p0=p0, tau=tau, gamma=g, tasks=tasks, reps=reps, alpha=alpha,
                          nsim=nsim, seed=seed + int(g * 1000) + int(p0 * 97),
                          secondary=True, perm_draws=perm_draws)
            mc = 1.96 * math.sqrt(alpha * (1 - alpha) / nsim)
            rows.append({"gamma": g, "p0": p0, "tau": tau, "tasks": tasks, "reps": reps,
                         "nsim": nsim, "alpha": alpha, "mc_halfwidth": mc,
                         "type_i_primary": pt.power_primary,
                         "type_i_cmh": pt.power_cmh,
                         "type_i_cmh_uncorrected": pt.power_cmh_uncorrected,
                         "type_i_permutation": pt.power_perm,
                         "coverage_primary": pt.coverage_primary,
                         "delta_logit_at_null": pt.delta_logit})
    return rows


def mdd_table(*, p0s: Sequence[float] = (0.05, 0.20, 0.50), tau: float = 0.5,
              gammas: Sequence[float] = (0.0, 0.5, 1.0),
              tasks: int = DESIGN_TASKS, reps: int = DESIGN_REPS,
              alpha: float = 0.05, family: int = DESIGN_FAMILY,
              nsim: int = 4000, seed: int = 20260805) -> list[dict[str, Any]]:
    """MDD at 80% power for every (control rate, heterogeneity) cell, unadjusted and
    Bonferroni-adjusted for the family of treatment-vs-control contrasts."""
    rows = []
    for p0 in p0s:
        for g in gammas:
            un = mdd(p0=p0, tau=tau, gamma=g, tasks=tasks, reps=reps, alpha=alpha,
                     nsim=nsim, seed=seed)
            bo = mdd(p0=p0, tau=tau, gamma=g, tasks=tasks, reps=reps,
                     alpha=alpha / family, nsim=nsim, seed=seed + 1)
            rows.append({"p0": p0, "tau": tau, "gamma": g, "tasks": tasks, "reps": reps,
                         "alpha": alpha, "family": family,
                         "mdd": un.get("mdd"), "power_at_mdd": un.get("power"),
                         "coverage_at_mdd": un.get("coverage"),
                         "ci_width_at_mdd": un.get("mean_ci_width"),
                         "mdd_bonferroni": bo.get("mdd"),
                         "note": un.get("note", "")})
    return rows


# ── calibration against real pilot data ──────────────────────────────────────
def calibrate_from_pilot(pilot_dir: str | Path, *, outcome: str = "used",
                         arm: str = "d2", control: str = "ctrl",
                         cfg: U.AnalysisConfig = U.DEFAULT_CONFIG) -> dict[str, Any]:
    """Estimate (p0, tau, gamma) from a real aggregate directory.

    p0    the control arm's marginal outcome rate
    tau   SD across tasks of the control log-odds (0.5 continuity correction)
    gamma uptake_lib.gamma_hat — the same DerSimonian-Laird estimator the
          suppression rule in §12 is stated in, so the power study and the
          decision rule cannot disagree about what gamma means.
    """
    tables = U.load_tables(pilot_dir)
    df, excl = U.analysis_frame(tables, cfg)
    sub = df[df["condition_id"].isin([arm, control])]
    ctrl_rows = sub[sub["condition_id"] == control]
    vals = U._bool_series(ctrl_rows, outcome).dropna()
    p0 = float(vals.mean()) if len(vals) else float("nan")
    lo = []
    for _, g in ctrl_rows.groupby("task_id", dropna=False):
        v = U._bool_series(g, outcome).dropna()
        if not len(v):
            continue
        k, n = float(v.sum()), float(len(v))
        lo.append(math.log((k + 0.5) / (n - k + 0.5)))
    tau = float(np.std(lo, ddof=1)) if len(lo) > 1 else 0.0
    g_est = U.gamma_hat(sub, arm, control, outcome)
    per_task = U.per_task_rates(sub, outcome)
    return {"pilot_dir": str(pilot_dir), "outcome": outcome, "arm": arm, "control": control,
            "p0": p0, "tau": tau, "gamma": g_est.value,
            "n_tasks": int(per_task["task_id"].nunique()),
            "n_reps_median": float(per_task["n"].median()) if len(per_task) else None,
            "n_runs": int(len(sub)), "exclusions": excl.to_dict(),
            "gamma_detail": g_est.to_dict()}


# ── the fast path must equal the reported estimator ──────────────────────────
def check_agreement(n_frames: int = 6, tasks: int = 8, reps: int = 5,
                    seed: int = 11) -> dict[str, Any]:
    """Assert the vectorized simulator computes what uptake_lib computes.

    A power study whose estimator is not the paper's estimator measures nothing.
    Compares the paired t (estimate, CI, p), CMH chi2/p, and the permutation
    p-value (which is stochastic, so it is compared to a tolerance rather than to
    machine precision).
    """
    import pandas as pd

    rng = np.random.default_rng(seed)
    worst = {"t_p": 0.0, "t_est": 0.0, "t_ci": 0.0, "cmh_chi2": 0.0, "cmh_p": 0.0,
             "perm_p": 0.0}
    for _ in range(n_frames):
        m = solve_m(0.3, 0.5)
        delta = solve_delta(m, 0.5, 0.3, 0.15)
        k_a, k_c = draw(tasks, reps, m, 0.5, delta, 0.3, 1, rng)
        rows = []
        for t in range(tasks):
            for arm, k in (("d2", int(k_a[0, t])), ("ctrl", int(k_c[0, t]))):
                for r in range(reps):
                    rows.append({"run_id": f"{t}-{arm}-{r}", "task_id": f"t{t}",
                                 "condition_id": arm, "rep": r,
                                 "used": bool(r < k), "analyzable": True,
                                 "quarantined": False, "prior_check_status": "pass"})
        df = pd.DataFrame(rows)
        df["used"] = df["used"].astype("boolean")
        df["analyzable"] = df["analyzable"].astype("boolean")
        df["quarantined"] = df["quarantined"].astype("boolean")

        fast = primary_paired_t(k_a, k_c, reps)
        slow = U.paired_cluster_t(df, "d2", "ctrl", "used")
        worst["t_est"] = max(worst["t_est"], abs(float(fast["estimate"][0]) - slow.mean_diff))
        worst["t_p"] = max(worst["t_p"], abs(float(fast["p"][0]) - slow.p_value))
        worst["t_ci"] = max(worst["t_ci"], abs(float(fast["ci_low"][0]) - slow.ci_low),
                            abs(float(fast["ci_high"][0]) - slow.ci_high))

        f_cmh = cmh_vec(k_a, k_c, reps)
        s_cmh = U.cmh_test(df, "d2", "ctrl", "used", suppress=False)
        worst["cmh_chi2"] = max(worst["cmh_chi2"], abs(float(f_cmh["chi2"][0]) - s_cmh.statistic))
        worst["cmh_p"] = max(worst["cmh_p"], abs(float(f_cmh["p"][0]) - s_cmh.p_value))

        f_perm = permutation_vec(k_a, k_c, reps, draws=4000,
                                 rng=np.random.default_rng(5))
        s_perm = U.permutation_test_within_task(df, "d2", "ctrl", "used", suppress=False,
                                                draws=4000)
        worst["perm_p"] = max(worst["perm_p"], abs(float(f_perm["p"][0]) - s_perm.p_value))
    ok = (worst["t_est"] < 1e-12 and worst["t_p"] < 1e-12 and worst["t_ci"] < 1e-12
          and worst["cmh_chi2"] < 1e-9 and worst["cmh_p"] < 1e-9 and worst["perm_p"] < 0.05)
    return {"ok": bool(ok), "max_abs_diff": worst, "n_frames": n_frames,
            "note": "permutation p is a Monte-Carlo quantity; tolerance 0.05"}


# ── report ───────────────────────────────────────────────────────────────────
def _fmt(x: Any, nd: int = 3) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def render_markdown(results: dict[str, Any]) -> str:
    L: list[str] = []
    L.append(f"# WUR power simulation ({POWER_VERSION})")
    L.append("")
    d = results["design"]
    L.append(f"Design: {d['tasks']} tasks x {d['arms']} arms x {d['reps']} reps "
             f"= {d['tasks'] * d['arms'] * d['reps']} runs. "
             f"n for every interval is **{d['tasks']} tasks**, not the run count.")
    L.append(f"Simulations per point: {d['nsim']}. alpha = {d['alpha']}. "
             f"tau (between-task baseline SD, logit) = {d['tau']}.")
    L.append("")
    L.append("## Type-I error under the true null (average risk difference = 0)")
    L.append("")
    L.append("| p0 | gamma | primary paired t | CMH (cc) | CMH (no cc) | within-task permutation "
             "| primary CI coverage |")
    L.append("|---|---|---|---|---|---|---|")
    for r in results["type_i"]:
        L.append(f"| {r['p0']:.2f} | {r['gamma']:.1f} | {_fmt(r['type_i_primary'])} "
                 f"| {_fmt(r['type_i_cmh'])} | {_fmt(r.get('type_i_cmh_uncorrected'))} "
                 f"| {_fmt(r['type_i_permutation'])} | {_fmt(r['coverage_primary'])} |")
    mc = results["type_i"][0]["mc_halfwidth"]
    L.append("")
    L.append(f"Monte-Carlo half-width at the nominal rate: ±{mc:.4f}.")
    L.append("")
    L.append("## Minimum detectable difference at 80% power (primary test)")
    L.append("")
    L.append("| control rate p0 | gamma | MDD (risk difference) | realized power | "
             "95% CI coverage | mean CI width | MDD, Bonferroni k=9 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in results["mdd"]:
        L.append(f"| {r['p0']:.2f} | {r['gamma']:.1f} | {_fmt(r['mdd'])} | "
                 f"{_fmt(r['power_at_mdd'])} | {_fmt(r['coverage_at_mdd'])} | "
                 f"{_fmt(r['ci_width_at_mdd'])} | {_fmt(r['mdd_bonferroni'])} |")
    if results.get("calibration"):
        c = results["calibration"]
        L.append("")
        L.append("## Calibrated against pilot data")
        L.append("")
        L.append(f"`{c['pilot_dir']}` · outcome `{c['outcome']}` · {c['arm']} vs {c['control']} · "
                 f"{c['n_tasks']} tasks · {c['n_runs']} runs")
        L.append(f"p0 = {_fmt(c['p0'])} · tau = {_fmt(c['tau'])} · gamma-hat = {_fmt(c['gamma'])}")
        if results.get("mdd_calibrated"):
            r = results["mdd_calibrated"]
            L.append("")
            L.append(f"MDD under the pilot's own parameters: **{_fmt(r.get('mdd'))}** "
                     f"(power {_fmt(r.get('power'))}, coverage {_fmt(r.get('coverage'))})")
    if results.get("agreement"):
        a = results["agreement"]
        L.append("")
        L.append(f"## Estimator agreement: {'PASS' if a['ok'] else 'FAIL'}")
        L.append("")
        L.append(f"`{json.dumps(a['max_abs_diff'])}`")
    return "\n".join(L) + "\n"


def run(args: argparse.Namespace) -> dict[str, Any]:
    gammas_ti = tuple(float(x) for x in args.type_i_gammas.split(","))
    gammas = tuple(float(x) for x in args.gammas.split(","))
    p0s = tuple(float(x) for x in args.p0s.split(","))
    results: dict[str, Any] = {
        "version": POWER_VERSION,
        "design": {"tasks": args.tasks, "arms": DESIGN_ARMS, "reps": args.reps,
                   "nsim": args.nsim, "alpha": args.alpha, "tau": args.tau,
                   "seed": args.seed, "family": args.family},
        "type_i": type_i_table(p0s=p0s, tau=args.tau, gammas=gammas_ti,
                               tasks=args.tasks, reps=args.reps, alpha=args.alpha,
                               nsim=args.nsim, seed=args.seed, perm_draws=args.perm_draws),
        "mdd": mdd_table(p0s=p0s, tau=args.tau, gammas=gammas, tasks=args.tasks,
                         reps=args.reps, alpha=args.alpha, family=args.family,
                         nsim=args.nsim, seed=args.seed),
    }
    if args.check:
        results["agreement"] = check_agreement()
    if args.pilot_dir:
        cal = calibrate_from_pilot(args.pilot_dir, outcome=args.outcome, arm=args.arm,
                                   control=args.control)
        results["calibration"] = cal
        if cal["p0"] == cal["p0"] and 0 < cal["p0"] < 1:
            results["mdd_calibrated"] = mdd(
                p0=cal["p0"], tau=cal["tau"] or 0.0, gamma=cal["gamma"] or 0.0,
                tasks=max(cal["n_tasks"], 2), reps=int(cal["n_reps_median"] or args.reps),
                alpha=args.alpha, nsim=args.nsim, seed=args.seed)
    return results


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="WUR power / type-I / MDD simulation")
    p.add_argument("--tasks", type=int, default=DESIGN_TASKS)
    p.add_argument("--reps", type=int, default=DESIGN_REPS)
    p.add_argument("--alpha", type=float, default=0.05)
    p.add_argument("--tau", type=float, default=0.5,
                   help="between-task baseline SD on the logit scale")
    p.add_argument("--gammas", default="0,0.5,1.0", help="heterogeneity grid for the MDD table")
    p.add_argument("--type-i-gammas", default="0,0.5,1.0,2.0",
                   help="heterogeneity grid for the type-I table; 2.0 tests §12's quoted figure")
    p.add_argument("--p0s", default="0.05,0.20,0.50",
                   help="control rates for both the MDD and the type-I tables")
    p.add_argument("--nsim", type=int, default=4000)
    p.add_argument("--perm-draws", type=int, default=400)
    p.add_argument("--family", type=int, default=DESIGN_FAMILY)
    p.add_argument("--seed", type=int, default=20260805)
    p.add_argument("--check", action="store_true", help="verify the fast path == uptake_lib")
    p.add_argument("--pilot-dir", default=None, help="an aggregate output dir to calibrate from")
    p.add_argument("--outcome", default="used")
    p.add_argument("--arm", default="d2")
    p.add_argument("--control", default="ctrl")
    p.add_argument("--json", default=None, help="write the raw results here")
    a = p.parse_args(argv)

    results = run(a)
    print(render_markdown(results))
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=2, sort_keys=True, default=str) + "\n",
                                encoding="utf-8")
        print(f"wrote {a.json}", file=sys.stderr)
    if a.check and not results.get("agreement", {}).get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
