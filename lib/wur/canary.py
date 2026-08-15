#!/usr/bin/env python3
"""
canary.py — what the model was actually given, per (job, arm), before any run.

RESPONSIBILITY
  Answer two questions that no run artifact can answer, once per (job, arm):

  1. WHAT WAS IN THE CONTEXT WINDOW AT t=0?  Capture the `system/init` event
     verbatim into .registry/conditions/<arm>/context_manifest.json and stamp its
     canonicalized hash as `init_sha256`.
  2. DID THE PUSHED CHANNEL ACTUALLY PUSH?  Run the D0 autoload assay: the same
     command line with `--tools ""` and a prompt asking for the sentinel,
     requiring the nonce IN the answer for `d0-push` and ABSENT for d1/d2/d3.

  The assay exists because auto-loaded `CLAUDE.md` content appears in NEITHER
  `stream.jsonl` NOR the on-disk transcript NOR `--debug api` (S2d, measured:
  the body is nowhere, only the model's own echo of it). So `d0-push` scores
  `exposure_basis = "manifest_canary"` — asserted, not scanned — and this run is
  the entire evidence for that assertion. If the assay fails, the `d0-push` arm
  has no mechanism and its rows are not admissible.

INPUTS
  a canary workspace (a checkout of the arm's PLANTED tree — the same bytes the
  run will see), the arm's sentinel nonce, the model id, and a credentials file
  to seed an isolated CLAUDE_CONFIG_DIR with.

OUTPUTS  (default under $JOB_DIR/.registry/conditions/<arm>/)
  context_manifest.json      the verbatim system/init event
  canary.md                  the model-authored "what was I given" dump
  canary.json               the machine record: verdict, hashes, argv, cost
  canary_<phase>.stream.jsonl / .err     raw child output, before anything parses it
  init_sha256, canary_sha256 for run_meta.json / run_record.condition

THREE MEASURED CONSTRAINTS SHAPE THIS FILE
  V18  `system/init.agents` is NEVER empty ([claude, Explore, general-purpose,
       Plan, statusline-setup]) even under full hygiene, and init varies run to
       run in exactly four keys: cwd, memory_paths, session_id, uuid. So the
       hygiene assertion is `tools` set-equals the frozen six plus
       mcp_servers/skills/slash_commands/plugins empty — never `agents == []`,
       which would fail closed on every run — and `init_sha256` hashes a
       CANONICALIZED init with those four keys dropped. Hashing the raw event
       produces a different value every run and defeats its own purpose.
  V19  Every one-shot claude invocation passes `< /dev/null`. Without it each
       pays a 3 s stall and writes "Warning: no stdin data received in 3s" to
       stderr, which a naive `[ -s stderr ]` health check reads as failure. This
       module therefore uses stdin=DEVNULL and never treats stderr as a verdict.
  V5   `--setting-sources ''` kills workspace CLAUDE.md auto-discovery and
       `--add-dir` does not restore it, so the canary must use the run's exact
       `--setting-sources project`, or it would measure a different machine.

WHAT THIS COMMAND LINE DELIBERATELY OMITS versus
  --input-format stream-json / --replay-user-messages / --include-hook-events /
  --settings / --session-id / --max-budget-usd: all of them are about the driver's
  transport and the barrier, none of them changes what the model is given. The
  omission is recorded in canary.json as `argv_delta_from_run` rather than left
  for a reader to notice. --settings in particular MUST be omitted: it would bind
  the PreToolUse barrier, and with no driver running, every tool call would block
  until the gate timeout.

CLI
  python3 lib/wur/canary.py --workspace WS --arm d0-push --sentinel NONCE \\
      [--out-dir DIR] [--job-id ID] [--model claude-sonnet-5] [--expect present|absent]
      [--phases init,assay,dump] [--dry-run]
  Exits 1 on any failed assertion (fail closed); artifacts are written either way.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import protocol  # noqa: E402  (lib/wur is on sys.path; see wur/__init__.py)

CANARY_SCHEMA_VERSION = "1"

#: The tool set asserted positively at preflight and here (--tools silently
#: ignores names it does not honour, so the allowlist is checked by reading it
#: back out of system/init, never by trusting the flag).
FROZEN_TOOLS: tuple[str, ...] = ("Bash", "Read", "Write", "Edit", "Glob", "Grep")

#: The four keys that vary run to run. Dropping exactly these yielded ONE
#: identical sha256 across four runs.
INIT_VOLATILE_KEYS: tuple[str, ...] = ("cwd", "memory_paths", "session_id", "uuid")

#: Must be empty under the hygiene recipe (S2a). `agents` is NOT in this list and
#: must never be added to it — it is never empty, and the assertion would fail
#: closed on every run. It is harmless because Task is absent from --tools.
INIT_MUST_BE_EMPTY: tuple[str, ...] = ("mcp_servers", "skills", "slash_commands", "plugins")

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_TIMEOUT_S = 180
PHASES = ("init", "assay", "dump")

#: A trivial prompt: the init event is emitted before the model does anything, so
#: the cheapest possible turn is the right one.
INIT_PROMPT = "Reply with exactly: OK"

#: The assay prompt, in the shape measured to work in S2b/S2c: the pushed arm
#: answered with the sentinel, the pulled arm answered NO-SENTINEL.
ASSAY_PROMPT = (
    "Without using any tools, state the project sentinel token exactly as it appears in your "
    "loaded instructions. Reply with only the token. If there is no such token in your "
    "instructions, reply with exactly: NO-SENTINEL"
)
NO_SENTINEL_LITERAL = "NO-SENTINEL"

#: The "what was I given" dump that becomes canary.md.
DUMP_PROMPT = (
    "Without using any tools, describe exactly what was placed in your context before this "
    "message: every instruction file or document that was loaded automatically, its path if "
    "you know it, and its content reproduced verbatim inside a fenced code block. Do not "
    "guess and do not summarize. If nothing was loaded automatically, reply with exactly: NONE"
)

ARGV_DELTA_FROM_RUN = {
    "--input-format stream-json": "transport only; the canary is a one-shot with stdin closed",
    "--replay-user-messages": "transport only; there are no injected user messages here",
    "--include-hook-events": "no hooks are bound for the canary",
    "--settings": "binding the PreToolUse barrier with no driver running would block every tool call",
    "--session-id": "the canary does not need its transcript; the id is read back out of system/init",
    "--max-budget-usd": "a one-shot turn cannot reach a run budget",
    "--autocompact": "irrelevant to a single turn",
}


# ── hashing / canonicalization ───────────────────────────────────────────────
def sha256(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def canonical_init(init: dict[str, Any]) -> dict[str, Any]:
    """`init` minus the four keys that vary every run."""
    return {k: v for k, v in (init or {}).items() if k not in INIT_VOLATILE_KEYS}


def init_sha256(init: dict[str, Any]) -> str:
    """The stamp: sha256 of the canonicalized init, keys sorted.

    Verified identical across four runs. Hashing the raw event instead produces a
    fresh value every run, which would make the stamp unable to detect the drift
    it exists to detect.
    """
    blob = json.dumps(canonical_init(init), sort_keys=True, separators=(",", ":"))
    return sha256(blob)


def canary_sha256(answer_text: str) -> str:
    """The stamp recorded in run_record.condition.canary_sha256.

    Defined on the ASSAY answer — the text the d0-push verdict is computed from —
    not on the whole record, so a change of verdict always changes the stamp.
    """
    return sha256(answer_text or "")


# ── init hygiene (no API call; operates on an init event someone captured) ───
def init_hygiene_problems(init: dict[str, Any],
                          expect_tools: Iterable[str] | None = FROZEN_TOOLS) -> list[str]:
    """Every hygiene violation visible in a `system/init` event. Empty means clean.

    `expect_tools=None` skips the tool assertion — the assay phase runs with
    `--tools ""` on purpose, so its init legitimately lists no tools.
    """
    problems: list[str] = []
    if not isinstance(init, dict):
        return [f"init is {type(init).__name__}, expected an object"]
    if init.get("type") != "system" or init.get("subtype") != "init":
        problems.append(f"not a system/init event: type={init.get('type')!r} "
                        f"subtype={init.get('subtype')!r}")
    for key in INIT_MUST_BE_EMPTY:
        val = init.get(key)
        if val is None:
            problems.append(f"init has no {key} key")
        elif val:
            problems.append(f"init.{key} is not empty: {val!r}")
    if expect_tools is not None:
        got = set(init.get("tools") or ())
        want = set(expect_tools)
        if got != want:
            problems.append(
                f"init.tools {sorted(got)} != frozen allowlist {sorted(want)} "
                f"(missing {sorted(want - got)}, extra {sorted(got - want)})"
            )
    mp = init.get("memory_paths")
    if isinstance(mp, dict):
        for label, path in mp.items():
            if path and str(Path(str(path)).expanduser()).startswith(str(Path.home() / ".claude")):
                problems.append(f"init.memory_paths[{label}] points into the user home: {path}")
    if init.get("apiKeySource") not in (None, "none"):
        problems.append(f"init.apiKeySource is {init.get('apiKeySource')!r}, want 'none' "
                        f"(runs go through the subscription path, env -u ANTHROPIC_API_KEY)")
    return problems


# ── the child ────────────────────────────────────────────────────────────────
def build_argv(prompt: str, *, model: str = DEFAULT_MODEL, tools: Sequence[str] | None,
               pacing: bool = True) -> list[str]:
    """The one-shot canary command line.

    `tools=None` renders `--tools ""` (the assay: the model may not read the file
    it is being asked about). `tools=FROZEN_TOOLS` renders the run's own set.
    """
    argv = [
        "claude", "--print", "--output-format", "stream-json", "--verbose",
        "--setting-sources", "project", "--strict-mcp-config", "--disable-slash-commands",
        "--permission-mode", "bypassPermissions", "--model", model,
        "--tools", "" if tools is None else ",".join(tools),
    ]
    if pacing:
        argv += ["--append-system-prompt", protocol.PACING_PROMPT]
    argv.append(prompt)
    return argv


def seed_home(home: str | os.PathLike[str], credentials: str | os.PathLike[str] | None = None
              ) -> Path:
    """An isolated CLAUDE_CONFIG_DIR holding `.credentials.json` and nothing else.

    Exactly the recipe S2 proved clean: dir 700, file 600, no settings.json, no
    CLAUDE.md, no projects/. The credential copy must never become committable —
    it lives under the job's run root, which .gitignore excludes.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    src = Path(credentials) if credentials else (Path.home() / ".claude" / ".credentials.json")
    dst = home / ".credentials.json"
    if not src.is_file():
        raise FileNotFoundError(f"no credentials to seed the canary home from: {src}")
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    return home


