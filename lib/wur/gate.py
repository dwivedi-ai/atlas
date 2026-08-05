#!/usr/bin/env python3
"""
gate.py — the PreToolUse barrier and the SessionStart liveness marker.

RESPONSIBILITY
  Be the one process the agent's tool calls pass through, and never be the reason
  a run dies. Two modes and no more: `pre` (the barrier) and `session-start` (the
  marker). There is deliberately NO post mode — PostToolUse does not fire when a
  tool errors, its `tool_response` is not what the model saw (V6), and hooks are
  forbidden from carrying model-visible text (§5.1(1)), so a post hook would cost
  a fork per tool call to observe nothing trustworthy.

  This module is also the drop-in replacement for the deleted
  lib/hooks/log_tool_event.sh: `--mode log` (or WUR_PROBE_MODE=log) appends the
  same barrier row and returns immediately, so a non-WUR claude job gets tool-call
  timing without ever blocking.

INPUTS
  stdin   the Claude Code hook payload, one JSON object. PreToolUse carries
          {session_id, transcript_path, cwd, permission_mode, hook_event_name,
           tool_name, tool_input, tool_use_id} and, on a sidechain call,
          parent_tool_use_id (impossible with --tools, V10 — recorded if seen).
  argv    everything else. Hooks are bound by ARGV, never by environment: the
          child is launched under `env -u ANTHROPIC_API_KEY` and a run must not
          depend on which variables survived that scrub. Env vars are read only
          as a fallback (WUR_PROBE_MODE, WUR_GATE_TIMEOUT_MS) so the ladder's
          existing env-driven wiring keeps working.

OUTPUTS
  stdout  exactly one JSON object: `{}` to allow, or the PreToolUse deny object
          when the driver said deny. Nothing else, ever.
  $RUN_DIR/gate/tool_calls.jsonl   flock'd append, one row per barrier fire:
          {ts, barrier, tool_use_id, tool_name, tool_input, ...} (STATUS.md §3).
  $RUN_DIR/gate/req/<tid>.json     the barrier request the driver answers.
  $RUN_DIR/gate/anomalies.jsonl    gate_timeout / sidechain_barrier / hook_error.
  $RUN_DIR/watch/hooks_alive       SessionStart marker — the driver aborts if it
          does not appear within 90 s (V12: a settings file that fails validation
          is silently ignored in --print mode, yielding zero hooks and no error).
  $RUN_DIR/watch/transcript_path   the on-disk transcript path, from the payload.

THE SEVEN-STEP CONTROL FLOW IS NORMATIVE (§6.2)
  1. read payload; tid = payload["tool_use_id"]; parent_tool_use_id set ⇒ record
     it and stamp probe_integrity: "sidechain_barrier";
  2. flock + append to gate/tool_calls.jsonl;
  3. mode == log ⇒ print {} and exit 0;
  4. else write gate/req/<tid>.json atomically and BLOCK, polling
     gate/resp/<tid>.json every 5 ms up to the gate timeout (default 300 000 ms);
  5. deny ⇒ print the PreToolUse deny object; else {};
  6. timeout ⇒ print {} — FAIL OPEN, a harness failure must never wedge a run —
     and log gate_timeout;
  7. always exit 0, never write stderr.

  Blocking here is safe (V11: hooks are synchronous, a 3 s sleep hook moved wall
  time 14.09 s → 26.96 s over 3 tool calls with strict enter/exit pairing) but it
  is only ever safe in ONE direction: the driver must inject on stdin and THEN
  release the barrier. Holding the barrier until the model answers DEADLOCKS —
  20 s held produced zero child output and the injected message was not even
  replayed until the hook returned (V13). No response written into gate/resp/ may
  depend on model output.

FAIL-OPEN IS THE WHOLE CONTRACT
  Every failure path — unreadable payload, unwritable gate dir, a corrupt
  response file, an unhandled exception, a timeout — ends the same way: `{}` on
  stdout, exit 0, nothing on stderr. fd 2 is redirected to /dev/null before any
  work starts so that even an interpreter traceback cannot reach the agent.

CLI
  python3 lib/wur/gate.py pre --run-dir $RUN_DIR [--mode auto|barrier|log]
                              [--timeout-ms N] [--poll-ms N]
  python3 lib/wur/gate.py session-start --run-dir $RUN_DIR
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from typing import Any, Sequence

# ── constants ────────────────────────────────────────────────────────────────
GATE_SCHEMA_VERSION = "1"

#: Barrier poll timeout. job.yaml probe.gate_timeout_ms overrides it per job.
DEFAULT_TIMEOUT_MS = 300_000
#: §6.2 step 4, verbatim: poll every 5 ms.
DEFAULT_POLL_MS = 5
#: SessionStart and log mode never block; this bounds a pathological filesystem.
MODES = ("auto", "barrier", "log")

#: A single barrier row must never be able to blow up tool_calls.jsonl. The full
#: input is in stream.jsonl verbatim; here we keep a bounded copy plus a hash of
#: the whole thing, so truncation is visible and checkable rather than silent.
TOOL_INPUT_MAX_BYTES = 131_072

ALLOW: dict[str, Any] = {}

_FILENAME_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


# ── never touch the agent ────────────────────────────────────────────────────
def silence_stderr() -> None:
    """Point fd 2 at /dev/null so nothing this process does can reach stderr.

    §5.1(1): hooks print exactly `{}` and write nothing to stderr. Redirecting
    the file descriptor (not just sys.stderr) also covers interpreter-level
    tracebacks and anything a C extension might emit.
    """
    try:
        fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(fd, 2)
        if fd > 2:
            os.close(fd)
    except Exception:
        pass


def _emit(obj: dict[str, Any]) -> None:
    """Write the single JSON object the CLI expects, and nothing else."""
    try:
        sys.stdout.write(json.dumps(obj))
        sys.stdout.flush()
    except Exception:
        try:
            os.write(1, b"{}")
        except Exception:
            pass


def deny_object(reason: str) -> dict[str, Any]:
    """The PreToolUse deny object, in the shape measured to reach the model.

    This deny reason is the ONLY hook-authored text that ever reaches the model
    (§5.1(1)). It is a control action, not a channel: V14 measured that the model
    treats deny-reason text as prompt injection, refuses it, and RE-ISSUES the
    same tool call, which then succeeds. Budget stop is therefore
    deny-every-subsequent-call plus closing stdin, and a denied call costs TWO
    barrier fires — gate ordinals are not tool-call ordinals.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


