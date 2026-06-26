#!/usr/bin/env python3
"""
agent.py — headless invocation of a coding agent (codex | claude), used by the
judge for synthesis and adjudication. Returns the agent's final text and lets it
write files in `cwd` (workspace-write / bypassPermissions, same as a task run).

This is the judge's counterpart to lib/run_agent.sh (which is specialized for the
graded task run and captures a full transcript). Here we just need "run a prompt
in a directory, optionally let it write files, give me the final message."

Public API:
  resolve_agent_id(model) -> "codex" | "claude-sonnet-4-6"
  run_text(model, prompt, cwd, max_seconds=0) -> (text, exit_code)
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def resolve_agent_id(model: str) -> str:
    m = (model or "").lower()
    if m in ("codex", "openai"):
        return "codex"
    if m in ("claude", "sonnet", "claude-sonnet", "claude-sonnet-4-6"):
        return "claude-sonnet-4-6"
    raise ValueError(f"unknown model: {model!r} (use codex|claude)")


def _codex_final_text(stdout: str) -> str:
    """Last agent_message text from a codex `exec --json` JSONL stream."""
    last = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = ev.get("item", {})
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            last = item.get("text", last)
        # tolerate older/newer shapes
        elif ev.get("type") in ("agent_message", "assistant"):
            last = ev.get("text") or ev.get("message") or last
    return last


def _claude_final_text(stdout: str) -> str:
    """The `.result` field from claude `--print --output-format json`."""
    stdout = stdout.strip()
    if not stdout:
        return ""
    try:
        d = json.loads(stdout)
        return d.get("result") or d.get("text") or ""
    except json.JSONDecodeError:
        # Some versions stream multiple JSON lines; take the last object's result.
        last = ""
        for line in stdout.splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict) and (d.get("result") or d.get("text")):
                last = d.get("result") or d.get("text")
        return last


def run_text(model: str, prompt: str, cwd: str | os.PathLike,
             max_seconds: int = 0) -> tuple[str, int]:
    """Run the agent headless in `cwd`. Returns (final_text, exit_code)."""
    agent_id = resolve_agent_id(model)
    cwd = str(cwd)
    Path(cwd).mkdir(parents=True, exist_ok=True)
    timeout = max_seconds if max_seconds and max_seconds > 0 else None

    if agent_id == "codex":
        cmd = [
            "codex", "exec", "-C", cwd, "--sandbox", "workspace-write",
            "--dangerously-bypass-approvals-and-sandbox", "--ephemeral", "--json", prompt,
        ]
        env = dict(os.environ)
    else:
        cmd = [
            "claude", "--model", agent_id, "--output-format", "json", "--print",
            "--permission-mode", "bypassPermissions", prompt,
        ]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    try:
        proc = subprocess.run(
            cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        text = _codex_final_text(out) if agent_id == "codex" else _claude_final_text(out)
        return text, 124

    out = proc.stdout or ""
    text = _codex_final_text(out) if agent_id == "codex" else _claude_final_text(out)
    return text, proc.returncode


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Run a headless agent prompt, print final text.")
    p.add_argument("--model", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--max-seconds", type=int, default=0)
    p.add_argument("prompt")
    a = p.parse_args()
    text, code = run_text(a.model, a.prompt, a.cwd, a.max_seconds)
    print(f"[exit={code}]")
    print(text)
