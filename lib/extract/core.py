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

TOKEN ACCOUNTING (V7 — read this before comparing any number to a historical one)
  This module used to sum `usage` per transcript LINE. Claude Code writes one line per content
  block with a byte-identical `usage`, so a 6-block message was counted six times: input
  inflated 1.00x-4.90x (median 1.50x, pooled 2.09x) and output 1.00x-8.72x (median 1.94x) over
  116 transcripts. Usage is now attributed once per `message.id` — in the adapter, which zeroes
  the duplicate lines, and again defensively here, which is idempotent.
  EVERY TOKEN FIGURE THE EXISTING CONTEXT-LADDER EXPERIMENT PRODUCED IS INFLATED by a
  run-varying factor. `tokens.accounting_version` ("per_line_v1" vs "per_message_v2") is
  stamped on every record so the two generations can never be pooled by accident.
  `turns_total` (distinct assistant message.id) is the honest turn count; `message_count`
  remains the LINE count and was measured at up to 13.67x turns_total (V17).
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

# NOTE: `yaml` is imported lazily inside main() only. The library path
# (telemetry.py -> extract.core.extract) must import cleanly on a stock python3,
# which has neither yaml nor jsonschema until install.sh has run.

ACCOUNTING_VERSION = "per_message_v2"


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

def _dedupe_usage(events: list) -> list:
    """V7: keep the first event per `message.id`, drop the rest, for TOKEN PURPOSES ONLY.

    The claude_code adapter already zeroes duplicate lines' usage, so this is idempotent
    there; it is the authoritative rule for any caller that builds NormalizedEvents another
    way. Events with no `message_id` (every user line, and codex/gemini/agy, which have no
    such concept) are never deduped — they are all kept.
    """
    seen: set[str] = set()
    out = []
    for e in events:
        mid = getattr(e, "message_id", "") or ""
        if mid:
            if mid in seen:
                continue
            seen.add(mid)
        out.append(e)
    return out


def _probe_summary(probe_events: list[dict] | None) -> dict:
    """Fold the WUR probe rows into the operations counters.

    `probe_events` is an OPTIONAL, KEYWORD-ONLY list of dicts — one per probe interaction,
    normally `probes.jsonl` as produced by lib/wur/probes.py. Only these keys are read, and
    every one of them is optional:

      outcome           "answered" | "superseded" | "unanswered" | "refused"
      kind              "probe" (default) | "resume"  — resumes are counted separately
      turn_message_ids  [str, ...]  assistant message.ids that ARE probe turns
      tool_use_ids      [str, ...]  tool calls issued inside a probe turn

    The two id lists are what makes `tool_calls_task` (§4.4's difficulty-band metric)
    computable: tool_calls_total INCLUDES probe turns, tool_calls_task excludes them.
    Passing None leaves every probe counter null and tool_calls_task == tool_calls_total,
    which is the correct answer for a ladder run and for the no-probe arms.
    """
    if probe_events is None:
        return {"present": False, "sent": None, "answered": None, "resumes": None,
                "message_ids": set(), "tool_use_ids": set()}
    sent = answered = resumes = 0
    message_ids: set[str] = set()
    tool_use_ids: set[str] = set()
    for p in probe_events:
        if not isinstance(p, dict):
            continue
        if (p.get("kind") or "probe") == "resume":
            resumes += 1
        else:
            sent += 1
            if p.get("outcome") == "answered":
                answered += 1
        for mid in p.get("turn_message_ids") or []:
            if mid:
                message_ids.add(mid)
        for tid in p.get("tool_use_ids") or []:
            if tid:
                tool_use_ids.add(tid)
    return {"present": True, "sent": sent, "answered": answered, "resumes": resumes,
            "message_ids": message_ids, "tool_use_ids": tool_use_ids}


