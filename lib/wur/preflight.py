#!/usr/bin/env python3
"""
preflight.py — H1..H12: prove the run is uncontaminated BEFORE the child starts.

RESPONSIBILITY
  Run twelve mechanical assertions over one prepared run directory and write
  $RUN_DIR/hygiene.json. Fail closed: any failed check exits non-zero and the
  cell is not launched. A run that starts dirty produces a row that looks exactly
  like a clean one, and no amount of downstream analysis can tell them apart —
  which is why this gate is worth more than the runs it occasionally blocks.

  NO API CALL. Every check reads the filesystem, the git object database, or an
  artifact some earlier stage already wrote. The one subprocess that is not git
  is `claude --version`, which talks to nobody. (it is invoked with stdin
  closed, and its stderr is never read as a verdict.)

INPUTS
  --run-dir     $RUN_DIR — settings.json, claude_home/, gate/, watch/, workspace/
  --job-dir     $JOB_DIR — for .registry/ reachability and the arm's canary
  --manifest    the arm's plant manifest (nonces, planted paths, expectations)
  everything else is either derived from $RUN_DIR/run_meta.json or overridable.

OUTPUTS
  $RUN_DIR/hygiene.json   {ok, failed[], checks[], ambient_memory[], cli_version}
  stdout                  the hygiene.json path (setup/run scripts read this)
  stderr                  one line per non-pass check, then a summary
  exit 0 clean / 1 dirty / 2 unusable arguments

THE TWELVE
  H1  ambient_memory_absent   no CLAUDE.md above the workspace (pilot gate:
                              `ambient_memory` empty on every run)
  H2  registry_unreachable    .registry/ is not an ancestor of the workspace and
                              no ancestor holds one (under bypassPermissions
                              the agent READ a registry three `..` hops up)
  H3  credentials_isolated    claude_home 700, .credentials.json 600, nothing
                              else seeded — the S2 recipe, exactly
  H4  settings_rendered       the file the CLI will silently ignore if it is
                              malformed, re-validated from outside
  H5  gate_ready              gate/{req,resp} writable and EMPTY — a stale
                              tool_calls.jsonl would continue last run's ordinals
  H6  workspace_clean         no watcher artifact inside workspace/,
                              .claude/ and .cursor/ stripped
  H7  carrier_cardinality     exactly one NOTES.md (zero for ctrl-nofile), and
                              CLAUDE.md present iff the arm carries a pointer
  H8  plant_landed            the nonce IS in baseline_sha for a fact arm, and is
                              NOT for a control arm
  H9  plant_size_cap          every planted file <= 20 KB BY BYTES (the cut
                              point is content-dependent across a 2.4x spread and
                              a 200-byte-line file was cut at line 108, so a line
                              cap measurably does not mitigate anything)
  H10 baseline_committed      HEAD == baseline_sha, worktree clean
  H11 protocol_frozen         protocol.verify_frozen() clean; probe_plan.json sane
  H12 canary_recorded         this arm's autoload canary passed, its init is
                              clean, and the CLI is the version it was taken on

WHAT IS DELIBERATELY NOT ASSERTED
  `system/init.agents == []`. It is never empty — [claude, Explore,
  general-purpose, Plan, statusline-setup] appears under full hygiene on any
  2.1.222 — so that assertion would fail closed on every single run. The
  five are harmless because `Task` is absent from `--tools`. What IS
  asserted is that `tools` set-equals the frozen six and that mcp_servers,
  skills, slash_commands and plugins are empty.

CLI
  python3 lib/wur/preflight.py --run-dir $RUN_DIR --job-dir $JOB_DIR \\
      [--arm d2] [--baseline-sha SHA] [--manifest PATH] [--nonce N ...] \\
      [--expect-notes 1] [--expect-claude-md 0] [--fact-present|--no-fact-present]
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import canary as canary_mod  # noqa: E402
import protocol  # noqa: E402
import settings as settings_mod  # noqa: E402

HYGIENE_SCHEMA_VERSION = "1"

#: V16, and the reason it is a BYTE cap: Read truncation is silent, its cut point
#: is content-dependent (21.6 KB of repeated-char filler to ~51.5 KB of English
#: prose), and it is not a line cap. The value matches render.MAX_PLANT_BYTES so
#: that preflight enforces exactly what plant.py promised; a manifest that
#: declares its own `caps.max_bytes` overrides this.
MAX_PLANT_BYTES = 20_000

#: Auto-loaded memory filenames. AGENTS.md is deliberately absent: it is NOT
#: auto-loaded, which is exactly why rejected it as the carrier.
MEMORY_FILENAMES = ("CLAUDE.md", "CLAUDE.local.md")

#: Names this harness writes. None of them may ever appear inside workspace/ —
#: anything in the workspace is a context-entry channel.
HARNESS_ARTIFACTS = (
    "gate", "watch", "claude_home", "settings.json", "probe_plan.json", "hygiene.json",
    "run_meta.json", "stream.jsonl", "stream.jsonl.gz", "events.jsonl", "exposure.jsonl",
    "probes.jsonl", "fact_trace.jsonl", "use_detect.json", "judge.json", "run_record.json",
    "driver.log", "transcript.jsonl", "git.patch",
)

#: Stripped from the worktree by setup_run.sh; V5 established that
#: `--setting-sources project` keeps CLAUDE.md autoload alive, so a leftover
#: .claude/ would be live project settings the experiment never chose.
FORBIDDEN_WORKSPACE_DIRS = (".claude", ".cursor", ".agents")

PASS, FAIL, WARN, SKIP = "pass", "fail", "warn", "skip"


# ── context ──────────────────────────────────────────────────────────────────
@dataclass
class Ctx:
    """Everything the twelve checks read. Resolved once, from argv + run_meta."""

    run_dir: Path
    workspace: Path
    job_dir: Path | None = None
    arm: str | None = None
    run_id: str | None = None
    job_id: str | None = None
    baseline_sha: str | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    manifest_path: Path | None = None
    nonces: list[str] = field(default_factory=list)
    fact_present: bool | None = None
    expect_notes: int | None = None
    expect_claude_md: bool | None = None
    probe_enabled: bool | None = None
    canary_dir: Path | None = None
    max_plant_bytes: int = MAX_PLANT_BYTES
    check_cli_version: bool = True
    require_canary: bool = True


# ── small helpers ────────────────────────────────────────────────────────────
def _git(ws: Path, *args: str) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=120)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001
        return 127, "", f"{type(exc).__name__}: {exc}"


def _mode(path: Path) -> str | None:
    try:
        return oct(stat.S_IMODE(path.lstat().st_mode))
    except Exception:  # noqa: BLE001
        return None


def _ancestors(path: Path) -> list[Path]:
    """Every strict ancestor of `path`, nearest first, up to the filesystem root."""
    p = path.resolve()
    return list(p.parents)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def read_manifest(path: Path | None) -> dict[str, Any]:
    """Load the arm's plant manifest, tolerating the shapes plant.py may use.

    Read against plant.py's real shape ({arm, fact_present, nonce, paired_nonce,
    factors{}, notes_path, claude_md_path, caps{max_bytes}, files[{path, bytes,
    sha256, carries_nonce}]}) but deliberately tolerant of aliases, so a manifest
    that grows or renames a key degrades to "expectation comes from argv" rather
    than to a wrong answer. What it looks for:
      nonces        `nonce` | `paired_nonce` | `nonces[]` | `facts[].nonce`
      planted files `files[]` (str or {path,bytes}) | `paths[]` | `planted[]`
      fact_present  `fact_present` | `factors.fact_present`
      claude_md     `claude_md_path` | `claude_md` | `factors.pointer_regime`
      notes count   `notes_path` (null ⇒ 0, as in ctrl-nofile) | `notes_count`
      byte cap      `caps.max_bytes`

    `paired_nonce` is what makes a control arm checkable: plant.py leaves `nonce`
    null there but records the fact's nonce anyway, so H8 can assert ABSENCE
    instead of shrugging.
    """
    if not path:
        return {}
    obj = _load_json(path)
    return obj if isinstance(obj, dict) else {}


def manifest_nonces(man: dict[str, Any]) -> list[str]:
    out: list[str] = []
    raw = _first(man, "nonces", default=None)
    if isinstance(raw, str):
        out.append(raw)
    elif isinstance(raw, (list, tuple)):
        out.extend(str(x) for x in raw if x)
    for key in ("nonce", "paired_nonce"):
        single = man.get(key)
        if isinstance(single, str) and single:
            out.append(single)
    for f in man.get("facts") or []:
        if isinstance(f, dict) and f.get("nonce"):
            out.append(str(f["nonce"]))
    seen: list[str] = []
    for n in out:
        if n not in seen:
            seen.append(n)
    return seen


def manifest_files(man: dict[str, Any]) -> list[dict[str, Any]]:
    """[{path, bytes?}] for every file the plant wrote into the workspace."""
    rows: list[dict[str, Any]] = []
    raw = _first(man, "files", "paths", "planted", default=None)
    for item in raw or []:
        if isinstance(item, str):
            rows.append({"path": item})
        elif isinstance(item, dict):
            p = _first(item, "path", "dest", "target", "workspace_path")
            if p:
                rows.append({"path": str(p), "bytes": _first(item, "bytes", "size")})
    return rows


# ── the checks ───────────────────────────────────────────────────────────────
Check = Callable[[Ctx], tuple[str, str, dict[str, Any]]]


def h1_ambient_memory_absent(ctx: Ctx) -> tuple[str, str, dict[str, Any]]:
    """No auto-loaded memory file above the workspace, anywhere up to /."""
    hits: list[str] = []
    for anc in _ancestors(ctx.workspace):
        for name in MEMORY_FILENAMES:
            cand = anc / name
            try:
                if cand.is_file():
                    hits.append(str(cand))
            except Exception:  # noqa: BLE001 — an unreadable ancestor is not a hit
                continue
    home_md = Path.home() / ".claude" / "CLAUDE.md"
    detail = {"ambient_memory": hits,
              "user_memory_exists": home_md.is_file(),
              "note": "the user's ~/.claude/CLAUDE.md is out of reach by construction: "
                      "CLAUDE_CONFIG_DIR is per-run and holds only .credentials.json"}
    if hits:
        return FAIL, f"{len(hits)} ancestor memory file(s): {hits[:3]}", detail
    return PASS, "no ancestor CLAUDE.md", detail


def h2_registry_unreachable(ctx: Ctx) -> tuple[str, str, dict[str, Any]]:
    """The secrets are not on any `..` path out of the workspace."""
    problems: list[str] = []
    detail: dict[str, Any] = {"job_dir": str(ctx.job_dir) if ctx.job_dir else None}
    ancestors = _ancestors(ctx.workspace)
    detail["ancestor_count"] = len(ancestors)

    if ctx.job_dir:
        job_dir = ctx.job_dir.resolve()
        detail["job_dir_is_ancestor"] = job_dir in ancestors
        if job_dir in ancestors:
            problems.append(f"$JOB_DIR is an ancestor of the workspace: {job_dir}")
        registry = job_dir / ".registry"
        detail["registry"] = str(registry)
        detail["registry_exists"] = registry.is_dir()
        detail["registry_mode"] = _mode(registry)
        if registry.is_dir():
            if _mode(registry) != "0o700":
                problems.append(f".registry mode is {_mode(registry)}, want 0o700")
            if _is_within(registry, ctx.workspace):
                problems.append(".registry is inside the workspace")
        elif ctx.require_canary:
            problems.append(f".registry not found at {registry}")

    reachable: list[str] = []
    for anc in ancestors:
        for name in (".registry", "facts.yaml"):
            cand = anc / name
            try:
                if cand.exists():
                    reachable.append(str(cand))
            except Exception:  # noqa: BLE001
                continue
    detail["reachable_from_workspace"] = reachable
    if reachable:
        problems.append(f"registry material reachable by `..` from the workspace: {reachable[:3]}")



    if problems:
        return FAIL, "; ".join(problems), detail
    return PASS, "registry not reachable from the workspace", detail


def h3_credentials_isolated(ctx: Ctx) -> tuple[str, str, dict[str, Any]]:
    """The S2 home recipe: dir 700, .credentials.json 600, and nothing else."""
    home = ctx.run_dir / "claude_home"
    detail: dict[str, Any] = {"claude_home": str(home), "mode": _mode(home)}
    if not home.is_dir():
        return FAIL, f"claude_home missing: {home}", detail
    problems: list[str] = []
    if _mode(home) != "0o700":
        problems.append(f"claude_home mode is {_mode(home)}, want 0o700")
    creds = home / ".credentials.json"
    detail["credentials"] = str(creds)
    detail["credentials_mode"] = _mode(creds)
    if not creds.is_file():
        problems.append("claude_home/.credentials.json missing — the run cannot authenticate")
    elif _mode(creds) != "0o600":
        problems.append(f".credentials.json mode is {_mode(creds)}, want 0o600")
    entries = sorted(p.name for p in home.iterdir())
    detail["entries"] = entries
    for leaked in ("settings.json", "settings.local.json", *MEMORY_FILENAMES):
        if leaked in entries:
            problems.append(f"claude_home carries {leaked} — only .credentials.json may be seeded")
    if problems:
        return FAIL, "; ".join(problems), detail
    return PASS, "claude_home holds only a 600 credentials copy", detail


def h4_settings_rendered(ctx: Ctx) -> tuple[str, str, dict[str, Any]]:
    """The settings file parses and binds exactly the two argv-bound hooks."""
    path = settings_mod.settings_path(ctx.run_dir)
    problems = settings_mod.check_settings_file(path, run_dir=ctx.run_dir, require_barrier=True)
    detail: dict[str, Any] = {"path": str(path)}
    if path.is_file():
        detail["sha256"] = settings_mod.settings_sha256(path)
    if problems:
        return FAIL, "; ".join(problems), detail
    return PASS, "settings.json parses and binds PreToolUse + SessionStart by argv", detail


def h5_gate_ready(ctx: Ctx) -> tuple[str, str, dict[str, Any]]:
    """gate/ and watch/ exist, are writable, and carry nothing from a past run."""
    gate = ctx.run_dir / "gate"
    watch = ctx.run_dir / "watch"
    detail: dict[str, Any] = {"gate": str(gate), "watch": str(watch)}
    problems: list[str] = []
    for d in (gate, gate / "req", gate / "resp", watch):
        if not d.is_dir():
            problems.append(f"missing directory {d}")
        elif not os.access(d, os.W_OK | os.X_OK):
            problems.append(f"not writable: {d}")
    stale = {
        "tool_calls.jsonl": (gate / "tool_calls.jsonl").exists()
        and (gate / "tool_calls.jsonl").stat().st_size > 0,
        "broadcast.json": (gate / "broadcast.json").exists(),
        "req": len(list((gate / "req").glob("*.json"))) if (gate / "req").is_dir() else 0,
        "resp": len(list((gate / "resp").glob("*.json"))) if (gate / "resp").is_dir() else 0,
        "hooks_alive": (watch / "hooks_alive").exists(),
    }
    detail["stale"] = stale
    for name in ("tool_calls.jsonl", "broadcast.json", "hooks_alive"):
        if stale[name]:
            problems.append(f"stale {name} from a previous attempt — barrier ordinals would "
                            f"continue from it")
    for name in ("req", "resp"):
        if stale[name]:
            problems.append(f"{stale[name]} stale gate/{name} file(s)")
    if problems:
        return FAIL, "; ".join(problems), detail
    return PASS, "gate/ and watch/ ready and empty", detail


def h6_workspace_clean(ctx: Ctx) -> tuple[str, str, dict[str, Any]]:
    """No watcher artifact and no stray settings directory inside workspace/."""
    ws = ctx.workspace
    detail: dict[str, Any] = {"workspace": str(ws)}
    if not ws.is_dir():
        return FAIL, f"workspace missing: {ws}", detail
    problems: list[str] = []
    # Nested settings directories are found through git rather than a filesystem
    # walk: the fixture is ~7,200 LOC and the .git directory alone would dominate
    # a recursive glob, whereas the tracked listing is one command.
    tracked, tree_err = _tree_paths(ctx)
    tracked_top = {p.split("/", 1)[0] for p in tracked}
    # A repo may legitimately ship a file called settings.json or a directory
    # called watch/. What may not happen is one appearing that the baseline
    # commit does not contain — that one is ours, and it is a context-entry
    # channel the experiment never chose.
    found = [name for name in HARNESS_ARTIFACTS
             if (ws / name).exists() and name not in tracked_top]
    detail["harness_artifacts_in_workspace"] = found
    detail["tracked_namesakes"] = sorted(n for n in HARNESS_ARTIFACTS if n in tracked_top)
    if found:
        problems.append(f"untracked harness artifacts inside the workspace: {found}")
    settings_dirs = [d for d in FORBIDDEN_WORKSPACE_DIRS if (ws / d).exists()]
    detail["settings_dirs"] = settings_dirs
    if settings_dirs:
        problems.append(f"unstripped settings directories: {settings_dirs} "
                        f"(--setting-sources project would load them)")
    nested = sorted({p.split("/" + d + "/")[0] + "/" + d
                     for p in tracked for d in FORBIDDEN_WORKSPACE_DIRS
                     if "/" + d + "/" in p})
    detail["nested_settings_dirs"] = nested[:5]
    detail["tree_error"] = tree_err
    if nested:
        problems.append(f"{len(nested)} nested settings directory(ies) tracked in the baseline: "
                        f"{nested[:3]}")
    venv = ws / "venv"
    if venv.is_symlink() or venv.exists():
        rc, out, _ = _git(ws, "rev-parse", "--git-path", "info/exclude")
        excluded = False
        if rc == 0:
            exclude_path = Path(out) if Path(out).is_absolute() else ws / out
            try:
                excluded = "/venv" in exclude_path.read_text(encoding="utf-8").splitlines()
            except Exception:  # noqa: BLE001
                excluded = False
        detail["venv_excluded"] = excluded
        if not excluded:
            problems.append("workspace/venv exists but is not in info/exclude — it would land "
                            "in git.patch")
    if problems:
        return FAIL, "; ".join(problems), detail
    return PASS, "workspace carries no harness artifact", detail


def _tree_paths(ctx: Ctx) -> tuple[list[str], str | None]:
    if not ctx.baseline_sha:
        return [], "no baseline_sha"
    rc, out, err = _git(ctx.workspace, "ls-tree", "-r", "--name-only", ctx.baseline_sha)
    if rc != 0:
        return [], f"git ls-tree failed: {err[:200]}"
    return out.splitlines(), None


def h7_carrier_cardinality(ctx: Ctx) -> tuple[str, str, dict[str, Any]]:
    """Exactly one NOTES.md (zero in ctrl-nofile), CLAUDE.md iff the arm points."""
    paths, err = _tree_paths(ctx)
    detail: dict[str, Any] = {"baseline_sha": ctx.baseline_sha}
    if err:
        return FAIL, err, detail
    notes = [p for p in paths if os.path.basename(p) == "NOTES.md"]
    claude = [p for p in paths if os.path.basename(p) in MEMORY_FILENAMES]
    detail.update({"notes": notes, "claude_md": claude,
                   "expect_notes": ctx.expect_notes, "expect_claude_md": ctx.expect_claude_md})
    problems: list[str] = []
    want_notes = 1 if ctx.expect_notes is None else ctx.expect_notes
    if len(notes) != want_notes:
        problems.append(f"{len(notes)} NOTES.md in the baseline tree, expected {want_notes}: {notes}")
    if ctx.expect_claude_md is None:
        if claude:
            problems.append(f"CLAUDE.md present but no expectation was declared: {claude} "
                            f"(pass --expect-claude-md so this is a decision, not an accident)")
    else:
        want = 1 if ctx.expect_claude_md else 0
        if len(claude) != want:
            problems.append(f"{len(claude)} CLAUDE.md in the baseline tree, expected {want}: {claude}")
    if problems:
        return FAIL, "; ".join(problems), detail
    return PASS, f"{len(notes)} NOTES.md, {len(claude)} CLAUDE.md as declared", detail


def h8_plant_landed(ctx: Ctx) -> tuple[str, str, dict[str, Any]]:
    """, verbatim: the nonce is (or is not) in the planted baseline tree."""
    detail: dict[str, Any] = {"nonces": ctx.nonces, "fact_present": ctx.fact_present,
                              "baseline_sha": ctx.baseline_sha}
    if not ctx.baseline_sha:
        return FAIL, "no baseline_sha to grep", detail
    if not ctx.nonces:
        if ctx.fact_present is False:
            return WARN, ("no nonce supplied for a control arm — absence cannot be verified; "
                          "pass --nonce or --manifest to make this a real check"), detail
        return FAIL, ("no nonce supplied — `available` is unverifiable, and a run whose plant "
                      "did not land must be excluded, never counted as a miss"), detail
    found: dict[str, bool] = {}
    for nonce in ctx.nonces:
        rc, _out, err = _git(ctx.workspace, "grep", "-F", "-I", "-i", "-q", "-e", nonce,
                             ctx.baseline_sha, "--", ".")
        if rc not in (0, 1):
            return FAIL, f"git grep failed for {nonce!r}: {err[:200]}", detail
        found[nonce] = rc == 0
    detail["found"] = found
    want = True if ctx.fact_present is None else bool(ctx.fact_present)
    wrong = [n for n, hit in found.items() if hit != want]
    if wrong:
        if want:
            return FAIL, f"plant did not land: {wrong} absent from {ctx.baseline_sha}", detail
        return FAIL, f"control arm carries the nonce: {wrong} present in {ctx.baseline_sha}", detail
    return PASS, f"{len(found)} nonce(s) {'present' if want else 'absent'} as declared", detail


def h9_plant_size_cap(ctx: Ctx) -> tuple[str, str, dict[str, Any]]:
    """Every fact-bearing file <= 20 KB by BYTES."""
    detail: dict[str, Any] = {"max_bytes": ctx.max_plant_bytes}
    if not ctx.baseline_sha:
        return FAIL, "no baseline_sha to size", detail
    rc, out, err = _git(ctx.workspace, "ls-tree", "-r", "-l", ctx.baseline_sha)
    if rc != 0:
        return FAIL, f"git ls-tree -l failed: {err[:200]}", detail
    sizes: dict[str, int] = {}
    for line in out.splitlines():
        try:
            meta, path = line.split("\t", 1)
            size = meta.split()[3]
            sizes[path] = int(size) if size.isdigit() else -1
        except Exception:  # noqa: BLE001
            continue
    candidates = {row["path"] for row in manifest_files(ctx.manifest)}
    candidates |= {p for p in sizes
                   if os.path.basename(p) in ("NOTES.md", *MEMORY_FILENAMES)}
    over: dict[str, int] = {}
    missing: list[str] = []
    for path in sorted(candidates):
        if path not in sizes:
            missing.append(path)
            continue
        if sizes[path] > ctx.max_plant_bytes:
            over[path] = sizes[path]
    detail.update({"checked": sorted(candidates), "sizes": {p: sizes.get(p) for p in candidates},
                   "over": over, "not_in_tree": missing})
    if over:
        return FAIL, (f"planted file(s) over the byte cap: {over} — a fact past the Read cut is "
                      f"silently absent, with no marker anywhere"), detail
    if missing:
        return FAIL, f"manifest lists file(s) not in the baseline tree: {missing}", detail
    if not candidates:
        return WARN, "no planted file identified to size (no manifest, no NOTES.md)", detail
    return PASS, f"{len(candidates)} planted file(s) within {ctx.max_plant_bytes} B", detail


def h10_baseline_committed(ctx: Ctx) -> tuple[str, str, dict[str, Any]]:
    """HEAD is the post-overlay baseline and the worktree is clean."""
    detail: dict[str, Any] = {"baseline_sha": ctx.baseline_sha}
    rc, head, err = _git(ctx.workspace, "rev-parse", "HEAD")
    if rc != 0:
        return FAIL, f"workspace is not a git worktree: {err[:200]}", detail
    detail["head"] = head
    problems: list[str] = []
    if ctx.baseline_sha and head != ctx.baseline_sha:
        problems.append(f"HEAD {head[:12]} != baseline_sha {ctx.baseline_sha[:12]}")
    rc, dirty, _ = _git(ctx.workspace, "status", "--porcelain")
    detail["dirty"] = dirty.splitlines()[:20]
    if rc == 0 and dirty:
        problems.append(f"worktree is dirty before launch ({len(dirty.splitlines())} entries) — "
                        f"the overlay must be committed, or it lands in git.patch")
    # names a FLAT refs/atlas/baseline, but setup_run.sh deliberately writes
    # a per-run refs/atlas/baseline-run/<run_id> instead: refs outside
    # refs/worktree/ are shared by every worktree of the bare repo, so under
    # four-way concurrency the flat name is last-writer-wins and three of four
    # runs would fail this check for a name collision rather than a hygiene
    # problem. Accept either spelling; only warn when NEITHER is set, so a real
    # regression (setup_run.sh stopped pinning the baseline at all) still shows.
    ref_names = ["refs/atlas/baseline"]
    if ctx.run_id:
        ref_names.insert(0, f"refs/atlas/baseline-run/{ctx.run_id}")
    found: dict[str, str] = {}
    for name in ref_names:
        rc_r, ref, _ = _git(ctx.workspace, "rev-parse", name)
        if rc_r == 0:
            found[name] = ref
            if ctx.baseline_sha and ref != ctx.baseline_sha:
                problems.append(f"{name} {ref[:12]} != baseline_sha {ctx.baseline_sha[:12]}")
    detail["baseline_refs"] = found
    detail["refs_atlas_baseline"] = found.get("refs/atlas/baseline")
    if problems:
        return FAIL, "; ".join(problems), detail
    if not found:
        return WARN, (f"no baseline ref set (looked for {', '.join(ref_names)}) — "
                      f"nothing keeps the baseline commit reachable once the worktree "
                      f"is removed"), detail
    return PASS, (f"HEAD == baseline_sha {head[:12]}, worktree clean, "
                  f"baseline pinned by {next(iter(found))}"), detail


def h11_protocol_frozen(ctx: Ctx) -> tuple[str, str, dict[str, Any]]:
    """The frozen strings are intact and the probe plan is sane."""
    detail: dict[str, Any] = {"frozen_hashes": protocol.frozen_hashes()}
    problems = list(protocol.verify_frozen())
    plan_path = ctx.run_dir / "probe_plan.json"
    detail["probe_plan"] = str(plan_path)
    plan = _load_json(plan_path)
    if plan is None:
        problems.append(f"probe_plan.json missing or unparseable at {plan_path} — the seeded "
                        f"cadence must exist before the child starts, so it survives a dead run")
    else:
        fire_at = plan.get("fire_at")
        detail["fire_at"] = fire_at
        detail["seed_material"] = plan.get("seed_material")
        if not isinstance(fire_at, list):
            problems.append("probe_plan.fire_at is not a list")
        else:
            if any(b <= a for a, b in zip(fire_at, fire_at[1:])):
                problems.append(f"probe_plan.fire_at is not strictly increasing: {fire_at}")
            if ctx.probe_enabled and not fire_at:
                problems.append("probe arm but probe_plan.fire_at is empty")
            cap = plan.get("max_probes")
            if isinstance(cap, int) and len(fire_at) > cap:
                problems.append(f"probe_plan has {len(fire_at)} probes, over max_probes {cap}")
    if problems:
        return FAIL, "; ".join(problems), detail
    return PASS, "frozen protocol intact; probe plan present and monotone", detail


def _cli_version() -> tuple[str | None, str | None]:
    try:
        p = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    if p.returncode != 0:
        return None, (p.stderr or "").strip()[:200]
    return (p.stdout or "").strip().split()[0] if p.stdout.strip() else None, None


def h12_canary_recorded(ctx: Ctx) -> tuple[str, str, dict[str, Any]]:
    """This arm's autoload canary passed, and the CLI has not moved under it."""
    detail: dict[str, Any] = {"canary_dir": str(ctx.canary_dir) if ctx.canary_dir else None}
    problems: list[str] = []
    version, verr = (None, None)
    if ctx.check_cli_version:
        version, verr = _cli_version()
        detail["cli_version"] = version
        if verr:
            problems.append(f"claude --version failed: {verr}")

    if not ctx.canary_dir:
        if ctx.require_canary:
            problems.append("no canary directory known for this arm")
        return (FAIL if problems else SKIP), "; ".join(problems) or "canary not requested", detail



    rec = _load_json(ctx.canary_dir / "canary.json")
    manifest_init = _load_json(ctx.canary_dir / "context_manifest.json")
    detail["canary_json"] = bool(rec)
    detail["context_manifest"] = bool(manifest_init)
    if rec is None:
        problems.append(f"no canary.json in {ctx.canary_dir} — d0-push's exposure_basis is "
                        f"'manifest_canary' and this file is the whole evidence for it")
    else:
        detail["verdict"] = rec.get("verdict")
        detail["expect"] = rec.get("expect")
        detail["sentinel_present"] = rec.get("sentinel_present")
        detail["canary_sha256"] = rec.get("canary_sha256")
        detail["init_sha256"] = rec.get("init_sha256")
        if rec.get("verdict") != "pass":
            problems.append(f"canary verdict is {rec.get('verdict')!r}: {rec.get('problems')}")
        if ctx.arm and rec.get("arm") not in (None, ctx.arm):
            problems.append(f"canary is for arm {rec.get('arm')!r}, this run is {ctx.arm!r}")
        if version and rec.get("cli_version") and rec["cli_version"] != version:
            problems.append(f"CLI moved since the canary: {rec['cli_version']} -> {version}")
    if manifest_init is None:
        problems.append("no context_manifest.json (the verbatim system/init) for this arm")
    else:
        init_problems = canary_mod.init_hygiene_problems(manifest_init, canary_mod.FROZEN_TOOLS)
        detail["init_problems"] = init_problems
        detail["init_agents"] = list(manifest_init.get("agents") or ())
        problems.extend(f"init: {p}" for p in init_problems)
    if problems:
        return FAIL, "; ".join(problems), detail
    return PASS, "canary passed and the CLI matches the version it was taken on", detail


