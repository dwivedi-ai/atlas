"""
Adapter: Claude Code transcript / stream → normalized event stream.

RESPONSIBILITY
  Turn the raw JSONL a Claude Code session produces into a flat list of
  NormalizedEvent, with token usage attributed **once per assistant message**
  rather than once per transcript line.

INPUTS
  raw_lines : list[str]  — the lines of either
                             ~/.claude/projects/<slug>/<session_id>.jsonl   (on disk), or
                             stream.jsonl                                    (verbatim child stdout)
                           Both split one assistant message across several lines, and both
                           are handled by the same code path.

OUTPUTS
  list[NormalizedEvent]  — one per accepted line, in file order.
  terminal_result(raw_lines) -> dict | None  — the authoritative run totals from the terminal
                           `result` event (stream only; the on-disk transcript has none).

THE DEFECT THIS FILE EXISTS TO FIX
  Claude Code writes ONE LINE PER CONTENT BLOCK and repeats a BYTE-IDENTICAL `message.usage`
  object on every one of them. `extract/core.py` summed per line, so a message with 6 content
  blocks contributed its 32,356 input tokens six times. Measured over 116 transcripts: input
  inflation median 1.50x, pooled 2.09x, max 4.90x; output median 1.94x, max 8.72x.
  `message.id` is present on 100% of assistant lines and is the same across the split, so the
  fix is: attribute usage to the FIRST line carrying a given `message.id` and zero it on the
  rest. Deduped totals were measured to equal the terminal `result.usage` totals EXACTLY
  (53,292 == 53,292), which is why core.py also asserts that equality when a result event
  is available.

  Every token figure the existing context-ladder experiment produced is inflated by a
  run-varying factor. `tokens.accounting_version` distinguishes the two generations so
  pre-fix and post-fix records can never be pooled by accident.

STREAM vs TRANSCRIPT — MEASURED HERE, NOT IN 
  The two files are NOT interchangeable for tokens, and the difference is silent.
  `usage` is byte-identical across the lines of one message in BOTH, so dedupe works on
  both. But in stream.jsonl `message.usage.output_tokens` is a STREAMING PLACEHOLDER fixed
  at message start — observed values 1, 2, 3 for messages whose real output was 1,405 /
  2,101 / 3,099 tokens. `input_tokens` + the two cache counters are correct in both.
  Measured over 39 stream.jsonl files with a terminal `result` event: deduped INPUT from
  the stream equals `result.usage` in 27/29, while deduped OUTPUT from the stream is off by
  two to three orders of magnitude in 29/29.
  Take events from transcript.jsonl and totals from the stream's `result` event, and both
  match exactly: 10/12 sessions identical on input AND output. The 2 misses are sessions
  whose on-disk transcript accumulated MORE than one run (a resumed session id) — which is
  the failure mode "copy the transcript BY --session-id, never by newest mtime" exists to
  prevent, and it shows up as a non-zero `tokens.dedupe_delta_*` rather than silently.

LINE SHAPES
  {"type":"user",      "message":{"role":"user","content":...},        "timestamp":..., "uuid":...}
  {"type":"assistant", "message":{"id":"msg_...","role":"assistant",
                                  "content":[...], "usage":{...}},     "timestamp":..., "uuid":...}
  {"type":"attachment","attachment":{"type":...}}                       (aux)
  {"type":"queued_command", ...}                                        (aux)
  {"type":"system","subtype":"init", ...}                               (aux)
  {"type":"result","usage":{...},"total_cost_usd":...}                  (stream only)

  Tool use  : assistant content block {"type":"tool_use","id":"toolu_...","name":..,"input":{..}}
  Tool result: user content block     {"type":"tool_result","tool_use_id":"toolu_...", ...}
  Truncation: on disk the sidecar is camelCase `toolUseResult`; the stream uses snake_case
              `tool_use_result`. They are NOT interchangeable, so both spellings are read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Types carried through when include_aux=True. Kept OUT of the default path so the
# context-ladder's message_count / event_log stay exactly what they were.
AUX_TYPES = {"attachment", "queued_command", "system"}

# type -> the NormalizedEvent.role used for aux lines.
AUX_ROLE = {
    "attachment": "probe_in",
    "queued_command": "probe_in",
    "system": "system",
}


@dataclass
class NormalizedEvent:
    seq: int
    ts: str
    role: str                        # "user" | "assistant" | "probe_in" | "system"
    tokens_in: int
    tokens_out: int
    cache_read: int
    cache_write: int
    tools: list[dict]                # {name, input, tool_use_id} per tool_use block
    raw_content: list[dict]
    message_uuid: str = ""           # the PER-LINE uuid — unique on every line, never a message key
    # ── V7 ────────────────────────────────────────────────────────────────────
    message_id: str = ""             # message.id — SHARED by every line of one assistant message
    usage_duplicate: bool = False    # this line repeated an already-counted usage object; its
                                     # token fields have been zeroed. Nothing is lost: the first
                                     # line of the message carries the whole message's usage.
    # ── V16 / V6 / V10 ────────────────────────────────────────────────────────
    raw_type: str = ""               # the line's own "type", before role normalization
    is_sidechain: bool = False       # transcript isSidechain — a subagent turn (impossible under
                                     # --tools Bash,Read,Write,Edit,Glob,Grep, asserted not assumed)
    is_replay: bool = False          # stream `isReplay` — a message the DRIVER injected on stdin
                                     # and the CLI echoed back.'s harness_probe /
                                     # harness_resume channels are `user` + isReplay, asserted
                                     # nonce-free, and never count toward `read`.
    tool_results: list[dict] = field(default_factory=list)
    system_reminders: list[str] = field(default_factory=list)


def _content_list(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _tool_use_result(obj: dict) -> Any:
    """The truncation sidecar. camelCase on disk, snake_case in the stream — read both."""
    if "toolUseResult" in obj:
        return obj["toolUseResult"]
    return obj.get("tool_use_result")


def _harvest_tool_results(obj: dict, content: list[dict]) -> list[dict]:
    """One entry per tool_result block, carrying the truncation facts needs.

    `file.numLines < file.totalLines` is the SOLE Read-truncation signal, and it exists only in
    the on-disk transcript — stream.jsonl carries no truncation information at all.
    `file.truncated` is None on every read, truncated or not, so it is deliberately NOT used as
    a trigger; it is carried verbatim for the record and nothing else.
    """
    sidecar = _tool_use_result(obj)
    out: list[dict] = []
    for block in content:
        if block.get("type") != "tool_result":
            continue
        body = block.get("content")
        if isinstance(body, list):
            text = "".join(b.get("text", "") for b in body if isinstance(b, dict))
        else:
            text = body if isinstance(body, str) else ""
        entry: dict[str, Any] = {
            "tool_use_id": block.get("tool_use_id", ""),
            "is_error": bool(block.get("is_error") or block.get("isError")),
            "bytes": len(text),
            "num_lines": None,
            "total_lines": None,
            "truncated_field": None,     # V16: None on EVERY read. Not a trigger.
            "num_lines_lt_total": False, # the only Read-truncation signal there is
            "persisted_output_path": None,
        }
        if isinstance(sidecar, dict):
            f = sidecar.get("file")
            if isinstance(f, dict):
                entry["num_lines"] = f.get("numLines", f.get("num_lines"))
                entry["total_lines"] = f.get("totalLines", f.get("total_lines"))
                entry["truncated_field"] = f.get("truncated")
                if isinstance(entry["num_lines"], int) and isinstance(entry["total_lines"], int):
                    entry["num_lines_lt_total"] = entry["num_lines"] < entry["total_lines"]
            entry["persisted_output_path"] = (
                sidecar.get("persistedOutputPath") or sidecar.get("persisted_output_path")
            )
        out.append(entry)
    return out


def _system_reminders(content: list[dict]) -> list[str]:
    """<system-reminder> blocks are model-visible but are NOT an exposure channel ( —
    audit only). Surfaced so regions.py can classify them instead of hitting unknown_visible."""
    out = []
    for block in content:
        if block.get("type") != "text":
            continue
        text = block.get("text") or ""
        if "<system-reminder>" in text:
            out.append(text)
    return out


def normalize(raw_lines: list[str], *, include_aux: bool = False) -> list[NormalizedEvent]:
    """Parse raw Claude Code JSONL. One NormalizedEvent per accepted line, in file order.

    include_aux=False (default) accepts only `user` / `assistant` lines — byte-for-byte the
    set of lines the context-ladder extractor has always seen, so `message_count`,
    `event_log.jsonl` and the phase split are unchanged by this module's V7 fix.
    include_aux=True additionally emits `attachment` / `queued_command` (role `probe_in`,
    the stdin-injected probe channel) and `system` (role `system`, carries system/init).

    Usage is attributed to the first line of each `message.id` and zeroed on the rest.
    A line with no message.id (every user line, and any pre-V7 fixture) is never deduped.
    """
    events: list[NormalizedEvent] = []
    seq = 0
    seen_usage: set[str] = set()

    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue

        msg_type = obj.get("type")
        if msg_type in ("user", "assistant"):
            pass
        elif include_aux and msg_type in AUX_TYPES:
            pass
        else:
            continue

        message = obj.get("message") or {}
        if not isinstance(message, dict):
            message = {}
        role = message.get("role") or AUX_ROLE.get(msg_type, msg_type)
        usage = message.get("usage") or {}
        content = _content_list(message.get("content", []))
        timestamp = obj.get("timestamp", "")
        uuid = obj.get("uuid", "")
        message_id = message.get("id") or ""

        tools = []
        for block in content:
            if block.get("type") == "tool_use":
                tools.append({
                    "name": block.get("name", ""),
                    "input": block.get("input", {}) or {},
                    "tool_use_id": block.get("id", ""),
                })

        # ── V7: one usage per message.id, not per line ────────────────────────
        duplicate = bool(message_id) and message_id in seen_usage
        if message_id:
            seen_usage.add(message_id)

        # Claude's usage.input_tokens EXCLUDES cached tokens (cache_read/_creation are
        # reported separately). The experiment's "input" metric is the total prompt the model
        # processed per turn, so fold cache back in. This also keeps total_effective
        # (= total_input - 0.9*cache_read + output) positive, since total_input now always
        # includes cache_read. (L1 ran codex, which had no separate cache.)
        _in = usage.get("input_tokens", 0) or 0
        _cr = usage.get("cache_read_input_tokens", 0) or 0
        _cw = usage.get("cache_creation_input_tokens", 0) or 0
        _out = usage.get("output_tokens", 0) or 0
        if duplicate:
            _in = _cr = _cw = _out = 0

        events.append(NormalizedEvent(
            seq=seq,
            ts=timestamp,
            role=role,
            tokens_in=_in + _cr + _cw,
            tokens_out=_out,
            cache_read=_cr,
            cache_write=_cw,
            tools=tools,
            raw_content=content,
            message_uuid=uuid,
            message_id=message_id,
            usage_duplicate=duplicate,
            raw_type=msg_type or "",
            is_sidechain=bool(obj.get("isSidechain") or obj.get("is_sidechain")),
            is_replay=bool(obj.get("isReplay") or obj.get("is_replay")),
            tool_results=_harvest_tool_results(obj, content),
            system_reminders=_system_reminders(content),
        ))
        seq += 1

    return events


def terminal_result(raw_lines: list[str]) -> dict | None:
    """The last `type == "result"` event's authoritative run totals, or None.

    Present in stream.jsonl (the verbatim child stdout); ABSENT from the on-disk transcript.
    This is ground truth: deduped-by-message.id sums were measured to equal it exactly, so
    core.extract() records the delta and any non-zero value means the V7 fix regressed.
    """
    found = None
    for raw in raw_lines:
        raw = raw.strip()
        if not raw or '"result"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "result":
            continue
        usage = obj.get("usage") or {}
        _in = usage.get("input_tokens", 0) or 0
        _cr = usage.get("cache_read_input_tokens", 0) or 0
        _cw = usage.get("cache_creation_input_tokens", 0) or 0
        denials = obj.get("permission_denials")
        found = {
            "total_input": _in + _cr + _cw,
            "total_output": usage.get("output_tokens", 0) or 0,
            "cache_read": _cr,
            "cache_write": _cw,
            "cost_usd": obj.get("total_cost_usd"),
            "num_turns": obj.get("num_turns"),
            "permission_denials": len(denials) if isinstance(denials, list) else None,
            "is_error": bool(obj.get("is_error")),
            "subtype": obj.get("subtype"),
            "duration_ms": obj.get("duration_ms"),
        }
    return found
