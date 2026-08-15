#!/usr/bin/env python3
"""
pilot_triage.py — the pilot gates, evaluated across a whole job.

RESPONSIBILITY
  Answer one question with one exit code: MAY THE MAIN RUN START? IMPLEMENTATION
  .md pre-registers thirteen gates and says any failure blocks the 720-run
  matrix. reconcile.py already checks the per-run half of them one run at a time;
  this module is the job-level judgment — rates across runs, ceilings across
  tasks, and the fixture-wide alarms that are meaningless at n = 1.

  It is deliberately STDLIB ONLY. It runs at the end of the pilot on whatever
  python3 is on the box, before anyone has built .venv-analysis, and a gate that
  cannot run is a gate that gets skipped.

INPUTS   per run dir, whichever exist (a missing input makes ONE gate
         unevaluable, never the whole triage):
           run_record.json    operations.max_tool_uses_per_message,
                              tokens.dedupe_delta_{input,output}, accounting_version
           fact_trace.jsonl   read / available / ever_mention / used / success /
                              unexplained_possession / echoed / factors
           probes.jsonl       parse_ok, outcome
           hygiene.json       ambient_memory (H1)
           reconcile.json     join_coverage
OUTPUTS  a dict (and JSON on stdout / to --json) of Gate records, plus
         `blocked`: true when any blocking gate FAILED.

FAIL-CLOSED, BUT NOT FAIL-CONFUSED
  A gate whose input is absent reports status "unevaluable" and does NOT block by
  itself; the triage as a whole reports how many gates it could not evaluate, and
  --require-all turns that into a block. The distinction matters: "the pacing
  invariant broke" and "nobody wrote max_tool_uses_per_message" call for
  different actions, and collapsing them into one red light guarantees the wrong
  one gets taken.

CLI
  python3 lib/wur/pilot_triage.py --job-dir jobs/<id> [--runs-root DIR]
                                  [--json OUT] [--markdown] [--require-all]
    exit 0 all blocking gates pass · exit 1 a blocking gate failed
    exit 2 nothing to evaluate (no runs found)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

try:
    from . import aggregate as agg
except ImportError:  # flat context (lib/wur on sys.path)
    import aggregate as agg  # type: ignore

TRIAGE_VERSION = "wur-pilot-triage-v1"

# thresholds, in one place so a decision rule cannot drift from the document.
CONFAB_MAX = 0.05
UNEXPLAINED_POSSESSION_MAX = 0.05
READ_D1_MIN = 0.50
READ_D3_MAX = 0.90
DEPTH_CEILING_TASK_FRAC_MAX = 0.60
DEPTH_CEILING_READ_D1 = 0.90
DEPTH_CEILING_GAP = 0.05
PARSE_OK_MIN = 0.90
PACING_RUN_FRAC_MIN = 0.95
PLANT_VERIFICATION_MIN = 1.00
PHI_MAX = 0.80
JOIN_COVERAGE_MIN = 0.99


@dataclass
class Gate:
    """One pre-registered decision rule, with the number that decided it."""
    id: str
    name: str
    rule: str
    threshold: Any
    value: Any = None
    n: int | None = None
    status: str = "unevaluable"      # pass | fail | unevaluable
    blocking: bool = True
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool | None:
        return {"pass": True, "fail": False}.get(self.status)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["passed"] = self.passed
        return d


# ── loading ──────────────────────────────────────────────────────────────────
def load_runs(job_dir: str | os.PathLike,
              runs_root: str | os.PathLike | None = None) -> list[dict[str, Any]]:
    """One dict per run dir: identity plus every artifact the gates read."""
    out = []
    for rd in agg.iter_run_dirs(job_dir, runs_root):
        record = agg.read_json(rd / "run_record.json", {}) or {}
        out.append({
            "run_dir": str(rd),
            "run_id": (record.get("run", {}) or {}).get("run_id") or rd.name,
            "run_record": record,
            "hygiene": agg.read_json(rd / "hygiene.json", {}) or {},
            "reconcile": agg.read_json(rd / "reconcile.json", {}) or {},
            "fact_trace": agg.read_jsonl(rd / "fact_trace.jsonl"),
            "probes": agg.read_jsonl(rd / "probes.jsonl"),
        })
    return out


def _dig(d: Any, *keys: str) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _rows(runs: Sequence[dict], *, keep_quarantined: bool = False,
          keep_weak: bool = False) -> list[dict]:
    """fact_trace rows from every run.

    The default frame mirrors analysis/uptake_lib.analysis_frame: analyzable,
    un-quarantined, weak facts out. keep_quarantined=True is the ALARM frame — a
    control row that names the nonce is quarantined by construction and is also
    the entire numerator of confab_rate, so dropping it would make the gate
    report 0.00 on exactly the failure it exists to catch.
    """
    out = []
    for run in runs:
        for r in run["fact_trace"]:
            if r.get("analyzable") is False:
                continue
            if not keep_quarantined and r.get("quarantined") is True:
                continue
            if not keep_weak and r.get("prior_check_status") == "weak":
                continue
            out.append(r)
    return out


def _arm_of(row: dict) -> str | None:
    v = row.get("condition_id")
    return str(v) if v is not None else None


def _rate(rows: Iterable[dict], field_name: str) -> tuple[float | None, int, int, int]:
    """(rate, k, n_known, n_unknown). Null outcomes leave the denominator: `read`
    null means UNKNOWN (a truncation on a call targeting the fact file) and
    coercing it to 0 would make the bias run with the hypothesis."""
    k = n = unk = 0
    for r in rows:
        v = r.get(field_name)
        if v is None:
            unk += 1
            continue
        n += 1
        if v:
            k += 1
    return ((k / n) if n else None), k, n, unk


def _by_arm(rows: Sequence[dict], arm: str) -> list[dict]:
    return [r for r in rows if _arm_of(r) == arm]


# ── the thirteen gates ───────────────────────────────────────────────────────
def gate_confab(runs: Sequence[dict]) -> Gate:
    """P(mention | ¬read ∧ ¬echoed-before-mention) <= 0.05.

    A nonce cannot be guessed. A breach means it leaked into a prompt, the
    tier-(b) paraphrase regex over-matches, or an exposure channel is missing
    from the scanner — and any of those invalidates every exposure-conditioned
    metric in the study. Computed on the ALARM frame.

    fact_trace carries `echoed` as a run-level flag, not an echo-before-mention
    ordering, so the ¬echoed clause is applied as the conservative approximation
    it is; the unrestricted rate is reported beside it.
    """
    rows = _rows(runs, keep_quarantined=True, keep_weak=True)
    not_read = [r for r in rows if r.get("read") is False]
    k = sum(1 for r in not_read if r.get("ever_mention") is True)
    n = len(not_read)
    gate_rows = [r for r in not_read if r.get("echoed") is not True]
    kg = sum(1 for r in gate_rows if r.get("ever_mention") is True)
    ng = len(gate_rows)
    value = (kg / ng) if ng else None
    g = Gate("G1", "confab_rate", "P(mention | NOT read AND NOT echoed) <= 0.05",
             CONFAB_MAX, value, ng,
             detail={"k": kg, "n": ng,
                     "unrestricted_rate": (k / n) if n else None,
                     "unrestricted_k": k, "unrestricted_n": n,
                     "run_ids": [str(r.get("run_id")) for r in gate_rows
                                 if r.get("ever_mention") is True][:20]})
    if value is None:
        g.detail["reason"] = "no rows with read == False"
    else:
        g.status = "pass" if value <= CONFAB_MAX else "fail"
    return g


def gate_unexplained_possession(runs: Sequence[dict]) -> Gate:
    """The D4 alarm: a self_thinking hit with no prior inbound hit, <= 0.05.

    Folding thinking into `read` mutes confabulation detection; this restores it.
    A pilot rate above the threshold is a FIXTURE-WIDE FAILURE, not a data point.
    """
    rows = _rows(runs, keep_quarantined=True, keep_weak=True)
    value, k, n, _ = _rate(rows, "unexplained_possession")
    g = Gate("G2", "unexplained_possession", "rate <= 0.05", UNEXPLAINED_POSSESSION_MAX,
             value, n, detail={"k": k, "n": n,
                               "run_ids": [str(r.get("run_id")) for r in rows
                                           if r.get("unexplained_possession") is True][:20]})
    if value is not None:
        g.status = "pass" if value <= UNEXPLAINED_POSSESSION_MAX else "fail"
    return g


def gate_read_d1(runs: Sequence[dict]) -> Gate:
    """read_rate(d1) >= 0.50 — if even the root fact isn't reached, depth has no
    dynamic range and every deeper contrast is measuring noise."""
    rows = _by_arm(_rows(runs), "d1")
    value, k, n, unk = _rate(rows, "read")
    g = Gate("G3", "read_rate_d1", "read_rate(d1) >= 0.50", READ_D1_MIN, value, n,
             detail={"k": k, "n": n, "n_unknown": unk})
    if value is not None:
        g.status = "pass" if value >= READ_D1_MIN else "fail"
    return g


def gate_read_d3(runs: Sequence[dict]) -> Gate:
    """read_rate(d3) <= 0.90 — a ceiling makes the ladder undetectable."""
    rows = _by_arm(_rows(runs), "d3")
    value, k, n, unk = _rate(rows, "read")
    g = Gate("G4", "read_rate_d3", "read_rate(d3) <= 0.90", READ_D3_MAX, value, n,
             detail={"k": k, "n": n, "n_unknown": unk})
    if value is not None:
        g.status = "pass" if value <= READ_D3_MAX else "fail"
    return g


def gate_depth_insensitive(runs: Sequence[dict]) -> Gate:
    """NOT (read(d1) > 0.90 AND read(d3) - read(d1) < 0.05) on >= 60% of tasks.

    If docs/ is cheap enough to read exhaustively, the ladder measures nothing.
    This is a FIXTURE-WIDE failure with a fixture-wide remedy — grow docs/ and
    re-pilot — which is why it is evaluated per task and then counted, rather
    than as one pooled number that a couple of easy tasks could hide inside.
    """
    rows = _rows(runs)
    tasks = sorted({str(r.get("task_id")) for r in rows if r.get("task_id") is not None})
    per_task, flagged = [], 0
    for t in tasks:
        d1 = [r for r in rows if str(r.get("task_id")) == t and _arm_of(r) == "d1"]
        d3 = [r for r in rows if str(r.get("task_id")) == t and _arm_of(r) == "d3"]
        r1, _, n1, _ = _rate(d1, "read")
        r3, _, n3, _ = _rate(d3, "read")
        if r1 is None or r3 is None:
            per_task.append({"task_id": t, "read_d1": r1, "read_d3": r3, "ceiling": None})
            continue
        ceiling = bool(r1 > DEPTH_CEILING_READ_D1 and (r3 - r1) < DEPTH_CEILING_GAP)
        flagged += int(ceiling)
        per_task.append({"task_id": t, "read_d1": r1, "read_d3": r3, "n_d1": n1,
                         "n_d3": n3, "ceiling": ceiling})
    evaluable = [p for p in per_task if p["ceiling"] is not None]
    value = (flagged / len(evaluable)) if evaluable else None
    g = Gate("G5", "depth_insensitive",
             "fraction of tasks with (read(d1) > 0.90 AND read(d3)-read(d1) < 0.05) < 0.60",
             DEPTH_CEILING_TASK_FRAC_MAX, value, len(evaluable),
             detail={"n_tasks_flagged": flagged, "per_task": per_task})
    if value is not None:
        g.status = "pass" if value < DEPTH_CEILING_TASK_FRAC_MAX else "fail"
    return g


def gate_parse_ok(runs: Sequence[dict]) -> Gate:
    """probe parse_ok >= 0.90 — the answer format must be machine-readable."""
    n = k = 0
    for run in runs:
        for p in run["probes"]:
            n += 1
            k += int(bool(p.get("parse_ok")))
    value = (k / n) if n else None
    g = Gate("G6", "probe_parse_ok", "parse_ok rate >= 0.90", PARSE_OK_MIN, value, n,
             detail={"k": k, "n": n})
    if value is not None:
        g.status = "pass" if value >= PARSE_OK_MIN else "fail"
    return g


def gate_refused(runs: Sequence[dict]) -> Gate:
    """probe refused == 0 — a refusal means the trusted stream-json user channel
    broke, and the refusal text contaminates the run's final answer."""
    n = k = 0
    bad = []
    for run in runs:
        for p in run["probes"]:
            n += 1
            if p.get("outcome") == "refused":
                k += 1
                bad.append({"run_id": run["run_id"], "probe_id": p.get("probe_id")})
    g = Gate("G7", "probe_refused", "count of refused probes == 0", 0, k, n,
             detail={"refused": bad[:20]})
    if n:
        g.status = "pass" if k == 0 else "fail"
    return g


