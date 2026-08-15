#!/usr/bin/env python3
"""
settings.py — render and verify the per-run Claude Code settings file.

RESPONSIBILITY
  Produce $RUN_DIR/settings.json: the ONLY source of this run's hooks, and the
  file that replaced the global ~/.claude/settings.json mutation which used to
  serialize the whole matrix (setup_run.sh:117-145). Per-run CLAUDE_CONFIG_DIR +
  per-run --settings is what makes JOBS <= 4 possible at all.

  Two hooks, no more: PreToolUse (the barrier, matcher "*") and SessionStart (the
  liveness marker). There is no PostToolUse hook and no Stop hook — PostToolUse
  does not fire when a tool errors and its tool_response is not what the model
  saw, so it would cost a fork per tool call to observe nothing trustworthy.

  Both hook commands are ARGV-BOUND. The child is launched under
  `env -u ANTHROPIC_API_KEY`, and a run whose instrumentation depended on which
  environment variables survived a scrub would be silently unmeasured on the day
  someone tightened the scrub. Every parameter the hook needs — run dir, mode,
  gate timeout, poll interval — is on the command line, shell-quoted.

INPUTS
  run_dir      $RUN_DIR (its `gate/` and `watch/` subdirectories are derived)
  gate_py      path to lib/wur/gate.py (defaults to this file's sibling)
  python_exe   the interpreter to bind (defaults to sys.executable)
  mode         "barrier" (a WUR run) or "log" (the log_tool_event.sh drop-in)
  timeout_ms   the barrier's fail-open deadline; job.yaml probe.gate_timeout_ms

OUTPUTS
  $RUN_DIR/settings.json      atomically written, then RE-READ and re-validated
  render_settings() -> Path   the path it wrote
  settings_sha256()           the stamp callers put in run_meta.json

WHY THE RENDERER VALIDATES ITS OWN OUTPUT
  `claude --help` states that in --print mode "settings files that fail
  validation are silently ignored". A templating bug therefore yields zero hooks,
  zero barriers, zero probes and NO ERROR ANYWHERE — a run that looks fine and
  measures nothing. So: the template is loaded as JSON (never string-spliced into
  a shape that could stop being JSON), the sentinels are matched exactly, the
  rendered object is json.load'ed back off disk, and every structural expectation
  is re-asserted. Anything wrong raises SettingsError. preflight H4 then checks
  the same file again from the outside, because the two failures this guards
  against (a bad render, and a good render someone edited afterwards) are
  different failures.

CLI
  python3 lib/wur/settings.py --run-dir $RUN_DIR [--mode barrier|log]
                              [--timeout-ms 300000] [--poll-ms 5] [--print]
  Payload (the settings path, or the JSON with --print) goes to stdout; every
  human-facing message goes to stderr — setup_run.sh reads this by command
  substitution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = HERE / "settings_template.json"
GATE_PATH = HERE / "gate.py"

SETTINGS_FILENAME = "settings.json"
PRE_SENTINEL = "__PRE_COMMAND__"
SESSION_START_SENTINEL = "__SESSION_START_COMMAND__"

#: An UNRESOLVED template sentinel, and only that. A bare "__" test would reject
#: any run whose path happens to contain a double underscore — mkdtemp produces
#: one roughly one time in thirty, and /home/.../__pycache__/ always would.
PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")

#: The two events we bind, and nothing else. Asserted on both render and check.
HOOK_EVENTS = ("PreToolUse", "SessionStart")
#: matcher "*" for both — measured working in S7 (barrier fired on every tool
#: call; the SessionStart marker file appeared).
MATCHER = "*"

DEFAULT_TIMEOUT_MS = 300_000
DEFAULT_POLL_MS = 5
#: Claude Code kills a hook that outruns its own timeout, which would turn the
#: barrier into an unlogged fail-open. Give the CLI's timer this much slack over
#: the barrier's, so the barrier's fail-open (which logs gate_timeout) always
#: fires first.
HOOK_TIMEOUT_SLACK_S = 30


class SettingsError(RuntimeError):
    """Raised when the rendered settings file is not exactly what it must be."""


# ── command construction (argv-bound) ────────────────────────────────────────
def _q(part: str | os.PathLike[str]) -> str:
    return shlex.quote(str(part))


def _python_exe(explicit: str | None = None) -> str:
    if explicit:
        return str(explicit)
    if sys.executable:
        return sys.executable
    found = shutil.which("python3")
    if not found:
        raise SettingsError("no python3 interpreter to bind into the hook command")
    return found






def pre_command(run_dir: str | os.PathLike[str], *, gate_py: str | os.PathLike[str] = GATE_PATH,
                python_exe: str | None = None, mode: str = "barrier",
                timeout_ms: int = DEFAULT_TIMEOUT_MS, poll_ms: int = DEFAULT_POLL_MS) -> str:
    """The PreToolUse command line. Every knob is argv, none is environment."""
    return " ".join([
        _q(_python_exe(python_exe)), _q(gate_py), "pre",
        "--run-dir", _q(run_dir),
        "--mode", _q(mode),
        "--timeout-ms", str(int(timeout_ms)),
        "--poll-ms", str(int(poll_ms)),
    ])


def session_start_command(run_dir: str | os.PathLike[str], *,
                          gate_py: str | os.PathLike[str] = GATE_PATH,
                          python_exe: str | None = None) -> str:
    """The SessionStart command line — writes watch/hooks_alive, allows, exits 0."""
    return " ".join([
        _q(_python_exe(python_exe)), _q(gate_py), "session-start",
        "--run-dir", _q(run_dir),
    ])


# ── render ───────────────────────────────────────────────────────────────────
def settings_path(run_dir: str | os.PathLike[str]) -> Path:
    return Path(run_dir) / SETTINGS_FILENAME


def _hook_entry(obj: dict[str, Any], event: str) -> dict[str, Any]:
    """The single {type, command, timeout} entry for `event`, or raise."""
    hooks = obj.get("hooks")
    if not isinstance(hooks, dict):
        raise SettingsError("settings has no `hooks` object")
    matchers = hooks.get(event)
    if not isinstance(matchers, list) or len(matchers) != 1:
        raise SettingsError(f"hooks.{event} must be a list of exactly one matcher block")
    block = matchers[0]
    if not isinstance(block, dict):
        raise SettingsError(f"hooks.{event}[0] is not an object")
    entries = block.get("hooks")
    if not isinstance(entries, list) or len(entries) != 1:
        raise SettingsError(f"hooks.{event}[0].hooks must hold exactly one command")
    entry = entries[0]
    if not isinstance(entry, dict):
        raise SettingsError(f"hooks.{event}[0].hooks[0] is not an object")
    return entry


def load_template(template: str | os.PathLike[str] = TEMPLATE_PATH) -> dict[str, Any]:
    """Load the template and assert it still has the shape the renderer patches.

    Structural patching, not string splicing: the old lib/hooks/settings_template
    .json was rendered by str.replace over the serialized JSON, which cannot
    detect that the placeholder it was looking for is gone.
    """
    path = Path(template)
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SettingsError(f"settings template missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SettingsError(f"settings template is not valid JSON: {path}: {exc}") from exc
    if set(obj.get("hooks", {})) != set(HOOK_EVENTS):
        raise SettingsError(
            f"template must bind exactly {list(HOOK_EVENTS)}, found "
            f"{sorted(obj.get('hooks', {}))}"
        )
    for event, sentinel in ((HOOK_EVENTS[0], PRE_SENTINEL), (HOOK_EVENTS[1], SESSION_START_SENTINEL)):
        entry = _hook_entry(obj, event)
        if entry.get("command") != sentinel:
            raise SettingsError(f"template hooks.{event} lost its {sentinel} sentinel")
    return obj


def build_settings(run_dir: str | os.PathLike[str], *,
                   gate_py: str | os.PathLike[str] = GATE_PATH,
                   python_exe: str | None = None,
                   mode: str = "barrier",
                   timeout_ms: int = DEFAULT_TIMEOUT_MS,
                   poll_ms: int = DEFAULT_POLL_MS,
                   hook_timeout_s: int | None = None,
                   template: str | os.PathLike[str] = TEMPLATE_PATH) -> dict[str, Any]:
    """The settings object for one run. Pure: touches no file but the template."""
    if mode not in ("barrier", "log"):
        raise SettingsError(f"mode must be barrier|log, got {mode!r}")
    gate = Path(gate_py)
    if not gate.is_file():
        raise SettingsError(f"gate.py not found at {gate} — the hook would be a no-op")
    if hook_timeout_s is None:
        hook_timeout_s = int(timeout_ms // 1000) + HOOK_TIMEOUT_SLACK_S
    if hook_timeout_s * 1000 < timeout_ms:
        raise SettingsError(
            f"hook timeout {hook_timeout_s}s is shorter than the barrier timeout "
            f"{timeout_ms}ms; the CLI would kill the barrier before it can log gate_timeout"
        )

    obj = load_template(template)
    pre = _hook_entry(obj, "PreToolUse")
    pre["command"] = pre_command(run_dir, gate_py=gate, python_exe=python_exe, mode=mode,
                                 timeout_ms=timeout_ms, poll_ms=poll_ms)
    pre["timeout"] = int(hook_timeout_s)
    ss = _hook_entry(obj, "SessionStart")
    ss["command"] = session_start_command(run_dir, gate_py=gate, python_exe=python_exe)
    ss["timeout"] = int(min(hook_timeout_s, 60))
    return obj


def _argv_value(argv: list[str], flag: str) -> str | None:
    """The value following `flag` in an argv list, or None if it is not there."""
    try:
        i = argv.index(flag)
    except ValueError:
        return None
    return argv[i + 1] if i + 1 < len(argv) else None


def validate_settings(obj: Any, *, run_dir: str | os.PathLike[str] | None = None,
                      require_barrier: bool = False) -> list[str]:
    """Every problem with a settings object, as strings. Empty means good.

    Checked from the outside so preflight can re-run it on the file as it sits on
    disk at launch time, which is a different question from "did the render
    succeed" — a correct file can be edited afterwards.
    """
    problems: list[str] = []
    if not isinstance(obj, dict):
        return [f"settings is {type(obj).__name__}, expected an object"]
    if set(obj) != {"hooks"}:
        problems.append(f"settings must carry `hooks` and nothing else, found {sorted(obj)}")
    hooks = obj.get("hooks")
    if not isinstance(hooks, dict):
        return problems + ["settings.hooks is not an object"]
    if set(hooks) != set(HOOK_EVENTS):
        problems.append(f"hooks must bind exactly {list(HOOK_EVENTS)}, found {sorted(hooks)}")
    for event in HOOK_EVENTS:
        if event not in hooks:
            continue
        try:
            entry = _hook_entry(obj, event)
        except SettingsError as exc:
            problems.append(str(exc))
            continue
        block = hooks[event][0]
        if block.get("matcher") != MATCHER:
            problems.append(f"hooks.{event}[0].matcher is {block.get('matcher')!r}, want {MATCHER!r}")
        if entry.get("type") != "command":
            problems.append(f"hooks.{event} entry type is {entry.get('type')!r}, want 'command'")
        cmd = entry.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            problems.append(f"hooks.{event} command is empty")
            continue
        found = PLACEHOLDER_RE.search(cmd)
        if found:
            problems.append(f"hooks.{event} command still carries the placeholder "
                            f"{found.group(0)}: {cmd}")
        try:
            argv = shlex.split(cmd)
        except ValueError as exc:
            problems.append(f"hooks.{event} command does not lex: {exc}")
            continue
        if len(argv) < 3:
            problems.append(f"hooks.{event} command is too short to be argv-bound: {cmd}")
            continue
        script = argv[1]
        if not Path(script).is_file():
            problems.append(f"hooks.{event} points at a missing script: {script}")
        if os.path.basename(script) != "gate.py":
            problems.append(f"hooks.{event} does not point at gate.py: {script}")
        expected_sub = "pre" if event == "PreToolUse" else "session-start"
        if argv[2] != expected_sub:
            problems.append(f"hooks.{event} subcommand is {argv[2]!r}, want {expected_sub!r}")
        bound = _argv_value(argv, "--run-dir")
        if bound is None:
            problems.append(f"hooks.{event} is not argv-bound: no --run-dir (env may be scrubbed)")
        elif run_dir is not None and Path(bound).resolve() != Path(run_dir).resolve():
            problems.append(f"hooks.{event} --run-dir is {bound}, want {run_dir}")
        if event == "PreToolUse":
            if _argv_value(argv, "--timeout-ms") is None:
                problems.append("PreToolUse command does not carry --timeout-ms")
            if require_barrier and _argv_value(argv, "--mode") != "barrier":
                problems.append(
                    "PreToolUse is not bound in barrier mode; this run needs the barrier"
                )
        timeout = entry.get("timeout")
        if not isinstance(timeout, int) or timeout <= 0:
            problems.append(f"hooks.{event} timeout is {timeout!r}, want a positive integer (seconds)")
    if "PreToolUse" in hooks:
        try:
            entry = _hook_entry(obj, "PreToolUse")
            raw_ms = _argv_value(shlex.split(entry["command"]), "--timeout-ms")
            if raw_ms is not None and int(entry.get("timeout") or 0) * 1000 < int(raw_ms):
                problems.append(
                    "PreToolUse hook timeout is shorter than its own --timeout-ms; the CLI "
                    "would kill the barrier before it can log gate_timeout"
                )
        except Exception:  # noqa: BLE001 — malformed shapes are already reported above
            pass
    return problems


def settings_sha256(obj_or_path: Any) -> str:
    """sha256 of the settings as they will be read: canonical JSON, sorted keys."""
    obj = obj_or_path
    if isinstance(obj_or_path, (str, os.PathLike)):
        obj = json.loads(Path(obj_or_path).read_text(encoding="utf-8"))
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def render_settings(run_dir: str | os.PathLike[str], *,
                    gate_py: str | os.PathLike[str] = GATE_PATH,
                    python_exe: str | None = None,
                    mode: str = "barrier",
                    timeout_ms: int = DEFAULT_TIMEOUT_MS,
                    poll_ms: int = DEFAULT_POLL_MS,
                    hook_timeout_s: int | None = None,
                    template: str | os.PathLike[str] = TEMPLATE_PATH,
                    out_path: str | os.PathLike[str] | None = None) -> Path:
    """Write $RUN_DIR/settings.json and prove it is loadable and correct.

    Raises SettingsError rather than returning a path to a file the CLI would
    silently ignore. Also creates gate/req, gate/resp and watch/ so the
    first barrier does not race the driver over mkdir.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("gate", "gate/req", "gate/resp", "watch"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)

    obj = build_settings(run_dir, gate_py=gate_py, python_exe=python_exe, mode=mode,
                         timeout_ms=timeout_ms, poll_ms=poll_ms,
                         hook_timeout_s=hook_timeout_s, template=template)
    problems = validate_settings(obj, run_dir=run_dir)
    if problems:
        raise SettingsError("rendered settings are invalid: " + "; ".join(problems))

    path = Path(out_path) if out_path else settings_path(run_dir)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)

    # Re-read from disk. The renderer's own opinion of what it wrote is not
    # evidence; what the CLI will parse is.
    try:
        reread = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SettingsError(f"rendered settings do not parse from disk: {path}: {exc}") from exc
    problems = validate_settings(reread, run_dir=run_dir)
    if problems:
        raise SettingsError(f"settings written to {path} are invalid: " + "; ".join(problems))
    return path


