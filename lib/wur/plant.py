#!/usr/bin/env python3
"""
plant.py — render the §7.1 arms into overlays, stamp them, and prove they landed.

RESPONSIBILITY
  Turn one fact plus one arm into the exact bytes that are copied into a
  workspace, and into a manifest that says what those bytes are. Three things
  make this more than a file-copier:

    * SKELETON MATCHING. `docs/`, `docs/internal/` and `docs/internal/memory/`
      exist in EVERY arm, including both controls, each carrying one nonce-free
      filler so git tracks the directory (§7.1). If the directory tree differed
      by arm, "depth" would be confounded with "the repo has a docs/ tree", and
      the d1->d2->d3 read-rate curve — the price list this whole instrument
      exists to produce — would be uninterpretable.
    * ONE NOTES.md, ALWAYS NOTES.md. Exactly one per workspace (zero in
      `ctrl-nofile`), named NOTES.md at every depth, so filename salience is
      never confounded with depth. `AGENTS.md` was rejected as the carrier: it
      is not auto-loaded yet carries strong "read me" priors from training.
    * THE MANIFEST IS A SIBLING OF overlay/, NEVER INSIDE IT (§5.3). Anything
      inside overlay/ is copied into the workspace, and a manifest in the
      workspace would hand the agent the answer key — plus it would change
      `workspace_sha256` and appear in `git.patch`.

INPUTS
  A minted facts.Registry (or a single FactSpec), an arm id from ARMS, and a job
  dir. Optionally a workspace + baseline sha for `assert-applied`.

OUTPUTS
  $JOB_DIR/.registry/conditions/<condition_dir>/overlay/...   the planted files
  $JOB_DIR/.registry/conditions/<condition_dir>/manifest.json  sibling, stamped
  $JOB_DIR/.registry/_index/{probe_key,render_report}.json
  `overlay_sha256` — a content hash of the overlay tree, carried in the manifest
  and in run_record.condition.workspace_sha256's provenance chain.

  Every planted file is capped at render.MAX_PLANT_BYTES = 20,000 bytes. V16
  measured that `Read` truncates SILENTLY below its 256 KB ceiling, with a
  content-dependent cut as low as 21,600 bytes; a fact past the cut looks like
  "read but not used" when it was never exposed at all.

CONDITION DIRECTORY LAYOUT — a documented departure from §5.3
  §5.3 draws `.registry/conditions/<arm>/overlay/`, which holds exactly one
  overlay per arm. But §7.2 runs 12 TASKS x 12 arms with one fact per task (D1),
  and each task's NOTES.md carries a different fact — so a flat per-arm path
  cannot hold them. `condition_dir()` therefore emits `<arm>` for a single-fact
  registry (§5.3 verbatim, which is what a single-task job and the canary use)
  and `<task_id>/<arm>` for a multi-fact one. The manifest records which layout
  it used, and it is always a sibling of its own overlay/ either way.

CLI
  python3 lib/wur/plant.py arms
  python3 lib/wur/plant.py render        --job-dir D [--facts F] [--arms a,b] [--task-id T] [--force]
  python3 lib/wur/plant.py verify        --job-dir D [--arm A] [--task-id T]
  python3 lib/wur/plant.py assert-applied --job-dir D --arm A --workspace W
                                          [--task-id T] [--baseline-sha SHA]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # flat import (lib/wur on sys.path)
    sys.path.insert(0, str(_HERE))

try:  # package import shares one module object with the rest of lib/wur
    from . import facts as facts_mod
    from . import nonce as nonce_mod
    from . import render as render_mod
except ImportError:  # pragma: no cover - exercised when run as a script
    import facts as facts_mod  # type: ignore[no-redef]
    import nonce as nonce_mod  # type: ignore[no-redef]
    import render as render_mod  # type: ignore[no-redef]

PLANT_VERSION = "wur-plant-v1"
MANIFEST_BASENAME = "manifest.json"
OVERLAY_DIRNAME = "overlay"
CONDITIONS_DIRNAME = "conditions"
INDEX_DIRNAME = "_index"

NOTES_BASENAME = "NOTES.md"
CLAUDE_MD_BASENAME = "CLAUDE.md"

# §7.1 skeleton matching: present in every arm, each with one nonce-free filler
# so git tracks the directory.
SKELETON_DIRS: tuple[str, ...] = ("docs", "docs/internal", "docs/internal/memory")
SKELETON_FILLER_NAME = "index.md"

DIR_MODE = 0o700          # the registry is 700 (V9); overlay dirs inherit it
FILE_MODE = 0o644         # these files are destined for a workspace the agent reads


class PlantError(Exception):
    """A plant could not be rendered, does not verify, or did not land."""


# ── the arms (IMPLEMENTATION.md §7.1) ────────────────────────────────────────
@dataclass(frozen=True)
class Arm:
    """One row of the §7.1 arms table, in executable form."""

    arm_id: str
    depth: str | None            # d0 | d1 | d2 | d3 | None
    notes_path: str | None       # always basenamed NOTES.md, or None
    fmt: str | None              # prose | checklist | table | None
    channel: str | None          # push | pointer | pull | None
    pointer_regime: str          # import | prose | none
    distractors: int
    fact_present: bool
    probe: bool
    purpose: str

    @property
    def claude_md_path(self) -> str | None:
        return CLAUDE_MD_BASENAME if self.pointer_regime != "none" else None

    @property
    def exposure_basis(self) -> str:
        """`d0-push` is ASSERTED, not scanned (§4.2.2) — its content is in no log."""
        return "manifest_canary" if self.pointer_regime == "import" else "event_stream"

    @property
    def autoload_fact(self) -> bool:
        return self.pointer_regime == "import"

    def factors(self) -> dict:
        """run_record.condition.factors / fact_trace.factors, verbatim shape."""
        return {
            "depth": self.depth,
            "format": self.fmt,
            "channel": self.channel,
            "distractors": self.distractors,
            "fact_present": self.fact_present,
            "probe": self.probe,
            "pointer_regime": self.pointer_regime,
        }

    def to_dict(self) -> dict:
        return {
            "arm_id": self.arm_id,
            "notes_path": self.notes_path,
            "claude_md_path": self.claude_md_path,
            "exposure_basis": self.exposure_basis,
            "autoload_fact": self.autoload_fact,
            "purpose": self.purpose,
            **self.factors(),
        }


_D3 = "docs/internal/memory/" + NOTES_BASENAME
_D2 = "docs/" + NOTES_BASENAME
_D1 = NOTES_BASENAME

ARMS: dict[str, Arm] = {
    a.arm_id: a
    for a in (
        Arm("d0-push", "d0", _D1, "prose", "push", "import", 0, True, True, "pushed channel"),
        Arm("d1-ptr", "d1", _D1, "prose", "pointer", "prose", 0, True, True, "pointer without push"),
        Arm("d1", "d1", _D1, "prose", "pull", "none", 0, True, True, "shallowest pulled"),
        Arm("d2", "d2", _D2, "prose", "pull", "none", 0, True, True, "hub cell"),
        Arm("d3", "d3", _D3, "prose", "pull", "none", 0, True, True, "deepest pulled"),
        Arm("d2-check", "d2", _D2, "checklist", "pull", "none", 0, True, True, "format"),
        Arm("d2-table", "d2", _D2, "table", "pull", "none", 0, True, True, "format"),
        Arm("d2-dist", "d2", _D2, "prose", "pull", "none", 3, True, True, "discrimination"),
        Arm("ctrl", "d2", _D2, "prose", "pull", "none", 0, False, True, "primary control + Gate 2"),
        Arm("ctrl-nofile", None, None, None, None, "none", 0, False, True, "secondary control (D3)"),
        Arm("d1-np", "d1", _D1, "prose", "pull", "none", 0, True, False, "probe reactivity"),
        Arm("d3-np", "d3", _D3, "prose", "pull", "none", 0, True, False, "probe reactivity"),
        # §7.2's pilot arithmetic already uses a 13th arm: the difficulty band is
        # defined on ctrl + no-probe. It is not in the §7.1 table, so it is not in
        # ARMS_V1 and is never planted unless asked for by name.
        Arm("ctrl-np", "d2", _D2, "prose", "pull", "none", 0, False, False,
            "pilot difficulty band (§7.2), control without probe"),
    )
}

ARMS_V1: tuple[str, ...] = (
    "d0-push", "d1-ptr", "d1", "d2", "d3", "d2-check", "d2-table", "d2-dist",
    "ctrl", "ctrl-nofile", "d1-np", "d3-np",
)
assert len(ARMS_V1) == 12, "§7.1 defines exactly 12 arms"


def arm(arm_id: str) -> Arm:
    try:
        return ARMS[arm_id]
    except KeyError:
        raise PlantError(f"unknown arm {arm_id!r}; known: {sorted(ARMS)}") from None


# ── planning (pure: no filesystem) ───────────────────────────────────────────
@dataclass(frozen=True)
class PlantPlan:
    """Exactly what an arm's overlay contains, before anything is written."""

    arm_id: str
    task_id: str | None
    fact_id: str
    fact_present: bool
    nonce: str | None          # the nonce actually planted (None in control arms)
    paired_nonce: str | None   # the fact's nonce even when it is NOT planted
    files: dict[str, str]      # overlay-relative path -> content
    render_report: dict

    @property
    def overlay_sha256(self) -> str:
        return overlay_sha256(self.files)