def gate_pacing(runs: Sequence[dict]) -> Gate:
    """max(tool_uses_per_assistant_message) == 1 in >= 95% of runs.

    Pacing is the invariant everything rests on: it makes turn boundaries
    approximately tool-call boundaries, which is what the every-1-3-tool-calls
    cadence means. The producer must GROUP BY message.id before counting —
    stream.jsonl splits one assistant message across lines exactly like the
    transcript, so a per-line count inherits the V7 double-count bug.

    Runs with zero tool calls are excluded from the denominator: a run that never
    called a tool cannot violate pacing, and counting it as a violation would
    fail the gate on a run that crashed early.
    """
    ok = tot = zero = 0
    unknown = []
    for run in runs:
        v = _dig(run["run_record"], "operations", "max_tool_uses_per_message")
        if v is None:
            unknown.append(run["run_id"])
            continue
        if int(v) == 0:
            zero += 1
            continue
        tot += 1
        ok += int(int(v) == 1)
    value = (ok / tot) if tot else None
    g = Gate("G8", "pacing_one_tool_call_per_message",
             "share of runs with max_tool_uses_per_message == 1 >= 0.95",
             PACING_RUN_FRAC_MIN, value, tot,
             detail={"k": ok, "n": tot, "n_zero_tool_calls": zero,
                     "n_missing_field": len(unknown), "missing_run_ids": unknown[:20]})
    if value is not None:
        g.status = "pass" if value >= PACING_RUN_FRAC_MIN else "fail"
    return g


