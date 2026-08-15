"""
Adapter: Codex JSONL event stream → normalized event stream.

Codex `exec --json` writes newline-delimited JSON events to stdout.
The harness captures this stdout directly as transcript.jsonl.

Observed event types (codex-cli 0.139.0):

  {"type":"thread.started","thread_id":"..."}
  {"type":"turn.started"}
  {"type":"item.started","item":{"id":"...","type":"command_execution","command":"...","aggregated_output":"","exit_code":null,"status":"in_progress"}}
  {"type":"item.completed","item":{"id":"...","type":"command_execution","command":"...","aggregated_output":"...","exit_code":0,"status":"completed"}}
  {"type":"item.completed","item":{"id":"...","type":"agent_message","text":"..."}}
  {"type":"turn.completed","usage":{"input_tokens":N,"cached_input_tokens":N,"output_tokens":N,"reasoning_output_tokens":N}}

Key differences from Claude Code:
- All file access and shell operations come through command_execution items (no separate Read/Edit tools)
- Token usage is per-turn (in turn.completed), not per-message
- File writes are detected by parsing shell command patterns
- Navigation (file reads) is detected by command name (cat, head, grep, etc.)
"""

from __future__ import annotations
import json
import re
import shlex

from .claude_code import NormalizedEvent


# ── Shell command classifiers ────────────────────────────────────────────────


NAV_COMMANDS = {"cat", "head", "tail", "less", "more", "bat", "view", "open", "sed", "awk", "wc"}
SEARCH_COMMANDS = {"grep", "rg", "ag", "ack", "find", "fd", "fzf"}
GIT_COMMANDS = {"git"}
WRITE_COMMANDS = {"tee", "touch", "mkdir", "cp", "mv", "install"}

# Patterns that indicate file writes via shell redirection or common write operations
WRITE_PATTERNS = [
    r"\s+>{1,2}\s+\S",          # redirect > or >>
    r"^\s*echo\s+.+\s+>\s+",    # echo > file
    r"\btee\b",                  # tee command
    r"\bsed\s+-i\b",             # sed in-place
    r"\bpython3?\s+-c\b.*\.write\(", # python write
    r"\bpython3?\s+-c\b.*open\(.+['\"]w",  # python open write
    r"\btouch\b\s+\S",          # touch creates file
    r"\bmkdir\b",               # mkdir
    r"\bmv\b\s+\S+\s+\S+",      # mv (rename/move = write)
    r"\bcp\b\s+\S+\s+\S+",      # cp (copy = write to dest)
    r"\binstall\b",             # install command
]

WRITE_RE = re.compile("|".join(WRITE_PATTERNS), re.IGNORECASE)


def _unwrap_bash(cmd: str) -> str:
    """
    Codex wraps all commands as: /bin/bash -lc '<inner_command>'
    Unwrap to get the actual command the agent intended.
    """
    cmd = cmd.strip()
    # Pattern: /bin/bash -lc '...' or bash -c '...'
    import re
    m = re.match(r"(?:/bin/bash|bash)\s+-\w*c\s+'(.+)'$", cmd, re.DOTALL)
    if m:
        return m.group(1)
    m = re.match(r'(?:/bin/bash|bash)\s+-\w*c\s+"(.+)"$', cmd, re.DOTALL)
    if m:
        return m.group(1)
    return cmd


def _basename(cmd: str) -> str:
    """Get the bare command name (no path prefix)."""
    try:
        parts = shlex.split(cmd.strip())
        if parts:
            return parts[0].split("/")[-1]
    except ValueError:
        pass
    return ""


def _extract_read_files(command: str) -> list[str]:
    """
    Extract file paths from a read-type shell command (cat, head, grep, etc.).
    Returns list of paths (best-effort, may miss complex cases).
    """
    files = []
    try:
        parts = shlex.split(command)
    except ValueError:
        return []
    # Skip flags (-n, --color, etc.) and the command name itself
    for p in parts[1:]:
        if not p.startswith("-") and not p.startswith("'") and "/" in p or (
            not p.startswith("-") and "." in p and not p.startswith("$")
        ):
            files.append(p)
    return files[:5]  # cap at 5 to avoid noise


def classify_command(command: str) -> tuple[str, list[str]]:
    """
    Classify a shell command string.
    Returns (class, list_of_accessed_file_paths).
    class is one of: nav, search, git_op, edit, exec
    """
    if not command:
        return "exec", []

    # Unwrap Codex's /bin/bash -lc '...' wrapper before classifying
    inner = _unwrap_bash(command)

    # Compound commands (&&, ||, ;, |) — classify by the first token of the inner
    # but also check write patterns across the full string
    if WRITE_RE.search(inner):
        return "edit", []

    base = _basename(inner)

    if base in GIT_COMMANDS:
        return "git_op", []

    if base in SEARCH_COMMANDS:
        files = _extract_read_files(inner)
        return "search", files

    if base in NAV_COMMANDS:
        files = _extract_read_files(inner)
        return "nav", files

    if base in WRITE_COMMANDS:
        return "edit", []

    return "exec", []