def overlay_sha256(files: Mapping[str, str]) -> str:
    """Content hash of an overlay tree: sorted `path\\0sha256(content)` lines.

    Path-sensitive as well as content-sensitive, so moving NOTES.md from `docs/`
    to `docs/internal/memory/` changes the stamp — which is the entire point of
    the depth ladder.
    """
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(files[path].encode("utf-8")).hexdigest().encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def skeleton_files() -> dict[str, str]:
    """The three filler files. Byte-identical in every arm, by construction."""
    return {f"{d}/{SKELETON_FILLER_NAME}": render_mod.render_filler(f"{d}/{SKELETON_FILLER_NAME}")
            for d in SKELETON_DIRS}


def plan(fact: "facts_mod.FactSpec", arm_id: str) -> PlantPlan:
    """Compute one arm's overlay for one fact. Pure, deterministic, unwritten.

    Control arms get render.control_fact()'s fact-free twin, checked against the
    fact's own tier-(b) regexes: a control document that matched one would
    register as a mention with `available = false`, manufacturing exactly the
    confabulation that the `confab_rate <= 0.05` pilot gate exists to detect.
    """
    a = arm(arm_id)
    if a.fact_present and not fact.nonce:
        raise PlantError(
            f"{fact.fact_id}: no nonce minted — run `facts.py mint` before planting {arm_id!r}"
        )

    files: dict[str, str] = skeleton_files()

    if a.fact_present:
        available = fact.distractors
        if a.distractors > len(available):
            raise PlantError(
                f"{fact.fact_id}: arm {arm_id!r} needs {a.distractors} distractors, registry has "
                f"{len(available)}"
            )
        canonical = fact.canonical(with_distractors=a.distractors > 0)
        ds = canonical.distractors[: a.distractors]
        doc_fact = canonical
    else:
        doc_fact = render_mod.control_fact(
            fact.canonical(),
            control=fact.control,
            regexes=fact.paraphrase_regexes,
            distractor_tokens=fact.distractor_tokens(),
        )
        ds = ()

    report: dict = {}
    if a.notes_path is not None:
        if Path(a.notes_path).name != NOTES_BASENAME:
            raise PlantError(f"{arm_id}: carrier must be named {NOTES_BASENAME}, got {a.notes_path}")
        text = render_mod.render(doc_fact, a.fmt or "prose", distractors=ds)
        files[a.notes_path] = text
        report = render_mod.render_report(doc_fact, {a.fmt or "prose": text}, distractors=ds)

    if a.claude_md_path is not None:
        files[a.claude_md_path] = render_mod.render_claude_md(
            a.pointer_regime, a.notes_path or NOTES_BASENAME
        )

    _assert_plan_invariants(a, fact, files)
    return PlantPlan(
        arm_id=arm_id,
        task_id=fact.task_id,
        fact_id=fact.fact_id,
        fact_present=a.fact_present,
        nonce=fact.nonce if a.fact_present else None,
        paired_nonce=fact.nonce,
        files=files,
        render_report=report,
    )