def gate_token_dedupe(runs: Sequence[dict]) -> Gate:
    """deduped_token_total == result.usage total, EXACTLY, on every run.

    Dedupe-by-message.id was measured to equal the terminal `result` totals
    exactly (53,292 == 53,292), so this check is free and any drift means the V7
    fix regressed. It also refuses to pass a job that mixes accounting versions:
    per_line_v1 input is inflated 1.00x-4.90x against per_message_v2, and pooling
    them is a silent error with no other detector.
    """
    bad, n, versions = [], 0, {}
    for run in runs:
        tok = _dig(run["run_record"], "tokens") or {}
        v = tok.get("accounting_version") or "unset"
        versions[v] = versions.get(v, 0) + 1
        di, do = tok.get("dedupe_delta_input"), tok.get("dedupe_delta_output")
        if di is None and do is None:
            continue
        n += 1
        if (di or 0) != 0 or (do or 0) != 0:
            bad.append({"run_id": run["run_id"], "dedupe_delta_input": di,
                        "dedupe_delta_output": do})
    mixed = len([k for k in versions if k != "unset"]) > 1
    g = Gate("G9", "deduped_tokens_equal_result_usage",
             "dedupe_delta_input == 0 AND dedupe_delta_output == 0 on every run", 0,
             len(bad), n,
             detail={"offending_runs": bad[:20], "accounting_versions": versions,
                     "mixed_accounting_versions": mixed})
    if n:
        g.status = "pass" if (not bad and not mixed) else "fail"
    return g