CHECKS: tuple[tuple[str, str, Check], ...] = (
    ("H1", "ambient_memory_absent", h1_ambient_memory_absent),
    ("H2", "registry_unreachable", h2_registry_unreachable),
    ("H3", "credentials_isolated", h3_credentials_isolated),
    ("H4", "settings_rendered", h4_settings_rendered),
    ("H5", "gate_ready", h5_gate_ready),
    ("H6", "workspace_clean", h6_workspace_clean),
    ("H7", "carrier_cardinality", h7_carrier_cardinality),
    ("H8", "plant_landed", h8_plant_landed),
    ("H9", "plant_size_cap", h9_plant_size_cap),
    ("H10", "baseline_committed", h10_baseline_committed),
    ("H11", "protocol_frozen", h11_protocol_frozen),
    ("H12", "canary_recorded", h12_canary_recorded),
)


# ── driver ───────────────────────────────────────────────────────────────────
def preflight(ctx: Ctx) -> dict[str, Any]:
    """Run all twelve. A check that raises is a FAILED check, never a crash."""
    rows: list[dict[str, Any]] = []
    for cid, name, fn in CHECKS:
        started = time.time()
        try:
            status, reason, detail = fn(ctx)
        except Exception as exc:  # noqa: BLE001 — fail closed on our own bugs too
            status, reason, detail = FAIL, f"{type(exc).__name__}: {exc}", {}
        rows.append({"id": cid, "name": name, "status": status, "reason": reason,
                     "detail": detail, "seconds": round(time.time() - started, 4)})
    failed = [r["id"] for r in rows if r["status"] == FAIL]
    warned = [r["id"] for r in rows if r["status"] == WARN]
    ambient = next((r["detail"].get("ambient_memory", []) for r in rows if r["id"] == "H1"), [])
    return {
        "schema_version": HYGIENE_SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": ctx.run_id,
        "job_id": ctx.job_id,
        "arm": ctx.arm,
        "run_dir": str(ctx.run_dir),
        "workspace": str(ctx.workspace),
        "baseline_sha": ctx.baseline_sha,
        "ok": not failed,
        "failed": failed,
        "warned": warned,
        "ambient_memory": ambient,
        "cli_version": next((r["detail"].get("cli_version") for r in rows if r["id"] == "H12"), None),
        "checks": rows,
    }