def _assert_plan_invariants(a: Arm, fact: "facts_mod.FactSpec", files: Mapping[str, str]) -> None:
    problems: list[str] = []

    notes = [p for p in files if Path(p).name == NOTES_BASENAME]
    want = 0 if a.notes_path is None else 1
    if len(notes) != want:
        problems.append(f"expected {want} {NOTES_BASENAME}, found {len(notes)}: {notes}")

    for d in SKELETON_DIRS:
        if f"{d}/{SKELETON_FILLER_NAME}" not in files:
            problems.append(f"skeleton directory {d}/ missing its filler")

    ns = nonce_mod.NonceSet(
        {fact.fact_id: fact.nonce} if fact.nonce else {},
        surface_forms={fact.fact_id: list(fact.surface_forms)} if fact.surface_forms else None,
    )
    for path, text in files.items():
        rep = render_mod.length_report(text)
        if rep["bytes"] > render_mod.MAX_PLANT_BYTES:
            problems.append(f"{path}: {rep['bytes']} bytes over the {render_mod.MAX_PLANT_BYTES} cap")
        if rep["lines"] > render_mod.MAX_PLANT_LINES:
            problems.append(f"{path}: {rep['lines']} lines over the {render_mod.MAX_PLANT_LINES} cap")
        carries = bool(ns.find(text))
        is_carrier = path == a.notes_path and a.fact_present
        if carries and not is_carrier:
            problems.append(
                f"{path}: carries the nonce but is not the carrier — a pointer or a filler that "
                "quotes the fact pushes it, and the pointer/push contrast collapses"
            )
        if is_carrier and not carries:
            problems.append(f"{path}: is the carrier but does not contain the nonce")

    if a.pointer_regime == "import":
        stub = files.get(CLAUDE_MD_BASENAME, "")
        if f"@{a.notes_path}" not in stub:
            problems.append(f"{CLAUDE_MD_BASENAME}: import regime but no @{a.notes_path} stub")
    elif a.pointer_regime == "prose":
        stub = files.get(CLAUDE_MD_BASENAME, "")
        if f"@{a.notes_path}" in stub:
            problems.append(
                f"{CLAUDE_MD_BASENAME}: prose pointer must not carry an @import — it would push "
                "the fact and `d1-ptr` would stop isolating the pointer"
            )

    if problems:
        raise PlantError(f"arm {a.arm_id}: " + "; ".join(problems))