# ── small filesystem helpers (all best-effort, all silent) ───────────────────
def _utc_iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int((ts % 1) * 1000):03d}Z"


def _safe_key(tid: str, fallback: str) -> str:
    """A filesystem-safe request/response key derived from the tool_use_id."""
    raw = tid or fallback
    out = "".join(c if c in _FILENAME_SAFE else "_" for c in raw)[:96]
    return out or fallback


def _mkdirs(*paths: str) -> None:
    for p in paths:
        try:
            os.makedirs(p, exist_ok=True)
        except Exception:
            pass


def _atomic_write(path: str, data: str) -> bool:
    """Write `data` to `path` via .tmp + os.replace. Returns success."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


def _read_json(path: str) -> Any:
    """Decode a JSON file, or None if it is missing, partial or corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.loads(fh.read())
    except Exception:
        return None


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _protocol():
    """Import protocol.py without assuming how gate.py was launched.

    Lazy on purpose: the barrier runs on every tool call and only the deny path
    needs the frozen text. Returns None rather than raising — a hook may not die.
    """
    try:
        import protocol  # lib/wur on sys.path (script launched by absolute path)

        return protocol
    except Exception:
        pass
    try:
        from wur import protocol as _p  # lib/ on sys.path

        return _p
    except Exception:
        return None