def gate_plant_verification(runs: Sequence[dict]) -> Gate:
    """plant verification == 1.00: every fact-present run's baseline_sha contains
    the nonce. A plant that did not land is analyzable=false and EXCLUDED, never
    counted as a miss — so this gate is about the fixture, not about the agent."""
    rows = [r for r in _rows(runs, keep_quarantined=True, keep_weak=True)
            if (r.get("factors") or {}).get("fact_present") is True
            or r.get("factor_fact_present") is True]
    n = len(rows)
    k = sum(1 for r in rows if r.get("available") is True)
    value = (k / n) if n else None
    g = Gate("G10", "plant_verification", "available == true on every fact-present run",
             PLANT_VERIFICATION_MIN, value, n,
             detail={"k": k, "n": n,
                     "failed_run_ids": [str(r.get("run_id")) for r in rows
                                        if r.get("available") is not True][:20]})
    if value is not None:
        g.status = "pass" if value >= PLANT_VERIFICATION_MIN else "fail"
    return g


def gate_orthogonality(runs: Sequence[dict]) -> Gate:
    """|phi(used, success)| <= 0.8 per task.

    If the battery tests the mandate then success == used, the funnel collapses to
    one measurement, and the whole instrument is measuring itself. A breaching
    fact is discarded — the same disposition as a failed prior-check.
    """
    rows = _rows(runs)
    tasks = sorted({str(r.get("task_id")) for r in rows if r.get("task_id") is not None})
    per_task, worst, breaches = [], None, []
    for t in tasks:
        pairs = [(bool(r.get("used")), bool(r.get("success"))) for r in rows
                 if str(r.get("task_id")) == t
                 and r.get("used") is not None and r.get("success") is not None]
        phi = _phi(pairs)
        per_task.append({"task_id": t, "n": len(pairs), "phi": phi})
        if phi is None:
            continue
        if worst is None or abs(phi) > abs(worst):
            worst = phi
        if abs(phi) > PHI_MAX:
            breaches.append({"task_id": t, "phi": phi})
    g = Gate("G11", "orthogonality_phi", "max_t |phi(used, success)| <= 0.8", PHI_MAX,
             worst, len(per_task), detail={"per_task": per_task, "breaches": breaches})
    if worst is not None:
        g.status = "pass" if not breaches else "fail"
    return g


