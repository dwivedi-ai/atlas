#!/usr/bin/env python3
"""
figures.py — the two l1-doxed figures for a job, as PNG (white background).

Matches the experiments' look exactly (matplotlib, Agg, dpi 150):
  agent-analysis/fig_token_cost_by_env.png   per-task-normalized input-token cost
                                             by environment (E0=1.00), bars blue,
                                             E6/DOX red, value labels, dashed E0
                                             line — ported from l1-doxed gen_figures fig1.
  agent-analysis/fig_file_access_by_env.png  file × environment access-frequency
                                             heatmap (OrRd) — l1 fig12 style.

Reads every run_record.json under jobs/<id>/runs/. Environments are the context
conditions E0..E6; runs aggregate over tasks + reps within an environment.

Usage: python3 figures.py --job-dir jobs/<id>
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np               # noqa: E402


def _is_path(f: str) -> bool:
    """Keep real-looking repo paths; drop command fragments the nav extractor
    sometimes captures (quotes, parens, spaces, semicolons, etc.)."""
    if not f or len(f) > 80 or any(c in f for c in " '\"();|&$`\n\t"):
        return False
    return ("." in f or "/" in f) and not f.startswith("-")

ENVS_ALL = ["E0", "E1", "E2", "E3", "E4", "E5", "E6"]
ELAB = {"E0": "E0\nbare", "E1": "E1\n+README", "E2": "E2\n+AGENTS", "E3": "E3\n+PROJECT",
        "E4": "E4\nfull-XO", "E5": "E5\n+memory", "E6": "E6\nDOX"}
BLUE = "#4c72b0"
RED = "#c44e52"


def collect(job_dir: Path) -> list[dict]:
    runs = []
    for rr in sorted((job_dir / "runs").glob("*/run_record.json")):
        try:
            d = json.loads(rr.read_text())
        except Exception:  # noqa: BLE001
            continue
        nav = d.get("navigation", {})
        accessed = list(nav.get("files_read_sequence") or []) + list(nav.get("files_edited") or [])
        aid = d["condition"].get("agent_id", "")
        runs.append({
            "env": d["condition"].get("env_id", "E0"),
            "task": d["condition"].get("task_id", "t1"),
            "model": ("codex" if aid.startswith("codex")
                      else "claude" if aid.startswith("claude")
                      else "gemini" if aid.startswith("gemini")
                      else "agy" if aid.startswith("agy")
                      else (aid or "agent")),
            "input": d["tokens"].get("total_input") or 0,
            "files": [f for f in accessed if _is_path(f)],
        })
    return runs


def _boot(x, n=10000):
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return float(x.mean()), float(x.mean())
    bs = [np.mean(np.random.choice(x, len(x), True)) for _ in range(n)]
    return float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


# ── Figure 1 — token cost by environment (per-task normalized to E0) ─────────
def token_cost_png(runs, envs, out: Path) -> None:
    # per-task E0 baseline (mean input over E0 runs of that task)
    e0 = {}
    for t in {r["task"] for r in runs}:
        vals = [r["input"] for r in runs if r["env"] == "E0" and r["task"] == t and r["input"]]
        if vals:
            e0[t] = float(np.mean(vals))
    # normalized values per env
    norm = defaultdict(list)
    for r in runs:
        base = e0.get(r["task"])
        if base:
            norm[r["env"]].append(r["input"] / base)
        elif r["env"] == envs[0]:
            norm[r["env"]].append(1.0)
    means, errlo, errhi = [], [], []
    for e in envs:
        vs = norm.get(e, [])
        m = float(np.mean(vs)) if vs else 0.0
        lo, hi = _boot(np.array(vs)) if len(vs) > 1 else (m, m)
        means.append(m); errlo.append(m - lo); errhi.append(hi - m)

    fig, ax = plt.subplots(figsize=(9.5, 6))
    colors = [RED if e == "E6" else BLUE for e in envs]
    has_err = any(e > 0 for e in errlo + errhi)
    ax.bar(range(len(envs)), means, yerr=[errlo, errhi] if has_err else None,
           capsize=4, color=colors, edgecolor="black", lw=.6)
    ax.axhline(1.0, ls="--", c="gray", lw=1, zorder=0)
    for i, m in enumerate(means):
        ax.text(i, m + (errhi[i] if has_err else 0) + .04, f"{m:.2f}×",
                ha="center", fontsize=9, fontweight="bold")
    ax.set_xticks(range(len(envs)))
    ax.set_xticklabels([ELAB.get(e, e) for e in envs])
    ax.set_ylabel("Input-token cost  (per-task normalized to E0 = 1.00)")
    ax.set_ylim(0, max(2.25, max(means) + max(errhi) + .3 if means else 2.25))
    nreps = max((len(norm.get(e, [])) for e in envs), default=1)
    ntasks = len({r["task"] for r in runs})
    ax.set_title("Input-token cost rises with project context — by environment\n"
                 f"{_model_label(runs)} · {ntasks} task(s) × {len(envs)} envs × {nreps} rep(s)", fontsize=11)
    plt.tight_layout()
    plt.savefig(out, dpi=150, facecolor="white")
    plt.close()


def _model_label(runs) -> str:
    ms = sorted({r.get("model", "agent") for r in runs})
    return "/".join(ms) if ms else "agent"


# ── Figure 2 — file access by environment (heatmap) ──────────────────────────
def file_access_png(runs, envs, out: Path) -> None:
    per_env_n = Counter(r["env"] for r in runs)
    acc = defaultdict(lambda: defaultdict(float))
    total = Counter()
    for r in runs:
        for f, n in Counter(r["files"]).items():
            acc[f][r["env"]] += n
            total[f] += n
    files = [f for f, _ in total.most_common(20)]
    if not files:
        return
    M = np.array([[acc[f].get(e, 0.0) / max(1, per_env_n[e]) for e in envs] for f in files])

    fig, ax = plt.subplots(figsize=(max(7, 1.1 * len(envs) + 4), max(5, .42 * len(files) + 1.5)))
    im = ax.imshow(M, cmap="OrRd", aspect="auto", vmin=0, vmax=max(1.0, M.max()))
    ax.set_xticks(range(len(envs))); ax.set_xticklabels(envs)
    ax.set_yticks(range(len(files)))
    ax.set_yticklabels([f if len(f) <= 34 else "…" + f[-33:] for f in files])
    for i in range(len(files)):
        for j in range(len(envs)):
            v = M[i, j]
            if v > 0:
                ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=8,
                        color="white" if v > M.max() * 0.55 else "black")
    ax.set_title("File-access frequency by environment (mean accesses / run)\n"
                 f"{_model_label(runs)} · top {len(files)} files", fontsize=10.5)
    plt.colorbar(im, label="accesses / run")
    plt.tight_layout()
    plt.savefig(out, dpi=150, facecolor="white")
    plt.close()


def generate(job_dir: Path) -> list[Path]:
    job_dir = Path(job_dir)
    runs = collect(job_dir)
    out_dir = job_dir / "agent-analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not runs:
        return []
    present = {r["env"] for r in runs}
    envs = [e for e in ENVS_ALL if e in present] or sorted(present)
    written = []
    p1 = out_dir / "fig_token_cost_by_env.png"
    token_cost_png(runs, envs, p1); written.append(p1)
    p2 = out_dir / "fig_file_access_by_env.png"
    file_access_png(runs, envs, p2)
    if p2.exists():
        written.append(p2)
    return written


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-dir", required=True)
    a = ap.parse_args()
    for p in generate(Path(a.job_dir)):
        print(f"figure → {p}")


if __name__ == "__main__":
    main()