# ── on-disk layout ───────────────────────────────────────────────────────────
def conditions_root(job_dir: str | Path, *, create: bool = False) -> Path:
    d = facts_mod.registry_dir(job_dir, create=create) / CONDITIONS_DIRNAME
    if create:
        d.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    return d


def condition_dir(
    job_dir: str | Path, arm_id: str, task_id: str | None = None, *, create: bool = False
) -> Path:
    """`.registry/conditions/<arm>` or `.registry/conditions/<task_id>/<arm>`.

    See the module docstring: §5.3's flat path is kept for single-fact registries
    and extended by task when a job carries one fact per task (D1 x 12 tasks).
    """
    d = conditions_root(job_dir, create=create)
    d = d / task_id / arm_id if task_id else d / arm_id
    if create:
        d.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    return d


def manifest_path(job_dir: str | Path, arm_id: str, task_id: str | None = None) -> Path:
    return condition_dir(job_dir, arm_id, task_id) / MANIFEST_BASENAME


def overlay_dir(job_dir: str | Path, arm_id: str, task_id: str | None = None) -> Path:
    return condition_dir(job_dir, arm_id, task_id) / OVERLAY_DIRNAME


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, FILE_MODE)


def _manifest(
    plan_: PlantPlan,
    reg: "facts_mod.Registry | None",
    *,
    layout: str,
    generated_at: str | None,
) -> dict:
    a = arm(plan_.arm_id)
    ns = nonce_mod.NonceSet({plan_.fact_id: plan_.paired_nonce} if plan_.paired_nonce else {})
    files = []
    for path in sorted(plan_.files):
        text = plan_.files[path]
        rep = render_mod.length_report(text)
        files.append(
            {
                "path": path,
                "bytes": rep["bytes"],
                "lines": rep["lines"],
                "sha256": rep["sha256"],
                "carries_nonce": bool(ns.find(text)),
            }
        )
    return {
        "schema_version": "1",
        "plant_version": PLANT_VERSION,
        "renderer_version": render_mod.RENDERER_VERSION,
        "generated_at": generated_at,
        "layout": layout,
        "arm": plan_.arm_id,
        "task_id": plan_.task_id,
        "fact_id": plan_.fact_id,
        "fact_present": plan_.fact_present,
        "nonce": plan_.nonce,
        "paired_nonce": plan_.paired_nonce,
        "factors": a.factors(),
        "notes_path": a.notes_path,
        "claude_md_path": a.claude_md_path,
        "autoload_fact": a.autoload_fact,
        "exposure_basis": a.exposure_basis,
        "skeleton_dirs": list(SKELETON_DIRS),
        "skeleton_filler": SKELETON_FILLER_NAME,
        "caps": {"max_bytes": render_mod.MAX_PLANT_BYTES, "max_lines": render_mod.MAX_PLANT_LINES},
        "max_file_bytes": max((f["bytes"] for f in files), default=0),
        "files": files,
        "overlay_sha256": plan_.overlay_sha256,
        "render_report": plan_.render_report,
        "registry": {
            "salt": getattr(reg, "salt", None),
            "repo_sha": getattr(reg, "repo_sha", None),
            "schema_version": getattr(reg, "schema_version", None),
            "tier": getattr(reg, "tier", None),
        },
    }