def _phi(pairs: Sequence[tuple[bool, bool]]) -> float | None:
    """Pearson phi on a 2x2. None when either margin is degenerate — an undefined
    correlation is not a passing one and is not a failing one."""
    if len(pairs) < 2:
        return None
    n11 = sum(1 for u, s in pairs if u and s)
    n10 = sum(1 for u, s in pairs if u and not s)
    n01 = sum(1 for u, s in pairs if not u and s)
    n00 = sum(1 for u, s in pairs if not u and not s)
    num = n11 * n00 - n10 * n01
    den = math.sqrt((n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00))
    return (num / den) if den > 0 else None


def gate_ambient_memory(runs: Sequence[dict]) -> Gate:
    """ambient_memory empty on every run (preflight H1): no ancestor CLAUDE.md.

    An ancestor CLAUDE.md is uncontrolled context sitting directly upstream of the
    measurement; one run with one is not a noisy data point, it is a different
    experiment.
    """
    n = 0
    bad = []
    for run in runs:
        hyg = run["hygiene"]
        if not hyg:
            continue
        n += 1
        ambient = hyg.get("ambient_memory")
        if ambient:
            bad.append({"run_id": run["run_id"], "ambient_memory": ambient})
    g = Gate("G12", "ambient_memory_empty", "hygiene.ambient_memory empty on every run",
             0, len(bad), n, detail={"offending_runs": bad[:20]})
    if n:
        g.status = "pass" if not bad else "fail"
    return g


def gate_join_coverage(runs: Sequence[dict]) -> Gate:
    """join_coverage >= 0.99 on every run: stream <-> gate join integrity.

    Below it, `barrier` ordinals and `is_probe_turn` stop being trustworthy, and
    both feed the cadence and the difficulty-band metric.
    """
    vals, bad = [], []
    for run in runs:
        v = _dig(run["reconcile"], "gates", "join_coverage", "value")
        if v is None:
            v = _dig(run["reconcile"], "join_coverage")
        if v is None:
            continue
        vals.append(float(v))
        if float(v) < JOIN_COVERAGE_MIN:
            bad.append({"run_id": run["run_id"], "join_coverage": float(v)})
    value = min(vals) if vals else None
    g = Gate("G13", "join_coverage", "min over runs of join_coverage >= 0.99",
             JOIN_COVERAGE_MIN, value, len(vals),
             detail={"offending_runs": bad[:20],
                     "mean": (sum(vals) / len(vals)) if vals else None})
    if value is not None:
        g.status = "pass" if not bad else "fail"
    return g


