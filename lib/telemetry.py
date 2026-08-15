#!/usr/bin/env python3
"""
telemetry.py — exp-runner wrapper around the ported telemetry core.

RESPONSIBILITY
  Read a finished run dir and write the canonical record for it. This is where a run's
  raw bytes become the row every figure, dashboard and analysis reads.

INPUTS   <run>/run_meta.json          identity, condition, factors, the six sha256 stamps
         <run>/transcript.jsonl       the agent session (claude: copied BY --session-id)
         <run>/stream.jsonl[.gz]      WUR only — verbatim child stdout, and the ONLY place
                                      the authoritative terminal `result` totals exist
         <run>/probes.jsonl           WUR only — probe rows, for the probe counters
         <run>/judge.json             the verdict
         <run>/agent_exit_code
OUTPUTS  <run>/run_record.json  (schema_version 2, validated against
                                 schemas/run_record.schema.json when jsonschema is present)
         <run>/event_log.jsonl (one line per message/tool event, legacy shape)

TOKENS
  Totals are deduped by `message.id` in the adapter and again in extract/core.py, then
  OVERRIDDEN by the terminal `result` event where one exists. Deduped-vs-result deltas
  are recorded, not swallowed: they were measured to be exactly zero, so a non-zero delta
  is a regression signal and gets printed loudly.
  MEASURED HERE: stream.jsonl's per-message `output_tokens` is a streaming placeholder
  (1/2/3 for messages whose real output was 1,405/2,101/3,099), so events always come
  from transcript.jsonl and only the `result` event is read out of the stream.

Generalization vs the experiment: there is no task YAML or env condition in ladder mode,
so task_id := job_id and env_id := "asis" unless run_meta says otherwise.

Usage:
  python3 telemetry.py --run-dir jobs/<id>/runs/<run-id>
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent
ROOT = LIB.parent
sys.path.insert(0, str(LIB))

from extract.core import extract, _infer_provider, token_accounting_ok  # noqa: E402
from extract.adapters import claude_code, codex, gemini, antigravity  # noqa: E402

SCHEMA_PATH = ROOT / "schemas" / "run_record.schema.json"

# run_meta keys copied straight through to run_record.condition. All optional: a v1
# run_meta.json written by today's setup_run.sh has none of them and the record stays valid.
CONDITION_HASHES = ("cli_argv_sha256", "probe_text_sha256", "pacing_prompt_sha256",
                    "workspace_sha256", "init_sha256", "canary_sha256")
FACTOR_KEYS = ("depth", "format", "channel", "distractors", "fact_present", "probe",
               "pointer_regime")
RUN_V2_KEYS = ("matrix_seed", "cell_index", "run_order_index", "concurrency_at_launch")
# raw.<key> -> the file under the run dir it points at. Emitted only when it exists,
# so a ladder run's record does not claim artifacts it never produced.
RAW_ARTIFACTS = {
    "stream_path": ("stream.jsonl.gz", "stream.jsonl"),
    "run_meta_path": ("run_meta.json",),
    "hygiene_path": ("hygiene.json",),
    "probe_plan_path": ("probe_plan.json",),
    "gate_path": ("gate/tool_calls.jsonl",),
    "use_detect_path": ("use_detect.json",),
    "events_path": ("events.jsonl",),
    "exposure_path": ("exposure.jsonl",),
    "probes_path": ("probes.jsonl",),
    "fact_trace_path": ("fact_trace.jsonl",),
    "persisted_dir": ("watch/persisted",),
}


def _grade_from_judge(judge: dict | None, agent_exit_code: int) -> dict:
    if agent_exit_code == 124:
        terminal = "timeout"
    elif judge is None:
        terminal = "error"
    else:
        terminal = judge.get("verdict") or "error"
    ac_results = {}
    if judge:
        for c in judge.get("criteria", []):
            ac_results[c.get("id", c.get("criterion", "?"))] = {
                # `met` is tri-state since the judge stopped coercing an unevaluable
                # criterion to False; the schema types `passed` as boolean|null to match.
                "passed": c.get("met"),
                "output": (c.get("evidence") or c.get("error") or "")[:500],
            }
    return {
        "terminal_state": terminal,
        "score_automated": (judge or {}).get("score"),
        "score_human": None,
        "ac_results": ac_results,
    }


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _stream_lines(run_dir: Path) -> list[str]:
    """stream.jsonl, gz or not. Absent on every ladder run — that is not an error."""
    for name in ("stream.jsonl", "stream.jsonl.gz"):
        p = run_dir / name
        if p.exists():
            return _read_lines(p)
    return []


def _probe_rows(run_dir: Path) -> list[dict] | None:
    """probes.jsonl as produced by lib/wur/probes.py, or None when this is not a WUR run.

    None and [] are DIFFERENT: None means "this run has no probe channel at all" (ladder,
    or a no-probe arm that never wrote the file) and leaves the probe counters null; []
    means "the channel existed and fired zero probes", which is a measurement.
    """
    p = run_dir / "probes.jsonl"
    if not p.exists():
        return None
    rows = []
    for line in _read_lines(p):
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


def _overlay_gemini_tokens(run_dir: Path, agent_id: str, run_record: dict) -> None:
    """Replace summed transcript tokens with the authoritative stdout -o json stats.

    Gemini's stdout stats give the provider's own session totals:
      stats.models.<id>.tokens = {input, prompt, candidates, cached, thoughts, tool, total}
    where prompt = input + cached. We map total_input -> prompt (so it INCLUDES cache,
    matching the claude adapter's convention) and cache_read -> cached. cache_write has
    no Gemini equivalent (nulled)."""
    try:
        sj = json.loads((run_dir / "agent_stdout.json").read_text())
    except Exception:
        return
    models = (sj.get("stats") or {}).get("models") or {}
    tk = (models.get(agent_id) or next(iter(models.values()), {})).get("tokens")
    if not tk:
        return
    prompt = tk.get("prompt", tk.get("input", 0) + tk.get("cached", 0))
    cand = tk.get("candidates", 0)
    cached = tk.get("cached", 0)
    T = run_record["tokens"]
    T["total_input"] = prompt
    T["total_output"] = cand
    T["cache_read"] = cached
    T["cache_write"] = 0
    T["total_effective"] = prompt - int(cached * 0.9) + cand


def _overlay_agy_tokens(run_dir: Path, run_record: dict) -> None:
    """agy token usage lives in conversations/<uuid>.db (gen_metadata protobuf), copied to
    <run>/agy_conversation.db by teardown. Client-side ESTIMATES: no cache, output excludes
    hidden reasoning; each turn re-sends context so total_input = Σ per-call (billed upper bound)."""
    db = run_dir / "agy_conversation.db"
    if not db.exists():
        return
    try:
        import agy  # lib/agy.py
    except ImportError:
        return
    u = agy.extract_tokens(str(db))
    if not u or not u.get("num_calls"):
        return
    T = run_record["tokens"]
    T["total_input"] = u["total_input"]
    T["total_output"] = u["total_output"]
    T["cache_read"] = 0          # agy exposes no cache metric
    T["cache_write"] = 0
    T["total_effective"] = u["total_input"] + u["total_output"]


def _apply_v2(run_record: dict, meta: dict, run_dir: Path) -> None:
    """Copy the v2 fields run_meta.json carries into the record.

    Everything here is written only when the key is actually present, so a run_meta.json
    from the pre-WUR setup_run.sh produces a record with no v2 condition fields at all —
    except `run.experiment`, which defaults to "ladder" because a record that does not say
    which instrument produced it cannot be pooled safely by anything downstream.
    """
    run = run_record["run"]
    run["experiment"] = meta.get("experiment") or "ladder"
    for k in RUN_V2_KEYS:
        if meta.get(k) is not None:
            run[k] = meta[k]

    cond = run_record["condition"]
    # `env_id` carries the arm for back-compat (the dashboard, report.py and
    # figures.py all read it), but v2 also declares `condition_id` as the explicit,
    # non-overloaded field — and it was never populated, so anything keying off the
    # v2 name got null while only the overloaded ladder name worked.
    if meta.get("condition_id") is not None:
        cond["condition_id"] = meta["condition_id"]
    elif meta.get("env_id") is not None and (meta.get("experiment") == "wur"):
        cond["condition_id"] = meta["env_id"]
    for k in ("backend", "model", "cli_version"):
        if meta.get(k) is not None:
            cond[k] = meta[k]
    for k in CONDITION_HASHES:
        if meta.get(k):
            cond[k] = meta[k]
    factors = meta.get("factors")
    if isinstance(factors, dict) and factors:
        cond["factors"] = {k: factors.get(k) for k in FACTOR_KEYS if k in factors}

    raw = run_record["raw"]
    for key, candidates in RAW_ARTIFACTS.items():
        for rel in candidates:
            p = run_dir / rel
            if p.exists():
                raw[key] = str(p)
                break


def build(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "run_meta.json").read_text())
    agent_id = meta["agent_id"]

    judge = None
    if (run_dir / "judge.json").exists():
        judge = json.loads((run_dir / "judge.json").read_text())
    try:
        agent_exit_code = int((run_dir / "agent_exit_code").read_text().strip())
    except Exception:
        agent_exit_code = 0
    grade = _grade_from_judge(judge, agent_exit_code)

    # Enrich meta to the keys the ported extractor expects.
    job_id = meta.get("job_id", "job")
    enriched = {
        **meta,
        "experiment_id": job_id,
        "operator": None,
        "batch_id": None,
        "task_id": meta.get("task_id") or job_id,
        "env_id": meta.get("env_id") or "E0",
        "env_overlay_hash": meta.get("env_overlay_hash"),
    }

    provider = _infer_provider(agent_id)
    raw = _read_lines(run_dir / "transcript.jsonl")
    result_totals = None
    include_aux = (meta.get("experiment") == "wur")
    if provider == "anthropic":
        events = claude_code.normalize(raw, include_aux=include_aux)
        # The `result` event lives in stream.jsonl only. Its usage is ground truth; the
        # stream's PER-MESSAGE output_tokens is not (it is a streaming placeholder), so
        # the events themselves always come from the transcript.
        result_totals = claude_code.terminal_result(_stream_lines(run_dir))
    elif provider == "openai":
        events = codex.normalize(raw)
    elif provider == "google":
        events = gemini.normalize(raw)
    elif provider == "antigravity":
        events = antigravity.normalize(raw)
    else:
        events = []

    run_record, event_log = extract(events, enriched, {}, grade,
                                    probe_events=_probe_rows(run_dir),
                                    result_totals=result_totals)

    # Gemini: per-message transcript tokens may be per-turn or cumulative, so use the
    # authoritative aggregate from the stdout `-o json` stats instead. No-op on a
    # timeout (empty/absent stdout) -> the transcript-summed values stand as fallback.
    if provider == "google":
        _overlay_gemini_tokens(run_dir, agent_id, run_record)
    # agy stores NO tokens in the transcript — pull them from the conversation SQLite DB
    # (client-side estimates; no cache field). See AGY_DOCS.md
    elif provider == "antigravity":
        _overlay_agy_tokens(run_dir, run_record)

    # Generalized fields the experiment record didn't carry.
    run_record["condition"]["repo_url"] = meta.get("repo_url") or meta.get("repo_path")
    if judge:
        run_record["outcome"]["verdict"] = judge.get("verdict")
    run_record["raw"]["transcript_path"] = str(run_dir / "transcript.jsonl")
    run_record["raw"]["patch_path"] = str(run_dir / "git.patch")
    run_record["raw"]["grade_path"] = str(run_dir / "judge.json")
    _apply_v2(run_record, meta, run_dir)

    (run_dir / "run_record.json").write_text(json.dumps(run_record, indent=2))
    with open(run_dir / "event_log.jsonl", "w") as f:
        for e in event_log:
            f.write(json.dumps(e) + "\n")
    return run_record


def validate(run_dir: Path) -> list[str]:
    try:
        import jsonschema
    except ImportError:
        return []
    record = json.loads((Path(run_dir) / "run_record.json").read_text())
    schema = json.loads(SCHEMA_PATH.read_text())
    v = jsonschema.Draft202012Validator(schema)
    return [f"{list(e.path)}: {e.message}" for e in sorted(v.iter_errors(record), key=lambda e: list(e.path))]




def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    args = p.parse_args()
    run_dir = Path(args.run_dir)
    rec = build(run_dir)
    errs = validate(run_dir)
    t = rec["tokens"]
    ops = rec["operations"]
    print(f"run_record → {run_dir/'run_record.json'}  "
          f"input={t['total_input']} output={t['total_output']} "
          f"turns={ops.get('turns_total')} acct={t.get('accounting_version')} "
          f"verdict={rec['outcome'].get('verdict')} score={rec['outcome']['score_automated']}")
    # The free correctness check makes a pilot gate: dedupe-by-message.id was measured
    # to equal the terminal result.usage totals EXACTLY, so a delta means the V7 fix broke.
    for problem in token_accounting_ok(rec):
        print(f"TOKEN ACCOUNTING: {problem}", file=sys.stderr)
    if errs:
        print("SCHEMA WARNINGS:", file=sys.stderr)
        for e in errs:
            print("  " + e, file=sys.stderr)


if __name__ == "__main__":
    main()