# ── the flock'd writers (§5.1(4): one writer per file) ───────────────────────
def _locked_append(gate_dir: str, filename: str, row: dict[str, Any], *,
                   ordinal_field: str | None = None, count_key: str | None = None,
                   repeats_field: str | None = None) -> tuple[int, int]:
    """Append one JSON row under an exclusive flock. Returns (ordinal, key_repeats).

    `ordinal` is 1-based and counts the lines already in the file plus this one —
    the authoritative barrier index (STATUS.md §3). It is knowable only while the
    lock is held, so it is stamped into the row here (`ordinal_field`) rather than
    by the caller. `key_repeats` counts prior occurrences of `count_key` in the
    raw file, which is how a tool call re-issued after a deny becomes visible
    (V14) without parsing every row.

    V8 measured that unlocked hook writes lose increments (6 fires → 5) and that
    appends larger than PIPE_BUF interleave into unparseable lines, so the lock
    is not optional. It is held across one read and one write.
    """
    lock_path = os.path.join(gate_dir, ".lock")
    target = os.path.join(gate_dir, filename)
    ordinal, repeats = 0, 0
    lock_fh = None
    try:
        lock_fh = open(lock_path, "a+")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            with open(target, "rb") as fh:
                blob = fh.read()
        except Exception:
            blob = b""
        ordinal = blob.count(b"\n") + 1
        if count_key:
            repeats = blob.count(count_key.encode("utf-8", "replace"))
        if ordinal_field:
            row = {**row, ordinal_field: ordinal}
        if repeats_field:
            row = {**row, repeats_field: repeats}
        # json.dumps never emits a raw newline, so one row is always one line;
        # the strip is a guarantee, not a repair.
        line = json.dumps(row, ensure_ascii=False).replace("\n", " ").replace("\r", " ")
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        ordinal = ordinal if ordinal > 0 else 0
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock_fh.close()
            except Exception:
                pass
    return ordinal, repeats


def log_anomaly(gate_dir: str, kind: str, detail: dict[str, Any] | None = None) -> None:
    """Append one row to gate/anomalies.jsonl. Never raises.

    The home for everything the barrier notices but must not act on:
    gate_timeout (§6.2 step 6), sidechain_barrier (§6.2 step 1), hook_error.
    """
    row = {
        "schema_version": GATE_SCHEMA_VERSION,
        "ts": time.time(),
        "kind": kind,
        "pid": os.getpid(),
    }
    if detail:
        row.update(detail)
    try:
        _locked_append(gate_dir, "anomalies.jsonl", row)
    except Exception:
        pass


# ── the barrier row ──────────────────────────────────────────────────────────
def barrier_row(payload: dict[str, Any], *, ts: float, mode: str) -> dict[str, Any]:
    """The tool_calls.jsonl row for one PreToolUse fire, minus the ordinal.

    Shape is STATUS.md §3 ({ts, barrier, tool_use_id, tool_name, tool_input})
    plus the fields events.py needs to join and to detect the two conditions the
    join cannot infer: a sidechain barrier, and a re-issued tool_use_id.
    """
    tool_input = payload.get("tool_input")
    try:
        rendered = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    except Exception:
        rendered = json.dumps(str(tool_input), ensure_ascii=False)
    truncated = len(rendered.encode("utf-8", "replace")) > TOOL_INPUT_MAX_BYTES
    return {
        "schema_version": GATE_SCHEMA_VERSION,
        "ts": ts,
        "ts_iso": _utc_iso(ts),
        "barrier": None,
        "tool_use_id": payload.get("tool_use_id"),
        "tool_name": payload.get("tool_name"),
        "tool_input": None if truncated else tool_input,
        "tool_input_sha256": _sha256(rendered),
        "tool_input_bytes": len(rendered.encode("utf-8", "replace")),
        "tool_input_truncated": truncated,
        "parent_tool_use_id": payload.get("parent_tool_use_id"),
        "probe_integrity": "sidechain_barrier" if payload.get("parent_tool_use_id") else None,
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "hook_event_name": payload.get("hook_event_name"),
        "mode": mode,
        "pid": os.getpid(),
    }


# ── mode: pre (the barrier) ──────────────────────────────────────────────────
def resolve_mode(mode: str | None) -> str:
    """argv wins; WUR_PROBE_MODE is the fallback; barrier is the default."""
    m = (mode or "auto").strip().lower()
    if m in ("barrier", "log"):
        return m
    env = (os.environ.get("WUR_PROBE_MODE") or "").strip().lower()
    return "log" if env == "log" else "barrier"