#: Evaluation order = the table order below. Every gate is blocking.
GATES: tuple[Callable[[Sequence[dict]], Gate], ...] = (
    gate_confab,
    gate_unexplained_possession,
    gate_read_d1,
    gate_read_d3,
    gate_depth_insensitive,
    gate_parse_ok,
    gate_refused,
    gate_pacing,
    gate_token_dedupe,
    gate_plant_verification,
    gate_orthogonality,
    gate_ambient_memory,
    gate_join_coverage,
)


def triage(runs: Sequence[dict], *, require_all: bool = False) -> dict[str, Any]:
    """Evaluate every gate. `blocked` is the answer to "may the main run start"."""
    gates = [fn(runs) for fn in GATES]
    failed = [g for g in gates if g.status == "fail"]
    unevaluable = [g for g in gates if g.status == "unevaluable"]
    rows = _rows(runs, keep_quarantined=True, keep_weak=True)
    return {
        "triage_version": TRIAGE_VERSION,
        "n_runs": len(runs),
        "n_fact_trace_rows": len(rows),
        "n_tasks": len({str(r.get("task_id")) for r in rows if r.get("task_id") is not None}),
        "n_arms": len({_arm_of(r) for r in rows if _arm_of(r)}),
        "gates": [g.to_dict() for g in gates],
        "n_passed": sum(1 for g in gates if g.status == "pass"),
        "n_failed": len(failed),
        "n_unevaluable": len(unevaluable),
        "failed": [g.id for g in failed],
        "unevaluable": [g.id for g in unevaluable],
        "blocked": bool(failed) or (require_all and bool(unevaluable)),
    }


def render_markdown(result: dict[str, Any]) -> str:
    icon = {"pass": "PASS", "fail": "**FAIL**", "unevaluable": "—"}
    L = [f"# Pilot triage ({result['triage_version']})", "",
         f"{result['n_runs']} runs · {result['n_fact_trace_rows']} fact_trace rows · "
         f"{result['n_tasks']} tasks · {result['n_arms']} arms", "",
         "| gate | rule | value | threshold | n | verdict |", "|---|---|---|---|---|---|"]
    for g in result["gates"]:
        v = g["value"]
        vs = f"{v:.4f}" if isinstance(v, float) else ("—" if v is None else str(v))
        # Rules contain '|' (set-builder bars, absolute values); escape or the
        # markdown table silently loses columns.
        rule = str(g["rule"]).replace("|", "\\|")
        L.append(f"| {g['id']} {g['name']} | {rule} | {vs} | {g['threshold']} | "
                 f"{g['n'] if g['n'] is not None else '—'} | {icon[g['status']]} |")
    L += ["", f"passed {result['n_passed']} · failed {result['n_failed']} · "
              f"unevaluable {result['n_unevaluable']}", "",
          ("**BLOCKED — the main run may not start.**" if result["blocked"]
           else "Not blocked: every evaluable gate passed.")]
    return "\n".join(L) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="evaluate the pilot gates over a job")
    p.add_argument("--job-dir", required=True)
    p.add_argument("--runs-root", default=None)
    p.add_argument("--json", default=None, help="write the full result here")
    p.add_argument("--markdown", action="store_true", help="human table instead of JSON")
    p.add_argument("--require-all", action="store_true",
                   help="an unevaluable gate blocks too (use when every artifact should exist)")
    a = p.parse_args(argv)

    runs = load_runs(a.job_dir, a.runs_root)
    if not runs:
        print(f"ERROR: no run dirs found under {a.job_dir}", file=sys.stderr)
        return 2
    result = triage(runs, require_all=a.require_all)
    if a.markdown:
        print(render_markdown(result))
    else:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if a.json:
        Path(a.json).write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
                                encoding="utf-8")
    if result["blocked"]:
        print(f"BLOCKED: gates failed {result['failed']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