def build_ctx(args: argparse.Namespace) -> Ctx:
    """Resolve the context from argv, then fill the gaps from run_meta/manifest."""
    run_dir = Path(args.run_dir).resolve()
    workspace = Path(args.workspace).resolve() if args.workspace else run_dir / "workspace"
    meta = _load_json(run_dir / "run_meta.json") or {}
    manifest_path = Path(args.manifest).resolve() if args.manifest else None
    man = read_manifest(manifest_path)
    factors = man.get("factors") if isinstance(man.get("factors"), dict) else {}

    job_dir = args.job_dir or meta.get("job_dir")
    arm = args.arm or man.get("arm") or man.get("condition") or meta.get("condition_id") \
        or meta.get("env_id")
    baseline = args.baseline_sha or meta.get("baseline_sha") or man.get("baseline_sha") \
        or meta.get("base_repo_sha")

    nonces = list(args.nonce or [])
    nonces += [n for n in manifest_nonces(man) if n not in nonces]

    fact_present = args.fact_present
    if fact_present is None:
        fact_present = _first(man, "fact_present", default=factors.get("fact_present"))
    if fact_present is None and arm:
        fact_present = not str(arm).startswith("ctrl")

    expect_claude_md = args.expect_claude_md
    if expect_claude_md is None:
        if "claude_md_path" in man:
            expect_claude_md = bool(man["claude_md_path"])
        else:
            raw = _first(man, "claude_md", "has_claude_md", default=None)
            if raw is not None:
                expect_claude_md = bool(raw)
            else:
                regime = _first(man, "pointer_regime", default=factors.get("pointer_regime"))
                if regime is not None:
                    expect_claude_md = regime in ("prose", "import")
                elif arm in ("d0-push", "d1-ptr"):
                    expect_claude_md = True
                elif arm:
                    expect_claude_md = False

    expect_notes = args.expect_notes
    if expect_notes is None:
        if "notes_path" in man:
            expect_notes = 1 if man["notes_path"] else 0
        else:
            raw = _first(man, "notes_count", default=None)
            expect_notes = int(raw) if isinstance(raw, int) else (0 if arm == "ctrl-nofile" else 1)



    max_bytes = args.max_plant_bytes
    if max_bytes is None:
        caps = man.get("caps") if isinstance(man.get("caps"), dict) else {}
        raw_cap = caps.get("max_bytes")
        max_bytes = int(raw_cap) if isinstance(raw_cap, int) and raw_cap > 0 else MAX_PLANT_BYTES



    probe_enabled = args.probe
    if probe_enabled is None:
        probe_enabled = _first(man, "probe", default=factors.get("probe"))
    if probe_enabled is None and arm:
        probe_enabled = not str(arm).endswith("-np")

    canary_dir = Path(args.canary_dir).resolve() if args.canary_dir else (
        Path(job_dir).resolve() / ".registry" / "conditions" / str(arm) if job_dir and arm else None
    )

    return Ctx(
        run_dir=run_dir,
        workspace=workspace,
        job_dir=Path(job_dir).resolve() if job_dir else None,
        arm=arm,
        run_id=args.run_id or meta.get("run_id"),
        job_id=args.job_id or meta.get("job_id"),
        baseline_sha=baseline,
        manifest=man,
        manifest_path=manifest_path,
        nonces=nonces,
        fact_present=None if fact_present is None else bool(fact_present),
        expect_notes=expect_notes,
        expect_claude_md=None if expect_claude_md is None else bool(expect_claude_md),
        probe_enabled=None if probe_enabled is None else bool(probe_enabled),
        canary_dir=canary_dir,
        max_plant_bytes=max_bytes,
        check_cli_version=not args.skip_cli_version,
        require_canary=not args.no_canary,
    )