def resolve_timeout_ms(timeout_ms: int | None) -> int:
    """argv wins; WUR_GATE_TIMEOUT_MS is the fallback; 300 000 ms is the default."""
    if timeout_ms is not None and timeout_ms >= 0:
        return int(timeout_ms)
    raw = (os.environ.get("WUR_GATE_TIMEOUT_MS") or "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_TIMEOUT_MS


def _decision_from(obj: Any) -> tuple[str, str] | None:
    """Read a driver response into (decision, reason), or None if unusable.

    Accepts both the native shape `{"decision": "allow"|"deny", "reason": ...}`
    and the CLI's own `{"permissionDecision": ..., "permissionDecisionReason":
    ...}`, so a driver that echoes the hook contract back at us still works.
    """
    if not isinstance(obj, dict):
        return None
    dec = obj.get("decision")
    reason = obj.get("reason")
    if dec is None:
        hs = obj.get("hookSpecificOutput")
        src = hs if isinstance(hs, dict) else obj
        dec = src.get("permissionDecision")
        reason = src.get("permissionDecisionReason", reason)
    if not isinstance(dec, str):
        return None
    dec = dec.strip().lower()
    if dec in ("allow", "approve", "continue", "ok"):
        return ("allow", "")
    if dec in ("deny", "block"):
        return ("deny", reason if isinstance(reason, str) else "")
    return None


def wait_for_response(gate_dir: str, key: str, *, timeout_ms: int, poll_ms: int
                      ) -> tuple[str, str, float]:
    """Block until the driver answers this barrier. Returns (decision, reason, waited_s).

    decision is "allow", "deny" or "timeout". The 5 ms poll is §6.2 step 4.

    PRECEDENCE, and it matters: `gate/resp/<key>.json` if it is there when a poll
    looks, otherwise `gate/broadcast.json`. Since run_pre() moves a stale response
    aside before publishing the request, a broadcast decides IMMEDIATELY — it is a
    standing order, not a fallback the driver can race. That is exactly what
    budget stop wants (deny every subsequent call without answering each one,
    V14) and what a driver that has already closed stdin wants (the drain's tool
    calls resolve at once instead of burning the full timeout each).
    """
    resp_path = os.path.join(gate_dir, "resp", f"{key}.json")
    broadcast_path = os.path.join(gate_dir, "broadcast.json")
    poll_s = max(poll_ms, 1) / 1000.0
    started = time.monotonic()
    deadline = started + max(timeout_ms, 0) / 1000.0
    while True:
        got = _decision_from(_read_json(resp_path)) if os.path.exists(resp_path) else None
        if got is None and os.path.exists(broadcast_path):
            got = _decision_from(_read_json(broadcast_path))
        if got is not None:
            return (got[0], got[1], time.monotonic() - started)
        if time.monotonic() >= deadline:
            return ("timeout", "", time.monotonic() - started)
        time.sleep(poll_s)


def run_pre(payload: dict[str, Any], gate_dir: str, *, mode: str = "barrier",
            timeout_ms: int = DEFAULT_TIMEOUT_MS, poll_ms: int = DEFAULT_POLL_MS
            ) -> dict[str, Any]:
    """The barrier. Returns the hook response object; never raises, never blocks
    longer than `timeout_ms`, and always yields something printable."""
    ts = time.time()
    _mkdirs(gate_dir, os.path.join(gate_dir, "req"), os.path.join(gate_dir, "resp"))

    # 1 + 2 — record the fire before anything can go wrong with it.
    row = barrier_row(payload, ts=ts, mode=mode)
    key = _safe_key(row.get("tool_use_id") or "", f"nokey-{os.getpid()}-{int(ts * 1000)}")
    row["gate_key"] = key
    # Count the FIELD, not the bare id: the id also appears as `gate_key`, and a
    # bare-substring count would report two fires for every one.
    quoted_tid = (json.dumps({"tool_use_id": row["tool_use_id"]}, ensure_ascii=False)[1:-1]
                  if row.get("tool_use_id") else None)
    ordinal, repeats = _locked_append(
        gate_dir, "tool_calls.jsonl", row, ordinal_field="barrier", count_key=quoted_tid,
        repeats_field="tool_use_id_repeats",
    )
    row["barrier"] = ordinal
    row["tool_use_id_repeats"] = repeats
    if ordinal <= 0:
        # The row is lost but the run is not: keep going and let the anomaly log
        # (and events.py's join_coverage gate) surface it afterwards.
        log_anomaly(gate_dir, "gate_append_failed", {"gate_key": key})

    if row.get("parent_tool_use_id"):
        log_anomaly(gate_dir, "sidechain_barrier",
                    {"gate_key": key, "tool_use_id": row.get("tool_use_id"),
                     "parent_tool_use_id": row.get("parent_tool_use_id"), "barrier": ordinal})

    # 3 — log mode: the drop-in replacement for log_tool_event.sh. No barrier.
    if mode == "log":
        return ALLOW

    # 4 — publish the request, then block. The response slot is cleared FIRST:
    # a denied call is re-issued by the model under the SAME tool_use_id (V14),
    # so a leftover answer from the previous fire would decide this one, and a
    # driver polling for "requests without a response" would never see the retry
    # at all. Standing orders belong in broadcast.json, not in a stale resp file.
    stale_resp = os.path.join(gate_dir, "resp", f"{key}.json")
    if os.path.exists(stale_resp):
        try:
            os.replace(stale_resp, os.path.join(
                gate_dir, "resp", f"{key}.superseded.{ordinal}.json"))
        except Exception:
            pass
    req = {
        "schema_version": GATE_SCHEMA_VERSION,
        "gate_key": key,
        "barrier": ordinal,
        "ts": ts,
        "ts_iso": row["ts_iso"],
        "pid": os.getpid(),
        "tool_use_id": row.get("tool_use_id"),
        "tool_name": row.get("tool_name"),
        "parent_tool_use_id": row.get("parent_tool_use_id"),
        "tool_use_id_repeats": repeats,
        "payload": payload,
    }
    wrote = _atomic_write(os.path.join(gate_dir, "req", f"{key}.json"),
                          json.dumps(req, ensure_ascii=False))
    if not wrote:
        # The driver can never answer a request it cannot see: fail open now
        # rather than blocking for the full timeout on a certainty.
        log_anomaly(gate_dir, "gate_request_unwritable", {"gate_key": key, "barrier": ordinal})
        return ALLOW

    decision, reason, waited = wait_for_response(
        gate_dir, key, timeout_ms=timeout_ms, poll_ms=poll_ms
    )

    # 5 — deny is a control action, and the only text a hook may put in front of
    #     the model.
    if decision == "deny":
        if not reason:
            proto = _protocol()
            reason = getattr(proto, "BUDGET_STOP_TEXT", "") if proto else ""
        log_anomaly(gate_dir, "gate_deny",
                    {"gate_key": key, "barrier": ordinal, "waited_s": round(waited, 4),
                     "reason_sha256": _sha256(reason)})
        return deny_object(reason)

    # 6 — timeout: FAIL OPEN. A harness failure must never wedge a run.
    if decision == "timeout":
        log_anomaly(gate_dir, "gate_timeout",
                    {"gate_key": key, "barrier": ordinal, "waited_s": round(waited, 4),
                     "timeout_ms": timeout_ms})
    return ALLOW


# ── mode: session-start (the liveness marker) ────────────────────────────────
def run_session_start(payload: dict[str, Any], watch_dir: str, gate_dir: str | None = None
                      ) -> dict[str, Any]:
    """Write watch/hooks_alive and watch/transcript_path. Always allows.

    hooks_alive is the answer to V12: in --print mode a settings file that fails
    validation is silently ignored, so a templating bug yields zero hooks, zero
    barriers, zero probes and no error anywhere. The driver aborts if this file
    does not appear within 90 s. It carries no model-visible text — SessionStart
    hooks may inject additionalContext and this one deliberately does not.
    """
    ts = time.time()
    _mkdirs(watch_dir)
    marker = {
        "schema_version": GATE_SCHEMA_VERSION,
        "ts": ts,
        "ts_iso": _utc_iso(ts),
        "pid": os.getpid(),
        "session_id": payload.get("session_id"),
        "source": payload.get("source"),
        "cwd": payload.get("cwd"),
        "transcript_path": payload.get("transcript_path"),
        "hook_event_name": payload.get("hook_event_name"),
    }
    _atomic_write(os.path.join(watch_dir, "hooks_alive"),
                  json.dumps(marker, ensure_ascii=False) + "\n")
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp:
        _atomic_write(os.path.join(watch_dir, "transcript_path"), tp + "\n")
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid:
        _atomic_write(os.path.join(watch_dir, "session_id"), sid + "\n")
    if gate_dir:
        try:
            _mkdirs(gate_dir)
            log_anomaly(gate_dir, "session_start",
                        {"session_id": payload.get("session_id"), "source": payload.get("source")})
        except Exception:
            pass
    return ALLOW


# ── payload plumbing ─────────────────────────────────────────────────────────
def read_payload(stream: Any = None) -> dict[str, Any]:
    """Decode the hook payload from stdin. A bad payload is `{}`, not an error."""
    try:
        raw = (stream or sys.stdin).read()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _resolve_dirs(args: argparse.Namespace) -> tuple[str, str]:
    """(gate_dir, watch_dir) from --run-dir, with per-dir overrides.

    Falls back to the ladder's EXPERIMENT_RUN_ID / EXPERIMENT_RUNS_DIR pair so
    `--mode log` really is a drop-in for log_tool_event.sh on non-WUR jobs.
    """
    run_dir = args.run_dir
    if not run_dir:
        rid = os.environ.get("EXPERIMENT_RUN_ID")
        root = os.environ.get("EXPERIMENT_RUNS_DIR")
        if rid and root:
            run_dir = os.path.join(root, rid)
    gate_dir = args.gate_dir or (os.path.join(run_dir, "gate") if run_dir else "")
    watch_dir = args.watch_dir or (os.path.join(run_dir, "watch") if run_dir else "")
    return gate_dir, watch_dir


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gate.py", description="WUR PreToolUse barrier / SessionStart marker",
        add_help=False,
    )
    p.add_argument("mode_cmd", nargs="?", default="pre", choices=["pre", "session-start"])
    p.add_argument("--run-dir", default=None)
    p.add_argument("--gate-dir", default=None)
    p.add_argument("--watch-dir", default=None)
    p.add_argument("--mode", default="auto", choices=list(MODES))
    p.add_argument("--timeout-ms", type=int, default=None)
    p.add_argument("--poll-ms", type=int, default=DEFAULT_POLL_MS)
    return p


