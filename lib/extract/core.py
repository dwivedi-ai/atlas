#!/usr/bin/env python3
"""
extract_telemetry.py
--------------------
Responsibility: Parse an agent transcript → run_record.json + event_log.jsonl.

Inputs:
  --transcript   path to transcript.jsonl (raw agent session)
  --task-file    path to task definition YAML
  --run-meta     path to run_meta.json (written by setup_run.sh)
  --grade        path to grade.json (written by grade_run.py)
  --output       path to write run_record.json
  --event-log    path to write event_log.jsonl
  --provider     "anthropic" | "openai" (inferred from run_meta.agent_id if absent)

Outputs:
  run_record.json   — canonical telemetry record (validated against schema)
  event_log.jsonl   — one line per message/tool event
"""

from __future__ import annotations
import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


# ── Tool classification ──────────────────────────────────────────────────────

SEARCH_PATTERNS = {"grep", "find", "rg", "ag", "fzf", "cat", "head", "tail", "less"}
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}
NAV_TOOLS = {"Read"}
WEB_TOOLS = {"WebSearch", "WebFetch"}
AGENT_TOOLS = {"Agent"}


def classify_tool(tool_name: str, tool_input: dict) -> str:
    # Codex adapter pre-classifies Bash commands and attaches cmd_class
    if tool_name == "Bash" and "cmd_class" in tool_input:
        return tool_input["cmd_class"]
    if tool_name in EDIT_TOOLS:
        return "edit"
    if tool_name in NAV_TOOLS:
        return "nav"
    if tool_name in WEB_TOOLS:
        return "web"
    if tool_name in AGENT_TOOLS:
        return "agent"
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        first_token = cmd.strip().split()[0] if cmd.strip() else ""
        if first_token == "git":
            return "git_op"
        if first_token in SEARCH_PATTERNS or any(p in cmd for p in ("grep ", "find ", " rg ", " ag ")):
            return "search"
        return "exec"
    return "meta"


def is_edit_tool(tool_name: str, tool_input: dict | None = None) -> bool:
    if tool_name in EDIT_TOOLS:
        return True
    # Codex: Bash commands pre-classified as "edit" by the adapter
    if tool_name == "Bash" and tool_input and tool_input.get("cmd_class") == "edit":
        return True
    return False


def tool_input_summary(tool_name: str, tool_input: dict) -> str:
    if tool_name in ("Read", "Edit", "Write"):
        return tool_input.get("file_path", "")
    if tool_name == "Bash":
        # Codex adapter attaches read_files for nav/search commands
        read_files = tool_input.get("read_files", [])
        if read_files:
            return read_files[0]  # most significant accessed file
        cmd = tool_input.get("command", "")
        return cmd[:80] + ("..." if len(cmd) > 80 else "")
    return json.dumps(tool_input)[:80]


# ── Timestamp parsing ────────────────────────────────────────────────────────