def _bool_flag(p: argparse.ArgumentParser, name: str, dest: str, help_: str) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument(f"--{name}", dest=dest, action="store_true", default=None, help=help_)
    g.add_argument(f"--no-{name}", dest=dest, action="store_false", default=None)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="WUR preflight H1..H12 (fail closed, no API call)")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--workspace", default=None)
    p.add_argument("--job-dir", default=None)
    p.add_argument("--arm", "--condition", dest="arm", default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument("--job-id", default=None)
    p.add_argument("--baseline-sha", default=None)
    p.add_argument("--manifest", default=None, help="the arm's plant manifest.json")
    p.add_argument("--nonce", action="append", default=None)
    p.add_argument("--expect-notes", type=int, default=None)
    p.add_argument("--canary-dir", default=None)
    p.add_argument("--max-plant-bytes", type=int, default=None,
                   help=f"default: the manifest's caps.max_bytes, else {MAX_PLANT_BYTES}")
    p.add_argument("--skip-cli-version", action="store_true")
    p.add_argument("--no-canary", action="store_true",
                   help="this arm has no canary yet (bootstrap only; H12 degrades to a check "
                        "of the CLI version)")
    p.add_argument("--out", default=None)
    _bool_flag(p, "fact-present", "fact_present", "the arm plants a fact (default: from the arm id)")
    _bool_flag(p, "expect-claude-md", "expect_claude_md", "the arm carries a CLAUDE.md")
    _bool_flag(p, "probe", "probe", "the arm is probed")
    a = p.parse_args(argv)

    run_dir = Path(a.run_dir)
    if not run_dir.is_dir():
        print(f"preflight: run dir does not exist: {run_dir}", file=sys.stderr)
        return 2

    ctx = build_ctx(a)
    report = preflight(ctx)
    out = Path(a.out) if a.out else run_dir / "hygiene.json"
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, out)

    print(str(out))
    for row in report["checks"]:
        if row["status"] != PASS:
            print(f"{row['status'].upper()} {row['id']} {row['name']}: {row['reason']}",
                  file=sys.stderr)
    print(f"preflight {'OK' if report['ok'] else 'FAILED'}: "
          f"{len(report['failed'])} failed, {len(report['warned'])} warned, arm={ctx.arm} "
          f"-> {out}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