def extract(
    events: list,           # list of NormalizedEvent from adapter
    run_meta: dict,
    task_def: dict,
    grade: dict,
    *,
    probe_events: list[dict] | None = None,
    result_totals: dict | None = None,
) -> tuple[dict, list[dict]]:
    """
    Main extraction pass.
    Returns (run_record dict, list of event_log dicts).

    The two extras are KEYWORD-ONLY so the existing positional call
    `extract(events, enriched, {}, grade)` (telemetry.py) keeps working unchanged for the
    codex / gemini / antigravity adapters, none of which have probes or a `result` event.

      probe_events   see _probe_summary(); WUR only.
      result_totals  claude_code.terminal_result(stream_lines) — the authoritative run totals
                     from the terminal `result` event. When present it OVERRIDES the summed
                     totals, and the deduped-minus-result delta is recorded: the two were
                     measured to be exactly equal, so a non-zero delta means the V7 fix
                     regressed (§10's free correctness gate).
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

    # ── Pass 2: aggregate tokens — ONCE PER MESSAGE, never once per line (V7) ─
    tok_events = _dedupe_usage(events)
    assistant_events = [e for e in tok_events if e.role == "assistant"]
    deduped_input = sum(e.tokens_in for e in assistant_events)
    deduped_output = sum(e.tokens_out for e in assistant_events)
    cache_read = sum(e.cache_read for e in assistant_events)
    cache_write = sum(e.cache_write for e in assistant_events)

    total_input, total_output = deduped_input, deduped_output
    result_input = result_output = None
    dedupe_delta_input = dedupe_delta_output = None
    cost_usd = None
    if result_totals:
        result_input = result_totals.get("total_input")
        result_output = result_totals.get("total_output")
        cost_usd = result_totals.get("cost_usd")
        if isinstance(result_input, int):
            dedupe_delta_input = deduped_input - result_input
            total_input = result_input
        if isinstance(result_output, int):
            dedupe_delta_output = deduped_output - result_output
            total_output = result_output
        if isinstance(result_totals.get("cache_read"), int):
            cache_read = result_totals["cache_read"]
        if isinstance(result_totals.get("cache_write"), int):
            cache_write = result_totals["cache_write"]
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

    # Phase sums must also be per-message, not per-line (V7) — hence tok_events.
    orient_events = [e for e in tok_events if first_edit_seq is None or e.seq < first_edit_seq]
    impl_events = [e for e in tok_events if first_edit_seq is not None and
                   first_edit_seq <= e.seq <= (last_edit_seq or first_edit_seq)]
    verif_events = [e for e in tok_events if last_edit_seq is not None and e.seq > last_edit_seq]

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
    # turns_total is ASSISTANT-ONLY: user lines carry no message.id, so "distinct message id
    # count" is not "all messages" (V17). message_count below stays the LINE count, which was
    # measured at up to 13.67x this number — anything that means "turns" must read turns_total.
    assistant_message_ids = [getattr(e, "message_id", "") or "" for e in events
                             if e.role == "assistant"]
    distinct_ids = {m for m in assistant_message_ids if m}
    turns_total = len(distinct_ids) if distinct_ids else (
        len([e for e in events if e.role == "assistant"]) or None
    )

    # The §10 pacing gate: max tool_use blocks per assistant MESSAGE. Counting per stream
    # line would inherit the V7 bug — one message is split across lines and each line would
    # look like a 1-tool-call message (V17). Group by message.id first.
    per_message_tool_uses: Counter = Counter()
    for e in events:
        if e.role != "assistant":
            continue
        key = getattr(e, "message_id", "") or f"__line_{e.seq}"
        per_message_tool_uses[key] += len(e.tools or [])
    max_tool_uses_per_message = max(per_message_tool_uses.values(), default=0)

    probes = _probe_summary(probe_events)

    total_tool_calls = sum(1 for el in event_log if el.get("tool"))
    # tool_calls_task EXCLUDES probe turns — the difficulty-band metric (§4.4). Without probe
    # rows there is nothing to exclude, which is the right answer for ladder and no-probe arms.
    if probes["present"] and (probes["message_ids"] or probes["tool_use_ids"]):
        probe_tool_calls = 0
        for e in events:
            if e.role != "assistant":
                continue
            mid = getattr(e, "message_id", "") or ""
            for t in e.tools or []:
                if (mid and mid in probes["message_ids"]) or \
                   (t.get("tool_use_id") and t["tool_use_id"] in probes["tool_use_ids"]):
                    probe_tool_calls += 1
        tool_calls_task = max(0, total_tool_calls - probe_tool_calls)
    else:
        tool_calls_task = total_tool_calls

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
    # schema_version "2": every record now carries tokens.accounting_version, which is a v2
    # key. A record still on disk with "1" (and therefore no accounting_version) is
    # per_line_v1 by construction and must not be pooled with these.
    run_record = {
        "schema_version": "2",
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
            # V7. Never pool a per_line_v1 record with a per_message_v2 one.
            "accounting_version": ACCOUNTING_VERSION,
            "result_input": result_input,
            "result_output": result_output,
            "dedupe_delta_input": dedupe_delta_input,
            "dedupe_delta_output": dedupe_delta_output,
            "cost_usd": cost_usd,
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
            "message_count": len(events),          # LINES, not messages — see turns_total
            "turns_total": turns_total,
            "tool_calls_task": tool_calls_task,
            "probes_sent": probes["sent"],
            "probes_answered": probes["answered"],
            "resumes_sent": probes["resumes"],
            "permission_denials": (result_totals or {}).get("permission_denials"),
            "max_tool_uses_per_message": max_tool_uses_per_message,
        },
        "token_curve": token_curve,
        "raw": {
            "transcript_path": run_meta.get("transcript_path", ""),
            "patch_path": run_meta.get("patch_path", ""),
            "grade_path": run_meta.get("grade_path", ""),
        },
    }

    return run_record, event_log


def token_accounting_ok(run_record: dict) -> list[str]:
    """The free correctness check §10 makes a pilot gate. [] means clean.

    Dedupe-by-message.id was measured to equal the terminal `result.usage` totals EXACTLY
    (53,292 == 53,292), so any non-zero delta means the V7 fix regressed. Returns problems
    rather than raising: telemetry must still write a record for a run whose accounting drifted,
    or the evidence disappears with the failure.
    """
    problems = []
    tok = run_record.get("tokens") or {}
    if tok.get("accounting_version") != ACCOUNTING_VERSION:
        problems.append(
            f"tokens.accounting_version={tok.get('accounting_version')!r} "
            f"(expected {ACCOUNTING_VERSION!r}) — this record must not be pooled with post-V7 rows")
    for k in ("dedupe_delta_input", "dedupe_delta_output"):
        d = tok.get(k)
        if isinstance(d, int) and d != 0:
            problems.append(f"tokens.{k}={d} — deduped sum != terminal result.usage; V7 fix regressed")
    return problems


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

    import yaml  # lazy: the library path must import on a python3 without yaml installed
    with open(args.task_file) as f:
        task_def = yaml.safe_load(f)

    with open(args.grade) as f:
        grade = json.load(f)

    # Determine provider and select adapter
    provider = args.provider or _infer_provider(run_meta.get("agent_id", ""))
    # Inline import to avoid circular import issues when run as script
    result_totals = None
    if provider == "anthropic":
        from harness.extract.adapters.claude_code import normalize, terminal_result
        result_totals = terminal_result(raw_lines)
    elif provider == "openai":
        from harness.extract.adapters.codex import normalize
    else:
        print(f"ERROR: unknown provider: {provider}", file=sys.stderr)
        sys.exit(1)

    events = normalize(raw_lines)
    if not events:
        print("WARN: no events extracted from transcript", file=sys.stderr)

    run_record, event_log_entries = extract(events, run_meta, task_def, grade,
                                            result_totals=result_totals)
    for p in token_accounting_ok(run_record):
        print(f"WARN: {p}", file=sys.stderr)

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