def write_arm(
    job_dir: str | Path,
    fact: "facts_mod.FactSpec",
    arm_id: str,
    *,
    reg: "facts_mod.Registry | None" = None,
    layout: str = "auto",
    task_id: str | None = None,
    force: bool = False,
    generated_at: str | None = None,
) -> dict:
    """Render one arm to disk and return its manifest.

    The overlay directory is REPLACED wholesale under `--force`, never merged: a
    stale file left behind from a previous plant would be copied into the
    workspace and would not appear in overlay_sha256's inputs.
    """
    plan_ = plan(fact, arm_id)
    cdir = condition_dir(job_dir, arm_id, task_id, create=True)
    odir = cdir / OVERLAY_DIRNAME
    if odir.exists():
        if not force:
            raise PlantError(f"{odir} already exists; pass force=True to replace it")
        shutil.rmtree(odir)
    odir.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    for rel, text in plan_.files.items():
        _write(odir / rel, text)

    man = _manifest(plan_, reg, layout=layout, generated_at=generated_at)
    mpath = cdir / MANIFEST_BASENAME
    if mpath.parent != cdir or OVERLAY_DIRNAME in mpath.relative_to(cdir).parts:
        raise PlantError("manifest.json must be a sibling of overlay/, never inside it (§5.3)")
    _write(mpath, json.dumps(man, indent=2, sort_keys=False) + "\n")
    os.chmod(mpath, facts_mod.REGISTRY_FILE_MODE)
    return man


def render_job(
    job_dir: str | Path,
    reg: "facts_mod.Registry",
    *,
    arms: Sequence[str] = ARMS_V1,
    task_ids: Sequence[str] | None = None,
    layout: str = "auto",
    force: bool = False,
    stamp_time: bool = True,
) -> dict:
    """Render every (task, arm) overlay for a job, plus the two `_index` files.

    Returns {manifests: [...], probe_key: {...}, render_report: {...}}. Writing
    `_index/probe_key.json` here rather than in facts.py is deliberate: it maps
    fact_id -> source_path, and the planted path is knowledge only the plant has.
    """
    selected = [f for f in reg.facts if task_ids is None or f.task_id in set(task_ids)]
    if not selected:
        raise PlantError(f"no facts match task_ids={task_ids!r}")
    flat = layout == "flat" or (layout == "auto" and len(reg.facts) == 1)
    if layout == "flat" and len(selected) > 1:
        raise PlantError(
            "layout='flat' with more than one fact would overwrite one arm's overlay with "
            "another task's fact; use layout='by-task'"
        )
    generated_at = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat() if stamp_time else None
    )

    manifests: list[dict] = []
    probe_key: dict[str, dict] = {}
    reports: dict[str, dict] = {}
    for fact in selected:
        tid = None if flat else fact.task_id
        sources: dict[str, str | None] = {}
        for arm_id in arms:
            man = write_arm(
                job_dir, fact, arm_id, reg=reg, layout=("flat" if flat else "by-task"),
                task_id=tid, force=force, generated_at=generated_at,
            )
            manifests.append(man)
            sources[arm_id] = man["notes_path"] if man["fact_present"] else None
            if man["render_report"]:
                reports[f"{fact.fact_id}:{arm_id}"] = man["render_report"]
        probe_key[fact.fact_id] = {
            "token": fact.nonce,
            "surface_forms": list(fact.surface_forms),
            "gist": fact.gist,
            "task_id": fact.task_id,
            "distractor_tokens": list(fact.distractor_tokens()),
            "source_path": sources.get("d2") or next((v for v in sources.values() if v), None),
            "source_paths": sources,
        }

    idx = facts_mod.registry_dir(job_dir, create=True) / INDEX_DIRNAME
    idx.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    _write(idx / "probe_key.json", json.dumps(probe_key, indent=2, sort_keys=True) + "\n")
    os.chmod(idx / "probe_key.json", facts_mod.REGISTRY_FILE_MODE)
    _write(
        idx / "render_report.json",
        json.dumps(
            {"plant_version": PLANT_VERSION, "generated_at": generated_at, "facts": reports},
            indent=2, sort_keys=True,
        )
        + "\n",
    )
    os.chmod(idx / "render_report.json", facts_mod.REGISTRY_FILE_MODE)
    return {"manifests": manifests, "probe_key": probe_key, "render_report": reports}


