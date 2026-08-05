#!/usr/bin/env python3
"""
verify_pack.py — the truth-table gate for a fact pack, and the prior-check gate.

RESPONSIBILITY
  Prove, before a single agent run is spent, that a fact's detector actually
  DISCRIMINATES. A detector that fires on everything and a detector that fires
  on nothing are both worthless, and neither is visible from its source code —
  only from a truth table.

  TRUTH TABLE (default mode). For each fact:
      >= 3 REFERENCE cases  — patches that DO comply -> the detector MUST fire
      >= 2 NEAR-MISS cases  — patches that do NOT comply -> it MUST NOT fire
  A near-miss that comes out `eligible = false` is NOT a pass: the detector
  separated nothing, it simply had no site to look at. That is reported as a
  non-separating case unless the case explicitly declares allow_ineligible.

  PRIOR CHECK (--prior-check), IMPLEMENTATION.md §6.3, stages 1 and 1b, both at
  ZERO agent runs:
      Gate 1  — the pristine base tree. The detector must not fire on a repo
                nobody has touched, or the mandate is not counter-prior.
      Gate 1b — cross-task finished workspaces plus the >= 2 near-miss patches.
                The detector must not fire on a competent solution to a
                DIFFERENT task, or `used` is measuring "the agent wrote code".
  Gate 2 (>= 12 `ctrl` runs, admit at 0 fires) needs real runs and lives in the
  aggregation pass; this module writes the row Gate 2 later updates.

  Every case runs through exactly the production path — detect_use.build_scope()
  then lib/battery.py — so a pack that verifies here cannot behave differently
  during the matrix.

INPUTS
  --facts    the fact registry (.registry/facts.yaml or a .json equivalent),
             whose entries carry `verification: {reference: [...], near_miss: [...]}`.
             A case is a unified diff, or a directory overlay, plus optional
             `bash` (the ordered Bash commands — an ordering fact's near-miss is
             a command sequence, not a tree).
  --base-tree the pristine fixture tree (fixtures/ledgerline/tree).

OUTPUTS
  A verification report on stdout, or to --out. --prior-check additionally
  writes one row per task shaped for .registry/prior_check/<task_id>.json:
  {gate1, gate1b, control_fire_rate, prior_check_status, disposition}.
  prior_check_status uses the fact_trace enum: pass | weak | n_a.

CLI
  python3 lib/wur/verify_pack.py --facts F --base-tree DIR [--fact-id X] [--out P]
  python3 lib/wur/verify_pack.py --prior-check --facts F --base-tree DIR \
          [--workspace DIR ...] [--out-dir .registry/prior_check]
  exit 0 = every pack separated / every gate clean; 1 = a failure; 2 = usage.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

HERE = Path(__file__).resolve().parent
LIB = HERE.parent
for _p in (str(LIB), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import detect_use  # noqa: E402
import detectors  # noqa: E402
from detect_use import Binding, binding_from_entry  # noqa: E402

SCHEMA_VERSION = "1"

# §6.3 / the assignment: three that comply, two that do not. Fewer than this and
# the truth table cannot distinguish "the detector works" from "the one patch I
# wrote happens to match".
MIN_REFERENCE = 3
MIN_NEAR_MISS = 2

# D2: a fact that fires once in the prior-check corpus is admitted but
# PRE-REGISTERED as excluded from the primary analysis. Deciding that here,
# before any treatment data exists, is what removes the forking-paths risk.
WEAK_MAX_FIRES = 1


@dataclass
class Case:
    """One reference or near-miss patch, plus the command log it implies."""

    case_id: str
    kind: str  # reference | near_miss | prior_workspace | prior_base
    patch: Path | None = None
    bash: list[str] = field(default_factory=list)
    workspace: Path | None = None
    allow_ineligible: bool = False
    note: str = ""

    @property
    def expected_fired(self) -> bool:
        return self.kind == "reference"


def _as_case(raw: Any, kind: str, root: Path, idx: int) -> Case:
    if isinstance(raw, str):
        return Case(case_id=f"{kind}-{idx}", kind=kind, patch=(root / raw))
    if not isinstance(raw, dict):
        raise ValueError(f"{kind}[{idx}]: expected a path or an object")
    patch = raw.get("patch") or raw.get("overlay") or raw.get("path")
    ws = raw.get("workspace")
    bash = raw.get("bash") or raw.get("bash_commands") or []
    if isinstance(bash, str):
        bash = [bash]
    if not isinstance(bash, list) or any(not isinstance(x, str) for x in bash):
        raise ValueError(f"{kind}[{idx}]: bash must be a list of strings")
    return Case(
        case_id=str(raw.get("id") or f"{kind}-{idx}"),
        kind=kind,
        patch=(root / patch) if patch else None,
        bash=list(bash),
        workspace=(root / ws) if ws else None,
        allow_ineligible=bool(raw.get("allow_ineligible")),
        note=str(raw.get("note") or ""),
    )


@dataclass
class Pack:
    """One fact: its binding, its truth-table cases, and its prior-check corpus."""

    binding: Binding
    reference: list[Case] = field(default_factory=list)
    near_miss: list[Case] = field(default_factory=list)
    prior_workspaces: list[Case] = field(default_factory=list)
    base_tree: Path | None = None


def load_packs(
    facts_path: Path, *, root: Path | None = None,
    fact_id: str | None = None, task_id: str | None = None,
) -> list[Pack]:
    """Read the registry into Packs.

    Relative case paths resolve against, in order: an explicit `--root`; the
    entry's own `pack_dir` (stamped by facts.load(), because a merged registry
    draws its facts from several `tasks/<id>/` directories and the installed copy
    sits in `.registry/` beside none of their patches); else the registry file's
    own directory.
    """
    facts_path = Path(facts_path).resolve()
    explicit_root = Path(root) if root else None
    default_root = explicit_root or facts_path.parent
    doc = detect_use._load_structured(facts_path)
    packs: list[Pack] = []
    for entry in detect_use._iter_fact_entries(doc):
        pack_dir = entry.get("pack_dir")
        root = explicit_root or (Path(str(pack_dir)) if pack_dir else default_root)
        try:
            binding = binding_from_entry(entry)
        except ValueError:
            if entry.get("detector") or entry.get("detectors"):
                raise
            continue
        if fact_id and binding.fact_id != fact_id:
            continue
        if task_id and binding.task_id and binding.task_id != task_id:
            continue
        v = entry.get("verification") or entry.get("pack") or {}
        if not isinstance(v, dict):
            raise ValueError(f"{binding.fact_id}: verification must be an object")
        ref_raw = v.get("reference") or v.get("reference_patches") or []
        near_raw = v.get("near_miss") or v.get("near_miss_patches") or []
        ws_raw = v.get("prior_workspaces") or v.get("cross_task_workspaces") or []
        base = v.get("base_tree")
        packs.append(
            Pack(
                binding=binding,
                reference=[_as_case(r, "reference", root, i) for i, r in enumerate(ref_raw, 1)],
                near_miss=[_as_case(r, "near_miss", root, i) for i, r in enumerate(near_raw, 1)],
                prior_workspaces=[
                    _as_case(r, "prior_workspace", root, i) for i, r in enumerate(ws_raw, 1)
                ],
                base_tree=(root / base) if base else None,
            )
        )
    return packs


# ── running one case ─────────────────────────────────────────────────────────
def run_case(
    binding: Binding, case: Case, base_tree: Path, *,
    venv: Path | None = None, workdir: Path | None = None,
) -> dict:
    """Materialize one case and run the binding over it through battery.run."""
    scope_res = None
    try:
        if case.workspace is not None:
            scope_res = detect_use.build_scope(
                "workspace", workspace=case.workspace,
                bash_commands=case.bash, planted_paths=binding.planted_paths,
                venv=venv, workdir=workdir,
            )
        elif case.patch is not None:
            scope_res = detect_use.build_scope(
                "patch", tree=base_tree, patch=case.patch,
                bash_commands=case.bash, planted_paths=binding.planted_paths,
                venv=venv, workdir=workdir,
            )
        else:
            scope_res = detect_use.build_scope(
                "tree", tree=base_tree, bash_commands=case.bash,
                planted_paths=binding.planted_paths, venv=venv, workdir=workdir,
            )
        payload = detect_use.detect([binding], scope_res, with_diff_only=False, workdir=workdir)
    except (ValueError, RuntimeError) as e:
        return {
            "case_id": case.case_id, "kind": case.kind, "note": case.note,
            "fired": None, "eligible": None, "expected_fired": case.expected_fired,
            "ok": False, "evidence": [], "error": f"{type(e).__name__}: {e}",
            "n_bash": len(case.bash),
        }
    finally:
        if scope_res is not None:
            scope_res.cleanup()

    row = payload["facts"][0]
    fired, eligible, error = row["used"], row["eligible"], row["error"]
    if error:
        ok = False
    elif case.expected_fired:
        # A reference case must fire AND be eligible. A "fire" on an ineligible
        # site is impossible by construction (detectors._finish clamps it), so
        # this is really asserting the site was created by the reference patch.
        ok = bool(fired) and bool(eligible)
    else:
        # A near-miss must not fire, AND must have been eligible while not
        # firing — otherwise the detector separated nothing, it just had no site.
        ok = (not fired) and (bool(eligible) or case.allow_ineligible)
    return {
        "case_id": case.case_id,
        "kind": case.kind,
        "note": case.note,
        "fired": fired,
        "eligible": eligible,
        "expected_fired": case.expected_fired,
        "ok": ok,
        "evidence": row.get("evidence", [])[:8],
        "error": error,
        "n_bash": len(case.bash),
        "n_changed": (row.get("detail") or {}).get("n_changed"),
    }


# ── truth table ──────────────────────────────────────────────────────────────
def verify_pack(
    pack: Pack, base_tree: Path, *, venv: Path | None = None, workdir: Path | None = None,
) -> dict:
    """The full truth table for one fact: >=3 comply, >=2 do not, all separated."""
    tree = pack.base_tree or base_tree
    problems: list[str] = []
    if len(pack.reference) < MIN_REFERENCE:
        problems.append(
            f"only {len(pack.reference)} reference cases; >= {MIN_REFERENCE} required"
        )
    if len(pack.near_miss) < MIN_NEAR_MISS:
        problems.append(
            f"only {len(pack.near_miss)} near-miss cases; >= {MIN_NEAR_MISS} required"
        )
    rows = [run_case(pack.binding, c, tree, venv=venv, workdir=workdir)
            for c in list(pack.reference) + list(pack.near_miss)]
    for r in rows:
        if not r["ok"]:
            if r["error"]:
                problems.append(f"{r['case_id']}: error: {r['error']}")
            elif r["expected_fired"]:
                problems.append(
                    f"{r['case_id']}: reference patch did NOT fire "
                    f"(eligible={r['eligible']}) — the detector misses real compliance"
                )
            elif r["fired"]:
                problems.append(
                    f"{r['case_id']}: near-miss FIRED — the detector over-matches"
                )
            else:
                problems.append(
                    f"{r['case_id']}: near-miss was not eligible, so it separated "
                    "nothing; give the case a site or set allow_ineligible"
                )
    separates = not problems
    return {
        "fact_id": pack.binding.fact_id,
        "task_id": pack.binding.task_id,
        "detector": pack.binding.detector,
        "bucket": detectors.REGISTRY[pack.binding.detector].bucket,
        "params": pack.binding.params,
        "params_sha256": detectors.params_sha256(
            pack.binding.detector, detectors.normalize_params(
                pack.binding.detector, pack.binding.params)
        ),
        "base_tree": str(tree),
        "n_reference": len(pack.reference),
        "n_near_miss": len(pack.near_miss),
        "truth_table": rows,
        "separates": separates,
        "problems": problems,
    }


# ── prior check (Gate 1 + Gate 1b) ───────────────────────────────────────────
def prior_check(
    pack: Pack, base_tree: Path, *, extra_workspaces: Sequence[Path] = (),
    venv: Path | None = None, workdir: Path | None = None,
) -> dict:
    """Gate 1 (pristine base) + Gate 1b (cross-task workspaces + near-misses).

    Zero agent runs. A fire anywhere here means the mandate is not
    counter-prior: a competent agent already does it by default, so `used`
    would be measuring the baseline rather than the workspace.
    """
    tree = pack.base_tree or base_tree
    g1 = run_case(pack.binding, Case(case_id="gate1-pristine", kind="prior_base"),
                  tree, venv=venv, workdir=workdir)
    g1_fired = bool(g1["fired"])

    corpus: list[Case] = list(pack.prior_workspaces)
    corpus += [
        Case(case_id=f"gate1b-ws-{i}", kind="prior_workspace", workspace=Path(w))
        for i, w in enumerate(extra_workspaces, 1)
    ]
    # The near-misses double as Gate 1b material: §6.3 names ">= 2 near-miss
    # patches" as part of stage 1b, and they are already authored.
    corpus += [
        Case(case_id=f"gate1b-{c.case_id}", kind="prior_workspace", patch=c.patch,
             bash=c.bash, workspace=c.workspace, allow_ineligible=True, note=c.note)
        for c in pack.near_miss
    ]
    rows = [run_case(pack.binding, c, tree, venv=venv, workdir=workdir) for c in corpus]

    fires = int(g1_fired) + sum(1 for r in rows if r["fired"])
    n_checked = 1 + len(rows)
    errors = [r for r in rows if r["error"]] + ([g1] if g1["error"] else [])

    if n_checked <= 1 and not rows:
        status, disposition = "n_a", "admit"
    elif fires == 0:
        status, disposition = "pass", "admit"
    elif fires <= WEAK_MAX_FIRES:
        status, disposition = "weak", "admit_weak"
    else:
        status, disposition = "n_a", "reject"

    if len(pack.near_miss) < MIN_NEAR_MISS:
        status = "n_a"
        disposition = "reject"

    return {
        "fact_id": pack.binding.fact_id,
        "task_id": pack.binding.task_id,
        "detector": pack.binding.detector,
        "base_tree": str(tree),
        "gate1": g1,
        "gate1b": {"cases": rows, "n_cases": len(rows),
                   "n_fires": sum(1 for r in rows if r["fired"])},
        "gate1_ok": not g1_fired and not g1["error"],
        "gate1b_ok": all((not r["fired"]) and not r["error"] for r in rows),
        "n_checked": n_checked,
        "n_fires": fires,
        # Carried onto every fact_trace row so no analysis has to go looking for
        # it. Gate 2 (>= 12 ctrl runs) later replaces this with the measured rate.
        "control_fire_rate": (fires / n_checked) if n_checked else None,
        "prior_check_status": status,
        "disposition": disposition,
        "n_errors": len(errors),
        "ok": fires == 0 and not errors and len(pack.near_miss) >= MIN_NEAR_MISS,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────
def _stamp() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "detector_registry_version": detectors.REGISTRY_VERSION,
        "detector_registry_sha256": detectors.registry_sha256(),
        "detectors_module_sha256": detectors.module_sha256(),
        "min_reference": MIN_REFERENCE,
        "min_near_miss": MIN_NEAR_MISS,
    }


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="fact-pack truth table + prior check")
    p.add_argument("--facts", required=True)
    p.add_argument("--base-tree", required=True)
    p.add_argument("--fact-id")
    p.add_argument("--task-id")
    p.add_argument("--root", help="root for relative case paths (default: the facts file's dir)")
    p.add_argument("--venv", help="hermetic venv to symlink into materialized trees")
    p.add_argument("--workdir", help="where temp trees go (default: system temp)")
    p.add_argument("--prior-check", action="store_true",
                   help="run Gate 1 + Gate 1b instead of the truth table")
    p.add_argument("--workspace", action="append", default=[],
                   help="extra Gate 1b corpus: a finished cross-task workspace")
    p.add_argument("--out", help="write the report here instead of stdout")
    p.add_argument("--out-dir", help="--prior-check: one file per task_id/fact_id here")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)

    base_tree = Path(a.base_tree)
    if not base_tree.is_dir():
        print(f"--base-tree does not exist: {base_tree}", file=sys.stderr)
        return 2
    try:
        packs = load_packs(Path(a.facts), root=Path(a.root) if a.root else None,
                           fact_id=a.fact_id, task_id=a.task_id)
    except (ValueError, RuntimeError, OSError) as e:
        print(f"could not load packs: {e}", file=sys.stderr)
        return 2
    if not packs:
        print("no fact packs matched", file=sys.stderr)
        return 2

    venv = Path(a.venv) if a.venv else None
    workdir = Path(a.workdir) if a.workdir else None
    results: list[dict] = []
    for pack in packs:
        if a.prior_check:
            results.append(prior_check(pack, base_tree,
                                       extra_workspaces=[Path(w) for w in a.workspace],
                                       venv=venv, workdir=workdir))
        else:
            results.append(verify_pack(pack, base_tree, venv=venv, workdir=workdir))

    key = "ok" if a.prior_check else "separates"
    payload = {**_stamp(), "mode": "prior_check" if a.prior_check else "truth_table",
               "base_tree": str(base_tree), "facts": results,
               "ok": all(r[key] for r in results)}
    blob = json.dumps(payload, indent=2, sort_keys=True)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(blob)
    if a.out_dir and a.prior_check:
        outdir = Path(a.out_dir)
        outdir.mkdir(parents=True, exist_ok=True)
        # §5.3 names the artifact .registry/prior_check/<task_id>.json, which D1
        # (one fact per task) makes unambiguous. If a registry ever carries two
        # facts for one task, disambiguate rather than silently clobbering the
        # first fact's gate result.
        seen: dict[str, str] = {}
        for r in results:
            task = r.get("task_id") or r["fact_id"]
            name = task
            if task in seen and seen[task] != r["fact_id"]:
                name = f"{task}__{r['fact_id']}"
                print(f"[verify-pack] WARNING: {task} has more than one fact "
                      f"(D1 says one); writing {name}.json", file=sys.stderr)
            seen.setdefault(task, r["fact_id"])
            (outdir / f"{name}.json").write_text(json.dumps(r, indent=2, sort_keys=True))
    if not a.out and not a.out_dir:
        print(blob)

    if not a.quiet:
        for r in results:
            if a.prior_check:
                print(
                    f"[prior-check] {r['fact_id']}: fires={r['n_fires']}/{r['n_checked']} "
                    f"status={r['prior_check_status']} -> {r['disposition']}",
                    file=sys.stderr,
                )
            else:
                verdict = "SEPARATES" if r["separates"] else "FAILED"
                print(
                    f"[verify-pack] {r['fact_id']} ({r['detector']}): {verdict} "
                    f"({r['n_reference']} ref / {r['n_near_miss']} near-miss)",
                    file=sys.stderr,
                )
                for prob in r["problems"]:
                    print(f"    - {prob}", file=sys.stderr)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
