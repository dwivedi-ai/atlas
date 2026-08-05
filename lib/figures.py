#!/usr/bin/env python3
"""
figures.py — the job's headline figures, as PNG (white background).

RESPONSIBILITY
  Read every run_record.json under jobs/<id>/runs/ and render:
    agent-analysis/fig_token_cost_by_env.png   per-task-normalized input-token cost by
                                               condition (baseline = 1.00), value labels,
                                               dashed baseline line — ported from
                                               l1-doxed gen_figures fig1.
    agent-analysis/fig_file_access_by_env.png  file x condition access-frequency heatmap
                                               (OrRd) — l1 fig12 style.

INPUTS   jobs/<id>/runs/*/run_record.json, and jobs/<id>/job.yaml for `baseline_condition`.
OUTPUTS  the PNGs above; a refusal on stderr and a non-zero `--strict` exit when the
         normalization is not defined.

THE DEFECT THIS FILE USED TO HAVE
  The per-task baseline was computed from `r["env"] == "E0"` — a LITERAL. Any job whose
  conditions are not ladder rungs (every WUR job: ctrl, d1, d2, d3, ...) has no E0 run, so
  `e0` came out empty, every arm's normalized list came out empty, `means` came out 0.0,
  and the chart rendered a row of 0.00x bars under a y-axis label reading "normalized to
  E0 = 1.00". A figure that is confidently wrong is worse than a missing one, so the
  baseline is now threaded from job.yaml, the label is derived from it, and a job whose
  baseline arm has no usable runs REFUSES to plot instead of plotting zeros.

  It also guards `schema_version`: a record generation this file has never seen may have
  moved `tokens.total_input`, and V7 means a per_line_v1 record and a per_message_v2 one
  must never appear in the same bar chart — their token totals differ by 1.0x-4.9x for
  reasons that have nothing to do with the condition.

Usage: python3 figures.py --job-dir jobs/<id> [--baseline ctrl] [--strict]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np               # noqa: E402

# Record generations this file knows how to read. A record outside this set is not
# plotted — the alternative is a chart whose provenance nobody can state.
KNOWN_SCHEMA_VERSIONS = {"1", "2"}
# V7: per_line_v1 token totals are inflated by a run-varying 1.0x-4.9x. Absent means
# per_line_v1 by construction (the field did not exist before the fix).
KNOWN_ACCOUNTING_VERSIONS = {"per_line_v1", "per_message_v2"}


class FigureRefused(Exception):
    """Raised instead of rendering a figure that would be quietly wrong."""


def _is_path(f: str) -> bool:
    """Keep real-looking repo paths; drop command fragments the nav extractor
    sometimes captures (quotes, parens, spaces, semicolons, etc.)."""
    if not f or len(f) > 80 or any(c in f for c in " '\"();|&$`\n\t"):
        return False
    return ("." in f or "/" in f) and not f.startswith("-")


ENVS_ALL = ["E0", "E1", "E2", "E3", "E4", "E5", "E6"]
ELAB = {"E0": "E0\nbare", "E1": "E1\n+README", "E2": "E2\n+AGENTS", "E3": "E3\n+PROJECT",
        "E4": "E4\nfull-XO", "E5": "E5\n+memory", "E6": "E6\nDOX"}
# The §7.1 arms, in the order the design reads them: pushed/pointer, the depth ladder,
# the format contrasts, discrimination, then the two controls and the no-probe pair.
WUR_ARMS = ["d0-push", "d1-ptr", "d1", "d2", "d3", "d2-check", "d2-table", "d2-dist",
            "ctrl", "ctrl-nofile", "ctrl-np", "d1-np", "d3-np"]
WUR_LAB = {"d0-push": "d0-push\npushed", "d1-ptr": "d1-ptr\npointer", "d1": "d1\nroot",
           "d2": "d2\nhub", "d3": "d3\ndeep", "d2-check": "d2\nchecklist",
           "d2-table": "d2\ntable", "d2-dist": "d2\n+3 distractors",
           "ctrl": "ctrl\nfact-free", "ctrl-nofile": "ctrl\nno file",
           "ctrl-np": "ctrl\nno probe", "d1-np": "d1\nno probe", "d3-np": "d3\nno probe"}
BLUE = "#4c72b0"
RED = "#c44e52"
GREY = "#8c8c8c"


def _spec(job_dir: Path) -> dict:
    """job.yaml, however it can be read. Never fatal — a missing spec only costs the
    explicit baseline, and the fallback below is still principled."""
    p = Path(job_dir) / "job.yaml"
    if not p.exists():
        return {}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import jobspec  # noqa: PLC0415
        return jobspec.load(Path(job_dir))
    except Exception:  # noqa: BLE001
        pass
    try:
        import yaml  # noqa: PLC0415
        return yaml.safe_load(p.read_text()) or {}
    except Exception:  # noqa: BLE001
        return {}


def collect(job_dir: Path) -> tuple[list[dict], dict]:
    """Every run_record under runs/, plus what generation they came from.

    Returns (runs, provenance) where provenance carries the schema_version /
    accounting_version / experiment values SEEN, so the caller can refuse to mix them.
    """
    runs: list[dict] = []
    prov = {"schema_versions": Counter(), "accounting_versions": Counter(),
            "experiments": Counter(), "unreadable": 0}
    for rr in sorted((Path(job_dir) / "runs").glob("*/run_record.json")):
        try:
            d = json.loads(rr.read_text())
        except Exception:  # noqa: BLE001
            prov["unreadable"] += 1
            continue
        nav = d.get("navigation", {})
        accessed = list(nav.get("files_read_sequence") or []) + list(nav.get("files_edited") or [])
        cond = d.get("condition", {})
        aid = cond.get("agent_id", "")
        tok = d.get("tokens", {})
        prov["schema_versions"][str(d.get("schema_version"))] += 1
        prov["accounting_versions"][tok.get("accounting_version") or "per_line_v1"] += 1
        prov["experiments"][(d.get("run", {}) or {}).get("experiment") or "ladder"] += 1
        runs.append({
            "env": cond.get("env_id", "E0"),
            "task": cond.get("task_id", "t1"),
            "model": (cond.get("model")
                      or ("codex" if aid.startswith("codex")
                          else "claude" if aid.startswith("claude")
                          else "gemini" if aid.startswith("gemini")
                          else "agy" if aid.startswith("agy")
                          else (aid or "agent"))),
            "input": tok.get("total_input") or 0,
            "files": [f for f in accessed if _is_path(f)],
            "schema_version": str(d.get("schema_version")),
            "accounting_version": tok.get("accounting_version") or "per_line_v1",
        })
    return runs, prov


def guard(runs: list[dict], prov: dict) -> None:
    """Refuse to plot a set of records this file cannot honestly aggregate."""
    unknown_schema = {v for v in prov["schema_versions"] if v not in KNOWN_SCHEMA_VERSIONS}
    if unknown_schema:
        raise FigureRefused(
            f"run_record schema_version(s) {sorted(unknown_schema)} are not in "
            f"{sorted(KNOWN_SCHEMA_VERSIONS)} — this figure does not know where the fields "
            f"live in that generation. Refusing to plot rather than plotting whatever "
            f"`tokens.total_input` happens to return.")
    unknown_acct = {v for v in prov["accounting_versions"] if v not in KNOWN_ACCOUNTING_VERSIONS}
    if unknown_acct:
        raise FigureRefused(f"unknown tokens.accounting_version(s): {sorted(unknown_acct)}")
    if len(prov["accounting_versions"]) > 1:
        raise FigureRefused(
            f"records mix token accounting versions {dict(prov['accounting_versions'])}. "
            f"per_line_v1 totals are inflated 1.0x-4.9x by the V7 per-line double count, so "
            f"bars from the two generations are not comparable at all. Re-run telemetry.py "
            f"over the affected runs, or plot them as separate jobs.")


def _normalized_by_condition(runs: list[dict], conditions: list[str], baseline: str) -> dict:
    """Per-task baseline-normalized input cost, one list per condition.

    Shared with viz/server.py, which held a verbatim copy of this logic (and therefore a
    verbatim copy of the E0 bug). Import it; do not re-implement it.

    Raises FigureRefused when the baseline arm has no usable runs — the exact condition
    that used to produce a row of 0.00x bars under a "= 1.00" label.
    """
    base_by_task: dict[str, float] = {}
    for t in {r["task"] for r in runs}:
        vals = [r["input"] for r in runs
                if r["env"] == baseline and r["task"] == t and r["input"]]
        if vals:
            base_by_task[t] = float(np.mean(vals))
    if not base_by_task:
        present = sorted({r["env"] for r in runs})
        raise FigureRefused(
            f"baseline condition {baseline!r} has no runs with input tokens "
            f"(conditions present: {present}). Every other arm would normalize to 0.00x "
            f"under a label claiming the baseline is 1.00. Set baseline_condition in "
            f"job.yaml, or pass --baseline.")
    norm: dict[str, list[float]] = defaultdict(list)
    covered = 0
    for r in runs:
        base = base_by_task.get(r["task"])
        if base:
            norm[r["env"]].append(r["input"] / base)
            covered += 1
    if covered == 0:
        raise FigureRefused(
            f"no run shares a task with the {baseline!r} baseline — nothing to normalize.")
    return {c: norm.get(c, []) for c in conditions}


# public alias: viz/server.py's _normalized_by_env is this function with the bug in it.
normalized_by_condition = _normalized_by_condition


def _boot(x, n=10000, seed: int | None = None):
    """Percentile bootstrap CI, on an EXPLICITLY seeded generator.

    The old version drew from the module-global `np.random`, so the same run_records
    produced different error bars on every render and no figure was reproducible. The
    seed is derived from the data itself, so re-rendering a job gives byte-identical
    whiskers while two different arms still get independent draws.
    """
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return float(x.mean()), float(x.mean())
    if seed is None:
        seed = abs(hash(tuple(np.round(x, 9)))) % (2 ** 32)
    rng = np.random.default_rng(seed)
    bs = [float(np.mean(rng.choice(x, len(x), True))) for _ in range(n)]
    return float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def _labels(conditions: list[str], experiment: str) -> list[str]:
    table = WUR_LAB if experiment == "wur" else ELAB
    return [table.get(c, c) for c in conditions]


# ── Figure 1 — token cost by condition (per-task normalized to the baseline) ──
def token_cost_png(runs, conditions, out: Path, baseline: str = "E0",
                   experiment: str = "ladder") -> None:
    norm = _normalized_by_condition(runs, conditions, baseline)

    means, errlo, errhi, n_per = [], [], [], []
    for c in conditions:
        vs = norm.get(c, [])
        n_per.append(len(vs))
        m = float(np.mean(vs)) if vs else float("nan")
        lo, hi = _boot(np.array(vs)) if len(vs) > 1 else (m, m)
        means.append(m)
        errlo.append(0.0 if np.isnan(m) else m - lo)
        errhi.append(0.0 if np.isnan(m) else hi - m)

    if all(np.isnan(m) for m in means):
        raise FigureRefused("no condition has a normalizable run.")

    fig, ax = plt.subplots(figsize=(max(9.5, 0.95 * len(conditions) + 3), 6))
    # The baseline bar is grey (it is 1.00 by construction, not a measurement); the
    # design's extreme arm is red; an arm with NO runs is drawn as a gap, never as 0.00.
    hot = "E6" if experiment != "wur" else "d0-push"
    colors = [GREY if c == baseline else (RED if c == hot else BLUE) for c in conditions]
    plot_means = [0.0 if np.isnan(m) else m for m in means]
    has_err = any(e > 0 for e in errlo + errhi)
    ax.bar(range(len(conditions)), plot_means,
           yerr=[errlo, errhi] if has_err else None,
           capsize=4, color=colors, edgecolor="black", lw=.6)
    ax.axhline(1.0, ls="--", c="gray", lw=1, zorder=0)
    for i, m in enumerate(means):
        if np.isnan(m):
            ax.text(i, 0.05, "no runs", ha="center", fontsize=8, color=GREY, rotation=90)
            continue
        ax.text(i, m + (errhi[i] if has_err else 0) + .04, f"{m:.2f}×",
                ha="center", fontsize=9, fontweight="bold")
    ax.set_xticks(range(len(conditions)))
    ax.set_xticklabels(_labels(conditions, experiment))
    ax.set_ylabel(f"Input-token cost  (per-task normalized to {baseline} = 1.00)")
    finite = [m for m in means if not np.isnan(m)]
    ax.set_ylim(0, max(2.25, (max(finite) + max(errhi) + .3) if finite else 2.25))
    nreps = max(n_per, default=1)
    ntasks = len({r["task"] for r in runs})
    kind = "condition" if experiment == "wur" else "environment"
    ax.set_title(f"Input-token cost by {kind} — normalized to {baseline}\n"
                 f"{_model_label(runs)} · {ntasks} task(s) × {len(conditions)} arms × "
                 f"≤{nreps} rep(s) · tokens: {_accounting_label(runs)}", fontsize=11)
    plt.tight_layout()
    plt.savefig(out, dpi=150, facecolor="white")
    plt.close()


def _model_label(runs) -> str:
    ms = sorted({r.get("model", "agent") for r in runs})
    return "/".join(ms) if ms else "agent"


def _accounting_label(runs) -> str:
    vs = sorted({r.get("accounting_version", "per_line_v1") for r in runs})
    # Say it on the figure: a per_line_v1 chart is inflated by a run-varying factor and
    # nobody should quote it next to a post-fix one without knowing that.
    return "/".join(v + (" (INFLATED, V7)" if v == "per_line_v1" else "") for v in vs)


# ── Figure 2 — file access by condition (heatmap) ────────────────────────────
def file_access_png(runs, conditions, out: Path) -> None:
    per_cond_n = Counter(r["env"] for r in runs)
    acc = defaultdict(lambda: defaultdict(float))
    total = Counter()
    for r in runs:
        for f, n in Counter(r["files"]).items():
            acc[f][r["env"]] += n
            total[f] += n
    files = [f for f, _ in total.most_common(20)]
    if not files:
        return
    M = np.array([[acc[f].get(c, 0.0) / max(1, per_cond_n[c]) for c in conditions]
                  for f in files])

    fig, ax = plt.subplots(figsize=(max(7, 1.1 * len(conditions) + 4),
                                    max(5, .42 * len(files) + 1.5)))
    im = ax.imshow(M, cmap="OrRd", aspect="auto", vmin=0, vmax=max(1.0, M.max()))
    ax.set_xticks(range(len(conditions)))
    ax.set_xticklabels(conditions, rotation=45 if len(conditions) > 8 else 0,
                       ha="right" if len(conditions) > 8 else "center")
    ax.set_yticks(range(len(files)))
    ax.set_yticklabels([f if len(f) <= 34 else "…" + f[-33:] for f in files])
    for i in range(len(files)):
        for j in range(len(conditions)):
            v = M[i, j]
            if v > 0:
                ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=8,
                        color="white" if v > M.max() * 0.55 else "black")
    ax.set_title("File-access frequency by condition (mean accesses / run)\n"
                 f"{_model_label(runs)} · top {len(files)} files", fontsize=10.5)
    plt.colorbar(im, label="accesses / run")
    plt.tight_layout()
    plt.savefig(out, dpi=150, facecolor="white")
    plt.close()


def order_conditions(present: set[str], experiment: str) -> list[str]:
    canonical = WUR_ARMS if experiment == "wur" else ENVS_ALL
    ordered = [c for c in canonical if c in present]
    return ordered + sorted(present - set(ordered))


def generate(job_dir: Path, baseline_condition: str | None = None,
             strict: bool = False) -> list[Path]:
    """Render whatever can honestly be rendered. Refusals go to stderr.

    The heatmap does not depend on a baseline, so a refused normalization costs figure 1
    only — `strict=True` turns any refusal into a raised FigureRefused for callers that
    would rather stop.
    """
    job_dir = Path(job_dir)
    runs, prov = collect(job_dir)
    out_dir = job_dir / "agent-analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not runs:
        return []

    spec = _spec(job_dir)
    experiment = (spec.get("experiment") or
                  (prov["experiments"].most_common(1)[0][0] if prov["experiments"] else "ladder"))
    baseline = (baseline_condition or spec.get("baseline_condition")
                or ("ctrl" if experiment == "wur" else "E0"))
    present = {r["env"] for r in runs}
    conditions = order_conditions(present, experiment)

    written: list[Path] = []
    try:
        guard(runs, prov)
        p1 = out_dir / "fig_token_cost_by_env.png"
        token_cost_png(runs, conditions, p1, baseline=baseline, experiment=experiment)
        written.append(p1)
    except FigureRefused as e:
        print(f"figures: REFUSING to plot fig_token_cost_by_env — {e}", file=sys.stderr)
        if strict:
            raise

    p2 = out_dir / "fig_file_access_by_env.png"
    file_access_png(runs, conditions, p2)
    if p2.exists():
        written.append(p2)
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-dir", required=True)
    ap.add_argument("--baseline", help="the condition every other arm is normalized to")
    ap.add_argument("--strict", action="store_true",
                    help="exit 3 on a refusal instead of skipping the figure")
    a = ap.parse_args()
    try:
        for p in generate(Path(a.job_dir), a.baseline, strict=a.strict):
            print(f"figure → {p}")
    except FigureRefused as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
