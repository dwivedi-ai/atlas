#!/usr/bin/env python3
"""
agent.py — headless invocation of a coding agent, and the ONE place a model name
turns into a concrete backend + model id.

RESPONSIBILITY
  1. `BACKENDS` / `resolve_agent()` — the two-level model registry (§6.5). `backend`
     picks the CLI adapter; `model` is the concrete id it is invoked with; `effort` is
     the backend-specific reasoning knob. This replaces the old `resolve_agent_id`,
     which hard-pinned `claude-sonnet-4-6` in a chain of `if` statements, and the
     duplicate bash `case` in run_job.sh.
  2. `run_text()` — run a prompt headless in a directory, let it write files, return
     the final message. This is the judge's counterpart to lib/run_agent.sh (which is
     specialized for the graded task run and captures a full transcript).

INPUTS   a model or backend name, a prompt, a cwd, optionally a CLAUDE_CONFIG_DIR.
OUTPUTS  (final_text, exit_code); the `resolve` CLI prints one field or a JSON blob
         on stdout (payload to stdout, diagnostics to stderr — run_job.sh captures
         this with command substitution).

Public API:
  BACKENDS: dict[str, dict]
  resolve_agent(model=None, backend=None, effort=None) -> dict
  resolve_agent_id(model) -> str            # DEPRECATED shim, kept for callers
  run_text(model, prompt, cwd, max_seconds=0, config_dir=None, tools=None,
           settings=None, hygiene=True) -> (text, exit_code)
  CLI: python3 lib/agent.py resolve --model M [--backend B] [--field K] [--json]
       python3 lib/agent.py --model M --cwd D "prompt"      (legacy run form)

MODEL PIN
  The claude default is `claude-sonnet-5`, not `claude-sonnet-4-6`. Sonnet 5 emits
  ~30% more tokens than Sonnet 4.6 for the same text, so NO historical Atlas baseline
  sizes a Sonnet 5 run without rescaling — and see V7, those baselines are inflated
  1.0x-4.9x on top of that.

HYGIENE (§6.5, verified by spike S2)
  Every one-shot claude invocation runs with a per-call `CLAUDE_CONFIG_DIR` when one is
  given, `--setting-sources project`, `--strict-mcp-config`, `--disable-slash-commands`
  and the frozen six-tool allowlist, so the judge and the fact-authoring calls sit under
  the same isolation as the task run. stdin is /dev/null on every backend: without it
  each claude call pays a 3 s stall and writes `Warning: no stdin data received in 3s`
  to stderr, which a naive `[ -s stderr ]` health check reads as failure (V19).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# The frozen tool allowlist (V10). `--tools` silently ignores names it does not honour,
# so this list is asserted positively at preflight against `system/init.tools`, never
# assumed. Task and judge share it so the judge is not a differently-privileged agent.
FROZEN_TOOLS = ("Bash", "Read", "Write", "Edit", "Glob", "Grep")


# ── the backend registry ─────────────────────────────────────────────────────
# One entry per CLI adapter. `aliases` maps the loose names a human types (and every
# name the pre-registry resolve_agent_id accepted) onto the backend. `models` is the
# set of concrete ids that CLI is known to accept; `model_prefixes` admits ids that
# have not been enumerated here yet, so a new model id needs no code change.
BACKENDS: dict[str, dict] = {
    "claude": {
        "cli": "claude",
        "provider": "anthropic",
        "default_model": "claude-sonnet-5",
        "aliases": ("claude", "sonnet", "claude-sonnet", "anthropic"),
        "models": ("claude-sonnet-5", "claude-sonnet-4-6", "claude-opus-4-1",
                   "claude-haiku-4-5"),
        "model_prefixes": ("claude-",),
        "efforts": (None,),
        "supports_config_dir": True,
    },
    "codex": {
        "cli": "codex",
        "provider": "openai",
        "default_model": "codex",
        "aliases": ("codex", "openai"),
        "models": ("codex",),
        "model_prefixes": ("gpt-", "o3", "o4"),
        "efforts": (None, "low", "medium", "high"),
        "supports_config_dir": False,
    },
    "gemini": {
        "cli": "gemini",
        "provider": "google",
        "default_model": "gemini-2.5-pro",
        "aliases": ("gemini", "google", "gemini-flash", "gemini-flash-lite"),
        "models": ("gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"),
        "model_prefixes": ("gemini-",),
        "efforts": (None,),
        "supports_config_dir": False,
    },
    "agy": {
        "cli": "agy",
        "provider": "antigravity",
        "default_model": "agy-flash-low",
        "aliases": ("agy", "antigravity", "agy-flash"),
        "models": ("agy-flash-low", "agy-flash-med", "agy-flash-high",
                   "agy-pro-low", "agy-pro-high", "agy-sonnet", "agy-opus", "agy-gpt-oss"),
        "model_prefixes": ("agy-",),
        "efforts": (None,),
        "supports_config_dir": False,
    },
}

# Loose names that must keep resolving to a specific model, not just a backend.
# Everything the pre-registry resolve_agent_id honoured is here, so no job.yaml breaks.
MODEL_ALIASES: dict[str, str] = {
    "sonnet": "claude-sonnet-5",
    "claude-sonnet": "claude-sonnet-5",
    "gemini-flash": "gemini-2.5-flash",
    "gemini-flash-lite": "gemini-2.5-flash-lite",
    "agy-flash": "agy-flash-low",
    "agy-pro": "agy-pro-low",
}

_ALIAS_TO_BACKEND = {a: b for b, spec in BACKENDS.items() for a in spec["aliases"]}


def _backend_of_model(m: str) -> str | None:
    for name, spec in BACKENDS.items():
        if m in spec["models"]:
            return name
    for name, spec in BACKENDS.items():
        if any(m.startswith(p) for p in spec["model_prefixes"]):
            return name
    return None


def resolve_agent(model: str | None = None, backend: str | None = None,
                  effort: str | None = None) -> dict:
    """Resolve (model, backend) -> the full agent identity. The single source of truth.

    Either argument may be omitted:
      resolve_agent("claude")             -> backend claude, model claude-sonnet-5
      resolve_agent("claude-sonnet-4-6")  -> backend claude, model claude-sonnet-4-6 (pin kept)
      resolve_agent(backend="gemini")     -> backend gemini, model gemini-2.5-pro
      resolve_agent("gemini-2.5-flash", backend="gemini")  -> both, cross-checked

    Returns {backend, model, agent_id, agent_dash, provider, cli, effort, default_model}.
    `agent_id` is the id written into run_meta.json / run_record.condition.agent_id, and
    `agent_dash` is it with dots turned into dashes (run_job.sh's run-id component).
    Raises ValueError with a human message on an unknown name — never guesses.
    """
    m = (model or "").strip().lower()
    b = (backend or "").strip().lower() or None

    if b is not None:
        b = _ALIAS_TO_BACKEND.get(b, b)
        if b not in BACKENDS:
            raise ValueError(f"unknown backend: {backend!r} (use {'|'.join(BACKENDS)})")

    resolved_model: str | None = None
    if m:
        m = MODEL_ALIASES.get(m, m)
        if m in _ALIAS_TO_BACKEND:
            # a bare backend name given in the model slot: "claude", "codex", ...
            inferred = _ALIAS_TO_BACKEND[m]
            if b and b != inferred:
                raise ValueError(f"model {model!r} belongs to backend {inferred!r}, not {b!r}")
            b = b or inferred
            resolved_model = BACKENDS[b]["default_model"]
        else:
            inferred = _backend_of_model(m)
            if inferred is None:
                raise ValueError(
                    f"unknown model: {model!r} (use {'|'.join(BACKENDS)} or a known model id)")
            if b and b != inferred:
                raise ValueError(f"model {model!r} belongs to backend {inferred!r}, not {b!r}")
            b = b or inferred
            resolved_model = m

    if b is None:
        raise ValueError("resolve_agent needs a model or a backend")
    if resolved_model is None:
        resolved_model = BACKENDS[b]["default_model"]

    spec = BACKENDS[b]
    eff = effort or None
    if eff is not None and spec["efforts"] != (None,) and eff not in spec["efforts"]:
        raise ValueError(f"backend {b!r} does not accept effort {eff!r} "
                         f"(accepts {[e for e in spec['efforts'] if e]})")
    if eff is not None and spec["efforts"] == (None,):
        raise ValueError(f"backend {b!r} has no effort knob (got effort={eff!r})")

    # codex is invoked as the bare CLI, so its agent_id stays "codex" — the historical
    # value in every existing run_meta.json and every _infer_provider() call site.
    agent_id = "codex" if b == "codex" else resolved_model
    return {
        "backend": b,
        "model": resolved_model,
        "agent_id": agent_id,
        "agent_dash": agent_id.replace(".", "-"),
        "provider": spec["provider"],
        "cli": spec["cli"],
        "effort": eff,
        "default_model": spec["default_model"],
    }


def resolve_agent_id(model: str) -> str:
    """DEPRECATED — use resolve_agent(). Kept so existing callers keep working.

    Behaviour change that is deliberate, not a regression: the bare name "claude" now
    resolves to `claude-sonnet-5`, where it used to hard-pin `claude-sonnet-4-6`. An
    explicit "claude-sonnet-4-6" still resolves to itself, so a pinned job is untouched.
    """
    return resolve_agent(model)["agent_id"]


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


def _final_text(backend: str, out: str) -> str:
    if backend == "codex":
        return _codex_final_text(out)
    if backend == "gemini":
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


def _seed_claude_home(config_dir: str) -> str:
    """Per-call CLAUDE_CONFIG_DIR seeded with the credentials file ONLY (spike S2).

    Anything else copied out of ~/.claude is uncontrolled context sitting directly upstream
    of the measurement. Modes are tightened because the copy is a credential: a credential
    copy must never be world-readable and must never be committable.
    """
    import shutil
    d = Path(config_dir)
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    src = Path.home() / ".claude" / ".credentials.json"
    dst = d / ".credentials.json"
    if src.exists() and not dst.exists():
        shutil.copy(src, dst)
        try:
            os.chmod(dst, 0o600)
        except OSError:
            pass
    return str(d)


def claude_argv(model: str, prompt: str, *, tools: tuple[str, ...] | None = FROZEN_TOOLS,
                settings: str | os.PathLike | None = None, hygiene: bool = True,
                output_format: str = "json") -> list[str]:
    """The one-shot claude command line. Exposed so preflight/canary can hash it (§5.1(6)).

    Deliberately NOT here: --input-format stream-json (that is the driver's channel, and the
    child does not exit after `result` under it, V15) and --max-turns (no such flag in 2.1.222).
    """
    cmd = ["claude", "--model", model, "--output-format", output_format, "--print"]
    if hygiene:
        cmd += ["--setting-sources", "project", "--strict-mcp-config", "--disable-slash-commands"]
        if tools:
            cmd += ["--tools", ",".join(tools)]
    if settings:
        cmd += ["--settings", str(settings)]
    cmd += ["--permission-mode", "bypassPermissions", prompt]
    return cmd


def run_text(model: str, prompt: str, cwd: str | os.PathLike,
             max_seconds: int = 0, config_dir: str | os.PathLike | None = None,
             tools: tuple[str, ...] | None = FROZEN_TOOLS,
             settings: str | os.PathLike | None = None,
             hygiene: bool = True,
             backend: str | None = None,
             effort: str | None = None) -> tuple[str, int]:
    """Run the agent headless in `cwd`. Returns (final_text, exit_code).

    config_dir  a directory used as CLAUDE_CONFIG_DIR for this call, seeded with the
                credentials file only. Give it and the judge / fact-authoring calls run
                under the same isolation as the task run: no user CLAUDE.md, no MCP
                servers, no skills, no slash commands (verified by S2). Ignored by the
                backends that have no equivalent (codex/gemini keep their own isolation).
    tools       the tool allowlist; defaults to the frozen six. None disables --tools.
    hygiene     False drops the isolation flags entirely — for a backend or CLI version
                that does not honour them. Claude 2.1.222 honours all of them.
    """
    ident = resolve_agent(model, backend=backend, effort=effort)
    b, agent_id = ident["backend"], ident["agent_id"]
    cwd = str(cwd)
    Path(cwd).mkdir(parents=True, exist_ok=True)
    timeout = max_seconds if max_seconds and max_seconds > 0 else None
    _agy_home: str | None = None

    if b == "codex":
        cmd = [
            "codex", "exec", "-C", cwd, "--sandbox", "workspace-write",
            "--dangerously-bypass-approvals-and-sandbox", "--ephemeral", "--json", prompt,
        ]
        env = dict(os.environ)
    elif b == "gemini":
        # -p inline (never stdin); yolo+skip-trust = headless auto-approval; -o json
        # returns {session_id, response, stats}. Trust env var belts the --skip-trust flag.
        cmd = [
            "gemini", "-p", prompt, "--model", agent_id,
            "--approval-mode", "yolo", "--skip-trust", "--output-format", "json",
        ]
        env = dict(os.environ); env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
    elif b == "agy":
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
        cmd = claude_argv(agent_id, prompt, tools=tools, settings=settings, hygiene=hygiene)
        # env -u ANTHROPIC_API_KEY: runs go down the subscription path, not the API path.
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        if config_dir:
            env["CLAUDE_CONFIG_DIR"] = _seed_claude_home(str(config_dir))

    try:
        proc = subprocess.run(
            # stdin=DEVNULL is `< /dev/null`: without it every claude one-shot pays a 3 s
            # stall and writes a stderr warning a health check would read as failure (V19).
            cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout,
        )
        code = proc.returncode
        out = proc.stdout or ""
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        code = 124

    if b == "agy":
        _harvest_agy_scratch(_agy_home, cwd)
        return _agy_final_text(cwd, str(Path(cwd) / ".agy_runtext.log"), out, _agy_home), code
    return _final_text(b, out), code


# ── CLI ──────────────────────────────────────────────────────────────────────
def _cmd_resolve(a) -> int:
    try:
        ident = resolve_agent(a.model, backend=a.backend, effort=a.effort)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(ident))
    elif a.field:
        if a.field not in ident:
            print(f"ERROR: no such field {a.field!r} (have {', '.join(ident)})", file=sys.stderr)
            return 2
        print("" if ident[a.field] is None else ident[a.field])
    else:
        print(ident["agent_id"])
    return 0


def _cmd_backends(a) -> int:
    if a.json:
        print(json.dumps({k: {kk: vv for kk, vv in v.items()} for k, v in BACKENDS.items()},
                         indent=2))
    else:
        for name, spec in BACKENDS.items():
            print(f"{name}\t{spec['default_model']}\t{spec['provider']}")
    return 0


def _cmd_run(a) -> int:
    text, code = run_text(a.model, a.prompt, a.cwd, a.max_seconds, config_dir=a.config_dir,
                          backend=a.backend, effort=a.effort)
    print(f"[exit={code}]")
    print(text)
    return 0


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Agent registry + headless prompt runner.")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("resolve", help="print the resolved agent identity (for run_job.sh)")
    r.add_argument("--model")
    r.add_argument("--backend")
    r.add_argument("--effort")
    r.add_argument("--field", help="one of: backend model agent_id agent_dash provider cli effort")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=_cmd_resolve)

    bl = sub.add_parser("backends", help="list the registry")
    bl.add_argument("--json", action="store_true")
    bl.set_defaults(func=_cmd_backends)

    rn = sub.add_parser("run", help="run a headless prompt, print final text")
    rn.add_argument("--model", required=True)
    rn.add_argument("--backend")
    rn.add_argument("--effort")
    rn.add_argument("--cwd", required=True)
    rn.add_argument("--config-dir")
    rn.add_argument("--max-seconds", type=int, default=0)
    rn.add_argument("prompt")
    rn.set_defaults(func=_cmd_run)

    # Legacy form: `agent.py --model M --cwd D "prompt"` with no subcommand.
    if len(sys.argv) > 1 and sys.argv[1].startswith("-"):
        lp = argparse.ArgumentParser(description="Run a headless agent prompt, print final text.")
        lp.add_argument("--model", required=True)
        lp.add_argument("--backend")
        lp.add_argument("--effort")
        lp.add_argument("--cwd", required=True)
        lp.add_argument("--config-dir")
        lp.add_argument("--max-seconds", type=int, default=0)
        lp.add_argument("prompt")
        return _cmd_run(lp.parse_args())

    a = p.parse_args()
    if not getattr(a, "func", None):
        p.print_help(sys.stderr)
        return 2
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
