"""
Adapter: agy (Antigravity CLI) transcript_full.jsonl -> normalized event stream.

teardown_run.sh copies the located `transcript_full.jsonl` to <run>/transcript.jsonl and the
conversation `.db` to <run>/agy_conversation.db. This adapter builds the event stream from the
JSONL; TOKENS come from the DB (agy stores none in the transcript) and are overlaid by
telemetry.py via lib/agy.extract_tokens. See AGY_DOCS.md,,

Event model: a linear step stream. Tool INTENTS live in `PLANNER_RESPONSE.tool_calls[]={name,args}`
(one model turn); outcome events (RUN_COMMAND/VIEW_FILE/CODE_ACTION/…) are their results and are
redundant for the taxonomy, so we derive tools from the PLANNER_RESPONSE turns only.
"""
from __future__ import annotations

import json

from .claude_code import NormalizedEvent
from . import codex as _codex   # reuse the shell-command classifier for run_command


def _tool_entry(name: str, args: dict) -> dict:
    """Map an agy tool_call onto the {name,input} shape core.classify_tool expects."""
    a = args or {}
    if name in ("view_file", "view_code_item"):
        return {"name": "Read", "input": {"file_path": a.get("AbsolutePath", "")}}
    if name == "list_dir":
        return {"name": "Read", "input": {"file_path": a.get("DirectoryPath", "")}}
    if name in ("write_to_file", "create_file", "notebook_edit"):
        return {"name": "Write", "input": {"file_path": a.get("TargetFile", "")}}
    if name in ("replace_file_content", "multi_replace_file_content"):
        return {"name": "Edit", "input": {"file_path": a.get("TargetFile", "")}}
    if name == "run_command":
        cmd = a.get("CommandLine", "")
        cls, reads = _codex.classify_command(cmd)
        return {"name": "Bash", "input": {"command": cmd, "cmd_class": cls, "read_files": reads}}
    if name in ("grep_search", "codebase_search", "find_by_name"):
        tgt = a.get("SearchPath") or a.get("SearchDirectory") or ""
        q = a.get("Query") or a.get("Pattern") or ""
        return {"name": "Bash", "input": {"command": f"grep {q}", "cmd_class": "search",
                                          "read_files": [tgt] if tgt else []}}
    if name in ("search_web", "google_web_search"):
        return {"name": "WebSearch", "input": {"query": a.get("Query", "")}}
    if name in ("read_url_content", "open_browser_url", "read_browser_page") or name.startswith("Browser"):
        return {"name": "WebFetch", "input": {"url": a.get("Url", "")}}
    if name in ("invoke_subagent", "define_subagent", "manage_subagents"):
        return {"name": "Agent", "input": {"description": a.get("Role", "subagent")}}
    if name in ("KnowledgeWriteToFile", "KnowledgeReplaceFileContent", "DeleteKnowledge"):
        # agy's KB memory tools -> count as a write under memory/
        return {"name": "Edit", "input": {"file_path": "memory/" + str(a.get("Path") or "knowledge")}}
    # ask_permission / manage_task / command_status -> control/meta
    return {"name": "Bash", "input": {"command": name, "cmd_class": "meta", "read_files": []}}


def normalize(raw_lines: list[str]) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []
    seq = 0
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        typ = d.get("type")
        ts = d.get("created_at", "")
        if typ == "USER_INPUT":
            events.append(NormalizedEvent(
                seq=seq, ts=ts, role="user",
                tokens_in=0, tokens_out=0, cache_read=0, cache_write=0,
                tools=[], raw_content=[{"type": "text", "text": d.get("content", "") or ""}],
                message_uuid=str(d.get("step_index", "")),
            ))
            seq += 1
        elif typ == "PLANNER_RESPONSE":
            tools = [_tool_entry(tc.get("name", ""), tc.get("args", {}))
                     for tc in (d.get("tool_calls") or [])]
            events.append(NormalizedEvent(
                seq=seq, ts=ts, role="assistant",
                tokens_in=0, tokens_out=0, cache_read=0, cache_write=0,
                tools=tools, raw_content=[{"type": "text", "text": d.get("content", "") or ""}],
                message_uuid=str(d.get("step_index", "")),
            ))
            seq += 1
        # RUN_COMMAND / VIEW_FILE / SYSTEM_MESSAGE / CHECKPOINT / … are outcomes/meta: skipped
        # (the tool intents are already captured from PLANNER_RESPONSE.tool_calls).
    return events