# ── verification ─────────────────────────────────────────────────────────────
def _read_overlay(odir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(odir.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(odir)).replace(os.sep, "/")] = p.read_text(
                encoding="utf-8", errors="replace"
            )
    return out


def verify_arm(job_dir: str | Path, arm_id: str, task_id: str | None = None) -> dict:
    """Re-derive one arm's overlay from disk and check every plant invariant."""
    cdir = condition_dir(job_dir, arm_id, task_id)
    mpath = cdir / MANIFEST_BASENAME
    odir = cdir / OVERLAY_DIRNAME
    if not mpath.exists():
        raise PlantError(f"no manifest at {mpath}")
    if not odir.is_dir():
        raise PlantError(f"no overlay at {odir}")
    man = json.loads(mpath.read_text())
    a = arm(arm_id)
    files = _read_overlay(odir)
    problems: list[str] = []

    if (odir / MANIFEST_BASENAME).exists():
        problems.append("manifest.json is INSIDE overlay/ — it would be copied into the workspace")

    got = overlay_sha256(files)
    if got != man["overlay_sha256"]:
        problems.append(f"overlay_sha256 {got} != manifest {man['overlay_sha256']}")

    listed = {f["path"]: f for f in man["files"]}
    for extra in sorted(set(files) - set(listed)):
        problems.append(f"{extra}: on disk but not in the manifest")
    for gone in sorted(set(listed) - set(files)):
        problems.append(f"{gone}: in the manifest but not on disk")
    for path, text in files.items():
        entry = listed.get(path)
        if not entry:
            continue
        rep = render_mod.length_report(text)
        if rep["sha256"] != entry["sha256"]:
            problems.append(f"{path}: sha256 drift")
        if rep["bytes"] > render_mod.MAX_PLANT_BYTES:
            problems.append(f"{path}: {rep['bytes']} bytes over the plant cap")
        if rep["lines"] > render_mod.MAX_PLANT_LINES:
            problems.append(f"{path}: {rep['lines']} lines over the line cap")

    notes = [p for p in files if Path(p).name == NOTES_BASENAME]
    want = 0 if a.notes_path is None else 1
    if len(notes) != want:
        problems.append(f"expected {want} {NOTES_BASENAME}, found {notes}")
    if a.notes_path and notes and notes[0] != a.notes_path:
        problems.append(f"{NOTES_BASENAME} at {notes[0]}, expected {a.notes_path}")
    for d in SKELETON_DIRS:
        if f"{d}/{SKELETON_FILLER_NAME}" not in files:
            problems.append(f"skeleton directory {d}/ missing (skeleton matching is per-arm)")

    token = man.get("paired_nonce") or man.get("nonce")
    if token:
        ns = nonce_mod.NonceSet({man["fact_id"]: token})
        carriers = sorted(p for p, t in files.items() if ns.find(t))
        if a.fact_present:
            if carriers != [a.notes_path]:
                problems.append(f"nonce carried by {carriers}, expected exactly [{a.notes_path!r}]")
        elif carriers:
            problems.append(f"control arm carries the nonce in {carriers}")

    if a.distractors:
        dt = [f["path"] for f in man["files"] if f["path"] == a.notes_path]
        if not dt:
            problems.append("distractor arm has no carrier file")

    if problems:
        raise PlantError(f"arm {arm_id}" + (f" task {task_id}" if task_id else "") + ": "
                         + "; ".join(problems))
    return {
        "ok": True,
        "arm": arm_id,
        "task_id": task_id,
        "files": len(files),
        "overlay_sha256": got,
        "max_file_bytes": max((render_mod.length_report(t)["bytes"] for t in files.values()), default=0),
    }