def check_settings_file(path: str | os.PathLike[str], *,
                        run_dir: str | os.PathLike[str] | None = None,
                        require_barrier: bool = False) -> list[str]:
    """Problems with the settings file as it sits on disk. Never raises."""
    p = Path(path)
    if not p.is_file():
        return [f"settings file missing: {p}"]
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [f"settings file does not parse: {p}: {exc}"]
    return validate_settings(obj, run_dir=run_dir, require_barrier=require_barrier)


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="render the per-run Claude Code settings file")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--gate-py", default=str(GATE_PATH))
    p.add_argument("--python", dest="python_exe", default=None)
    p.add_argument("--mode", default="barrier", choices=["barrier", "log"])
    p.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    p.add_argument("--poll-ms", type=int, default=DEFAULT_POLL_MS)
    p.add_argument("--hook-timeout-s", type=int, default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--check", action="store_true",
                   help="validate an existing settings file instead of rendering")
    p.add_argument("--print", dest="print_json", action="store_true",
                   help="print the settings JSON on stdout instead of the path")
    a = p.parse_args(argv)

    if a.check:
        path = Path(a.out) if a.out else settings_path(a.run_dir)
        problems = check_settings_file(path, run_dir=a.run_dir)
        for prob in problems:
            print(f"SETTINGS: {prob}", file=sys.stderr)
        print(str(path))
        return 1 if problems else 0

    try:
        path = render_settings(
            a.run_dir, gate_py=a.gate_py, python_exe=a.python_exe, mode=a.mode,
            timeout_ms=a.timeout_ms, poll_ms=a.poll_ms, hook_timeout_s=a.hook_timeout_s,
            out_path=a.out,
        )
    except SettingsError as exc:
        print(f"SETTINGS: {exc}", file=sys.stderr)
        return 1
    if a.print_json:
        print(path.read_text(encoding="utf-8"), end="")
    else:
        print(str(path))
    print(f"settings rendered: {path} (sha256 {settings_sha256(path)[:12]}…)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
