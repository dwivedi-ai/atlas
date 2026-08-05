#!/usr/bin/env python3
"""
driver.py — the parent process of the Claude Code child for one WUR run.

RESPONSIBILITY
  Own the child from launch to exit. The driver is the child's PARENT, never a
  downstream pipe stage (§6.1): a tap in the pipe means a tap crash kills the
  agent with SIGPIPE and truncates the only copy of the raw stream. It does four
  things and nothing else —

    1. capture   every byte the child writes on stdout, VERBATIM, to
                 $RUN_DIR/stream.jsonl BEFORE anything parses it (fsync every 16
                 lines). The live parse runs on a separate thread behind a
                 bounded queue and every parse is try/except-wrapped, so a parse
                 bug can never reach the reader loop.
    2. gate      serve the PreToolUse barrier: poll gate/req/<tool_use_id>.json,
                 decide, write gate/resp/<tool_use_id>.json. The barrier IS the
                 step budget — there is no --max-turns in 2.1.222.
    3. inject    write probe / retry / resume user messages on the child's stdin
                 (the stream-json USER channel — V1/V2: the hook channel is
                 refused as prompt injection and must never carry probe text).
    4. terminate close stdin, then drain. NEVER wait() before closing stdin (V15:
                 the child does not exit after `result` and the driver hangs
                 forever).

ORDERING IS NORMATIVE (V13)
  Inject on stdin, THEN release the barrier. Holding the barrier until the probe
  is answered DEADLOCKS: 20 s held produced zero child output and the injected
  message was not even replayed until the hook returned. No gate response may
  depend on model output — every decision here is a pure function of counters.

INPUTS
  $RUN_DIR/settings.json     the ONLY source of this run's hooks (required)
  $RUN_DIR/claude_home/      CLAUDE_CONFIG_DIR, credentials-only (required)
  $RUN_DIR/run_meta.json     condition, session id, budget, probe config (optional)
  $RUN_DIR/probe_plan.json   the frozen cadence; regenerated from cadence.py only
                             if absent and (task_id, rep) are known (optional)
  $RUN_DIR/workspace/        the child's cwd
  the task prompt            --task-prompt / --task-prompt-file / $TASK_PROMPT

OUTPUTS  (every path under $RUN_DIR; nothing is ever written inside workspace/)
  stream.jsonl               VERBATIM child stdout — the ground truth
  watch/stream.err           verbatim child stderr
  gate/resp/<tid>.json       one barrier decision per tool call
  gate/decisions.jsonl       the driver's own decision log (one writer: this one)
  probe_sends.jsonl          every message injected on stdin, with its barrier
  driver.log                 human-readable timeline
  driver_summary.json        live-parsed counters: tokens (deduped by message.id),
                             pacing, probes, gate, termination — the free
                             correctness checks §8.1/§10 ask for
  agent_exit_code            written by THIS process; the driver always exits 0,
                             which is the contract lib/run_job.sh depends on

WHAT THIS MODULE DELIBERATELY DOES NOT DO
  No derivation. events.jsonl / exposure.jsonl / probes.jsonl / fact_trace.jsonl
  are produced offline by reconcile.py from stream.jsonl + transcript.jsonl, and
  must remain re-derivable months later after a scanner bugfix (§5.1(3)). The
  counters in driver_summary.json are a live cross-check, not a substitute.

CLI
  python3 lib/wur/driver.py --run-dir $RUN_DIR --task-prompt-file task.txt
  python3 lib/wur/driver.py --run-dir $RUN_DIR --print-argv     # no child launched
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

LIB_WUR = Path(__file__).resolve().parent
LIB_DIR = LIB_WUR.parent
for _p in (str(LIB_DIR), str(LIB_WUR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:  # `from wur import ...` when lib/ is on sys.path
    from wur import cadence, protocol  # type: ignore
except Exception:  # flat import when lib/wur/ is on sys.path (gate.py style)
    import cadence  # type: ignore
    import protocol  # type: ignore

DRIVER_VERSION = "wur-driver-v1"

# The frozen six (V10). --tools silently ignores names it does not honour, so
# preflight asserts the realized set against this list; the driver only sends it.
TOOLS_ALLOWLIST: tuple[str, ...] = ("Bash", "Read", "Write", "Edit", "Glob", "Grep")

# ── tunables (every one of these is a measurement, not a guess) ──────────────
FSYNC_EVERY_LINES = 16          # raw-before-derived durability (§5.1(3))
GATE_POLL_S = 0.005             # matches the hook's own 5 ms poll (§6.2 step 4)
MAIN_POLL_S = 0.05
PARSE_QUEUE_MAX = 20_000        # bounded: a wedged parser must not eat RAM
DEFAULT_HOOKS_ALIVE_TIMEOUT_S = 90.0    # V12: silently-ignored settings file
DEFAULT_STALL_TIMEOUT_S = 900.0
DEFAULT_DRAIN_TIMEOUT_S = 180.0         # measured drain after stdin close: 20.6 s
DEFAULT_TERM_GRACE_S = 20.0
DEFAULT_KILL_GRACE_S = 10.0
DEFAULT_GATE_TIMEOUT_MS = 300_000       # §6.2 step 4 default
DEFAULT_BUDGET_STEPS = 60
DEFAULT_MAX_RETRIES = 1                 # RETRY_TEXT re-asks per probe
DEFAULT_MAX_RESUMES = 3                 # §6.2: capped at 3 CONSECUTIVE
TIMEOUT_EXIT_CODE = 124                 # telemetry.py maps 124 -> terminal "timeout"
ABORT_EXIT_CODE = 70                    # driver-side abort (hooks dead, no child, ...)


# ── small helpers ────────────────────────────────────────────────────────────
def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts if ts is not None else time.time())) + "Z"


def _dig(obj: Any, *paths: str, default: Any = None) -> Any:
    """First present, non-null value among dotted `paths` in a nested dict."""
    for path in paths:
        cur = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


def _read_json(path: Path) -> Any:
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _write_json_atomic(path: Path, obj: Any) -> None:
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else None


def _as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    return None


def _first(*vals: Any) -> Any:
    for v in vals:
        if v is not None:
            return v
    return None


def _uuid_for(run_id: str) -> str:
    """A deterministic session UUID derived from run_id.

    Deterministic on purpose: teardown copies the transcript BY --session-id
    (§5.2 step 2), so the id must be reconstructible if run_meta.json is lost.
    uuid4 is banned as measured randomness (§6.2) and this is not randomness.
    """
    digest = hashlib.sha256(("wur-session|" + run_id).encode()).digest()[:16]
    return str(uuid.UUID(bytes=digest, version=4))


# ── configuration ────────────────────────────────────────────────────────────
@dataclass
class DriverConfig:
    """Everything the driver needs, resolved CLI > env > run_meta.json > default."""

    run_dir: Path
    workspace: Path
    task_prompt: str
    run_id: str
    session_id: str
    settings_path: Path
    config_dir: Path
    model: str = "claude-sonnet-5"
    claude_bin: str = "claude"
    effort: str | None = None
    budget_steps: int = DEFAULT_BUDGET_STEPS
    max_usd: float = 0.0
    max_seconds: float = 0.0
    probe_enabled: bool = True
    max_retries: int = DEFAULT_MAX_RETRIES
    max_resumes: int = DEFAULT_MAX_RESUMES
    gate_timeout_ms: int = DEFAULT_GATE_TIMEOUT_MS
    hooks_alive_timeout_s: float = DEFAULT_HOOKS_ALIVE_TIMEOUT_S
    stall_timeout_s: float = DEFAULT_STALL_TIMEOUT_S
    drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S
    term_grace_s: float = DEFAULT_TERM_GRACE_S
    kill_grace_s: float = DEFAULT_KILL_GRACE_S
    task_id: str | None = None
    rep: int | None = None
    condition_id: str | None = None
    probe_lo: int = cadence.DEFAULT_LO
    probe_hi: int = cadence.DEFAULT_HI
    probe_max: int = cadence.DEFAULT_MAX_PROBES
    probe_salt: str = cadence.DEFAULT_SALT
    require_config_dir: bool = True


def build_config(args: argparse.Namespace) -> DriverConfig:
    """Resolve the run's configuration. Precedence: CLI > env > run_meta > default."""
    run_dir = Path(_first(args.run_dir, _env("RUN_DIR")) or "").expanduser()
    if not str(run_dir):
        raise SystemExit("driver: --run-dir (or $RUN_DIR) is required")
    run_dir = run_dir.resolve()
    meta = _read_json(run_dir / "run_meta.json") or {}

    workspace = Path(
        _first(args.workspace, _env("WORKSPACE"), _dig(meta, "workspace"), str(run_dir / "workspace"))
    ).expanduser()

    prompt = args.task_prompt
    if prompt is None and args.task_prompt_file:
        prompt = Path(args.task_prompt_file).read_text(encoding="utf-8")
    if prompt is None:
        pf = _env("TASK_PROMPT_FILE")
        if pf and Path(pf).exists():
            prompt = Path(pf).read_text(encoding="utf-8")
    if prompt is None:
        prompt = _first(_env("TASK_PROMPT"), _dig(meta, "task_prompt", "task.prompt"))
    if not prompt:
        raise SystemExit("driver: a task prompt is required (--task-prompt/--task-prompt-file/$TASK_PROMPT)")

    run_id = str(_first(args.run_id, _env("EXPERIMENT_RUN_ID"), _dig(meta, "run_id", "run.run_id"), run_dir.name))
    session_id = str(
        _first(args.session_id, _env("SESSION_UUID"), _dig(meta, "session_id", "run.session_id"), _uuid_for(run_id))
    )
    model = str(
        _first(args.model, _env("WUR_MODEL"), _dig(meta, "model", "agent.model", "condition.model"), "claude-sonnet-5")
    )
    # AGENT_ID is the ladder harness's name for the same thing; accept it only as
    # a claude-* value so a codex/gemini id can never be handed to `claude`.
    agent_id = _env("AGENT_ID")
    if args.model is None and _env("WUR_MODEL") is None and agent_id and agent_id.startswith("claude-"):
        model = agent_id

    probe_enabled = _first(
        _as_bool(args.probe) if args.probe is not None else None,
        _as_bool(_env("WUR_PROBE_ENABLED")),
        _as_bool(_dig(meta, "probe.enabled", "condition.factors.probe", "factors.probe")),
        True,
    )

    def _num(cli, env_name, *meta_paths, default, cast=int):
        raw = _first(cli, _env(env_name), _dig(meta, *meta_paths), default)
        try:
            return cast(raw)
        except Exception:
            return cast(default)

    cfg = DriverConfig(
        run_dir=run_dir,
        workspace=workspace,
        task_prompt=prompt,
        run_id=run_id,
        session_id=session_id,
        settings_path=Path(_first(args.settings, str(run_dir / "settings.json"))),
        config_dir=Path(_first(args.config_dir, _env("CLAUDE_CONFIG_DIR"), str(run_dir / "claude_home"))),
        model=model,
        claude_bin=str(_first(args.claude_bin, _env("WUR_CLAUDE_BIN"), "claude")),
        effort=_first(args.effort, _env("WUR_EFFORT"), _dig(meta, "agent.effort")),
        budget_steps=_num(args.budget_steps, "WUR_BUDGET_STEPS", "budget.steps", default=DEFAULT_BUDGET_STEPS),
        max_usd=_num(args.max_usd, "WUR_MAX_USD", "budget.max_usd", default=0.0, cast=float),
        max_seconds=_num(args.max_seconds, "MAX_SECONDS", "budget.max_seconds", default=0.0, cast=float),
        probe_enabled=bool(probe_enabled),
        max_retries=_num(args.max_retries, "WUR_PROBE_MAX_RETRIES", "probe.max_retries", default=DEFAULT_MAX_RETRIES),
        max_resumes=_num(args.max_resumes, "WUR_MAX_RESUMES", "probe.max_resumes", default=DEFAULT_MAX_RESUMES),
        gate_timeout_ms=_num(
            args.gate_timeout_ms, "WUR_GATE_TIMEOUT_MS", "probe.gate_timeout_ms", default=DEFAULT_GATE_TIMEOUT_MS
        ),
        hooks_alive_timeout_s=_num(
            args.hooks_alive_timeout, "WUR_HOOKS_ALIVE_TIMEOUT_S", default=DEFAULT_HOOKS_ALIVE_TIMEOUT_S, cast=float
        ),
        stall_timeout_s=_num(args.stall_timeout, "WUR_STALL_TIMEOUT_S", default=DEFAULT_STALL_TIMEOUT_S, cast=float),
        drain_timeout_s=_num(args.drain_timeout, "WUR_DRAIN_TIMEOUT_S", default=DEFAULT_DRAIN_TIMEOUT_S, cast=float),
        task_id=_first(args.task_id, _env("TASK_ID"), _dig(meta, "task_id", "condition.task_id")),
        rep=_first(args.rep, _dig(meta, "rep", "run.replication")),
        condition_id=_first(args.condition_id, _env("CONDITION_ID"), _dig(meta, "condition_id", "condition.env_id")),
        probe_lo=_num(None, "WUR_PROBE_LO", "probe.lo", default=cadence.DEFAULT_LO),
        probe_hi=_num(None, "WUR_PROBE_HI", "probe.hi", default=cadence.DEFAULT_HI),
        probe_max=_num(None, "WUR_PROBE_MAX", "probe.max_probes", default=cadence.DEFAULT_MAX_PROBES),
        probe_salt=str(_first(_env("WUR_PROBE_SALT"), _dig(meta, "probe.salt"), cadence.DEFAULT_SALT)),
        require_config_dir=not args.allow_missing_home,
    )
    if cfg.rep is not None:
        try:
            cfg.rep = int(cfg.rep)
        except Exception:
            cfg.rep = None
    return cfg


