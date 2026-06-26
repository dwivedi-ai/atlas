"""
Adapter: Claude Code transcript → normalized event stream.

Claude Code writes JSONL transcripts at:
  ~/.claude/projects/{base64_workspace_hash}/{session_id}.jsonl

Each line is one of:
  {"type": "user",      "message": {"role": "user",      "content": ...}, "timestamp": "...", "uuid": "..."}
  {"type": "assistant", "message": {"role": "assistant", "content": [...], "usage": {...}}, "timestamp": "...", "uuid": "..."}

Tool use appears as content blocks in assistant messages:
  {"type": "tool_use", "id": "toolu_...", "name": "Read", "input": {...}}

Tool results appear as content blocks in user messages following tool_use:
  {"type": "tool_result", "tool_use_id": "toolu_...", "content": "..."}
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NormalizedEvent:
    seq: int
    ts: str
    role: str                        # "user" | "assistant"
    tokens_in: int
    tokens_out: int
    cache_read: int
    cache_write: int
    tools: list[dict]                # list of {name, input, class} extracted from this message
    raw_content: list[dict]
    message_uuid: str = ""


def normalize(raw_lines: list[str]) -> list[NormalizedEvent]:
    """
    Parse raw JSONL lines from a Claude Code transcript.
    Returns a flat list of NormalizedEvent, one per message line.
    """
    events = []
    seq = 0

    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue

        msg_type = obj.get("type")
        if msg_type not in ("user", "assistant"):
            continue

        message = obj.get("message", {})
        role = message.get("role", msg_type)
        usage = message.get("usage") or {}
        content = message.get("content", [])
        timestamp = obj.get("timestamp", "")
        uuid = obj.get("uuid", "")

        # Normalise content to list
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]

        # Extract tool_use blocks
        tools = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tools.append({
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                    "tool_use_id": block.get("id", ""),
                })

        # Claude's usage.input_tokens EXCLUDES cached tokens (cache_read/_creation
        # are reported separately). The experiment's "input" metric is the total
        # prompt the model processed per turn, so fold cache back in. This also
        # keeps total_effective (= total_input - 0.9*cache_read + output) positive,
        # since total_input now always includes cache_read. (L1 ran codex, which
        # had no separate cache, so this never surfaced there.)
        _in = usage.get("input_tokens", 0)
        _cr = usage.get("cache_read_input_tokens", 0)
        _cw = usage.get("cache_creation_input_tokens", 0)
        event = NormalizedEvent(
            seq=seq,
            ts=timestamp,
            role=role,
            tokens_in=_in + _cr + _cw,
            tokens_out=usage.get("output_tokens", 0),
            cache_read=_cr,
            cache_write=_cw,
            tools=tools,
            raw_content=content,
            message_uuid=uuid,
        )
        events.append(event)
        seq += 1

    return events