def verify(job_dir: str | Path, *, arm_id: str | None = None, task_id: str | None = None) -> dict:
    """Verify one arm, or every planted arm plus the cross-arm invariants.

    The cross-arm check is the one a per-arm loop cannot make: the skeleton
    filler must be BYTE-IDENTICAL in every arm, because a filler that varied by
    arm would itself be an uncontrolled treatment sitting in the same directory
    tree whose depth we are pricing.
    """
    if arm_id:
        return {"arms": [verify_arm(job_dir, arm_id, task_id)], "cross_arm": None}

    root = conditions_root(job_dir)
    if not root.is_dir():
        raise PlantError(f"no conditions directory at {root}")
    found: list[tuple[str | None, str]] = []
    for m in sorted(root.rglob(MANIFEST_BASENAME)):
        rel = m.parent.relative_to(root).parts
        if len(rel) == 1:
            found.append((None, rel[0]))          # flat layout: conditions/<arm>
        elif len(rel) == 2:
            found.append((rel[0], rel[1]))        # by-task: conditions/<task>/<arm>
        else:
            # Never skip silently: `verified: N` must mean N, or the plant
            # verification == 1.00 pilot gate is reporting on a subset.
            raise PlantError(
                f"manifest at unexpected depth: {m} (expected conditions/<arm>/ or "
                "conditions/<task_id>/<arm>/)"
            )
    if not found:
        raise PlantError(f"no manifests under {root}")

    results = [verify_arm(job_dir, a, t) for (t, a) in found if task_id in (None, t)]

    fillers: dict[str, set[str]] = {}
    for t, a in found:
        if task_id not in (None, t):
            continue
        odir = overlay_dir(job_dir, a, t)
        for d in SKELETON_DIRS:
            p = odir / d / SKELETON_FILLER_NAME
            if p.exists():
                fillers.setdefault(d, set()).add(
                    hashlib.sha256(p.read_bytes()).hexdigest()
                )
    drift = {d: sorted(h) for d, h in fillers.items() if len(h) > 1}
    if drift:
        raise PlantError(
            f"skeleton filler differs across arms: {drift} — the skeleton must be identical in "
            "every arm or it becomes an uncontrolled treatment"
        )
    return {
        "arms": results,
        "cross_arm": {"skeleton_identical": True, "skeleton_dirs": list(SKELETON_DIRS)},
        "verified": len(results),
    }


# ── did it land? ─────────────────────────────────────────────────────────────
def assert_applied(
    job_dir: str | Path,
    arm_id: str,
    workspace: str | Path,
    *,
    task_id: str | None = None,
    baseline_sha: str | None = None,
    repo_dir: str | Path | None = None,
) -> dict:
    """Assert this arm's plant is present in the run's workspace and baseline commit.

    Two halves, both required for the `plant verification == 1.00` pilot gate:

      1. FILE-LEVEL — every manifest file exists in the workspace with the same
         sha256. A plant that did not land makes the run `analyzable = false`; it
         is NEVER counted as the agent ignoring the fact (§4.1).
      2. TREE-LEVEL — `git grep -F -I -i` at `baseline_sha` finds the nonce for a
         fact-present arm and does NOT find it for a control arm. This is the
         mechanical definition of `available` (§4.1), and the control half is
         what keeps `confab_rate`'s denominator honest.
    """
    man = json.loads(manifest_path(job_dir, arm_id, task_id).read_text())
    ws = Path(workspace)
    problems: list[str] = []
    checked = 0
    for entry in man["files"]:
        p = ws / entry["path"]
        if not p.is_file():
            problems.append(f"{entry['path']}: missing from the workspace")
            continue
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        if got != entry["sha256"]:
            problems.append(f"{entry['path']}: sha256 {got[:12]} != planted {entry['sha256'][:12]}")
        checked += 1

    stray = sorted(
        str(p.relative_to(ws)).replace(os.sep, "/")
        for p in ws.rglob(NOTES_BASENAME)
        if p.is_file() and ".git" not in p.parts
    )
    want_notes = [man["notes_path"]] if man["notes_path"] else []
    if stray != want_notes:
        problems.append(f"workspace has {NOTES_BASENAME} at {stray}, expected {want_notes}")

    available: bool | None = None
    token = man.get("paired_nonce") or man.get("nonce")
    if token and baseline_sha:
        ns = nonce_mod.NonceSet({man["fact_id"]: token}, repo_dir=repo_dir or ws)
        found = ns.grep_repo(baseline_sha)
        available = bool(found.get(man["fact_id"]))
        if man["fact_present"] and not available:
            problems.append(f"nonce not found in {baseline_sha}: the plant did not land")
        if not man["fact_present"] and available:
            problems.append(
                f"nonce FOUND in {baseline_sha} for control arm {arm_id!r} — `available` is "
                "supposed to be false here and every control-conditioned metric is void"
            )

    if problems:
        raise PlantError(f"arm {arm_id}: " + "; ".join(problems))
    return {
        "ok": True,
        "arm": arm_id,
        "task_id": man.get("task_id"),
        "fact_id": man["fact_id"],
        "files_checked": checked,
        "available": available,
        "baseline_sha": baseline_sha,
        "overlay_sha256": man["overlay_sha256"],
        "exposure_basis": man["exposure_basis"],
    }


