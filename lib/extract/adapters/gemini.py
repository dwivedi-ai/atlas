"""
Adapter: Gemini CLI session JSONL -> normalized event stream (verified on v0.49.0).

teardown_run.sh copies the located session file to <run>/transcript.jsonl. Record
shapes (VERIFIED against the installed binary):

  header (line 0):  {"sessionId","projectHash","startTime","lastUpdated","kind"}  # NO "type"
  snapshot:         {"$set": {"messages": [<message>, ...]}}
  appended message: <message>   (bare object, one per line)

  <message> user:   {"id","timestamp","type":"user",
                     "content":[{"text"} | {"functionResponse":{...}}]}
            gemini: {"id","timestamp","type":"gemini","content",
                     "tokens":{"input","output","cached","thoughts","tool","total"},
                     "model","thoughts":[...],
                     "toolCalls":[{"id","name","args","result","status","timestamp",...}]}

CRITICAL: the same message.id is emitted multiple times as it mutates (e.g. once
before toolCalls resolve, once after). Reconstruct final state by upserting on
message.id (last-wins) across BOTH bare appends and $set snapshots, or tokens and
tool calls double-count.

Aggregate token TOTALS are NOT trusted from here (per-message `tokens` may be per-turn
or cumulative — unverified). telemetry.py overrides run_record totals from the
authoritative stdout `-o json` stats. Per-message tokens are kept only to shape the
token_curve / phase split.
"""
from __future__ import annotations
import json

from .claude_code import NormalizedEvent
from . import codex as _codex   # reuse the shell-command classifier for run_shell_command


def _reconstruct(raw_lines: list[str]) -> list[dict]:
    """Replay the event-sourced log -> messages in first-seen order (last-wins merge)."""
    order: list[str] = []
    by_id: dict[str, dict] = {}

    def upsert(msg: dict) -> None:
        mid = msg.get("id")
        if not mid:
            return
        if mid not in by_id:
            by_id[mid] = {}
            order.append(mid)
        by_id[mid].update(msg)          # shallow last-wins merge

    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "$set" in obj:
            for m in (obj.get("$set") or {}).get("messages") or []:
                if isinstance(m, dict):
                    upsert(m)
        elif isinstance(obj, dict) and obj.get("type") in ("user", "gemini") and obj.get("id"):
            upsert(obj)
        # header / anything else: ignore
    return [by_id[mid] for mid in order]


def _tool_entries(tool_calls: list | None) -> list[dict]:
    """Map gemini snake_case tools onto the {name,input} shapes core.classify_tool expects."""
    out: list[dict] = []
    for tc in tool_calls or []:
        name = tc.get("name", "")
        args = tc.get("args", {}) or {}
        tid = tc.get("id", "")
        if name == "read_file":
            out.append({"name": "Read", "input": {"file_path": args.get("file_path", "")}, "tool_use_id": tid})
        elif name == "read_many_files":
            for p in (args.get("paths") or args.get("files") or []):
                out.append({"name": "Read", "input": {"file_path": p}, "tool_use_id": tid})
        elif name == "write_file":
            out.append({"name": "Write", "input": {"file_path": args.get("file_path", "")}, "tool_use_id": tid})
        elif name in ("replace", "edit"):
            out.append({"name": "Edit", "input": {"file_path": args.get("file_path", "")}, "tool_use_id": tid})
        elif name == "run_shell_command":
            cmd = args.get("command", "")
            cls, reads = _codex.classify_command(cmd)   # nav/search/git_op/edit/exec + read_files
            out.append({"name": "Bash",
                        "input": {"command": cmd, "cmd_class": cls, "read_files": reads},
                        "tool_use_id": tid})
        elif name == "search_file_content":
            out.append({"name": "Bash",
                        "input": {"command": "grep " + str(args.get("pattern", "")),
                                  "cmd_class": "search", "read_files": []},
                        "tool_use_id": tid})
        elif name in ("glob", "list_directory"):
            out.append({"name": "Bash",
                        "input": {"command": name, "cmd_class": "search", "read_files": []},
                        "tool_use_id": tid})
        elif name == "google_web_search":
            out.append({"name": "WebSearch", "input": {"query": args.get("query", "")}, "tool_use_id": tid})
        elif name == "web_fetch":
            out.append({"name": "WebFetch", "input": {"url": args.get("url", "")}, "tool_use_id": tid})
        elif name == "save_memory":
            # gemini's key/value store, not the XO memory/ file convention -> count as exec.
            out.append({"name": "Bash",
                        "input": {"command": "save_memory", "cmd_class": "exec", "read_files": []},
                        "tool_use_id": tid})
        else:
            out.append({"name": "Bash",
                        "input": {"command": name, "cmd_class": "meta", "read_files": []},
                        "tool_use_id": tid})
    return out


def normalize(raw_lines: list[str]) -> list[NormalizedEvent]:
    # Gemini's per-message `tokens.input`/`tokens.cached` are CUMULATIVE (the growing
    # prompt across the ReAct loop), while `tokens.output` is per-turn. core.extract()
    # SUMS assistant tokens_in, so feeding cumulative values overcounts wildly. Emit
    # per-turn DELTAS instead: their running sum reconstructs the final cumulative
    # prompt (== the provider's session total) and keeps the token_curve monotonic.
    # When the stdout `-o json` stats are present, telemetry.py still overrides these
    # with the exact provider aggregate; the deltas make the no-stdout fallback (e.g. a
    # quota error or timeout that leaves stdout empty) correct rather than 10x too high.
    events: list[NormalizedEvent] = []
    seq = 0
    prev_prompt = 0      # cumulative input + cached seen so far
    prev_cached = 0      # cumulative cached seen so far
    for m in _reconstruct(raw_lines):
        mtype = m.get("type")
        ts = m.get("timestamp", "")
        if mtype == "user":
            content = m.get("content")
            events.append(NormalizedEvent(
                seq=seq, ts=ts, role="user",
                tokens_in=0, tokens_out=0, cache_read=0, cache_write=0,
                tools=[], raw_content=content if isinstance(content, list) else [],
                message_uuid=m.get("id", ""),
            ))
            seq += 1
        elif mtype == "gemini":
            tok = m.get("tokens", {}) or {}
            cum_prompt = tok.get("input", 0) + tok.get("cached", 0)
            cum_cached = tok.get("cached", 0)
            d_prompt = max(0, cum_prompt - prev_prompt)
            d_cached = max(0, cum_cached - prev_cached)
            prev_prompt, prev_cached = cum_prompt, cum_cached
            events.append(NormalizedEvent(
                seq=seq, ts=ts, role="assistant",
                tokens_in=d_prompt, tokens_out=tok.get("output", 0),
                cache_read=d_cached, cache_write=0,
                tools=_tool_entries(m.get("toolCalls")),
                raw_content=[{"type": "text", "text": m.get("content", "") or ""}],
                message_uuid=m.get("id", ""),
            ))
            seq += 1
    return events
