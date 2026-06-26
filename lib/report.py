#!/usr/bin/env python3
"""
report.py — human-readable reports.

  --run-dir <dir>   write that run's report.md (verdict, criteria, tokens, patch,
                    self-analysis pointer).
  --job-dir <dir>   write a job-level REPORT.md scorecard across all the job's runs.

Reads judge.json + run_record.json + run_meta.json (all already written by teardown).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def _fmt_int(n) -> str:
    return f"{n:,}" if isinstance(n, (int, float)) else "—"


def run_report(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    meta = _load(run_dir / "run_meta.json") or {}
    judge = _load(run_dir / "judge.json") or {}
    rec = _load(run_dir / "run_record.json") or {}
    run_id = meta.get("run_id", run_dir.name)

    verdict = judge.get("verdict", "—")
    score = judge.get("score")
    score_s = f"{score:.2f}" if isinstance(score, (int, float)) else "—"
    badge = {"accepted": "✅ ACCEPTED", "partial": "🟡 PARTIAL",
             "rejected": "❌ REJECTED", "timeout": "⏱ TIMEOUT",
             "error": "⚠️ ERROR"}.get(verdict, verdict)

    L = []
    L.append(f"# Run report — {run_id}\n")
    L.append(f"**Verdict:** {badge}  ·  **Score:** {score_s}  ·  **Model:** {meta.get('agent_id','—')}\n")
    L.append(f"- Repo: `{meta.get('repo_url') or meta.get('repo_path','—')}` @ `{(meta.get('base_repo_sha') or '')[:10]}`")
    L.append(f"- Started: {meta.get('timestamp_start','—')}  ·  Ended: {meta.get('timestamp_end','—')}\n")

    # Criteria table
    L.append("## Acceptance criteria\n")
    crits = judge.get("criteria", [])
    if crits:
        L.append("| ? | Criterion | Source | Evidence |")
        L.append("|---|-----------|--------|----------|")
        for c in crits:
            mark = "✅" if c.get("met") else "❌"
            ev = (c.get("evidence") or "").replace("\n", " ").replace("|", "\\|")[:120]
            L.append(f"| {mark} | {c.get('criterion','')[:80]} | {c.get('source','')} | {ev} |")
        b = judge.get("battery", {})
        L.append(f"\n_Mechanical battery: {b.get('passed','?')}/{b.get('total','?')} passed._\n")
    else:
        L.append("_No criteria were graded._\n")

    # Tokens / timing
    tok = rec.get("tokens", {})
    tim = rec.get("timing", {})
    L.append("## Cost & timing\n")
    L.append(f"- Input tokens: **{_fmt_int(tok.get('total_input'))}**  ·  "
             f"Output: **{_fmt_int(tok.get('total_output'))}**  ·  "
             f"Cache read: {_fmt_int(tok.get('cache_read'))}")
    wc = tim.get("wall_clock_seconds")
    ttfe = tim.get("time_to_first_edit_seconds")
    L.append(f"- Wall clock: {wc if wc is not None else '—'}s  ·  "
             f"Time-to-first-edit: {ttfe if ttfe is not None else '—'}s")
    ops = rec.get("operations", {})
    L.append(f"- Tool calls: {ops.get('tool_calls_total','—')}  ·  "
             f"files edited: {', '.join(rec.get('navigation',{}).get('files_edited',[])) or '—'}\n")

    # Artifacts
    L.append("## Artifacts\n")
    L.append(f"- Solution diff: `{run_dir/'git.patch'}`  → apply with `git apply git.patch`")
    L.append(f"- Transcript: `{run_dir/'transcript.jsonl'}`")
    if (run_dir / "analysis.md").exists():
        L.append(f"- Agent self-analysis: `{run_dir/'analysis.md'}`")
    L.append("")

    # Self-analysis inline (it's the research payoff)
    analysis = run_dir / "analysis.md"
    if analysis.exists():
        L.append("## Agent self-analysis\n")
        L.append(analysis.read_text().strip() + "\n")

    out = run_dir / "report.md"
    out.write_text("\n".join(L))
    return out


def job_report(job_dir: Path) -> Path:
    job_dir = Path(job_dir)
    runs_dir = job_dir / "runs"
    spec = _load(job_dir / "job.yaml") if (job_dir / "job.yaml").exists() else {}
    # job.yaml is YAML, not JSON; load leniently
    if spec is None:
        try:
            import yaml
            spec = yaml.safe_load((job_dir / "job.yaml").read_text())
        except Exception:  # noqa: BLE001
            spec = {}

    rows = []
    for rd in sorted(runs_dir.glob("*")) if runs_dir.exists() else []:
        if not rd.is_dir():
            continue
        judge = _load(rd / "judge.json") or {}
        rec = _load(rd / "run_record.json") or {}
        meta = _load(rd / "run_meta.json") or {}
        rows.append({
            "run": rd.name,
            "task": meta.get("task_id") or rec.get("condition", {}).get("task_id", "—"),
            "env": meta.get("env_id") or rec.get("condition", {}).get("env_id", "E0"),
            "verdict": judge.get("verdict", "—"),
            "score": judge.get("score"),
            "input": rec.get("tokens", {}).get("total_input"),
            "output": rec.get("tokens", {}).get("total_output"),
        })

    tasks = spec.get("tasks") if isinstance(spec, dict) else None

    L = []
    L.append(f"# Job report — {job_dir.name}\n")
    if spec:
        L.append(f"**Model:** {spec.get('model','—')}  ·  **Reps/task:** {spec.get('reps','—')}  ·  "
                 f"**Tasks:** {len(tasks) if tasks else 1}\n")
        for t in (tasks or []):
            L.append(f"### Task `{t['id']}`\n")
            L.append("```\n" + (t.get("task", "").strip()) + "\n```")
            L.append("_Accepted when:_ " + " ".join((t.get("accept", "").strip()).split()) + "\n")

    # Per-task accepted-rate summary
    if rows:
        by_task = {}
        for r in rows:
            by_task.setdefault(r["task"], []).append(r)
        L.append("## Acceptance by task\n")
        L.append("| Task | Accepted | Mean score | Mean input tok |")
        L.append("|------|----------|------------|----------------|")
        for t, rs in by_task.items():
            acc = sum(1 for r in rs if r["verdict"] == "accepted")
            scores = [r["score"] for r in rs if isinstance(r["score"], (int, float))]
            inputs = [r["input"] for r in rs if isinstance(r["input"], (int, float))]
            ms = f"{sum(scores)/len(scores):.2f}" if scores else "—"
            mi = _fmt_int(round(sum(inputs)/len(inputs))) if inputs else "—"
            L.append(f"| {t} | {acc}/{len(rs)} | {ms} | {mi} |")
        L.append("")

    # Per-environment cost summary (the headline of a context experiment)
    envs_present = []
    for r in rows:
        if r["env"] not in envs_present:
            envs_present.append(r["env"])
    if rows and len(envs_present) > 1:
        by_env = {}
        for r in rows:
            by_env.setdefault(r["env"], []).append(r)
        L.append("## Cost by environment\n")
        L.append("| Env | Runs | Accepted | Mean input tok | Median input tok |")
        L.append("|-----|------|----------|----------------|------------------|")
        for e in sorted(by_env, key=lambda x: (0, int(x[1:])) if x[:1] in "Ee" and x[1:].isdigit() else (1, x)):
            rs = by_env[e]
            acc = sum(1 for r in rs if r["verdict"] == "accepted")
            inputs = sorted(r["input"] for r in rs if isinstance(r["input"], (int, float)))
            mean = _fmt_int(round(sum(inputs) / len(inputs))) if inputs else "—"
            med = _fmt_int(inputs[len(inputs) // 2]) if inputs else "—"
            L.append(f"| {e} | {len(rs)} | {acc}/{len(rs)} | {mean} | {med} |")
        L.append("\n_See `agent-analysis/fig_token_cost_by_env.svg` and "
                 "`fig_file_access_by_env.svg`._\n")

    L.append("## Runs\n")
    if rows:
        L.append("| Run | Task | Env | Verdict | Score | Input tok |")
        L.append("|-----|------|-----|---------|-------|-----------|")
        accepted = 0
        for r in rows:
            sc = f"{r['score']:.2f}" if isinstance(r["score"], (int, float)) else "—"
            accepted += 1 if r["verdict"] == "accepted" else 0
            L.append(f"| {r['run']} | {r['task']} | {r['env']} | {r['verdict']} | {sc} | "
                     f"{_fmt_int(r['input'])} |")
        L.append(f"\n**{accepted}/{len(rows)} runs accepted.**\n")
    else:
        L.append("_No runs yet._\n")

    # Figures (auto-generated into agent-analysis/)
    aa = job_dir / "agent-analysis"
    figs = sorted(aa.glob("fig_*.svg")) if aa.exists() else []
    if figs:
        L.append("## Figures\n")
        for f in figs:
            L.append(f"![{f.stem}]({f.relative_to(job_dir)})")
        L.append("")

    L.append("## Self-analyses\n")
    if aa.exists():
        for f in sorted(aa.glob("*.md")):
            L.append(f"- [`{f.name}`]({f.relative_to(job_dir)})")
    L.append("")

    out = job_dir / "REPORT.md"
    out.write_text("\n".join(L))
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-dir")
    g.add_argument("--job-dir")
    a = p.parse_args()
    if a.run_dir:
        print(f"report → {run_report(Path(a.run_dir))}")
    else:
        print(f"job report → {job_report(Path(a.job_dir))}")


if __name__ == "__main__":
    main()
