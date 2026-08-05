#!/usr/bin/env python3
"""
report.py — human-readable reports.

RESPONSIBILITY
  Render what a run (or a job) actually produced, including the parts that are unknown.
  A report that shows an unevaluable criterion as a red X, or a token total without
  saying which accounting produced it, is a report that misleads its reader.

  --run-dir <dir>   write that run's report.md (verdict, criteria, tokens, patch,
                    self-analysis pointer).
  --job-dir <dir>   write a job-level REPORT.md scorecard across all the job's runs.

INPUTS   judge.json + run_record.json + run_meta.json (all written by teardown),
         job.yaml for the job-level view.
OUTPUTS  <run>/report.md, <job>/REPORT.md.

TWO THINGS THIS FILE NOW SAYS OUT LOUD
  - `met` is TRI-STATE. The judge stopped coercing "could not be evaluated" to False, so
    ✅ / ❌ / ⚠️ are three different facts and the ⚠️ rows carry their error.
  - Token totals are labelled with `tokens.accounting_version`. A per_line_v1 number is
    inflated by a run-varying 1.0x-4.9x (V7) and must never be quoted beside a
    per_message_v2 one without that caveat attached.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

MET_MARK = {True: "✅", False: "❌", None: "⚠️"}
# V7. Absent means per_line_v1 by construction: the field did not exist before the fix.
ACCOUNTING_NOTE = {
    "per_message_v2": "per-message (V7-corrected)",
    "per_line_v1": "per-LINE — INFLATED 1.0×–4.9× (V7), do not compare to corrected runs",
}


def _load(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def _fmt_int(n) -> str:
    return f"{n:,}" if isinstance(n, (int, float)) else "—"


def _accounting(rec: dict) -> str:
    v = (rec.get("tokens") or {}).get("accounting_version") or "per_line_v1"
    return ACCOUNTING_NOTE.get(v, v)


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
            # ✅ met · ❌ not met · ⚠️ COULD NOT BE EVALUATED. The third is not a failure:
            # it is a broken criterion, and it is excluded from the score.
            mark = MET_MARK.get(c.get("met"), "⚠️")
            ev = (c.get("evidence") or c.get("error") or "")
            ev = ev.replace("\n", " ").replace("|", "\\|")[:120]
            L.append(f"| {mark} | {c.get('criterion','')[:80]} | {c.get('source','')} | {ev} |")
        b = judge.get("battery", {})
        line = f"\n_Mechanical battery: {b.get('passed','?')}/{b.get('total','?')} passed"
        if b.get("errored"):
            line += f", {b['errored']} unevaluable"
        L.append(line + "._")
        n_err = judge.get("criteria_errored") or 0
        if n_err:
            L.append(f"\n> ⚠️ {n_err} of {judge.get('criteria_total','?')} criteria could not be "
                     f"evaluated and are EXCLUDED from the score — the score below is over "
                     f"{judge.get('criteria_graded','?')} graded criteria, not all of them.\n")
        else:
            L.append("")
    else:
        L.append("_No criteria were graded._\n")

    # Tokens / timing
    tok = rec.get("tokens", {})
    tim = rec.get("timing", {})
    L.append("## Cost & timing\n")
    L.append(f"- Input tokens: **{_fmt_int(tok.get('total_input'))}**  ·  "
             f"Output: **{_fmt_int(tok.get('total_output'))}**  ·  "
             f"Cache read: {_fmt_int(tok.get('cache_read'))}")
    L.append(f"- Token accounting: `{_accounting(rec)}`")
    if tok.get("cost_usd") is not None:
        L.append(f"- Measured cost: ${tok['cost_usd']:.4f}")
    for k, label in (("dedupe_delta_input", "input"), ("dedupe_delta_output", "output")):
        d = tok.get(k)
        if isinstance(d, int) and d != 0:
            L.append(f"- ⚠️ **{label} token drift {d:+,}** vs the terminal `result` event — "
                     f"the deduped sum and the CLI's own total disagree, so neither is "
                     f"trustworthy for this run.")
    wc = tim.get("wall_clock_seconds")
    ttfe = tim.get("time_to_first_edit_seconds")
    L.append(f"- Wall clock: {wc if wc is not None else '—'}s  ·  "
             f"Time-to-first-edit: {ttfe if ttfe is not None else '—'}s")
    ops = rec.get("operations", {})
    turns = ops.get("turns_total")
    L.append(f"- Tool calls: {ops.get('tool_calls_total','—')}"
             + (f" (task-only: {ops['tool_calls_task']})"
                if ops.get("tool_calls_task") is not None
                and ops.get("tool_calls_task") != ops.get("tool_calls_total") else "")
             + f"  ·  turns: {turns if turns is not None else '—'}"
             + f"  ·  lines: {ops.get('message_count','—')}")
    if ops.get("probes_sent") is not None:
        L.append(f"- Probes: {ops.get('probes_answered','—')}/{ops['probes_sent']} answered  ·  "
                 f"resumes: {ops.get('resumes_sent','—')}  ·  "
                 f"denials: {ops.get('permission_denials','—')}  ·  "
                 f"max tool-uses/message: {ops.get('max_tool_uses_per_message','—')}")
    L.append(f"- Files edited: {', '.join(rec.get('navigation',{}).get('files_edited',[])) or '—'}\n")

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
            "accounting": (rec.get("tokens", {}) or {}).get("accounting_version") or "per_line_v1",
            "unevaluable": judge.get("criteria_errored") or 0,
        })

    tasks = spec.get("tasks") if isinstance(spec, dict) else None
    experiment = (spec.get("experiment") or "ladder") if isinstance(spec, dict) else "ladder"
    arm_word = "Condition" if experiment == "wur" else "Env"

    L = []
    L.append(f"# Job report — {job_dir.name}\n")
    if spec:
        agent = spec.get("agent") if isinstance(spec.get("agent"), dict) else {}
        model = agent.get("model") or agent.get("backend") or spec.get("model") or "—"
        L.append(f"**Model:** {model}  ·  **Experiment:** {experiment}  ·  "
                 f"**Reps/task:** {spec.get('reps','—')}  ·  "
                 f"**Tasks:** {len(tasks) if tasks else 1}  ·  "
                 f"**Baseline:** {spec.get('baseline_condition','—')}\n")
    # V7: a report that pools two accounting generations in one table is a wrong report.
    accts = {r["accounting"] for r in rows}
    if len(accts) > 1:
        L.append(f"> ⚠️ **These runs mix token accounting versions {sorted(accts)}.** "
                 f"`per_line_v1` totals are inflated by a run-varying 1.0×–4.9× (V7); the "
                 f"token columns below are NOT comparable across those runs.\n")
    elif accts and "per_line_v1" in accts:
        L.append("> ⚠️ Token totals here are `per_line_v1` — inflated by a run-varying "
                 "1.0×–4.9× (V7). Re-run `telemetry.py` to correct them.\n")
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
        L.append("| Task | Accepted | Mean score | Mean input tok | Unevaluable criteria |")
        L.append("|------|----------|------------|----------------|----------------------|")
        for t, rs in by_task.items():
            acc = sum(1 for r in rs if r["verdict"] == "accepted")
            scores = [r["score"] for r in rs if isinstance(r["score"], (int, float))]
            inputs = [r["input"] for r in rs if isinstance(r["input"], (int, float))]
            ms = f"{sum(scores)/len(scores):.2f}" if scores else "—"
            mi = _fmt_int(round(sum(inputs)/len(inputs))) if inputs else "—"
            # A run whose score is None was NOT graded 0 — it could not be graded.
            n_ungraded = sum(1 for r in rs if r["score"] is None)
            unev = sum(r["unevaluable"] for r in rs)
            note = f"{unev}" + (f" (+{n_ungraded} run(s) ungraded)" if n_ungraded else "")
            L.append(f"| {t} | {acc}/{len(rs)} | {ms} | {mi} | {note} |")
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
        baseline = spec.get("baseline_condition") if isinstance(spec, dict) else None
        L.append(f"## Cost by {arm_word.lower()}\n")
        L.append(f"| {arm_word} | Runs | Accepted | Mean input tok | Median input tok | vs baseline |")
        L.append("|-----|------|----------|----------------|------------------|-------------|")
        base_mean = None
        if baseline in by_env:
            bi = [r["input"] for r in by_env[baseline] if isinstance(r["input"], (int, float))]
            base_mean = (sum(bi) / len(bi)) if bi else None
        for e in sorted(by_env, key=lambda x: (0, int(x[1:])) if x[:1] in "Ee" and x[1:].isdigit() else (1, x)):
            rs = by_env[e]
            acc = sum(1 for r in rs if r["verdict"] == "accepted")
            inputs = sorted(r["input"] for r in rs if isinstance(r["input"], (int, float)))
            mean = _fmt_int(round(sum(inputs) / len(inputs))) if inputs else "—"
            med = _fmt_int(inputs[len(inputs) // 2]) if inputs else "—"
            # "—" when there is no baseline to divide by, never a fabricated 0.00×.
            rel = (f"{(sum(inputs)/len(inputs))/base_mean:.2f}×"
                   if inputs and base_mean else "—")
            L.append(f"| {e} | {len(rs)} | {acc}/{len(rs)} | {mean} | {med} | {rel} |")
        if base_mean is None and baseline:
            L.append(f"\n_No runs for baseline `{baseline}` — relative cost is undefined, "
                     f"so it is left blank rather than shown as 0.00×._")
        L.append("\n_See `agent-analysis/fig_token_cost_by_env.png` and "
                 "`fig_file_access_by_env.png`._\n")

    L.append("## Runs\n")
    if rows:
        L.append(f"| Run | Task | {arm_word} | Verdict | Score | Input tok |")
        L.append("|-----|------|-----|---------|-------|-----------|")
        accepted = 0
        for r in rows:
            sc = f"{r['score']:.2f}" if isinstance(r["score"], (int, float)) else "—"
            accepted += 1 if r["verdict"] == "accepted" else 0
            L.append(f"| {r['run']} | {r['task']} | {r['env']} | {r['verdict']} | {sc} | "
                     f"{_fmt_int(r['input'])} |")
        n_ungraded = sum(1 for r in rows if r["score"] is None)
        line = f"\n**{accepted}/{len(rows)} runs accepted.**"
        if n_ungraded:
            line += (f"  ({n_ungraded} run(s) have no score at all — their battery could not "
                     f"be evaluated, which is not the same as scoring 0.)")
        L.append(line + "\n")
    else:
        L.append("_No runs yet._\n")

    # Figures (auto-generated into agent-analysis/)
    aa = job_dir / "agent-analysis"
    figs = sorted(aa.glob("fig_*.png")) if aa.exists() else []
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
