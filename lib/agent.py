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
    if m in ("gemini", "google", "gemini-2.5-pro"):
        return "gemini-2.5-pro"
    if m in ("gemini-2.5-flash", "gemini-flash"):
        return "gemini-2.5-flash"
    if m in ("gemini-2.5-flash-lite", "gemini-flash-lite"):
        return "gemini-2.5-flash-lite"
    if m in ("agy", "antigravity", "agy-flash", "agy-flash-low"):
        return "agy-flash-low"
    if m.startswith("agy-"):  # agy-flash-med|-high, agy-pro-low|-high, agy-sonnet|-opus, agy-gpt-oss
        return m
    raise ValueError(f"unknown model: {model!r} (use codex|claude|gemini|agy)")


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


def _gemini_final_text(stdout: str) -> str:
    """The `.response` field from gemini `-p --output-format json`."""
    stdout = stdout.strip()
    if not stdout:
        return ""
    try:
        return json.loads(stdout).get("response") or ""
    except json.JSONDecodeError:
        return ""


def _final_text(agent_id: str, out: str) -> str:
    if agent_id == "codex":
        return _codex_final_text(out)
    if agent_id.startswith("gemini-"):
        return _gemini_final_text(out)
    return _claude_final_text(out)


def _agy():
    try:
        import agy
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        import agy
    return agy


def _agy_final_text(cwd: str, log_path: str, stdout: str, gemini_dir: str | None = None) -> str:
    """Clean final answer from the agy transcript (stdout is noisy narrative)."""
    agy = _agy()
    cid = agy.conversation_id(log_path=log_path, cwd=cwd, gemini_dir=gemini_dir)
    if cid:
        t = agy.transcript_path(cid, gemini_dir)
        if Path(t).exists():
            ans = agy.final_answer(t)
            if ans:
                return ans
    return stdout.strip()


def _seed_agy_home(home: str) -> None:
    """Seed a per-call isolated agy home with just the auth/onboarding files."""
    import shutil
    src = Path.home() / ".gemini" / "antigravity-cli"
    ac = Path(home) / "antigravity-cli"
    (ac / "cache").mkdir(parents=True, exist_ok=True)
    for f in ("antigravity-oauth-token", "settings.json", "jetski_state.pbtxt", "installation_id"):
        if (src / f).exists():
            shutil.copy(src / f, ac / f)
    for f in ("onboarding.json", "default_project_id.txt"):
        if (src / "cache" / f).exists():
            shutil.copy(src / "cache" / f, ac / "cache" / f)


def _harvest_agy_scratch(gemini_dir: str | None, cwd: str) -> None:
    """agy writes NEW files into <home>/antigravity-cli/scratch/, not the --add-dir workspace.
    Copy those back into `cwd` (preserving structure) so callers that expect a produced file
    (criteria.json, a context artifact) actually find it. Only for the isolated-home path, where
    scratch holds exactly this call's files."""
    import shutil
    if not gemini_dir:
        return
    scratch = Path(gemini_dir) / "antigravity-cli" / "scratch"
    if not scratch.is_dir():
        return
    for p in scratch.rglob("*"):
        if p.is_file():
            dst = Path(cwd) / p.relative_to(scratch)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(p, dst)
            except Exception:
                pass


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
    elif agent_id.startswith("gemini-"):
        # -p inline (never stdin); yolo+skip-trust = headless auto-approval; -o json
        # returns {session_id, response, stats}. Trust env var belts the --skip-trust flag.
        cmd = [
            "gemini", "-p", prompt, "--model", agent_id,
            "--approval-mode", "yolo", "--skip-trust", "--output-format", "json",
        ]
        env = dict(os.environ); env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
    elif agent_id.startswith("agy"):
        # agy writes NEW files into its OWN private scratch (cwd != workspace), and its shared
        # scratch/knowledge carry over between calls. Run in a fresh isolated home (no carryover)
        # and harvest that scratch back into `cwd` after the run so a produced file (criteria.json
        # / a context artifact) lands where the caller looks. stdout is narrative → the clean
        # answer comes from the transcript. See AGY_DOCS.md §10-11.
        agy = _agy()
        _agy_log = str(Path(cwd) / ".agy_runtext.log")
        _agy_home = str(Path(cwd) / ".agy_home")
        _seed_agy_home(_agy_home)
        cmd = [
            "agy", "-p", prompt, "--model", agy.cli_model(agent_id),
            "--dangerously-skip-permissions", "--add-dir", str(cwd),
            "--print-timeout", "10m", "--log-file", _agy_log,
        ]
        if (Path(_agy_home) / "antigravity-cli" / "antigravity-oauth-token").exists():
            cmd += ["--gemini_dir", _agy_home]
        else:
            _agy_home = None
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
        code = proc.returncode
        out = proc.stdout or ""
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        code = 124

    if agent_id.startswith("agy"):
        _harvest_agy_scratch(_agy_home, cwd)
        return _agy_final_text(cwd, str(Path(cwd) / ".agy_runtext.log"), out, _agy_home), code
    return _final_text(agent_id, out), code


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