def parse_ts(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(ts_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def ts_delta_seconds(a: str, b: str) -> float | None:
    dt_a, dt_b = parse_ts(a), parse_ts(b)
    if dt_a and dt_b:
        return abs((dt_b - dt_a).total_seconds())
    return None


# ── Shannon entropy ──────────────────────────────────────────────────────────

def shannon_entropy(items: list[str]) -> float | None:
    if not items:
        return None
    counts = Counter(items)
    total = sum(counts.values())
    probs = [c / total for c in counts.values()]
    return -sum(p * math.log2(p) for p in probs if p > 0)


# ── Core extraction ──────────────────────────────────────────────────────────

def extract(
    events: list,           # list of NormalizedEvent from adapter
    run_meta: dict,
    task_def: dict,
    grade: dict,
) -> tuple[dict, list[dict]]:
    """
    Main extraction pass.
    Returns (run_record dict, list of event_log dicts).
    """

    # ── Pass 1: build flat event log ─────────────────────────────────────────
    event_log = []
    cumulative_input = 0
    token_curve = []

    for ev in events:
        # One event-log entry per tool in the message (or one bare if no tools)
        tools_in_msg = ev.tools if ev.tools else [None]
        for tool_entry in tools_in_msg:
            if tool_entry:
                t_name = tool_entry["name"]
                t_input = tool_entry["input"]
                t_class = classify_tool(t_name, t_input)
                t_edit = is_edit_tool(t_name, t_input)
                t_summary = tool_input_summary(t_name, t_input)
            else:
                t_name = None
                t_input = None
                t_class = None
                t_edit = False
                t_summary = None

            cumulative_input += ev.tokens_in if tool_entry == tools_in_msg[0] else 0

            event_log.append({
                "seq": ev.seq,
                "ts": ev.ts,
                "role": ev.role,
                "tokens_in": ev.tokens_in if tool_entry == tools_in_msg[0] else 0,
                "tokens_out": ev.tokens_out if tool_entry == tools_in_msg[0] else 0,
                "cache_read": ev.cache_read if tool_entry == tools_in_msg[0] else 0,
                "cache_write": ev.cache_write if tool_entry == tools_in_msg[0] else 0,
                "tool": t_name,
                "tool_class": t_class,
                "tool_input_summary": t_summary,
                "is_edit": t_edit,
                "cumulative_input_tokens": cumulative_input,
            })

        if ev.role == "assistant":
            token_curve.append([ev.seq, cumulative_input])

    # ── Pass 2: aggregate tokens ─────────────────────────────────────────────
    assistant_events = [e for e in events if e.role == "assistant"]
    total_input = sum(e.tokens_in for e in assistant_events)
    total_output = sum(e.tokens_out for e in assistant_events)
    cache_read = sum(e.cache_read for e in assistant_events)
    cache_write = sum(e.cache_write for e in assistant_events)
    total_effective = total_input - int(cache_read * 0.9) + total_output

    # ── Pass 3: timing ───────────────────────────────────────────────────────
    all_ts = [e.ts for e in events if e.ts]
    first_ts = all_ts[0] if all_ts else None
    last_ts = all_ts[-1] if all_ts else None
    wall_clock = ts_delta_seconds(first_ts, last_ts) if first_ts and last_ts else None

    # First edit event
    first_edit_ev = next(
        (el for el in event_log if el.get("is_edit")), None
    )
    first_edit_ts = first_edit_ev["ts"] if first_edit_ev else None
    ttfua = ts_delta_seconds(first_ts, first_edit_ts) if first_ts and first_edit_ts else None

    # Last edit
    edit_events = [el for el in event_log if el.get("is_edit")]
    last_edit_ts = edit_events[-1]["ts"] if edit_events else first_edit_ts

    # ── Pass 4: phase boundaries ─────────────────────────────────────────────
    first_edit_seq = first_edit_ev["seq"] if first_edit_ev else None
    last_edit_seq = edit_events[-1]["seq"] if edit_events else None

    orient_events = [e for e in events if first_edit_seq is None or e.seq < first_edit_seq]
    impl_events = [e for e in events if first_edit_seq is not None and
                   first_edit_seq <= e.seq <= (last_edit_seq or first_edit_seq)]
    verif_events = [e for e in events if last_edit_seq is not None and e.seq > last_edit_seq]

    def phase_tokens(evs): return sum(e.tokens_in for e in evs if e.role == "assistant")

    # Single-turn fallback: Codex emits one token event after all edits.
    # Reclassify as orientation-only so phase_orientation_input = total_input.
    _orient_tok = phase_tokens(orient_events)
    _impl_tok = phase_tokens(impl_events)
    _verif_tok = phase_tokens(verif_events)
    if _orient_tok == 0 and _impl_tok == 0 and total_input > 0:
        _orient_tok = total_input
        _verif_tok = 0
    def phase_seconds(evs):
        ts_list = [e.ts for e in evs if e.ts]
        return ts_delta_seconds(ts_list[0], ts_list[-1]) if len(ts_list) >= 2 else None

    # Context acquisition = all input tokens before first edit.
    # Codex reports all tokens in a single turn.completed event that arrives
    # AFTER edit events, so the sequence-based sum yields 0. Fall back to
    # total_input in that case (single-turn agents consume all context upfront).
    context_acq = sum(
        el["tokens_in"] for el in event_log
        if (first_edit_ev is None or el["seq"] <= first_edit_ev["seq"])
        and el["tokens_in"] > 0
    )
    if context_acq == 0 and total_input > 0:
        context_acq = total_input

    # ── Pass 5: navigation ───────────────────────────────────────────────────
    # For Claude Code: file reads come via the "Read" tool.
    # For Codex: file reads come via Bash tool_class="nav" (cat/sed/head etc.).
    workspace_prefix = (run_meta.get("workspace_path") or "").rstrip("/") + "/"

    def _is_read(el: dict) -> bool:
        return el.get("tool") == "Read" or el.get("tool_class") == "nav"

    def _rel_path(path: str) -> str:
        """Strip absolute workspace prefix to get repo-relative path."""
        if path and path.startswith(workspace_prefix):
            return path[len(workspace_prefix):]
        return path

    all_reads = [
        _rel_path(el["tool_input_summary"])
        for el in event_log
        if _is_read(el) and el.get("tool_input_summary")
    ]
    pre_edit_reads = [
        _rel_path(el["tool_input_summary"])
        for el in event_log
        if _is_read(el)
        and el.get("tool_input_summary")
        and (first_edit_ev is None or el["seq"] <= first_edit_ev["seq"])
    ]
    files_edited = list(dict.fromkeys(
        _rel_path(el["tool_input_summary"])
        for el in event_log
        if el.get("is_edit") and el.get("tool_input_summary")
    ))

    nav_entropy = shannon_entropy(all_reads)
    nav_entropy_pre_edit = shannon_entropy(pre_edit_reads)

    memory_reads = sum(1 for el in event_log
                       if _is_read(el)
                       and (_rel_path(el.get("tool_input_summary") or "")).startswith("memory/"))
    memory_writes = sum(1 for el in event_log
                        if el.get("is_edit")
                        and (_rel_path(el.get("tool_input_summary") or "")).startswith("memory/"))
    xo_reads = sum(1 for el in event_log
                   if _is_read(el)
                   and (_rel_path(el.get("tool_input_summary") or "")).startswith(".xo/"))

    agents_md_read = any(
        "AGENTS.md" in (_rel_path(el.get("tool_input_summary") or ""))
        for el in event_log if _is_read(el)
    )
    project_md_read = any(
        "PROJECT.md" in (_rel_path(el.get("tool_input_summary") or ""))
        for el in event_log if _is_read(el)
    )

    # ── Pass 6: operations ────────────────────────────────────────────────────
    total_tool_calls = sum(1 for el in event_log if el.get("tool"))
    bash_calls = sum(1 for el in event_log if el.get("tool") == "Bash")
    search_calls = sum(1 for el in event_log if el.get("tool_class") == "search")
    git_calls = sum(1 for el in event_log if el.get("tool_class") == "git_op")
    web_searches = sum(1 for el in event_log if el.get("tool_class") == "web")
    agent_spawns = sum(1 for el in event_log if el.get("tool_class") == "agent")
    replanning = sum(
        1 for el in event_log
        if el.get("is_edit")
        and "PLAN.md" in (el.get("tool_input_summary") or "")
    )

    # ── Assemble run_record ───────────────────────────────────────────────────
    run_record = {
        "schema_version": "1",
        "run": {
            "run_id": run_meta["run_id"],
            "experiment_id": run_meta.get("experiment_id", "unknown"),
            "operator": (run_meta.get("operator") or None),
            "batch_id": run_meta.get("batch_id", None),
            "replication": run_meta.get("replication", 1),
            "timestamp_start": run_meta.get("timestamp_start", first_ts or ""),
            "timestamp_end": run_meta.get("timestamp_end", last_ts or ""),
        },
        "condition": {
            "task_id": run_meta["task_id"],
            "env_id": run_meta["env_id"],
            "agent_id": run_meta["agent_id"],
            "agent_provider": _infer_provider(run_meta["agent_id"]),
            "base_repo_sha": run_meta.get("base_repo_sha", ""),
            "env_overlay_hash": run_meta.get("env_overlay_hash", None),
        },
        "outcome": {
            "terminal_state": grade.get("terminal_state", "error"),
            "score_automated": grade.get("score_automated", None),
            "score_human": grade.get("score_human", None),
            "ac_results": grade.get("ac_results", {}),
        },
        "tokens": {
            "total_input": total_input,
            "total_output": total_output,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "total_effective": total_effective,
            "context_acquisition": context_acq,
            "phase_orientation_input": _orient_tok,
            "phase_implementation_input": _impl_tok,
            "phase_verification_input": _verif_tok,
        },
        "timing": {
            "wall_clock_seconds": wall_clock,
            "time_to_first_edit_seconds": ttfua,
            "phase_orientation_seconds": phase_seconds(orient_events),
            "phase_implementation_seconds": phase_seconds(impl_events),
            "phase_verification_seconds": phase_seconds(verif_events),
        },
        "navigation": {
            "file_reads_total": len(all_reads),
            "file_reads_unique": len(set(all_reads)),
            "nav_entropy": nav_entropy,
            "nav_entropy_pre_edit": nav_entropy_pre_edit,
            "files_read_sequence": all_reads[:200],  # cap for storage
            "files_edited": files_edited,
            "memory_reads": memory_reads,
            "memory_writes": memory_writes,
            "xo_reads": xo_reads,
            "agents_md_read": agents_md_read,
            "project_md_read": project_md_read,
        },
        "operations": {
            "tool_calls_total": total_tool_calls,
            "bash_calls": bash_calls,
            "search_calls": search_calls,
            "git_calls": git_calls,
            "web_searches": web_searches,
            "agent_spawns": agent_spawns,
            "replanning_events": replanning,
            "message_count": len(events),
        },
        "token_curve": token_curve,
        "raw": {
            "transcript_path": run_meta.get("transcript_path", ""),
            "patch_path": run_meta.get("patch_path", ""),
            "grade_path": run_meta.get("grade_path", ""),
        },
    }

    return run_record, event_log


def _infer_provider(agent_id: str) -> str:
    if agent_id.startswith("claude"):
        return "anthropic"
    if agent_id in ("codex", "gpt-4", "gpt-4o"):
        return "openai"
    if agent_id.startswith("gemini"):
        return "google"
    if agent_id.startswith("agy"):
        return "antigravity"
    return "other"


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Extract telemetry from agent transcript.")
    parser.add_argument("--transcript",  required=True, help="Path to transcript.jsonl")
    parser.add_argument("--task-file",   required=True, help="Path to task YAML")
    parser.add_argument("--run-meta",    required=True, help="Path to run_meta.json")
    parser.add_argument("--grade",       required=True, help="Path to grade.json")
    parser.add_argument("--output",      required=True, help="Output run_record.json path")
    parser.add_argument("--event-log",   required=True, help="Output event_log.jsonl path")
    parser.add_argument("--provider",    default=None,  help="Provider override: anthropic|openai")
    args = parser.parse_args()

    # Load inputs
    transcript_path = Path(args.transcript)
    if not transcript_path.exists():
        print(f"ERROR: transcript not found: {transcript_path}", file=sys.stderr)
        sys.exit(1)

    raw_lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()

    with open(args.run_meta) as f:
        run_meta = json.load(f)

    with open(args.task_file) as f:
        task_def = yaml.safe_load(f)

    with open(args.grade) as f:
        grade = json.load(f)

    # Determine provider and select adapter
    provider = args.provider or _infer_provider(run_meta.get("agent_id", ""))
    # Inline import to avoid circular import issues when run as script
    if provider == "anthropic":
        from harness.extract.adapters.claude_code import normalize
    elif provider == "openai":
        from harness.extract.adapters.codex import normalize
    else:
        print(f"ERROR: unknown provider: {provider}", file=sys.stderr)
        sys.exit(1)

    events = normalize(raw_lines)
    if not events:
        print("WARN: no events extracted from transcript", file=sys.stderr)

    run_record, event_log_entries = extract(events, run_meta, task_def, grade)

    # Patch raw paths into run_record
    run_dir = Path(args.output).parent
    run_record["raw"]["transcript_path"] = str(transcript_path)
    run_record["raw"]["patch_path"] = str(run_dir / "git.patch")
    run_record["raw"]["grade_path"] = str(run_dir / "grade.json")

    # Write outputs
    Path(args.output).write_text(json.dumps(run_record, indent=2))
    with open(args.event_log, "w") as f:
        for entry in event_log_entries:
            f.write(json.dumps(entry) + "\n")

    ttfua_val = run_record["timing"]["time_to_first_edit_seconds"]
    ttfua_str = f"{ttfua_val:.1f}s" if ttfua_val is not None else "None"
    print(f"run_record  → {args.output}")
    print(f"event_log   → {args.event_log}")
    print(f"total_input={run_record['tokens']['total_input']}  "
          f"TTFUA={ttfua_str}  "
          f"score={run_record['outcome']['score_automated']}")


if __name__ == "__main__":
    # Allow running as a script from experiments/ root
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    main()