def build_argv(cfg: DriverConfig) -> list[str]:
    """The §5.2 command line, built from a LIST — never a shell string.

    Order follows §5.2 so a diff against the spec is readable. --max-budget-usd
    and --effort are conditional; everything else is unconditional and any change
    to it changes cli_argv_sha256, which is exactly the point (§5.1(6)).
    """
    argv = [
        cfg.claude_bin,
        "--print",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--replay-user-messages",
        "--include-hook-events",
        "--append-system-prompt", protocol.PACING_PROMPT,
        "--settings", str(cfg.settings_path),
        "--setting-sources", "project",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--tools", ",".join(TOOLS_ALLOWLIST),
        "--session-id", cfg.session_id,
        "--model", cfg.model,
        "--permission-mode", "bypassPermissions",
        "--autocompact", "1000000",
    ]
    if cfg.max_usd and cfg.max_usd > 0:
        argv += ["--max-budget-usd", f"{cfg.max_usd:g}"]
    if cfg.effort:
        argv += ["--effort", str(cfg.effort)]
    return argv


def canonical_argv(argv: Sequence[str], cfg: DriverConfig) -> list[str]:
    """argv with the four per-run varying values replaced by placeholders.

    Same lesson as init_sha256 (V18): a hash of the raw argv differs on every run
    and defeats its own purpose. The canonical form is what makes
    cli_argv_sha256 comparable across runs; the raw argv is recorded verbatim
    beside it.
    """
    subs = [
        (str(cfg.run_dir), "$RUN_DIR"),
        (str(cfg.workspace), "$WORKSPACE"),
        (str(cfg.config_dir), "$CLAUDE_CONFIG_DIR"),
        (cfg.session_id, "$SESSION_UUID"),
    ]
    out = []
    for i, a in enumerate(argv):
        s = "claude" if i == 0 else a
        for old, new in subs:
            if old:
                s = s.replace(old, new)
        out.append(s)
    return out


