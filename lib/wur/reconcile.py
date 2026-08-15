#!/usr/bin/env python3
"""
reconcile.py — the derivation chain, orchestrated idempotently.

RESPONSIBILITY
  Rebuild the four analysis tables of one run from its raw bytes, in the only
  order the data dependencies allow, writing each through a .tmp + os.replace so
  a crash never leaves a half-written table behind. is the whole point:
  raw before derived, derivation offline, re-runnable MONTHS LATER after a
  scanner bugfix, with the same raw bytes producing the same tables.

  Nothing in here talks to the agent, the network, or the workspace.

ORDER, AND WHY IT IS NOT's ORDER
   lists events before exposure. The data dependency runs
  the other way: events.jsonl carries `nonce_hits`, and `result_digest` is
  defined as "computed AFTER nonce scanning" (scan-before-truncate). So the
  chain is

      regions -> exposure -> events -> probes -> trace -> validate

  and exposure.assert_scan_before_truncate() makes the precondition checkable
  rather than a comment. Regions are extracted ONCE and shared, so every table
  is keyed to the same canonical `seq`.

INPUTS
  $RUN_DIR/stream.jsonl(.gz), transcript.jsonl, gate/tool_calls.jsonl,
  probe_plan.json, run_meta.json, use_detect.json, judge.json, hygiene.json,
  plus the fact registry (probe_key.json / facts.yaml / --facts).

OUTPUTS
  $RUN_DIR/{exposure,events,probes,fact_trace}.jsonl
  $RUN_DIR/reconcile.json    the summary telemetry.py folds into run_record.json:
                             join_coverage, the token dedupe delta, the pacing
                             count, unknown_visible, and every alarm that fired.
                             Deliberately timestamp-free, so byte-identical
                             re-runs prove idempotence.

CLI
  python3 lib/wur/reconcile.py --run-dir DIR [--facts F] [--no-validate]
                               [--strict] [--check-idempotent]
    exit 1 on a validation error, an unknown_visible region, or (with --strict)
    any failed pilot gate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

try:
    from . import (events as events_mod, exposure as exposure_mod, probes as probes_mod,
                   protocol, regions as regions_mod, trace as trace_mod,
                   validate as validate_mod)
except ImportError:  # flat context
    import events as events_mod  # type: ignore
    import exposure as exposure_mod  # type: ignore
    import probes as probes_mod  # type: ignore
    import protocol  # type: ignore
    import regions as regions_mod  # type: ignore
    import trace as trace_mod  # type: ignore
    import validate as validate_mod  # type: ignore

RECONCILE_VERSION = "wur-reconcile-v1"

#: The tables this module owns. Anything else in $RUN_DIR is raw or someone
#: else's output and is never touched.
OUTPUTS = ("exposure.jsonl", "events.jsonl", "probes.jsonl", "fact_trace.jsonl")


def reconcile(run_dir: str | os.PathLike, facts: Any = None, *, validate: bool = True,
              schemas_dir: str | os.PathLike | None = None) -> dict:
    """Rebuild every derived table for one run. Returns the summary dict."""
    rd = Path(run_dir)
    if not (rd / "stream.jsonl").exists() and not (rd / "stream.jsonl.gz").exists():
        raise FileNotFoundError(f"{rd}/stream.jsonl is missing — nothing to reconcile from")

    ident = probes_mod.identity_of(rd)
    run_id = str(ident.get("run_id") or rd.name)

    # 1. regions — extracted once, shared by everything downstream.
    rs = regions_mod.from_run(rd)

    # 2. exposure — the nonce scan, BEFORE anything digests or truncates.
    facts_src = facts if facts is not None else exposure_mod.find_facts_file(rd)
    cards = exposure_mod.load_fact_cards(facts_src) if facts_src is not None else []
    exposure_rows = exposure_mod.scan(rs, cards, run_id)
    regions_mod.write_jsonl_atomic(rd / "exposure.jsonl", exposure_rows)

    # 3. events — the stream x gate join; digests computed after the scan.
    ev = events_mod.build(rs, events_mod.load_gate(rd), exposure_rows, run_id)
    regions_mod.write_jsonl_atomic(rd / "events.jsonl", ev.rows)

    # 4. probes — the answers, the parse ladder, the source audit.
    probe_rows = probes_mod.build(ev.probe_turns, ev.rows, probes_mod.load_probe_plan(rd),
                                  cards, run_id, ident,
                                  task_text=probes_mod.task_text_of(rs))
    regions_mod.write_jsonl_atomic(rd / "probes.jsonl", probe_rows)

    # 5. trace — the headline table.
    trace_rows: list[dict] = []
    trace_summary: dict = {}
    trace_error: str | None = None
    try:
        trace_rows, trace_summary = trace_mod.run(
            rd, facts=facts_src, exposure_rows=exposure_rows, event_rows=ev.rows,
            probe_rows=probe_rows, events_summary=ev.summary)
    except trace_mod.D0PushSeqViolation as exc:
        trace_error = f"D0PushSeqViolation: {exc}"

    summary = _summarize(rd, run_id, ident, rs, exposure_rows, ev, probe_rows,
                         trace_rows, trace_summary, trace_error, cards, facts_src)

    if validate:
        summary["validation"] = validate_mod.validate_run(
            rd, schemas_dir, require=("events", "fact_trace") if cards else ())
        summary["ok"] = summary["ok"] and bool(summary["validation"]["ok"])

    regions_mod.write_json_atomic(rd / "reconcile.json", summary)
    return summary


def _summarize(rd: Path, run_id: str, ident: dict, rs: Any, exposure_rows: Sequence[dict],
               ev: Any, probe_rows: Sequence[dict], trace_rows: Sequence[dict],
               trace_summary: dict, trace_error: str | None, cards: Sequence[Any],
               facts_src: Any) -> dict:
    esum = ev.summary
    tokens = esum.get("tokens", {})
    unknown = len(rs.unknown_visible)
    parse_rate = (sum(1 for r in probe_rows if r.get("parse_ok")) / len(probe_rows)
                  if probe_rows else None)
    n_refused = sum(1 for r in probe_rows if r.get("outcome") == "refused")

    gates = {
        "join_coverage": {
            "value": esum.get("join_coverage"), "threshold": events_mod.JOIN_COVERAGE_GATE,
            "pass": bool(esum.get("join_coverage_pass"))},
        "pacing_one_tool_call_per_message": {
            "value": esum.get("max_tool_uses_per_message"), "threshold": 1,
            "pass": bool(esum.get("pacing_ok"))},
        "deduped_tokens_equal_result_usage": {
            "value": [tokens.get("dedupe_delta_input"), tokens.get("dedupe_delta_output")],
            "threshold": 0, "pass": bool(tokens.get("dedupe_equals_result"))},
        "channel_enum_closed": {
            "value": unknown, "threshold": 0, "pass": unknown == 0},
        "probe_parse_ok": {
            "value": parse_rate, "threshold": trace_mod.PARSE_OK_GATE,
            "pass": (parse_rate is None or parse_rate >= trace_mod.PARSE_OK_GATE)},
        "probe_refused": {"value": n_refused, "threshold": 0, "pass": n_refused == 0},
        # Zero fact cards means exposure.jsonl and fact_trace.jsonl are empty BY
        # CONSTRUCTION, not because the fact was never read. The registry lives in
        # $JOB_DIR/.registry/ and the run dir lives under $ATLAS_RUNS_ROOT, which
        # are deliberately different trees — so the caller must pass --facts.
        # Failing loudly here is the difference between "no uptake" and "no input".
        "fact_cards_loaded": {"value": len(cards), "threshold": 1, "pass": len(cards) >= 1},
    }
    alarms = {
        "unexplained_possession": trace_summary.get("unexplained_possession", 0),
        "ordering_violations": trace_summary.get("ordering_violations", 0),
        "confabulations": trace_summary.get("confabulations", 0),
        "quarantined": trace_summary.get("quarantined", 0),
        "unknown_visible": unknown,
        "trace_error": trace_error,
    }
    ok = all(g["pass"] for g in gates.values()) and trace_error is None

    return {
        "reconcile_version": RECONCILE_VERSION,
        "protocol_version": protocol.PROTOCOL_VERSION,
        "run_id": run_id,
        "job_id": ident.get("job_id"),
        "task_id": ident.get("task_id"),
        "condition_id": ident.get("condition_id"),
        "ok": ok,
        "inputs": _input_manifest(rd),
        "facts_source": str(facts_src) if isinstance(facts_src, (str, os.PathLike)) else "inline",
        "n_facts": len(cards),
        "counts": {
            "records": len(rs.records),
            "regions": len(rs.regions),
            "signals": len(rs.signals),
            "exposure_rows": len(exposure_rows),
            "events": len(ev.rows),
            "probes": len(probe_rows),
            "fact_trace": len(trace_rows),
        },
        "channels": rs.meta.get("channels", {}),
        "has_transcript": rs.meta.get("has_transcript"),
        "truncation_unavailable": rs.meta.get("truncation_unavailable", False),
        "events_summary": esum,
        "probe_summary": {
            "n_probes": len(probe_rows),
            "parse_ok_rate": parse_rate,
            "n_refused": n_refused,
            "n_superseded": sum(1 for r in probe_rows if r.get("outcome") == "superseded"),
            "n_unanswered": sum(1 for r in probe_rows if r.get("outcome") == "unanswered"),
        },
        "trace_summary": {k: v for k, v in trace_summary.items() if k != "diagnostics"},
        "trace_diagnostics": trace_summary.get("diagnostics", []),
        "gates": gates,
        "alarms": alarms,
        "outputs": {name: str(rd / name) for name in OUTPUTS},
    }


def run_record_patch(summary: dict, run_dir: str | os.PathLike) -> dict:
    """The run_record.json v2 fragments this chain owns, ready to merge.

    telemetry.py writes run_record.json; everything below is measured here and
    has nowhere else to come from. `tokens.accounting_version` travels with the
    numbers so a V7-era per_line_v1 record can never be pooled with these.
    """
    rd = Path(run_dir)
    esum = summary.get("events_summary", {})
    tok = esum.get("tokens", {})
    return {
        "tokens": {
            "accounting_version": tok.get("accounting_version"),
            "result_input": tok.get("result_input"),
            "result_output": tok.get("result_output"),
            "dedupe_delta_input": tok.get("dedupe_delta_input"),
            "dedupe_delta_output": tok.get("dedupe_delta_output"),
            "cost_usd": tok.get("cost_usd"),
        },
        "operations": {
            "turns_total": esum.get("turns_total"),
            "tool_calls_task": esum.get("tool_calls_task"),
            "max_tool_uses_per_message": esum.get("max_tool_uses_per_message"),
            "probes_sent": esum.get("probes_sent"),
            "probes_answered": esum.get("probes_answered"),
            "resumes_sent": esum.get("resumes_sent"),
            "permission_denials": esum.get("permission_denials"),
        },
        "raw": {
            "events_path": str(rd / "events.jsonl"),
            "exposure_path": str(rd / "exposure.jsonl"),
            "probes_path": str(rd / "probes.jsonl"),
            "fact_trace_path": str(rd / "fact_trace.jsonl"),
            "gate_path": str(rd / "gate" / "tool_calls.jsonl"),
            "probe_plan_path": str(rd / "probe_plan.json"),
        },
    }


def _input_manifest(rd: Path) -> dict:
    """sha256 + size of every raw input, so a re-run can prove it saw the same bytes."""
    out: dict[str, Any] = {}
    for name in ("stream.jsonl", "stream.jsonl.gz", "transcript.jsonl",
                 "gate/tool_calls.jsonl", "probe_plan.json", "run_meta.json",
                 "use_detect.json", "judge.json", "hygiene.json"):
        p = rd / name
        if not p.exists():
            continue
        data = p.read_bytes()
        out[name] = {"bytes": len(data), "sha256": protocol.sha256(data)}
    return out


def check_idempotent(run_dir: str | os.PathLike, facts: Any = None) -> dict:
    """Reconcile twice and compare the tables byte for byte.

    This is the property the whole design rests on: a scanner bugfix
    months from now must reproduce the tables from raw, not from a cached shape.
    """
    rd = Path(run_dir)
    reconcile(rd, facts, validate=False)
    before = {n: protocol.sha256((rd / n).read_bytes()) for n in OUTPUTS if (rd / n).exists()}
    reconcile(rd, facts, validate=False)
    after = {n: protocol.sha256((rd / n).read_bytes()) for n in OUTPUTS if (rd / n).exists()}
    diff = sorted(n for n in set(before) | set(after) if before.get(n) != after.get(n))
    return {"idempotent": not diff, "changed": diff, "sha256": after}


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="rebuild the derived tables of one run")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--facts", default=None, help="facts JSON/YAML or probe_key.json")
    p.add_argument("--schemas-dir", default=None)
    p.add_argument("--no-validate", action="store_true")
    p.add_argument("--strict", action="store_true", help="exit 1 on any failed gate")
    p.add_argument("--allow-no-facts", action="store_true",
                   help="do not fail when zero fact cards loaded (diagnostics only)")
    p.add_argument("--run-record-patch", action="store_true",
                   help="print the run_record.json v2 fragments on stdout and exit")
    p.add_argument("--check-idempotent", action="store_true")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)

    if a.check_idempotent:
        res = check_idempotent(a.run_dir, a.facts)
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0 if res["idempotent"] else 1

    summary = reconcile(a.run_dir, a.facts, validate=not a.no_validate,
                        schemas_dir=a.schemas_dir)
    if a.run_record_patch:
        print(json.dumps(run_record_patch(summary, a.run_dir), indent=2, sort_keys=True))
        return 0
    if not a.quiet:
        print(json.dumps({k: v for k, v in summary.items()
                          if k not in ("trace_diagnostics", "events_summary", "inputs")},
                         indent=2, sort_keys=True))
    for name, gate in summary["gates"].items():
        if not gate["pass"]:
            print(f"GATE FAILED {name}: {gate['value']} vs {gate['threshold']}", file=sys.stderr)
    if summary["alarms"].get("trace_error"):
        print(f"TRACE ERROR: {summary['alarms']['trace_error']}", file=sys.stderr)

    no_facts = not summary["gates"]["fact_cards_loaded"]["pass"] and not a.allow_no_facts
    if no_facts:
        print("NO FACT CARDS LOADED — exposure.jsonl and fact_trace.jsonl are empty by "
              "construction. Pass --facts $JOB_DIR/.registry/_index/probe_key.json; the "
              "registry is deliberately not an ancestor of the run dir.",
              file=sys.stderr)
    hard_fail = (summary["alarms"]["unknown_visible"] > 0
                 or summary["alarms"].get("trace_error")
                 or no_facts
                 or (summary.get("validation") and not summary["validation"]["ok"]))
    if hard_fail:
        return 1
    if a.strict and not summary["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