def main(argv: Sequence[str] | None = None, stdin: Any = None) -> int:
    """Always returns 0. Always prints exactly one JSON object.

    Every branch below is wrapped: an unparseable argv, a missing run dir, an
    exploded filesystem and an unhandled exception all converge on `{}`.
    """
    response: dict[str, Any] = ALLOW
    gate_dir = ""
    try:
        args, _unknown = build_parser().parse_known_args(list(argv or []))
        gate_dir, watch_dir = _resolve_dirs(args)
        payload = read_payload(stdin)
        if args.mode_cmd == "session-start":
            if watch_dir:
                response = run_session_start(payload, watch_dir, gate_dir or None)
        elif not gate_dir:
            response = ALLOW  # nothing to record against; do not block the agent
        else:
            response = run_pre(
                payload, gate_dir,
                mode=resolve_mode(args.mode),
                timeout_ms=resolve_timeout_ms(args.timeout_ms),
                poll_ms=args.poll_ms,
            )
    except BaseException as exc:  # noqa: BLE001 — a hook may never propagate
        response = ALLOW
        if gate_dir:
            try:
                log_anomaly(gate_dir, "hook_error",
                            {"error": f"{type(exc).__name__}: {exc}"[:500]})
            except Exception:
                pass
    _emit(response)
    return 0


if __name__ == "__main__":
    silence_stderr()
    try:
        main(sys.argv[1:])
    except BaseException:  # noqa: BLE001 — belt and braces; step 7 is absolute
        try:
            _emit(ALLOW)
        except Exception:
            pass
    # Not sys.exit(): nothing at interpreter shutdown may change the exit code.
    os._exit(0)