# ── per-probe state ──────────────────────────────────────────────────────────
@dataclass
class ProbeState:
    """One probe's realized life. Mirrors the probes.jsonl fields the DRIVER knows.

    probes.py re-derives the authoritative row from stream.jsonl; these are the
    transport facts only this process can observe (when it went out, at which
    barrier, whether a retry was sent).
    """

    idx: int
    probe_id: str
    planned_at_barrier: int | None = None
    sampled_interval: int | None = None
    sent_at_barrier: int | None = None
    sent_ts: float | None = None
    answered_ts: float | None = None
    outcome: str = "unanswered"          # answered | superseded | unanswered | refused
    parse_ok: bool | None = None
    parse_tier: str | None = None
    echoed_probe_id: str | None = None
    retry_sent: bool = False
    answered_after_retry: bool = False
    saw_echo: bool = False
    refusal_markers: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)
    raw_response: str | None = None

    def to_dict(self) -> dict:
        """The probe's transport record, as driver_summary.json carries it."""
        d = dict(self.__dict__)
        d.pop("saw_echo", None)
        d["sent_ts_iso"] = _iso(self.sent_ts) if self.sent_ts else None
        d["answered_ts_iso"] = _iso(self.answered_ts) if self.answered_ts else None
        return d


# ── the driver ───────────────────────────────────────────────────────────────
class Driver:
    """One run. Construct, call run(), read exit_code / summary."""

    def __init__(self, cfg: DriverConfig) -> None:
        self.cfg = cfg
        rd = cfg.run_dir
        self.gate_dir = rd / "gate"
        self.req_dir = self.gate_dir / "req"
        self.resp_dir = self.gate_dir / "resp"
        self.watch_dir = rd / "watch"
        for d in (self.gate_dir, self.req_dir, self.resp_dir, self.watch_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.stream_path = rd / "stream.jsonl"
        self.stderr_path = self.watch_dir / "stream.err"
        self.log_path = rd / "driver.log"
        self.sends_path = rd / "probe_sends.jsonl"
        self.decisions_path = self.gate_dir / "decisions.jsonl"
        self.summary_path = rd / "driver_summary.json"
        self.exit_code_path = rd / "agent_exit_code"

        self.t0 = time.time()
        self.child: subprocess.Popen | None = None
        self.exit_code_raw: int | None = None
        self.exit_code: int = ABORT_EXIT_CODE
        self.abort_reason: str | None = None
        self.termination_reason: str | None = None
        self.term_ladder: list[str] = []
        self.errors: list[str] = []

        # ── live-parse state (all under _lock) ──
        self._lock = threading.Lock()
        self._stdin_lock = threading.Lock()
        self._stdin_closed = False
        self._stdin_closed_at: float | None = None
        self._stop = threading.Event()
        self._finish_evt = threading.Event()
        self._actions: queue.Queue[tuple] = queue.Queue()
        self._parse_q: queue.Queue[bytes | None] = queue.Queue(maxsize=PARSE_QUEUE_MAX)

        self.stream_lines = 0
        self.unparsed_lines = 0
        self.parse_dropped = 0
        self.parse_errors = 0
        self.last_line_ts = self.t0

        self.barriers = 0                       # distinct tool_use_ids at the barrier
        self.barrier_requests = 0               # req files served, INCLUDING repeats
        self.repeat_requests = 0
        self.unparseable_requests = 0
        self.sidechain_barriers = 0
        self.denials = 0
        self.seen_tids: dict[str, dict] = {}
        self.budget_exhausted = False
        self.transcript_path: str | None = None

        self.probes: list[ProbeState] = []
        self.next_probe_idx = 0
        self.pending_probe: ProbeState | None = None
        self._answer_buf: list[str] = []
        self.resumes_sent = 0
        self.consecutive_resumes = 0
        self.max_consecutive_resumes = 0
        self.retries_sent = 0

        self.tool_uses_by_mid: dict[str, int] = {}
        self.usage_by_mid: dict[str, dict] = {}
        self.tool_use_ids: set[str] = set()
        self.tool_uses_since_result = 0
        self.results: list[dict] = []
        self.permission_denials = 0
        self.init_event: dict | None = None
        self.cli_version: str | None = None

        self.hooks_alive = False
        self.hooks_alive_after_s: float | None = None

        # probe plan
        self.fire_at: list[int] = []
        self.intervals: list[int] = []
        self.probe_plan_source = "none"

        self._log_fh = open(self.log_path, "a", encoding="utf-8")

    # ── logging ──
    def log(self, kind: str, detail: str = "") -> None:
        """One timestamped line to driver.log AND to stderr.

        Non-empty stderr is NOT a failure signal for this harness (V19); the
        health check is $RUN_DIR/agent_exit_code plus driver_summary.json.
        """
        line = f"[{time.time() - self.t0:8.2f}] {kind}: {detail}"
        try:
            self._log_fh.write(line + "\n")
            self._log_fh.flush()
        except Exception:
            pass
        print(line, file=sys.stderr, flush=True)

    def _append_jsonl(self, path: Path, obj: dict) -> None:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
                fh.flush()
        except Exception as exc:  # never let bookkeeping kill a run
            self.errors.append(f"append {path.name}: {exc}")

    # ── probe plan ──
    def _load_probe_plan(self) -> None:
        plan_path = self.cfg.run_dir / "probe_plan.json"
        plan = _read_json(plan_path)
        if isinstance(plan, dict) and isinstance(plan.get("fire_at"), list):
            self.fire_at = [int(x) for x in plan["fire_at"]]
            self.intervals = [int(x) for x in plan.get("intervals") or []]
            self.probe_plan_source = "probe_plan.json"
            return
        if not self.cfg.probe_enabled:
            self.probe_plan_source = "none (probe disabled)"
            return
        if self.cfg.task_id is None or self.cfg.rep is None:
            self.log(
                "probe-plan",
                "MISSING probe_plan.json and no (task_id, rep) to regenerate it — probes DISABLED",
            )
            self.cfg.probe_enabled = False
            self.probe_plan_source = "none (unavailable)"
            return
        plan = cadence.schedule(
            self.cfg.task_id,
            self.cfg.rep,
            self.cfg.budget_steps,
            lo=self.cfg.probe_lo,
            hi=self.cfg.probe_hi,
            max_probes=self.cfg.probe_max,
            salt=self.cfg.probe_salt,
        )
        # setup_run.sh owns this file (§5.2); regenerating it is a fallback for
        # standalone/driver-only invocation and is logged loudly.
        _write_json_atomic(plan_path, plan)
        self.fire_at = list(plan["fire_at"])
        self.intervals = list(plan["intervals"])
        self.probe_plan_source = "regenerated by driver (cadence.schedule)"
        self.log("probe-plan", f"probe_plan.json was absent; regenerated: fire_at={self.fire_at}")

    # ── preconditions ──
    def _preflight(self) -> str | None:
        """Cheap, local, fail-closed checks. Returns an abort reason or None."""
        if not self.cfg.workspace.is_dir():
            return f"workspace missing: {self.cfg.workspace}"
        if not self.cfg.settings_path.is_file():
            return f"settings.json missing: {self.cfg.settings_path} (no settings ⇒ no hooks ⇒ no barriers, V12)"
        if _read_json(self.cfg.settings_path) is None:
            return f"settings.json does not parse: {self.cfg.settings_path} (silently ignored in --print, V12)"
        if self.cfg.require_config_dir and not self.cfg.config_dir.is_dir():
            return f"CLAUDE_CONFIG_DIR missing: {self.cfg.config_dir}"
        problems = protocol.verify_frozen()
        if problems:
            return "frozen protocol strings drifted: " + "; ".join(problems)
        # A retry into a dirty run dir must not inherit the previous attempt's
        # liveness marker — that would mask exactly the V12 failure the 90 s
        # watchdog exists to catch. The SessionStart hook rewrites it.
        for stale in (self.watch_dir / "hooks_alive", self.gate_dir / "hooks_alive"):
            if stale.exists():
                try:
                    stale.unlink()
                    self.log("preflight", f"removed stale {stale.name} from a previous attempt")
                except Exception as exc:
                    return f"cannot clear stale {stale}: {exc}"
        if self.stream_path.exists() and self.stream_path.stat().st_size:
            # Append, never truncate: raw bytes are never destroyed (§5.1(3)).
            self.log(
                "preflight",
                f"stream.jsonl already holds {self.stream_path.stat().st_size} bytes from a previous "
                "attempt — this run APPENDS; reconcile.py must treat the file as multi-session",
            )
        leftovers = len([p for p in self.resp_dir.glob("*.json")])
        if leftovers:
            self.log("preflight", f"{leftovers} stale gate/resp files present (tool_use_ids are session-unique)")
        return None

    # ── child ──
    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Subscription path, not a stray API key (§7.2 billing; run_agent.sh does
        # the same with `env -u`).
        env.pop("ANTHROPIC_API_KEY", None)
        env["CLAUDE_CONFIG_DIR"] = str(self.cfg.config_dir)
        # Consumed by gate.py when the settings template is env-bound rather than
        # argv-bound; harmless when it is argv-bound.
        env["WUR_RUN_DIR"] = str(self.cfg.run_dir)
        env["WUR_GATE_DIR"] = str(self.gate_dir)
        env["WUR_WATCH_DIR"] = str(self.watch_dir)
        env["WUR_PROBE_MODE"] = "gate"        # "log" is the non-WUR pass-through mode
        env["WUR_GATE_TIMEOUT_MS"] = str(self.cfg.gate_timeout_ms)
        env["WUR_RUN_ID"] = self.cfg.run_id
        env["WUR_SESSION_ID"] = self.cfg.session_id
        env["EXPERIMENT_RUN_ID"] = self.cfg.run_id
        return env

    def _start_child(self, argv: list[str]) -> None:
        self.log("child", f"exec {argv[0]} ({len(argv)} argv items) cwd={self.cfg.workspace}")
        self.child = subprocess.Popen(
            argv,
            cwd=str(self.cfg.workspace),
            env=self._child_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.log("child", f"started pid={self.child.pid}")

    # ── threads ──
    def _reader(self) -> None:
        """Raw before derived: write the byte, THEN hand it to the parser.

        This loop contains no json, no protocol logic and no branching on
        content. It is the only writer of stream.jsonl.
        """
        assert self.child is not None and self.child.stdout is not None
        n = 0
        try:
            with open(self.stream_path, "ab") as fh:
                for raw in self.child.stdout:
                    fh.write(raw)
                    fh.flush()
                    n += 1
                    if n % FSYNC_EVERY_LINES == 0:
                        os.fsync(fh.fileno())
                    with self._lock:
                        self.stream_lines = n
                        self.last_line_ts = time.time()
                    try:
                        self._parse_q.put_nowait(raw)
                    except queue.Full:
                        with self._lock:
                            self.parse_dropped += 1
                os.fsync(fh.fileno())
        except Exception as exc:
            self.errors.append(f"reader: {exc}")
            self.log("reader", f"ERROR {exc}")
        finally:
            try:
                self._parse_q.put_nowait(None)
            except queue.Full:
                pass
            self.log("reader", f"child stdout closed after {n} lines")
            self._actions.put(("eof", "stdout"))

    def _stderr_reader(self) -> None:
        assert self.child is not None and self.child.stderr is not None
        try:
            with open(self.stderr_path, "ab") as fh:
                for raw in self.child.stderr:
                    fh.write(raw)
                    fh.flush()
        except Exception as exc:
            self.errors.append(f"stderr reader: {exc}")

    def _parser(self) -> None:
        """Minimal live parse. Every line is wrapped; nothing here can raise out."""
        while True:
            try:
                raw = self._parse_q.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            if raw is None:
                return
            try:
                obj = json.loads(raw)
            except Exception:
                with self._lock:
                    self.unparsed_lines += 1
                continue
            try:
                self._handle_event(obj)
            except Exception as exc:
                with self._lock:
                    self.parse_errors += 1
                self.errors.append(f"live parse: {exc}")

    # ── live parse ──
    def _handle_event(self, obj: dict) -> None:
        if not isinstance(obj, dict):
            return
        etype = obj.get("type")
        if etype == "assistant":
            self._on_assistant(obj)
        elif etype == "result":
            self._on_result(obj)
        elif etype == "system":
            if obj.get("subtype") == "init":
                with self._lock:
                    if self.init_event is None:
                        self.init_event = obj
                        self.cli_version = obj.get("claude_code_version")

    def _on_assistant(self, obj: dict) -> None:
        msg = obj.get("message") or {}
        mid = msg.get("id")
        content = msg.get("content") or []
        if not isinstance(content, list):
            content = []
        tool_uses = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]
        texts = [c.get("text") or "" for c in content if isinstance(c, dict) and c.get("type") == "text"]

        with self._lock:
            if mid:
                # V17: stream.jsonl splits ONE assistant message across lines
                # exactly like the transcript. Group by message.id or inherit V7.
                self.tool_uses_by_mid[mid] = self.tool_uses_by_mid.get(mid, 0) + len(tool_uses)
                usage = msg.get("usage")
                if isinstance(usage, dict) and mid not in self.usage_by_mid:
                    self.usage_by_mid[mid] = usage
            for tu in tool_uses:
                tid = tu.get("id")
                if tid:
                    self.tool_use_ids.add(tid)
            self.tool_uses_since_result += len(tool_uses)
            pending = self.pending_probe

        if texts:
            self._on_assistant_text("\n".join(texts))
        if tool_uses and pending is not None:
            # The model went back to work; if it echoed the probe id and the reply
            # never parsed, this is the moment to re-ask (RETRY_TEXT).
            self._maybe_retry(pending)

    def _on_assistant_text(self, text: str) -> None:
        with self._lock:
            probe = self.pending_probe
            if probe is None:
                return
            looks_like_answer = (probe.probe_id in text) or ('"facts"' in text and "probe_id" in text)
            if not looks_like_answer and not self._answer_buf:
                return
            if probe.probe_id in text:
                probe.saw_echo = True
            self._answer_buf.append(text)
            buf = "\n".join(self._answer_buf)

        parsed = protocol.parse_answer(buf, expect_probe_id=probe.probe_id)
        markers = protocol.refusal_markers(buf)
        with self._lock:
            probe.raw_response = buf
            probe.parse_ok = parsed.parse_ok
            probe.parse_tier = parsed.parse_tier
            probe.echoed_probe_id = parsed.probe_id
            probe.parse_errors = list(parsed.errors)
            probe.refusal_markers = markers
            if parsed.parse_ok:
                probe.outcome = "answered"
                probe.answered_ts = time.time()
                if probe.retry_sent:
                    probe.answered_after_retry = True
                self.pending_probe = None
                self._answer_buf = []
                done = True
            else:
                if markers:
                    probe.outcome = "refused"
                done = False
        if done:
            self.log(
                "probe",
                f"{probe.probe_id} ANSWERED tier={probe.parse_tier} "
                f"echo={probe.echoed_probe_id} retry={probe.retry_sent}",
            )

    def _maybe_retry(self, probe: ProbeState) -> None:
        with self._lock:
            if probe.outcome == "answered" or probe.retry_sent:
                return
            if not probe.saw_echo:
                return
            if self.cfg.max_retries <= 0:
                return
            probe.retry_sent = True
        self._actions.put(("retry", probe.idx))

    def _on_result(self, obj: dict) -> None:
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        denials = obj.get("permission_denials")
        with self._lock:
            n_tools = self.tool_uses_since_result
            self.tool_uses_since_result = 0
            pending = self.pending_probe
            self.results.append(
                {
                    "subtype": obj.get("subtype"),
                    "is_error": obj.get("is_error"),
                    "stop_reason": obj.get("stop_reason"),
                    "terminal_reason": obj.get("terminal_reason"),
                    "num_turns": obj.get("num_turns"),
                    "total_cost_usd": obj.get("total_cost_usd"),
                    "tool_uses_in_segment": n_tools,
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
                    "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "ts": time.time(),
                }
            )
            if isinstance(denials, list):
                self.permission_denials = max(self.permission_denials, len(denials))
            terminating = self._finish_evt.is_set()
            is_error = bool(obj.get("is_error"))
            resumes = self.consecutive_resumes
        self.log(
            "result",
            f"subtype={obj.get('subtype')} is_error={obj.get('is_error')} "
            f"stop={obj.get('stop_reason')} tools_in_segment={n_tools} cost={obj.get('total_cost_usd')}",
        )
        if terminating:
            return
        if is_error:
            self._actions.put(("finish", "agent_error"))
            return
        if n_tools == 0:
            # A probe whose reply never parsed, on a turn that also did no work:
            # RETRY_TEXT ends with the same "resume the task ... without waiting
            # for me" clause, so the re-ask doubles as the resume. Bounded at
            # max_retries per probe, so this cannot loop.
            if (
                pending is not None
                and pending.outcome != "answered"
                and pending.saw_echo
                and not pending.retry_sent
                and self.cfg.max_retries > 0
            ):
                self._maybe_retry(pending)
                return
            # §6.2: zero-tool-call result ⇒ RESUME_TEXT, capped at 3 CONSECUTIVE.
            # Fires identically in the no-probe arms — it is not a probed-arm-only
            # intervention, or it confounds the probe-reactivity contrast.
            if resumes < self.cfg.max_resumes:
                self._actions.put(("resume", None))
            else:
                self._actions.put(("finish", "resume_cap"))
        else:
            with self._lock:
                self.consecutive_resumes = 0
            self._actions.put(("finish", "agent_complete"))

    # ── stdin injection ──
    def _send(self, text: str, kind: str, probe: ProbeState | None = None) -> bool:
        """Write one stream-json user message. The ONLY probe channel (V1/V2)."""
        payload = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self._stdin_lock:
            if self._stdin_closed or self.child is None or self.child.stdin is None:
                self.log("stdin", f"DROPPED {kind}: stdin already closed")
                return False
            try:
                self.child.stdin.write(line)
                self.child.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                self.errors.append(f"stdin write ({kind}): {exc}")
                self.log("stdin", f"WRITE FAILED {kind}: {exc}")
                return False
        with self._lock:
            barrier = self.barriers
        self._append_jsonl(
            self.sends_path,
            {
                "ts": time.time(),
                "ts_iso": _iso(),
                "elapsed_s": round(time.time() - self.t0, 3),
                "kind": kind,
                "probe_idx": probe.idx if probe else None,
                "probe_id": probe.probe_id if probe else None,
                "barrier": barrier,
                "bytes": len(line),
                "sha256": protocol.sha256(text),
            },
        )
        self.log("stdin", f"sent {kind}" + (f" {probe.probe_id}" if probe else "") + f" at barrier {barrier}")
        return True

    def _fire_probe(self, barrier: int) -> None:
        """Inject probe k. Called from the gate thread BEFORE the barrier is released."""
        with self._lock:
            if self.next_probe_idx >= len(self.fire_at):
                return
            k = self.next_probe_idx
            self.next_probe_idx += 1
            prev = self.pending_probe
            probe = ProbeState(
                idx=k,
                probe_id=protocol.probe_id(self.cfg.run_id, k),
                planned_at_barrier=self.fire_at[k],
                sampled_interval=self.intervals[k] if k < len(self.intervals) else None,
                sent_at_barrier=barrier,
                sent_ts=time.time(),
            )
            # §6.2: NEVER suppress. If k+1's barrier arrives while k is pending,
            # send k+1 anyway and mark k superseded.
            if prev is not None and prev.outcome == "unanswered":
                prev.outcome = "superseded"
            self.probes.append(probe)
            self.pending_probe = probe
            self._answer_buf = []
        if prev is not None and prev.outcome == "superseded":
            self.log("probe", f"{prev.probe_id} SUPERSEDED by {probe.probe_id}")
        ok = self._send(protocol.render_probe(probe.probe_id), "probe", probe)
        if not ok:
            with self._lock:
                probe.outcome = "unanswered"
                probe.sent_at_barrier = None
                probe.sent_ts = None

    # ── the barrier service ──
    def _gate_loop(self) -> None:
        """Poll gate/req/<tid>.json, decide, write gate/resp/<tid>.json.

        Every decision is a pure function of counters (§6.2): no gate response
        may depend on model output, because holding the barrier for an answer
        deadlocks (V13).
        """
        try:
            self._gate_loop_body()
        except Exception as exc:  # the barrier must never wedge a run
            self.errors.append(f"gate loop died: {exc}")
            self.log("gate", f"LOOP DIED ({exc}) — writing a standing order so no hook can wedge")
            self._write_broadcast("deny" if self.budget_exhausted else "allow",
                                  protocol.BUDGET_STOP_TEXT if self.budget_exhausted else None)

    def _gate_loop_body(self) -> None:
        pending_unparsed: dict[str, int] = {}
        while not self._stop.is_set():
            try:
                names = os.listdir(self.req_dir)
            except FileNotFoundError:
                time.sleep(GATE_POLL_S)
                continue
            except Exception as exc:
                self.errors.append(f"gate listdir: {exc}")
                time.sleep(0.1)
                continue
            work = [
                n
                for n in sorted(names)
                if n.endswith(".json") and ".tmp" not in n and not n.startswith(".")
            ]
            for name in work:
                tid = name[:-5]
                resp_path = self.resp_dir / name
                if resp_path.exists():
                    continue
                payload = _read_json(self.req_dir / name)
                if payload is None:
                    # A partially-written request: retry a few polls, then fail
                    # OPEN. Never wedge a run on harness failure (§6.2 step 6).
                    cnt = pending_unparsed.get(tid, 0) + 1
                    pending_unparsed[tid] = cnt
                    if cnt < 40:
                        continue
                    with self._lock:
                        self.unparseable_requests += 1
                    payload = {}
                pending_unparsed.pop(tid, None)
                try:
                    self._serve(tid, payload if isinstance(payload, dict) else {}, resp_path)
                except Exception as exc:
                    self.errors.append(f"gate serve {tid}: {exc}")
                    self._write_response(resp_path, tid, "allow", None, -1)
            time.sleep(GATE_POLL_S)

    def _serve(self, tid: str, payload: dict, resp_path: Path) -> None:
        # gate.py wraps the raw hook payload: {gate_key, barrier, tool_use_id,
        # tool_name, parent_tool_use_id, tool_use_id_repeats, payload{...}}. Read
        # the top level first and fall back to the nested payload, so a leaner
        # request (the S7 shape: the bare hook payload) also works.
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        real_tid = str(payload.get("tool_use_id") or inner.get("tool_use_id") or payload.get("tid") or tid)
        tool_name = payload.get("tool_name") or inner.get("tool_name") or payload.get("tool") or None
        parent = (
            payload.get("parent_tool_use_id")
            or inner.get("parent_tool_use_id")
            or payload.get("parentToolUseId")
        )
        tpath = payload.get("transcript_path") or inner.get("transcript_path")
        gate_barrier = payload.get("barrier")
        gate_repeats = payload.get("tool_use_id_repeats")

        with self._lock:
            self.barrier_requests += 1
            if tpath and not self.transcript_path:
                self.transcript_path = str(tpath)
            if parent:
                self.sidechain_barriers += 1
            first_time = real_tid not in self.seen_tids
            if first_time:
                # V14: a DENIED call is retried by the model, so barrier fires are
                # not tool-call ordinals — count DISTINCT tool_use_ids.
                self.barriers += 1
                barrier = self.barriers
                self.seen_tids[real_tid] = {"barrier": barrier, "tool_name": tool_name}
            else:
                # LOWER BOUND, by construction: gate.py reuses gate_key as the
                # response filename, so a re-fired barrier for an already-answered
                # tool_use_id reads the existing response and never reaches the
                # driver at all. The authoritative repeat count is gate.py's
                # tool_use_id_repeats / gate/tool_calls.jsonl, which is why
                # decisions.jsonl carries it.
                self.repeat_requests += 1
                barrier = self.seen_tids[real_tid]["barrier"]
            over_budget = self.cfg.budget_steps > 0 and barrier > self.cfg.budget_steps
            if over_budget and not self.budget_exhausted:
                self.budget_exhausted = True
                newly_exhausted = True
            else:
                newly_exhausted = False
            if over_budget:
                self.denials += 1

        fired: list[str] = []
        if not over_budget and first_time and self.cfg.probe_enabled:
            # ORDERING IS NORMATIVE (V13): inject on stdin, THEN release.
            while True:
                with self._lock:
                    k = self.next_probe_idx
                    due = k < len(self.fire_at) and self.fire_at[k] <= barrier
                if not due:
                    break
                before = len(self.probes)
                self._fire_probe(barrier)
                with self._lock:
                    if len(self.probes) == before:
                        break
                    fired.append(self.probes[-1].probe_id)

        decision = "deny" if over_budget else "allow"
        reason = protocol.BUDGET_STOP_TEXT if over_budget else None
        self._write_response(resp_path, real_tid, decision, reason, barrier)
        self._append_jsonl(
            self.decisions_path,
            {
                "ts": time.time(),
                "ts_iso": _iso(),
                "barrier": barrier,
                "gate_barrier": gate_barrier,        # gate.py's own ordinal (counts repeats)
                "gate_key": payload.get("gate_key"),
                "tool_use_id": real_tid,
                "tool_use_id_repeats": gate_repeats,
                "tool_name": tool_name,
                "decision": decision,
                "reason_kind": "budget_exhausted" if over_budget else None,
                "repeat": not first_time,
                "sidechain": bool(parent),
                "probes_fired": fired,
            },
        )
        if over_budget:
            self.log("gate", f"DENY barrier={barrier} tid={real_tid} (budget {self.cfg.budget_steps} spent)")
        if newly_exhausted:
            # V14: deny-with-reason does NOT stop a run — the model treats the
            # reason as injection and re-issues the call. The stop is
            # deny-every-subsequent-call PLUS closing stdin.
            self._write_broadcast("deny", protocol.BUDGET_STOP_TEXT)
            self._actions.put(("finish", "budget_exhausted"))

    def _write_broadcast(self, decision: str, reason: str | None) -> None:
        """gate/broadcast.json — the standing order for every unanswered barrier.

        gate.py falls back to this file when gate/resp/<key>.json is absent, which
        is how budget stop denies EVERY subsequent call (V14) without the driver
        having to race each new request, and how the post-stdin-close drain avoids
        burning a full gate timeout per in-flight tool call.
        """
        body = {
            "decision": decision,
            "permissionDecision": decision,
            "reason": reason,
            "permissionDecisionReason": reason,
            "ts": time.time(),
            "driver_pid": os.getpid(),
            "standing_order": True,
        }
        try:
            _write_json_atomic(self.gate_dir / "broadcast.json", body)
            self.log("gate", f"broadcast standing order: {decision}")
        except Exception as exc:
            self.errors.append(f"gate broadcast: {exc}")

    def _write_response(self, resp_path: Path, tid: str, decision: str, reason: str | None, barrier: int) -> None:
        body = {
            "tool_use_id": tid,
            "decision": decision,
            "permissionDecision": decision,   # gate.py may read either spelling
            "reason": reason,
            "permissionDecisionReason": reason,
            "barrier": barrier,
            "ts": time.time(),
            "driver_pid": os.getpid(),
        }
        tmp = resp_path.with_name(resp_path.name + f".tmp.{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(body, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, resp_path)
        except Exception as exc:
            self.errors.append(f"gate response {tid}: {exc}")

    # ── termination ──
    def _finish(self, reason: str) -> None:
        if not self._finish_evt.is_set():
            self.termination_reason = reason
            self._finish_evt.set()
            self.log("finish", f"reason={reason}")

    def _close_stdin(self) -> None:
        with self._stdin_lock:
            if self._stdin_closed:
                return
            self._stdin_closed = True
            self._stdin_closed_at = time.time()
            try:
                if self.child is not None and self.child.stdin is not None:
                    self.child.stdin.close()
            except Exception as exc:
                self.errors.append(f"stdin close: {exc}")
        self.term_ladder.append("close_stdin")
        self.log("term", "stdin closed (graceful drain: the in-flight turn completes, V15)")

    def _terminate(self, drain_s: float | None = None) -> int:
        """The termination ladder. NEVER wait() before closing stdin (V15)."""
        assert self.child is not None
        drain_s = self.cfg.drain_timeout_s if drain_s is None else drain_s
        self._close_stdin()
        try:
            rc = self.child.wait(timeout=drain_s)
            self.term_ladder.append(f"drained rc={rc}")
            self.log("term", f"child exited {rc} after {time.time() - (self._stdin_closed_at or 0):.2f}s drain")
            return rc
        except subprocess.TimeoutExpired:
            self.log("term", f"still alive {drain_s:.0f}s after stdin close — SIGTERM")
        self.term_ladder.append("sigterm")
        try:
            self.child.terminate()
            rc = self.child.wait(timeout=self.cfg.term_grace_s)
            self.term_ladder.append(f"sigterm rc={rc}")
            return rc
        except subprocess.TimeoutExpired:
            self.log("term", "SIGTERM ignored — SIGKILL")
        except Exception as exc:
            self.errors.append(f"terminate: {exc}")
        self.term_ladder.append("sigkill")
        try:
            self.child.kill()
            rc = self.child.wait(timeout=self.cfg.kill_grace_s)
        except Exception as exc:
            self.errors.append(f"kill: {exc}")
            rc = -9
        self.term_ladder.append(f"sigkill rc={rc}")
        return rc

    # ── main ──
    def run(self) -> int:
        """Launch, drive and terminate one child. Returns the recorded exit code.

        Order matters: preflight (fail closed, no API call) -> probe plan ->
        argv -> child -> threads -> task prompt -> main loop -> termination
        ladder -> finalize (agent_exit_code + driver_summary.json).
        """
        cfg = self.cfg
        self.log("driver", f"{DRIVER_VERSION} run_id={cfg.run_id} session={cfg.session_id}")
        self.log(
            "config",
            f"model={cfg.model} probe={cfg.probe_enabled} budget_steps={cfg.budget_steps} "
            f"max_usd={cfg.max_usd} max_seconds={cfg.max_seconds} gate_timeout_ms={cfg.gate_timeout_ms}",
        )
        abort = self._preflight()
        if abort:
            self.abort_reason = abort
            self.termination_reason = "preflight_abort"
            self.log("abort", abort)
            self._finalize(None)
            return self.exit_code

        self._load_probe_plan()
        argv = build_argv(cfg)
        self.argv = argv
        self.argv_canonical = canonical_argv(argv, cfg)

        try:
            self._start_child(argv)
        except Exception as exc:
            self.abort_reason = f"child failed to start: {exc}"
            self.termination_reason = "spawn_failed"
            self.log("abort", self.abort_reason)
            self._finalize(None)
            return self.exit_code

        threads = [
            threading.Thread(target=self._reader, name="reader", daemon=True),
            threading.Thread(target=self._stderr_reader, name="stderr", daemon=True),
            threading.Thread(target=self._parser, name="parser", daemon=True),
            threading.Thread(target=self._gate_loop, name="gate", daemon=True),
        ]
        for t in threads:
            t.start()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_a: self._actions.put(("finish", "signal")))
            except Exception:
                pass

        self._send(cfg.task_prompt, "task")
        self._main_loop()

        # Closing stdin is a graceful drain, not a stop (V15) — the in-flight turn
        # runs to completion. That is what we want on a normal finish, but an
        # aborted or timed-out run must not sit through a full drain window.
        if self.abort_reason:
            drain = min(cfg.drain_timeout_s, 15.0)
        elif self.termination_reason in ("max_seconds", "stall"):
            drain = min(cfg.drain_timeout_s, 60.0)
        else:
            drain = None
        rc = self._terminate(drain)
        self._stop.set()
        for t in threads:
            t.join(timeout=5.0)
        self._finalize(rc)
        return self.exit_code

    def _main_loop(self) -> None:
        cfg = self.cfg
        eof = False
        while True:
            if self.child is not None and self.child.poll() is not None:
                self.log("child", f"exited on its own: rc={self.child.returncode}")
                self._finish("child_exited")
                break
            try:
                while True:
                    kind, arg = self._actions.get_nowait()
                    if kind == "finish":
                        self._finish(str(arg))
                    elif kind == "resume":
                        self._do_resume()
                    elif kind == "retry":
                        self._do_retry(int(arg))
                    elif kind == "eof":
                        eof = True
            except queue.Empty:
                pass
            if eof:
                self._finish("stdout_eof")
            if self._finish_evt.is_set():
                break
            now = time.time()
            if not self.hooks_alive:
                if self._hooks_alive_marker():
                    self.hooks_alive = True
                    self.hooks_alive_after_s = round(now - self.t0, 2)
                    self.log("hooks", f"alive after {self.hooks_alive_after_s}s")
                elif now - self.t0 > cfg.hooks_alive_timeout_s:
                    # V12: a settings file that fails validation is SILENTLY
                    # ignored ⇒ zero hooks, zero barriers, zero probes, no error.
                    self.abort_reason = (
                        f"watch/hooks_alive did not appear within {cfg.hooks_alive_timeout_s:.0f}s — "
                        "the settings file was silently ignored (V12); this run has no barrier"
                    )
                    self.log("abort", self.abort_reason)
                    self._finish("hooks_not_alive")
                    break
            if cfg.max_seconds and now - self.t0 > cfg.max_seconds:
                self.log("watchdog", f"wall clock {cfg.max_seconds:.0f}s exceeded")
                self._finish("max_seconds")
                break
            with self._lock:
                idle = now - self.last_line_ts
            if cfg.stall_timeout_s and idle > cfg.stall_timeout_s:
                self.log("watchdog", f"no child output for {idle:.0f}s")
                self._finish("stall")
                break
            time.sleep(MAIN_POLL_S)

    def _hooks_alive_marker(self) -> bool:
        # §5.3 puts it in watch/; the gate dir is accepted as a fallback so a
        # template that keeps the S7 layout still starts.
        return (self.watch_dir / "hooks_alive").exists() or (self.gate_dir / "hooks_alive").exists()

    def _do_resume(self) -> None:
        with self._lock:
            if self.consecutive_resumes >= self.cfg.max_resumes:
                cap = True
            else:
                cap = False
                self.consecutive_resumes += 1
                self.resumes_sent += 1
                self.max_consecutive_resumes = max(self.max_consecutive_resumes, self.consecutive_resumes)
        if cap:
            self._finish("resume_cap")
            return
        if not self._send(protocol.RESUME_TEXT, "resume"):
            self._finish("stdin_closed")

    def _do_retry(self, idx: int) -> None:
        with self._lock:
            probe = self.probes[idx] if 0 <= idx < len(self.probes) else None
            if probe is None or probe.outcome == "answered":
                return
            self.retries_sent += 1
        self.log("probe", f"{probe.probe_id} unparsed — sending RETRY_TEXT")
        self._send(protocol.render_retry(probe.probe_id), "retry", probe)

    # ── accounting + summary ──
    def _token_accounting(self) -> dict:
        """Dedupe by message.id (V7) and check it against the `result` events.

        MEASURED HERE, and it refines V7 in two ways that matter (both verified on
        real 2.1.222 runs during this module's smoke test):

        1. `result.usage` is PER TURN, not cumulative — a two-turn run's second
           result carried exactly its own turn's tokens (4 / 33,977 / 656 =
           16,940 + 17,037 + 2 + 2), while `total_cost_usd` DID accumulate. So the
           authoritative total is the SUM over result events; `result_*_last` is
           kept only as a diagnostic. The §10 gate is
           `deduped_input == result_input_sum` — measured EXACT (51,579 == 51,579).
        2. The stream's per-assistant-message `usage.output_tokens` is a
           PLACEHOLDER (observed 1, 2 and 5 against a real 772 and 499). Output
           tokens are therefore NOT computable from stream.jsonl at all; the
           result events (or the on-disk transcript) are the only source. The
           equality gate applies to INPUT only, and `stream_output_unreliable`
           records the discrepancy instead of silently reporting 9 output tokens.
        """
        with self._lock:
            usages = dict(self.usage_by_mid)
            results = list(self.results)
        ded_in = ded_out = 0
        for u in usages.values():
            ded_in += int(u.get("input_tokens") or 0)
            ded_in += int(u.get("cache_read_input_tokens") or 0)
            ded_in += int(u.get("cache_creation_input_tokens") or 0)
            ded_out += int(u.get("output_tokens") or 0)
        r_last = results[-1] if results else {}
        last_in = int(r_last.get("input_tokens", 0)) + int(r_last.get("cache_read_input_tokens", 0)) + int(
            r_last.get("cache_creation_input_tokens", 0)
        )
        last_out = int(r_last.get("output_tokens", 0))
        sum_in = sum(
            int(r.get("input_tokens", 0))
            + int(r.get("cache_read_input_tokens", 0))
            + int(r.get("cache_creation_input_tokens", 0))
            for r in results
        )
        sum_out = sum(int(r.get("output_tokens", 0)) for r in results)
        cost = None
        for r in results:
            if r.get("total_cost_usd") is not None:
                cost = r["total_cost_usd"]
        return {
            "accounting_version": "wur-v2-message-id-dedupe",
            # what the analysis should use
            "authoritative_input": sum_in,
            "authoritative_output": sum_out,
            "cost_usd": cost,                      # total_cost_usd IS cumulative
            # the dedupe, and the free correctness check (§8.1 / §10)
            "deduped_input": ded_in,
            "deduped_output_stream": ded_out,      # placeholder values; see docstring
            "distinct_message_ids": len(usages),
            "n_results": len(results),
            "dedupe_delta_input": ded_in - sum_in,
            "dedupe_delta_output": ded_out - sum_out,
            "dedupe_equals_result_input": ded_in == sum_in,
            "stream_output_unreliable": ded_out != sum_out,
            # diagnostics
            "result_input_sum": sum_in,
            "result_output_sum": sum_out,
            "result_input_last": last_in,
            "result_output_last": last_out,
            "per_result": [
                {
                    "input": int(r.get("input_tokens", 0))
                    + int(r.get("cache_read_input_tokens", 0))
                    + int(r.get("cache_creation_input_tokens", 0)),
                    "output": int(r.get("output_tokens", 0)),
                    "cost_usd": r.get("total_cost_usd"),
                    "tool_uses_in_segment": r.get("tool_uses_in_segment"),
                }
                for r in results
            ],
        }

    def _canonical_init(self) -> tuple[str | None, dict | None]:
        """init_sha256 over a CANONICALIZED system/init (V18).

        cwd, memory_paths, session_id and uuid vary every run; hashing the raw
        event defeats its own purpose. With those four dropped the hash was
        identical across four runs.
        """
        with self._lock:
            init = self.init_event
        if not isinstance(init, dict):
            return None, None
        canon = {k: v for k, v in init.items() if k not in ("cwd", "memory_paths", "session_id", "uuid")}
        blob = json.dumps(canon, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return protocol.sha256(blob), canon

    def _finalize(self, rc: int | None) -> None:
        self.exit_code_raw = rc
        reason = self.termination_reason or "unknown"
        if reason == "max_seconds":
            code = TIMEOUT_EXIT_CODE          # telemetry.py maps 124 -> "timeout"
        elif self.abort_reason:
            code = ABORT_EXIT_CODE
        elif rc is None:
            code = ABORT_EXIT_CODE
        elif rc < 0:
            code = 128 + abs(rc)              # shell convention for a signalled child
        else:
            code = rc
        self.exit_code = code

        with self._lock:
            max_tu = max(self.tool_uses_by_mid.values()) if self.tool_uses_by_mid else 0
            turns_total = len(self.tool_uses_by_mid)
            probes = [p.to_dict() for p in self.probes]
            outcomes = [p.outcome for p in self.probes]
            parse_ok = sum(1 for p in self.probes if p.parse_ok)
        pacing_ok = max_tu <= 1
        integrity = "ok"
        exclusion = None
        if self.sidechain_barriers:
            integrity = "sidechain_barrier"
        if not pacing_ok:
            # NOTE: "pacing_failed" is NOT a member of fact_trace.probe_integrity's
            # enum — it IS a member of exclusion_reason. Both are emitted; trace.py
            # routes them (see the driver's report).
            integrity = "pacing_failed"
            exclusion = "pacing_failed"
        init_sha, init_canon = self._canonical_init()

        summary = {
            "schema_version": "1",
            "driver_version": DRIVER_VERSION,
            "protocol_version": protocol.PROTOCOL_VERSION,
            "run_id": self.cfg.run_id,
            "session_id": self.cfg.session_id,
            "condition_id": self.cfg.condition_id,
            "task_id": self.cfg.task_id,
            "rep": self.cfg.rep,
            "run_dir": str(self.cfg.run_dir),
            "workspace": str(self.cfg.workspace),
            "started_at": _iso(self.t0),
            "ended_at": _iso(),
            "wall_seconds": round(time.time() - self.t0, 3),
            "abort_reason": self.abort_reason,
            "child": {
                "pid": self.child.pid if self.child else None,
                "argv": getattr(self, "argv", None),
                "argv_canonical": getattr(self, "argv_canonical", None),
                "cli_argv_sha256": protocol.sha256(json.dumps(getattr(self, "argv_canonical", []), sort_keys=False))
                if getattr(self, "argv_canonical", None)
                else None,
                "cli_argv_raw_sha256": protocol.sha256(json.dumps(getattr(self, "argv", []), sort_keys=False))
                if getattr(self, "argv", None)
                else None,
                "cli_version": self.cli_version,
                "exit_code_raw": rc,
                "exit_code": code,
                "transcript_path": self.transcript_path,
            },
            "termination": {
                "reason": reason,
                "ladder": self.term_ladder,
                "stdin_closed_at_s": round(self._stdin_closed_at - self.t0, 3) if self._stdin_closed_at else None,
            },
            "hooks": {
                "hooks_alive": self.hooks_alive,
                "hooks_alive_after_s": self.hooks_alive_after_s,
                "settings_path": str(self.cfg.settings_path),
            },
            "gate": {
                "barriers": self.barriers,
                "requests_served": self.barrier_requests,
                "repeat_requests": self.repeat_requests,
                "unparseable_requests": self.unparseable_requests,
                "sidechain_barriers": self.sidechain_barriers,
                "denials": self.denials,
                "gate_timeout_ms": self.cfg.gate_timeout_ms,
            },
            "budget": {
                "steps": self.cfg.budget_steps,
                "exhausted": self.budget_exhausted,
                "max_usd": self.cfg.max_usd,
                "max_seconds": self.cfg.max_seconds,
            },
            "probes": {
                "enabled": self.cfg.probe_enabled,
                "plan_source": self.probe_plan_source,
                "planned_fire_at": self.fire_at,
                "sent": len(self.probes),
                "answered": outcomes.count("answered"),
                "superseded": outcomes.count("superseded"),
                "unanswered": outcomes.count("unanswered"),
                "refused": outcomes.count("refused"),
                "parse_ok": parse_ok,
                "retries_sent": self.retries_sent,
                "records": probes,
            },
            "resumes": {
                "sent": self.resumes_sent,
                "cap": self.cfg.max_resumes,
                "max_consecutive": self.max_consecutive_resumes,
            },
            "operations": {
                "turns_total": turns_total,
                "tool_calls_total": len(self.tool_use_ids),
                "tool_calls_barriered": self.barriers,
                "max_tool_uses_per_message": max_tu,
                "probes_sent": len(self.probes),
                "probes_answered": outcomes.count("answered"),
                "resumes_sent": self.resumes_sent,
                "permission_denials": self.permission_denials,
                "stream_lines": self.stream_lines,
            },
            "tokens": self._token_accounting(),
            "integrity": {
                "pacing_ok": pacing_ok,
                "probe_integrity": integrity,
                "exclusion_reason": exclusion,
                "unparsed_lines": self.unparsed_lines,
                "parse_errors": self.parse_errors,
                "parse_lines_dropped": self.parse_dropped,
            },
            "init": {"sha256": init_sha, "canonical": init_canon},
            "frozen": protocol.frozen_hashes(),
            "errors": self.errors,
        }
        try:
            _write_json_atomic(self.summary_path, summary)
        except Exception as exc:
            self.log("summary", f"FAILED to write driver_summary.json: {exc}")
        self.summary = summary

        # The contract lib/run_job.sh depends on: this file exists, and the
        # driver itself exits 0.
        try:
            self.exit_code_path.write_text(f"{code}\n", encoding="utf-8")
        except Exception as exc:
            self.log("exit", f"FAILED to write agent_exit_code: {exc}")
        if not pacing_ok:
            self.log("integrity", f"PACING FAILED: max tool_uses per assistant message = {max_tu} (expected 1)")
        self.log(
            "driver",
            f"done reason={reason} exit_code={code} barriers={self.barriers} "
            f"probes={len(self.probes)}/{outcomes.count('answered')} answered "
            f"resumes={self.resumes_sent} lines={self.stream_lines}",
        )
        try:
            self._log_fh.close()
        except Exception:
            pass


# ── CLI ──────────────────────────────────────────────────────────────────────
def _parser_argv() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WUR stream-json driver (parent of the claude child)")
    p.add_argument("--run-dir", help="the run directory (default $RUN_DIR)")
    p.add_argument("--workspace", help="child cwd (default $RUN_DIR/workspace)")
    p.add_argument("--task-prompt", help="the task text (default $TASK_PROMPT)")
    p.add_argument("--task-prompt-file", help="file holding the task text")
    p.add_argument("--run-id")
    p.add_argument("--session-id", help="must be a UUID; default derived from run_id")
    p.add_argument("--model")
    p.add_argument("--effort", default=None)
    p.add_argument("--claude-bin")
    p.add_argument("--settings", help="settings file for --settings (default $RUN_DIR/settings.json)")
    p.add_argument("--config-dir", help="CLAUDE_CONFIG_DIR (default $RUN_DIR/claude_home)")
    p.add_argument("--budget-steps", type=int)
    p.add_argument("--max-usd", type=float)
    p.add_argument("--max-seconds", type=float)
    p.add_argument("--task-id")
    p.add_argument("--rep", type=int)
    p.add_argument("--condition-id")
    p.add_argument("--probe", dest="probe", action="store_const", const=True, default=None)
    p.add_argument("--no-probe", dest="probe", action="store_const", const=False)
    p.add_argument("--max-retries", type=int)
    p.add_argument("--max-resumes", type=int)
    p.add_argument("--gate-timeout-ms", type=int)
    p.add_argument("--hooks-alive-timeout", type=float)
    p.add_argument("--stall-timeout", type=float)
    p.add_argument("--drain-timeout", type=float)
    p.add_argument("--allow-missing-home", action="store_true", help="do not require CLAUDE_CONFIG_DIR to exist")
    p.add_argument("--print-argv", action="store_true", help="print the child argv as JSON and exit; no child")
    return p


def _config_error_exit(args: argparse.Namespace, exc: BaseException) -> int:
    """A misconfigured driver still honours the contract: agent_exit_code + exit 0."""
    msg = f"driver: configuration error: {exc}"
    print(msg, file=sys.stderr, flush=True)
    rd = args.run_dir or os.environ.get("RUN_DIR")
    if rd:
        try:
            p = Path(rd)
            p.mkdir(parents=True, exist_ok=True)
            with open(p / "driver.log", "a", encoding="utf-8") as fh:
                fh.write(f"[    0.00] abort: {msg}\n")
            (p / "agent_exit_code").write_text(f"{ABORT_EXIT_CODE}\n", encoding="utf-8")
        except Exception:
            pass
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. ALWAYS returns 0 — run_job.sh reads agent_exit_code."""
    args = _parser_argv().parse_args(argv)
    try:
        cfg = build_config(args)
    except SystemExit as exc:
        return _config_error_exit(args, exc)
    except Exception as exc:
        return _config_error_exit(args, exc)
    if args.print_argv:
        real = build_argv(cfg)
        print(
            json.dumps(
                {
                    "argv": real,
                    "argv_canonical": canonical_argv(real, cfg),
                    "cli_argv_sha256": protocol.sha256(json.dumps(canonical_argv(real, cfg))),
                    "cwd": str(cfg.workspace),
                    "env": {
                        "CLAUDE_CONFIG_DIR": str(cfg.config_dir),
                        "ANTHROPIC_API_KEY": "<unset by driver>",
                    },
                },
                indent=2,
            )
        )
        return 0
    driver = Driver(cfg)
    try:
        driver.run()
    except Exception as exc:  # a driver crash must still leave the contract intact
        driver.errors.append(f"driver crash: {exc}")
        driver.abort_reason = driver.abort_reason or f"driver crash: {exc}"
        try:
            driver.log("crash", str(exc))
            if driver.child is not None and driver.child.poll() is None:
                driver._terminate()
            driver._finalize(driver.exit_code_raw)
        except Exception:
            pass
    # ALWAYS 0: run_job.sh reads $RUN_DIR/agent_exit_code, not this process's code.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
