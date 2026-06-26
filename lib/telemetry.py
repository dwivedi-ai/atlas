#!/usr/bin/env python3
"""
telemetry.py — exp-runner wrapper around the ported telemetry core.

Reads a finished run dir (transcript.jsonl, run_meta.json, judge.json), runs the
ported extractor (lib/extract/core.py + adapters), and writes:
  run_record.json   — canonical telemetry, validated against schemas/run_record.schema.json
  event_log.jsonl   — one line per message/tool event

Generalization vs the experiment: there is no task YAML or env condition, so
task_id := job_id, env_id := "asis", and we surface repo_url + the judge verdict.

Usage:
  python3 telemetry.py --run-dir jobs/<id>/runs/<run-id>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent
ROOT = LIB.parent
sys.path.insert(0, str(LIB))

from extract.core import extract, _infer_provider  # noqa: E402
from extract.adapters import claude_code, codex      # noqa: E402

SCHEMA_PATH = ROOT / "schemas" / "run_record.schema.json"


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
                "passed": c.get("met"),
                "output": (c.get("evidence") or "")[:500],
            }
    return {
        "terminal_state": terminal,
        "score_automated": (judge or {}).get("score"),
        "score_human": None,
        "ac_results": ac_results,
    }


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
        "env_overlay_hash": None,
    }

    provider = _infer_provider(agent_id)
    raw = (run_dir / "transcript.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    if provider == "anthropic":
        events = claude_code.normalize(raw)
    elif provider == "openai":
        events = codex.normalize(raw)
    else:
        events = []

    run_record, event_log = extract(events, enriched, {}, grade)

    # Generalized fields the experiment record didn't carry.
    run_record["condition"]["repo_url"] = meta.get("repo_url") or meta.get("repo_path")
    if judge:
        run_record["outcome"]["verdict"] = judge.get("verdict")
    run_record["raw"]["transcript_path"] = str(run_dir / "transcript.jsonl")
    run_record["raw"]["patch_path"] = str(run_dir / "git.patch")
    run_record["raw"]["grade_path"] = str(run_dir / "judge.json")

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
    print(f"run_record → {run_dir/'run_record.json'}  "
          f"input={t['total_input']} output={t['total_output']} "
          f"verdict={rec['outcome'].get('verdict')} score={rec['outcome']['score_automated']}")
    if errs:
        print("SCHEMA WARNINGS:", file=sys.stderr)
        for e in errs:
            print("  " + e, file=sys.stderr)


if __name__ == "__main__":
    main()