def is_edit_command(command: str) -> bool:
    cls, _ = classify_command(command)
    return cls == "edit"


# ── Normalizer ───────────────────────────────────────────────────────────────

def normalize(raw_lines: list[str]) -> list[NormalizedEvent]:
    """
    Parse Codex exec --json JSONL events → list of NormalizedEvent.

    Strategy:
    - One NormalizedEvent per completed command_execution item (tool use proxy)
    - One NormalizedEvent per turn.completed (carries token counts)
    - agent_message items become text-only NormalizedEvents
    - Token usage from turn.completed is attached to the last event of that turn
    """
    # Parse all raw events
    raw_events: list[dict] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw_events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    events: list[NormalizedEvent] = []
    seq = 0
    turn_seq = 0
    turn_start_seq = 0   # first event seq in current turn
    pending_usage: dict = {}

    for raw in raw_events:
        ev_type = raw.get("type", "")

        if ev_type == "thread.started":
            # Session start — emit a synthetic user message
            events.append(NormalizedEvent(seq=seq, ts="", role="user",
                tokens_in=0, tokens_out=0, cache_read=0, cache_write=0,
                tools=[], raw_content=[{"type": "text", "text": "__session_start__"}],
                message_uuid=raw.get("thread_id", ""),
            ))
            seq += 1
            continue

        if ev_type == "turn.started":
            turn_start_seq = seq
            continue

        if ev_type == "turn.completed":
            usage = raw.get("usage", {})
            # Attach token counts to the last event emitted in this turn
            if events:
                last = events[-1]
                # Create a new event carrying the token totals for this turn
                events.append(NormalizedEvent(seq=seq, ts="", role="assistant",
                    tokens_in=usage.get("input_tokens", 0),
                    tokens_out=usage.get("output_tokens", 0),
                    cache_read=usage.get("cached_input_tokens", 0),
                    cache_write=0,  # Codex does not expose cache write count
                    tools=[],
                    raw_content=[{"type": "usage", "data": usage}],
                    message_uuid=f"turn_{turn_seq}",
            ))
                seq += 1
                turn_seq += 1
            continue

        if ev_type == "item.completed":
            item = raw.get("item", {})
            item_type = item.get("type", "")

            if item_type == "command_execution":
                command = item.get("command", "")
                output = item.get("aggregated_output", "")
                exit_code = item.get("exit_code")

                cmd_class, read_files = classify_command(command)
                edit = is_edit_command(command)

                # Build synthetic tool entry matching Claude Code adapter shape.
                # cmd_class and read_files go INSIDE input so that classify_tool()
                # and tool_input_summary() in extract_telemetry.py can find them.
                tool_entry = {
                    "name": "Bash",
                    "input": {
                        "command": command,
                        "cmd_class": cmd_class,
                        "read_files": read_files,
                    },
                    "tool_use_id": item.get("id", ""),
                    "exit_code": exit_code,
                }
                events.append(NormalizedEvent(seq=seq, ts="", role="assistant",
                    tokens_in=0, tokens_out=0, cache_read=0, cache_write=0,
                    tools=[tool_entry],
                    raw_content=[{"type": "command_output", "output": output[:500]}],
                    message_uuid=item.get("id", ""),
            ))
                seq += 1

            elif item_type == "agent_message":
                text = item.get("text", "")
                events.append(NormalizedEvent(seq=seq, ts="", role="assistant",
                    tokens_in=0, tokens_out=0, cache_read=0, cache_write=0,
                    tools=[],
                    raw_content=[{"type": "text", "text": text}],
                    message_uuid=item.get("id", ""),
            ))
                seq += 1

            elif item_type == "file_change":
                # Codex directly edits files via file_change items (not shell commands)
                changes = item.get("changes", [])
                file_paths = [ch.get("path", "") for ch in changes if ch.get("path")]
                for path in file_paths:
                    tool_entry = {
                        "name": "Edit",
                        "input": {"file_path": path},
                        "tool_use_id": item.get("id", ""),
                        "cmd_class": "edit",
                        "read_files": [],
                    }
                    events.append(NormalizedEvent(seq=seq, ts="", role="assistant",
                        tokens_in=0, tokens_out=0, cache_read=0, cache_write=0,
                        tools=[tool_entry],
                        raw_content=[{"type": "file_change", "path": path}],
                        message_uuid=item.get("id", ""),
            ))
                    seq += 1

            # Skip warning/error items (e.g. bypass-hook-trust notice)

        if ev_type == "item.started":
            # We use item.completed for commands; skip started events
            continue

    return events