def _env(home: str | os.PathLike[str]) -> dict[str, str]:
    """The run's environment: subscription path, isolated config dir."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["CLAUDE_CONFIG_DIR"] = str(home)
    return env


def parse_stream(raw: str) -> dict[str, Any]:
    """Pull the init event, the final answer and the result event out of stdout.

    Tolerant by construction: a canary that cannot parse its own output is a
    failed canary, not an exception — the raw stream is on disk either way.
    """
    init: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    texts: list[str] = []
    bad_lines = 0
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            bad_lines += 1
            continue
        if not isinstance(ev, dict):
            bad_lines += 1
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init" and init is None:
            init = ev
        elif ev.get("type") == "result":
            result = ev
        elif ev.get("type") == "assistant":
            for block in ((ev.get("message") or {}).get("content") or []):
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text") or ""))
    answer = ""
    if result and isinstance(result.get("result"), str):
        answer = result["result"]
    elif texts:
        answer = "\n".join(texts)
    return {
        "init": init,
        "result": result,
        "assistant_text": "\n".join(texts),
        "answer": answer,
        "bad_lines": bad_lines,
    }


def run_phase(phase: str, *, workspace: str | os.PathLike[str], out_dir: Path, home: Path,
              model: str, timeout_s: int = DEFAULT_TIMEOUT_S, pacing: bool = True,
              dry_run: bool = False) -> dict[str, Any]:
    """One claude invocation. Writes the raw stream before parsing a byte of it."""
    prompt = {"init": INIT_PROMPT, "assay": ASSAY_PROMPT, "dump": DUMP_PROMPT}[phase]
    tools = FROZEN_TOOLS if phase == "init" else None
    argv = build_argv(prompt, model=model, tools=tools, pacing=pacing)
    rec: dict[str, Any] = {
        "phase": phase,
        "argv": argv,
        "argv_sha256": sha256("\x00".join(argv)),
        "prompt_sha256": sha256(prompt),
        "tools_requested": list(tools) if tools else [],
        "stream_path": str(out_dir / f"canary_{phase}.stream.jsonl"),
        "stderr_path": str(out_dir / f"canary_{phase}.err"),
    }
    if dry_run:
        rec.update({"skipped": "dry-run", "exit_code": None})
        return rec

    started = time.time()
    try:
        proc = subprocess.run(
            argv, cwd=str(workspace), env=_env(home),
            stdin=subprocess.DEVNULL,  # V19 — never let it wait 3 s for stdin
            capture_output=True, text=True, timeout=timeout_s,
        )
        out, err, code = proc.stdout or "", proc.stderr or "", proc.returncode
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        code = 124
    except FileNotFoundError as exc:
        out, err, code = "", f"{exc}", 127

    # Raw before derived.
    Path(rec["stream_path"]).write_text(out, encoding="utf-8")
    Path(rec["stderr_path"]).write_text(err, encoding="utf-8")

    parsed = parse_stream(out)
    result = parsed.get("result") or {}
    rec.update({
        "exit_code": code,
        "seconds": round(time.time() - started, 3),
        "answer": parsed["answer"],
        "answer_sha256": sha256(parsed["answer"]),
        "bad_lines": parsed["bad_lines"],
        "init_captured": parsed["init"] is not None,
        "session_id": (parsed["init"] or {}).get("session_id"),
        "cli_version": (parsed["init"] or {}).get("claude_code_version"),
        "result_subtype": result.get("subtype"),
        "is_error": result.get("is_error"),
        "num_turns": result.get("num_turns"),
        "cost_usd": result.get("total_cost_usd"),
        # stderr is recorded, never graded: every one-shot writes a warning there
        # under some conditions and a `[ -s stderr ]` check would read it as
        # failure.
        "stderr_bytes": len(err),
    })
    rec["_init_event"] = parsed["init"]
    return rec


# ── the arm expectation ──────────────────────────────────────────────────────
def expected_presence(arm: str | None, *, pointer_regime: str | None = None,
                      channel: str | None = None) -> str:
    """"present" for the pushed arm, "absent" for every pulled arm.

    Only an `@NOTES.md` import stub actually pushes content into the context
    window (S2c). `d1-ptr`'s CLAUDE.md is a PROSE pointer — it names the file but
    does not import it — so its sentinel must be absent here, which is exactly
    the contrast that splits "pointer" from "push".
    """
    if channel == "push" or pointer_regime == "import" or (arm or "") == "d0-push":
        return "present"
    return "absent"


# ── the canary ───────────────────────────────────────────────────────────────
def run_canary(workspace: str | os.PathLike[str], *, arm: str, sentinel: str | None,
               out_dir: str | os.PathLike[str], job_id: str | None = None,
               model: str = DEFAULT_MODEL, expect: str | None = None,
               pointer_regime: str | None = None, channel: str | None = None,
               phases: Sequence[str] = PHASES, home: str | os.PathLike[str] | None = None,
               credentials: str | os.PathLike[str] | None = None,
               timeout_s: int = DEFAULT_TIMEOUT_S, pacing: bool = True,
               dry_run: bool = False) -> dict[str, Any]:
    """Capture init + run the autoload assay for one (job, arm). Never raises on
    a failed assertion — it returns a record whose `verdict` is "fail" and whose
    `problems` say why, so the caller writes the evidence before it aborts."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    expect = expect or expected_presence(arm, pointer_regime=pointer_regime, channel=channel)
    if expect not in ("present", "absent"):
        raise ValueError(f"expect must be present|absent, got {expect!r}")

    home_path = Path(home) if home else (out / "canary_home")
    problems: list[str] = []
    if not dry_run:
        try:
            seed_home(home_path, credentials)
        except Exception as exc:  # noqa: BLE001 — fail closed, with the reason
            problems.append(f"canary home not seeded: {exc}")

    record: dict[str, Any] = {
        "schema_version": CANARY_SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "job_id": job_id,
        "arm": arm,
        "model": model,
        "workspace": str(Path(workspace).resolve()),
        "out_dir": str(out.resolve()),
        "claude_config_dir": str(home_path),
        "sentinel": sentinel,
        "expect": expect,
        "phases": {},
        "argv_delta_from_run": ARGV_DELTA_FROM_RUN,
        "protocol_version": protocol.PROTOCOL_VERSION,
        "pacing_prompt_sha256": protocol.PACING_PROMPT_SHA256,
        "init_sha256": None,
        "canary_sha256": None,
        "sentinel_present": None,
        "cost_usd": 0.0,
        "verdict": "fail",
        "problems": problems,
    }

    init_event: dict[str, Any] | None = None
    for phase in phases:
        if phase not in PHASES:
            problems.append(f"unknown phase {phase!r}")
            continue
        rec = run_phase(phase, workspace=workspace, out_dir=out, home=home_path, model=model,
                        timeout_s=timeout_s, pacing=pacing, dry_run=dry_run)
        ev = rec.pop("_init_event", None)
        record["phases"][phase] = rec
        if isinstance(rec.get("cost_usd"), (int, float)):
            record["cost_usd"] = round(record["cost_usd"] + float(rec["cost_usd"]), 6)
        if rec.get("exit_code") not in (0, None):
            problems.append(f"{phase}: claude exited {rec['exit_code']}")

        if ev is None:
            if not dry_run:
                problems.append(f"{phase}: no system/init event in the stream")
        else:
            # The assay and dump phases run with `--tools ""`, so their init
            # legitimately lists no tools; every OTHER hygiene assertion still
            # applies to them, and they are two extra free samples of it.
            expect_tools = FROZEN_TOOLS if phase == "init" else None
            for prob in init_hygiene_problems(ev, expect_tools):
                problems.append(f"{phase} init: {prob}")
            if phase == "init":
                init_event = ev
                record["cli_version"] = ev.get("claude_code_version")
                # V18: agents is never empty. Recorded, never asserted.
                record["init_agents"] = list(ev.get("agents") or ())

        if phase == "assay" and not dry_run:
            answer = rec.get("answer") or ""
            present = bool(sentinel) and sentinel in answer
            present_ci = bool(sentinel) and sentinel.lower() in answer.lower()
            rec["sentinel_present"] = present
            rec["sentinel_present_case_insensitive"] = present_ci
            rec["said_no_sentinel"] = NO_SENTINEL_LITERAL in answer.upper()
            record["sentinel_present"] = present
            record["canary_sha256"] = canary_sha256(answer)
            if not sentinel:
                problems.append("assay: no sentinel supplied — presence is unfalsifiable")
            elif expect == "present" and not present:
                problems.append(
                    f"assay: sentinel {sentinel!r} ABSENT from the answer but this arm pushes "
                    f"it — d0-push has no mechanism on this machine; answer={answer[:200]!r}"
                )
            elif expect == "absent" and present:
                problems.append(
                    f"assay: sentinel {sentinel!r} PRESENT in a pulled arm's answer — the fact "
                    f"reached the context window with no tool call; answer={answer[:200]!r}"
                )
            elif expect == "absent" and present_ci and not present:
                problems.append(
                    f"assay: sentinel matched only case-insensitively in a pulled arm — "
                    f"answer={answer[:200]!r}"
                )

    if init_event is not None:
        (out / "context_manifest.json").write_text(
            json.dumps(init_event, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        record["init_sha256"] = init_sha256(init_event)
        record["context_manifest_path"] = str(out / "context_manifest.json")

    dump = (record["phases"].get("dump") or {}).get("answer")
    assay = (record["phases"].get("assay") or {}).get("answer")
    body = dump if isinstance(dump, str) and dump.strip() else assay
    if isinstance(body, str):
        (out / "canary.md").write_text(
            f"# canary — job={job_id} arm={arm} model={model}\n"
            f"<!-- generated {record['generated_at']} by lib/wur/canary.py; "
            f"model-authored text below this line -->\n\n{body}\n", encoding="utf-8")
        record["canary_md_path"] = str(out / "canary.md")

    record["verdict"] = "pass" if not problems else "fail"
    (out / "canary.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="per-(job, arm) system/init capture + D0 autoload assay")
    p.add_argument("--workspace", required=True, help="a checkout of the arm's PLANTED tree")
    p.add_argument("--arm", required=True)
    p.add_argument("--sentinel", default=None, help="the arm's nonce")
    p.add_argument("--out-dir", default=None,
                   help="default $JOB_DIR/.registry/conditions/<arm> when --job-dir is given")
    p.add_argument("--job-dir", default=None)
    p.add_argument("--job-id", default=None)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--expect", default=None, choices=["present", "absent"])
    p.add_argument("--pointer-regime", default=None, choices=["none", "prose", "import"])
    p.add_argument("--channel", default=None, choices=["pull", "push", "pointer"])
    p.add_argument("--phases", default=",".join(PHASES))
    p.add_argument("--home", default=None, help="CLAUDE_CONFIG_DIR to seed and use")
    p.add_argument("--credentials", default=None)
    p.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--no-pacing", action="store_true",
                   help="omit --append-system-prompt (ablation only; the run always sends it)")
    p.add_argument("--dry-run", action="store_true", help="print the argv, make no API call")
    a = p.parse_args(argv)

    out_dir = a.out_dir
    if not out_dir:
        if not a.job_dir:
            print("canary: --out-dir or --job-dir is required", file=sys.stderr)
            return 2
        out_dir = str(Path(a.job_dir) / ".registry" / "conditions" / a.arm)

    rec = run_canary(
        a.workspace, arm=a.arm, sentinel=a.sentinel, out_dir=out_dir, job_id=a.job_id,
        model=a.model, expect=a.expect, pointer_regime=a.pointer_regime, channel=a.channel,
        phases=[s.strip() for s in a.phases.split(",") if s.strip()],
        home=a.home, credentials=a.credentials, timeout_s=a.timeout_s,
        pacing=not a.no_pacing, dry_run=a.dry_run,
    )
    print(str(Path(out_dir) / "canary.json"))
    for prob in rec["problems"]:
        print(f"CANARY: {prob}", file=sys.stderr)
    print(
        f"canary {rec['verdict']}: arm={a.arm} expect={rec['expect']} "
        f"present={rec['sentinel_present']} init_sha256={(rec['init_sha256'] or '')[:12]} "
        f"cost=${rec['cost_usd']}",
        file=sys.stderr,
    )
    return 0 if rec["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