# ── CLI ──────────────────────────────────────────────────────────────────────
def _cli_arms(a: argparse.Namespace) -> int:
    print(json.dumps(
        {"v1": [ARMS[x].to_dict() for x in ARMS_V1],
         "extra": [v.to_dict() for k, v in ARMS.items() if k not in ARMS_V1]},
        indent=2,
    ))
    return 0


def _cli_render(a: argparse.Namespace) -> int:
    path = a.facts or facts_mod.registry_path(a.job_dir)
    reg = facts_mod.load(path)
    arms = [s.strip() for s in a.arms.split(",")] if a.arms else list(ARMS_V1)
    out = render_job(
        a.job_dir, reg, arms=arms,
        task_ids=[a.task_id] if a.task_id else None,
        layout=a.layout, force=a.force,
    )
    print(json.dumps(
        {
            "planted": len(out["manifests"]),
            "arms": arms,
            "overlays": {
                f"{m.get('task_id') or '-'}:{m['arm']}": m["overlay_sha256"] for m in out["manifests"]
            },
        },
        indent=2,
    ))
    return 0


def _cli_verify(a: argparse.Namespace) -> int:
    rep = verify(a.job_dir, arm_id=a.arm, task_id=a.task_id)
    print(json.dumps(rep, indent=2))
    return 0


def _cli_assert_applied(a: argparse.Namespace) -> int:
    rep = assert_applied(
        a.job_dir, a.arm, a.workspace, task_id=a.task_id,
        baseline_sha=a.baseline_sha, repo_dir=a.repo_dir,
    )
    print(json.dumps(rep, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="WUR arm planting (§7.1)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("arms", help="print the arms table").set_defaults(fn=_cli_arms)

    r = sub.add_parser("render", help="render overlays + manifests for a job")
    r.add_argument("--job-dir", required=True)
    r.add_argument("--facts", help="defaults to <job-dir>/.registry/facts.yaml")
    r.add_argument("--arms", help="comma-separated arm ids (default: the 12 of §7.1)")
    r.add_argument("--task-id")
    r.add_argument("--layout", default="auto", choices=["auto", "flat", "by-task"])
    r.add_argument("--force", action="store_true")
    r.set_defaults(fn=_cli_render)

    v = sub.add_parser("verify", help="re-derive overlays and check every invariant")
    v.add_argument("--job-dir", required=True)
    v.add_argument("--arm")
    v.add_argument("--task-id")
    v.set_defaults(fn=_cli_verify)

    ap = sub.add_parser("assert-applied", help="prove the plant landed in a workspace")
    ap.add_argument("--job-dir", required=True)
    ap.add_argument("--arm", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--task-id")
    ap.add_argument("--baseline-sha")
    ap.add_argument("--repo-dir")
    ap.set_defaults(fn=_cli_assert_applied)

    a = p.parse_args(argv)
    try:
        return int(a.fn(a))
    except (PlantError, facts_mod.RegistryError, nonce_mod.NonceError, render_mod.RenderError) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
