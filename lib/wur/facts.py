#!/usr/bin/env python3
"""
facts.py — the fact registry: load, validate, mint, collision-check, leak-check, gate.

RESPONSIBILITY
  Own `$JOB_DIR/.registry/facts.yaml` — the file that decides what every run is
  measuring. Four jobs, in the order run.sh performs them:

    1. LOAD + VALIDATE   the authored registry: one fact per task, a gist, a
                         statement carrying `{nonce}`, clause pairs, a detector
                         binding, and the tier-(b) paraphrase regexes that
                         requires FROZEN BEFORE ANY DATA EXISTS.
    2. MINT              nonces (and distractor tokens) via nonce.mint(), then
                         assert they are pairwise disjoint and absent from the
                         pinned baseline tree.
    3. LEAK-CHECK        the nonce must not occur in criteria.json, the task
                         text, the accept text, the probe text, or the
                         self-analysis prompt. A nonce reachable through any of
                         those lets the model name the fact without ever reading
                         the workspace — confabulation arriving through a channel
                         the `confab_rate <= 0.05` gate cannot see.
    4. PRIOR-CHECK       the three-stage counter-prior gate.

INPUTS
  facts.yaml (or facts.json — JSON is valid YAML, and the system python3 has no
  PyYAML until install.sh runs), a repo_sha, a git dir + baseline sha, the
  harness's own prompt texts, and a detector runner from lib/wur/detectors.py.

OUTPUTS
  Registry / FactSpec records; a nonce.NonceSet; leak and gate reports;
  `$JOB_DIR/.registry/facts.yaml` (mode 600 inside a mode-700 directory, V9);
  `$JOB_DIR/.registry/prior_check/<task_id>.json`.

THE THREE-STAGE GATE, AND WHY IT IS NOT ONE STAGE
  The mandate must be something a competent agent would NOT do by default while
  still being a legitimate solution — otherwise `used` measures nothing. The
  original single-stage gate was mathematically unsatisfiable: 0/10 has a Wilson
  upper bound of 0.278, above its own 0.25 threshold. So:

    Gate 1   pristine base tree                                    0 agent runs
    Gate 1b  cross-task finished workspaces + >=2 near-miss patches 0 agent runs
    Gate 2   >= 12 `ctrl` runs, admit at 0 fires                    12 agent runs

  D2: a fact firing exactly once in >= 12 control runs is admitted as `weak` and
  PRE-REGISTERED as excluded from primary — the exclusion is decided before any
  treatment data exists, which is what removes the forking-paths risk.

  NOTE (measured, not inherited): the spec quoted "Wilson upper 0/12 = 0.265".
  wilson_upper(0, 12) computes 0.2425 with the standard score interval, which
  also reproduces the spec's own worked example 0/10 = 0.278 exactly. The gate is
  therefore slightly STRONGER than the doc claims; the doc's 0.265 does not
  reproduce and is treated as a transcription slip.

CLI
  python3 lib/wur/facts.py validate    --facts F
  python3 lib/wur/facts.py mint        --facts F --repo-sha SHA [--job-dir D] [--repo-dir R]
  python3 lib/wur/facts.py nonces      --facts F
  python3 lib/wur/facts.py show        --facts F --fact-id ID      (a CanonicalFact, for render.py)
  python3 lib/wur/facts.py leak-check  --job-dir D [--facts F] [--extra PATH ...]
  python3 lib/wur/facts.py gate1       --facts F --fact-id ID --base-workspace W
  python3 lib/wur/facts.py gate1b      --facts F --fact-id ID --workspace W ... --patch P ...
  python3 lib/wur/facts.py gate2       --facts F --fact-id ID --fires K --n N
  python3 lib/wur/facts.py prior-check --job-dir D --facts F --fact-id ID [--gate-json J ...]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # flat import (lib/wur on sys.path)
    sys.path.insert(0, str(_HERE))

try:  # package import shares one module object with the rest of lib/wur
    from . import nonce as nonce_mod
    from . import render as render_mod
except ImportError:  # pragma: no cover - exercised when run as a script
    import nonce as nonce_mod  # type: ignore[no-redef]
    import render as render_mod  # type: ignore[no-redef]

REGISTRY_SCHEMA_VERSION = "1"
REGISTRY_DIRNAME = ".registry"
REGISTRY_BASENAME = "facts.yaml"
REGISTRY_DIR_MODE = 0o700   # V9: under bypassPermissions the agent read the registry
REGISTRY_FILE_MODE = 0o600

BUCKETS = ("constraint", "method", "ordering", "hidden_cue")
TIERS = ("A", "B")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Gate constants.
GATE2_MIN_N = 12
GATE2_WEAK_FIRES = 1        # D2: 1 fire in >= 12 ctrl runs => `weak`, not rejected
GATE1B_MIN_NEAR_MISS = 2
Z_95 = 1.959963984540054


class RegistryError(Exception):
    """The registry is malformed, or an operation on it cannot be completed."""


class DetectorUnavailable(RegistryError):
    """lib/wur/detectors.py is not importable, so Gate 1 / 1b cannot run."""


# ── YAML/JSON IO ─────────────────────────────────────────────────────────────
def _yaml():
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    return yaml


def load_mapping(path: str | Path) -> dict:
    """Parse a registry file. PyYAML when importable, else strict JSON.

    The system python3 has no PyYAML until install.sh runs, and this module is on
    the preflight path — so a missing dependency must degrade to something
    actionable, not to a traceback. JSON is valid YAML, so a JSON-bodied
    facts.yaml round-trips through both readers.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    y = _yaml()
    if y is not None:
        data = y.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise RegistryError(
                f"{p}: PyYAML is not importable on this interpreter and the file is not "
                f"JSON either ({e}). Run install.sh, or write the registry as JSON "
                f"(JSON is valid YAML)."
            ) from e
    if not isinstance(data, dict):
        raise RegistryError(f"{p}: registry must be a mapping, got {type(data).__name__}")
    return data


def dump_mapping(data: Mapping[str, Any]) -> str:
    """Serialize a registry. PyYAML when importable, else JSON (valid YAML)."""
    y = _yaml()
    if y is not None:
        return y.safe_dump(dict(data), sort_keys=False, default_flow_style=False, allow_unicode=True)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# ── the records ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DetectorBinding:
    """Which of the 6 closed predicates decides `used`, and with what params.

    Tier-B generation picks a registry `name` and fills `params`; it never
    authors a predicate. The predicates themselves live in lib/wur/detectors.py.
    """

    name: str
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "params": dict(self.params)}


@dataclass(frozen=True)
class FactSpec:
    """One planted fact. Exactly one per task."""

    fact_id: str
    task_id: str
    title: str
    statement: str                     # carries the literal `{nonce}` placeholder
    gist: str
    clauses: tuple[dict, ...] = ()
    bucket: str = "constraint"
    surface_forms: tuple[str, ...] = ()
    paraphrase_regexes: tuple[str, ...] = ()
    detector: DetectorBinding | None = None
    control: dict | None = None
    distractors: tuple[dict, ...] = ()
    nonce: str | None = None
    prior_check: dict | None = None
    notes: str = ""
    #: The authored `verification:` block (reference / near_miss / prior_workspaces)
    #: carried through VERBATIM so it survives install(). verify_pack.py's Gate 1b
    #: is the only consumer, and before this was round-tripped `install()` silently
    #: dropped it: the installed registry had no near-miss patches, so every fact
    #: came back `n_a -> reject` from a gate that had never actually run.
    verification: dict | None = None
    #: Absolute directory the `verification` block's RELATIVE patch paths resolve
    #: against. A merged registry draws its facts from several `tasks/<id>/`
    #: directories, so a single registry-wide root cannot locate them all.
    pack_dir: str | None = None

    # ── derived views ──
    def materialized_statement(self) -> str:
        return self._sub(self.statement)

    def _sub(self, text: str) -> str:
        if "{nonce}" not in text:
            return text
        if not self.nonce:
            raise RegistryError(
                f"{self.fact_id}: text carries {{nonce}} but no nonce is minted yet — "
                "run `facts.py mint` first"
            )
        return text.replace("{nonce}", self.nonce)

    def canonical(self, *, with_distractors: bool = False) -> "render_mod.CanonicalFact":
        """The render.CanonicalFact for this fact, with `{nonce}`/`{token}` substituted.

        A distractor's own `{token}` is substituted per distractor: `d2-dist`
        measures `wrong_value_in_slot` — "a distractor's token landed in the
        critical slot" — which is only observable if the token is
        actually IN the planted document, not merely minted into the registry.
        """
        ds = (tuple(render_mod.Distractor(token=str(d.get("token") or ""),
                    statement=self._sub(str(d.get("statement", ""))).replace(
                        "{token}", str(d.get("token") or "")
                    ),
                    label=str(d.get("label") or "Convention"),
            )
                for d in self.distractors
            )
            if with_distractors
            else ()
            )
        return render_mod.CanonicalFact(fact_id=self.fact_id,
            title=self.title,
            statement=self.materialized_statement(),
            clauses=tuple(
                render_mod.Clause(label=str(c["label"]), text=self._sub(str(c["text"])))
                for c in self.clauses
                    ),
            nonce=self.nonce,
            task_id=self.task_id,
            bucket=self.bucket,
            distractors=ds,
            )

    def _sub_deep(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self._sub(obj)
        if isinstance(obj, list):
            return [self._sub_deep(x) for x in obj]
        if isinstance(obj, Mapping):
            return {k: self._sub_deep(v) for k, v in obj.items()}
        return obj

    def detector_binding(self) -> dict:
        """The detector binding with `{nonce}` substituted throughout its params.

        A mandate is usually stated in terms of the token ("stamp the marker
        ZQ-… in the header"), so the predicate's patterns need the minted value,
        not the placeholder. Substitution is recursive so list/dict params work.
        """
        if self.detector is None:
            raise RegistryError(f"{self.fact_id}: no detector binding")
        return {"name": self.detector.name, "params": self._sub_deep(dict(self.detector.params))}


    def card(self) -> dict:
        """protocol.FactCard.from_dict()-shaped dict — the mention-matching identity."""
        return {
            "fact_id": self.fact_id,
            "nonce": self.nonce,
            "surface_forms": list(self.surface_forms),
            "regexes": list(self.paraphrase_regexes),
            "gist": self.gist,
            "distractor_tokens": [str(d.get("token") or "") for d in self.distractors if d.get("token")],
        }

    def distractor_tokens(self) -> tuple[str, ...]:
        return tuple(str(d.get("token")) for d in self.distractors if d.get("token"))

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "fact_id": self.fact_id,
            "task_id": self.task_id,
            "bucket": self.bucket,
            "title": self.title,
            "gist": self.gist,
            "statement": self.statement,
            "clauses": [dict(c) for c in self.clauses],
            "surface_forms": list(self.surface_forms),
            "paraphrase_regexes": list(self.paraphrase_regexes),
            "nonce": self.nonce,
        }
        if self.detector is not None:
            out["detector"] = self.detector.to_dict()
        if self.control:
            out["control"] = dict(self.control)
        if self.distractors:
            out["distractors"] = [dict(d) for d in self.distractors]
        if self.prior_check:
            out["prior_check"] = dict(self.prior_check)
        if self.notes:
            out["notes"] = self.notes
        if self.verification:
            out["verification"] = dict(self.verification)
        if self.pack_dir:
            out["pack_dir"] = str(self.pack_dir)
        return out

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "FactSpec":
        if not isinstance(d, Mapping):
            raise RegistryError(f"fact entry must be a mapping, got {type(d).__name__}")
        missing = [k for k in ("fact_id", "task_id", "title", "statement", "gist") if not d.get(k)]
        if missing:
            raise RegistryError(f"fact entry missing required key(s): {missing} in {dict(d)!r}")
        det = d.get("detector")
        return cls(fact_id=str(d["fact_id"]),
            task_id=str(d["task_id"]),
            title=str(d["title"]),
            statement=str(d["statement"]),
            gist=str(d["gist"]),
            clauses=tuple(dict(c) for c in (d.get("clauses") or ())),
            bucket=str(d.get("bucket") or "constraint"),
            surface_forms=tuple(str(s) for s in (d.get("surface_forms") or ())),
            paraphrase_regexes=tuple(
                str(r) for r in (d.get("paraphrase_regexes") or d.get("regexes") or ())
            ),
            detector=(
                DetectorBinding(name=str(det.get("name")), params=dict(det.get("params") or {}))
                if isinstance(det, Mapping) and det.get("name")
                else None
                    ),
            control=dict(d["control"]) if isinstance(d.get("control"), Mapping) else None,
            distractors=tuple(dict(x) for x in (d.get("distractors") or ())),
            nonce=(str(d["nonce"]) if d.get("nonce") else None),
            prior_check=dict(d["prior_check"]) if isinstance(d.get("prior_check"), Mapping) else None,
            notes=str(d.get("notes") or ""),
            verification=(dict(d["verification"]) if isinstance(d.get("verification"), Mapping)
                else (dict(d["pack"]) if isinstance(d.get("pack"), Mapping) else None)
                    ),
            pack_dir=(str(d["pack_dir"]) if d.get("pack_dir") else None),
            )


@dataclass
class Registry:
    """facts.yaml as a record. Mutable only through the functions in this module."""

    facts: list[FactSpec]
    schema_version: str = REGISTRY_SCHEMA_VERSION
    tier: str = "A"
    salt: str = nonce_mod.DEFAULT_SALT
    repo_sha: str | None = None
    nonce_prefix: str = nonce_mod.DEFAULT_PREFIX
    source_path: Path | None = None

    # ── views ──
    @property
    def by_id(self) -> dict[str, FactSpec]:
        return {f.fact_id: f for f in self.facts}

    @property
    def by_task(self) -> dict[str, FactSpec]:
        return {f.task_id: f for f in self.facts}

    def get(self, fact_id: str) -> FactSpec:
        try:
            return self.by_id[fact_id]
        except KeyError:
            raise RegistryError(f"no such fact_id: {fact_id!r}") from None

    def for_task(self, task_id: str) -> FactSpec:
        try:
            return self.by_task[task_id]
        except KeyError:
            raise RegistryError(f"no fact registered for task_id {task_id!r}") from None

    def nonce_set(self, *, repo_dir: str | Path | None = None, include_distractors: bool = True
    ) -> nonce_mod.NonceSet:
        """Every token this job scans for, as one compiled alternation.

        Distractor tokens ride along under `<fact_id>::distractor::<i>` labels so
        that disjointness is checked against the real nonces — a distractor that
        overlapped its own fact's token would make `wrong_value_in_slot`
        (the `d2-dist` discrimination measure) unreadable.
        """
        nonces: dict[str, str] = {}
        forms: dict[str, list[str]] = {}
        for f in self.facts:
            if f.nonce:
                nonces[f.fact_id] = f.nonce
                if f.surface_forms:
                    forms[f.fact_id] = list(f.surface_forms)
            if include_distractors:
                for i, tok in enumerate(f.distractor_tokens()):
                    nonces[f"{f.fact_id}::distractor::{i}"] = tok
        return nonce_mod.NonceSet(nonces, repo_dir=repo_dir, surface_forms=forms)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "tier": self.tier,
            "salt": self.salt,
            "repo_sha": self.repo_sha,
            "nonce_prefix": self.nonce_prefix,
            "facts": [f.to_dict() for f in self.facts],
        }


#: The per-task pack filename `load()` collects when handed a directory.
FACT_PACK_NAME = "fact_pack.yaml"


def load_dir_mapping(path: str | Path) -> dict:
    """Merge every `tasks/<task_id>/fact_pack.yaml` under `path` into one mapping.

    The authored facts live one-per-task beside the task text and the patches the
    detector was verified against (`tasks/<task_id>/fact_pack.yaml`), but
    `job.yaml:facts_file` names ONE registry and `install()` copies one file. This
    is the bridge, and it is a merge rather than a concatenation because the
    header keys (`schema_version`, `tier`, `salt`, `nonce_prefix`) are properties
    of the registry as a whole: a pack that disagrees with its siblings about the
    salt would silently re-mint every nonce in the job, so a conflict raises here
    instead of being resolved by file order.
    """
    root = Path(path)
    packs = sorted(root.glob(f"*/{FACT_PACK_NAME}"))
    if not packs:
        raise RegistryError(
            f"{root}: no */{FACT_PACK_NAME} found — point --facts at a registry file "
            f"or at a directory of per-task packs"
            )
    header: dict[str, Any] = {}
    merged: list[dict] = []
    seen: dict[str, Path] = {}
    for pack in packs:
        d = load_mapping(pack)
        for key in ("schema_version", "tier", "salt", "nonce_prefix", "repo_sha"):
            val = d.get(key)
            if val is None:
                continue
            if key in header and header[key] != val:
                raise RegistryError(
                    f"{pack}: {key}={val!r} disagrees with {header[key]!r} set by an "
                    f"earlier pack; the header is a property of the whole registry"
            )
            header[key] = val
        entries = d.get("facts")
        if not isinstance(entries, list) or not entries:
            raise RegistryError(f"{pack}: `facts` must be a non-empty list")
        for entry in entries:
            fid = str((entry or {}).get("fact_id") or "")
            if fid in seen:
                raise RegistryError(
                    f"{pack}: duplicate fact_id {fid!r} (already defined by {seen[fid]})"
            )
            seen[fid] = pack
            entry = dict(entry or {})
            # The near-miss / reference patches named in `verification` are relative
            # to the pack that declared them, and after the merge there is no single
            # registry-wide root that can find them all. Stamping the origin here is
            # what lets Gate 1b still run against the INSTALLED registry.
            entry.setdefault("pack_dir", str(pack.parent.resolve()))
            merged.append(entry)
    header["facts"] = merged
    return header


def load(path: str | Path) -> Registry:
    """Read a registry file — or a directory of per-task packs — into a Registry."""
    p = Path(path)
    d = load_dir_mapping(p) if p.is_dir() else load_mapping(p)
    facts = d.get("facts")
    if not isinstance(facts, list) or not facts:
        raise RegistryError(f"{path}: `facts` must be a non-empty list")
    if not p.is_dir():
        # Same reason as the directory case: install() copies the YAML but not the
        # patches beside it, so an unstamped registry loses Gate 1b on the way in.
        here = str(p.resolve().parent)
        facts = [{**dict(f), "pack_dir": (f or {}).get("pack_dir") or here} for f in facts]
    return Registry(facts=[FactSpec.from_dict(f) for f in facts],
        schema_version=str(d.get("schema_version") or REGISTRY_SCHEMA_VERSION),
        tier=str(d.get("tier") or "A"),
        salt=str(d.get("salt") or nonce_mod.DEFAULT_SALT),
        repo_sha=(str(d["repo_sha"]) if d.get("repo_sha") else None),
        nonce_prefix=str(d.get("nonce_prefix") or nonce_mod.DEFAULT_PREFIX),
        source_path=Path(path),
            )


def save(reg: Registry, path: str | Path, *, mode: int = REGISTRY_FILE_MODE) -> Path:
    """Write the registry, then tighten its mode. Parent dir is created 0700."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=REGISTRY_DIR_MODE)
    try:
        os.chmod(p.parent, REGISTRY_DIR_MODE)
    except OSError:  # pragma: no cover - non-POSIX or foreign owner
        pass
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(dump_mapping(reg.to_dict()), encoding="utf-8")
    os.replace(tmp, p)
    os.chmod(p, mode)
    return p


# ── registry location ─────────────────────────────────────────────────
def registry_dir(job_dir: str | Path, *, create: bool = True) -> Path:
    """`$JOB_DIR/.registry`, mode 700.

    V9: under `bypassPermissions` the agent DID read the fact registry when it
    sat three `..` hops above the workspace, verbatim. The run roots moved out to
    `$ATLAS_RUNS_ROOT` so no secret is an ancestor of a workspace, and this
    directory is 700 as the second line of defence.
    """
    d = Path(job_dir) / REGISTRY_DIRNAME
    if create:
        d.mkdir(parents=True, exist_ok=True, mode=REGISTRY_DIR_MODE)
        try:
            os.chmod(d, REGISTRY_DIR_MODE)
        except OSError:  # pragma: no cover
            pass
    return d


def registry_path(job_dir: str | Path, *, create: bool = False) -> Path:
    return registry_dir(job_dir, create=create) / REGISTRY_BASENAME


def install(source: str | Path, job_dir: str | Path, salt: str | None = None) -> Path:
    """Copy an authored registry into `$JOB_DIR/.registry/facts.yaml` (mode 600).

    `salt` overrides the registry's own — it is THE deployment secret. Every nonce is
    `blake2s(salt | repo_sha | fact_id)`, and `repo_sha` and `fact_id` are both public
    in any published checkout, so with the default salt every nonce in this repo is
    recomputable by anyone holding it in one command. That does not corrupt a run by
    itself, but a model that has seen the repository could emit a nonce it never read,
    which is precisely the thing `read` is trying to measure.

    Leave it at the default for smoke runs and reproducibility work. Set it — via
    `ATLAS_NONCE_SALT`, or `salt:` in the authored registry — before collecting
    anything you intend to publish or believe.
    """
    reg = load(source)
    if salt and salt != reg.salt:
        reg.salt = salt
        # Any nonce minted under the old salt is now wrong for this registry, and a
        # stale one would be asserted-absent against the wrong material. Drop them so
        # `mint` re-derives every one from the new secret. FactSpec is frozen, so this
        # is a rebuild rather than an assignment.
        reg.facts = [dataclasses.replace(f, nonce=None) for f in reg.facts]
    return save(reg, registry_path(job_dir, create=True))


def salt_is_default(reg_or_path) -> bool:
    """True when the registry still uses the public default salt."""
    reg = reg_or_path if isinstance(reg_or_path, Registry) else load(reg_or_path)
    return reg.salt == nonce_mod.DEFAULT_SALT


# ── validation ───────────────────────────────────────────────────────────────
# Generic text a tier-(b) paraphrase regex must NOT match. If it does, the regex
# will fire on control runs and manufacture the confabulation that the
# `confab_rate <= 0.05` pilot gate exists to detect.
GENERIC_CORPUS: tuple[str,...] = (*render_mod.CONTROL_BANK,
    *render_mod.CONTROL_STATEMENTS,
    *(f"{label}: {text}" for label, text in render_mod.CONTROL_CLAUSE_BANK),
    render_mod.render_filler("docs/index.md"),
    render_mod.render_filler("docs/internal/memory/index.md"),
    "The tests pass locally and in CI.",
    "Refactor the export module and add unit tests for the new helper.",
    "Read the repository documentation before changing the code.",
    "I will now run the test suite and inspect the failures.",
            )


def validate(reg: Registry) -> list[str]:
    """Hard errors that make the registry unusable. Empty list means usable."""
    errs: list[str] = []
    if reg.schema_version != REGISTRY_SCHEMA_VERSION:
        errs.append(f"schema_version must be {REGISTRY_SCHEMA_VERSION!r}, got {reg.schema_version!r}")
    if reg.tier not in TIERS:
        errs.append(f"tier must be one of {TIERS}, got {reg.tier!r}")

    seen_ids: set[str] = set()
    seen_tasks: set[str] = set()
    for f in reg.facts:
        tag = f.fact_id or "<unnamed>"
        if not ID_RE.fullmatch(f.fact_id or ""):
            errs.append(f"{tag}: fact_id must match {ID_RE.pattern}")
        if not ID_RE.fullmatch(f.task_id or ""):
            errs.append(f"{tag}: task_id must match {ID_RE.pattern}")
        if f.fact_id in seen_ids:
            errs.append(f"{tag}: duplicate fact_id")
        seen_ids.add(f.fact_id)
        if f.task_id in seen_tasks:
            errs.append(f"{tag}: task_id {f.task_id!r} already has a fact — D1 allows exactly one")
        seen_tasks.add(f.task_id)

        if f.bucket not in BUCKETS:
            errs.append(f"{tag}: bucket must be one of {BUCKETS}, got {f.bucket!r}")
        if "{nonce}" not in f.statement and not any("{nonce}" in str(c.get("text", "")) for c in f.clauses):
            errs.append(
                f"{tag}: neither the statement nor any clause carries the literal {{nonce}} "
                "placeholder — the plant would be unfindable and `available` would be false"
            )
        if not f.clauses:
            errs.append(f"{tag}: at least one clause is required (format renderings need a body)")
        labels = [str(c.get("label", "")) for c in f.clauses]
        if len(set(labels)) != len(labels):
            errs.append(f"{tag}: clause labels must be unique")
        for c in f.clauses:
            if not str(c.get("label", "")).strip() or not str(c.get("text", "")).strip():
                errs.append(f"{tag}: every clause needs a non-empty label and text")

        if not f.paraphrase_regexes:
            errs.append(f"{tag}: at least one tier-(b) paraphrase regex is required and  requires "
                "them frozen in facts.yaml BEFORE any data"
            )
        for pat in f.paraphrase_regexes:
            try:
                rx = re.compile(pat, re.IGNORECASE)
            except re.error as e:
                errs.append(f"{tag}: paraphrase regex {pat!r} does not compile: {e}")
                continue
            if rx.search(""):
                errs.append(f"{tag}: paraphrase regex {pat!r} matches the empty string")
                continue
            hits = [c for c in GENERIC_CORPUS if rx.search(c)]
            if hits:
                errs.append(f"{tag}: paraphrase regex {pat!r} matches generic text "
                    f"({hits[0][:60]!r}...) — it would fire on control runs and breach confab_rate"
            )

        if f.detector is None:
            errs.append(f"{tag}: a detector binding is required (`used` is undefined without one)")
        if f.nonce is not None and not nonce_mod.is_wellformed(f.nonce):
            errs.append(f"{tag}: nonce {f.nonce!r} is not a well-formed minted token")
        for i, d in enumerate(f.distractors):
            stmt = str(d.get("statement", ""))
            if not stmt.strip():
                errs.append(f"{tag}: distractor[{i}] needs a statement")
            tok = d.get("token")
            if tok is not None and not nonce_mod.is_wellformed(str(tok)):
                errs.append(f"{tag}: distractor[{i}] token {tok!r} is not a well-formed token")
            if not d.get("tokenless") and "{token}" not in stmt:
                errs.append(
                    f"{tag}: distractor[{i}] carries no {{token}} placeholder, so its minted token "
                    "would never reach the workspace and `wrong_value_in_slot` would be "
                    "unmeasurable — add {token} to the statement, or set tokenless: true"
            )
    return errs


def lint(reg: Registry) -> list[str]:
    """Soft warnings: things that are probably wrong but do not block a run."""
    warns: list[str] = []
    for f in reg.facts:
        if not f.control:
            warns.append(f"{f.fact_id}: no hand-authored `control:` block — render.control_fact will "
                "generate a length-matched generic twin, which matches structure but not register"
            )
        if f.nonce:
            stmt = f.materialized_statement()
            if not any(re.search(p, stmt, re.IGNORECASE) for p in f.paraphrase_regexes):
                warns.append(
                    f"{f.fact_id}: no paraphrase regex matches the fact's own statement — "
                    "tier (b) will only ever fire on wordings nobody has checked"
            )
        if not f.distractors:
            warns.append(f"{f.fact_id}: no distractors, so the `d2-dist` arm degenerates to `d2`")
    warns += _lint_detectors(reg)
    return warns


def _lint_detectors(reg: Registry) -> list[str]:
    """Check detector bindings against the closed registry, when it is importable.

    Kept OUT of validate() on purpose: validate() must give the same answer on a
    machine where lib/wur/detectors.py has not been written yet, so that a
    registry authored during Phase 4 does not start failing when Phase 1 lands.
    """
    try:
        mod = _import_detectors()
    except DetectorUnavailable:
        return []
    names = set(getattr(mod, "REGISTRY", {}) or ())
    check = getattr(mod, "validate_params", None)
    out: list[str] = []
    for f in reg.facts:
        if f.detector is None:
            continue
        if names and f.detector.name not in names:
            out.append(
                f"{f.fact_id}: detector {f.detector.name!r} is not in the closed registry "
                f"{sorted(names)} — Gate 1 will raise"
            )
            continue
        if callable(check):
            try:
                params = f.detector_binding()["params"] if f.nonce else dict(f.detector.params)
            except RegistryError:
                params = dict(f.detector.params)
            for problem in check(f.detector.name, params) or []:
                out.append(f"{f.fact_id}: detector params: {problem}")
    return out


# ── minting ──────────────────────────────────────────────────────────────────
def mint_nonces(reg: Registry,
    *,
    repo_sha: str | None = None,
    salt: str | None = None,
    repo_dir: str | Path | None = None,
    baseline_sha: str | None = None,
    force: bool = False,
) -> Registry:
    """Mint every missing nonce and distractor token, then check the invariants.

    Deterministic in (salt, repo_sha, fact_id): re-minting the same registry is a
    no-op, and a months-later re-derivation reproduces the same tokens. Facts
    that already carry a nonce are left alone unless `force`.

    On return the registry has passed BOTH nonce invariants:
      * pairwise disjoint (NonceSet.assert_disjoint)
      * absent from the pinned tree, when `repo_dir` is given
        (NonceSet.assert_absent_from_repo — a nonce already in the repo silently
        inflates read-rate on every run, and nothing downstream can detect it).
    """
    sha = repo_sha or reg.repo_sha
    if not sha:
        raise RegistryError("mint_nonces needs a repo_sha: the mint needs a stable repo_sha")
    s = salt or reg.salt

    # Tokens already in the registry are the ones a fresh mint must dodge. Under
    # --force nothing is kept, so nothing is dodged either.
    minted: list[str] = []
    if not force:
        minted = [f.nonce for f in reg.facts if f.nonce]
        minted += [t for f in reg.facts for t in f.distractor_tokens()]

    out: list[FactSpec] = []
    for f in reg.facts:
        n = f.nonce
        if force or not n:
            n = nonce_mod.mint(f.fact_id, sha, s, prefix=reg.nonce_prefix, avoid=minted)
            minted.append(n)
        ds: list[dict] = []
        for i, d in enumerate(f.distractors):
            d = dict(d)
            if d.get("tokenless"):
                d.pop("token", None)
            elif force or not d.get("token"):
                d["token"] = nonce_mod.mint(
                    f"{f.fact_id}|distractor|{i}", sha, s, prefix=reg.nonce_prefix, avoid=minted
            )
                minted.append(d["token"])
            ds.append(d)
        out.append(FactSpec(fact_id=f.fact_id,
                task_id=f.task_id,
                title=f.title,
                statement=f.statement,
                gist=f.gist,
                clauses=f.clauses,
                bucket=f.bucket,
                surface_forms=f.surface_forms,
                paraphrase_regexes=f.paraphrase_regexes,
                detector=f.detector,
                control=f.control,
                distractors=tuple(ds),
                nonce=n,
                prior_check=f.prior_check,
                notes=f.notes,
                # Field-by-field reconstruction silently drops anything added to
                # FactSpec later. These two are load-bearing for Gate 1b: without
                # them the minted registry has no near-miss patches and every fact
                # comes back `n_a -> reject` from a gate that never ran.
                verification=f.verification,
                pack_dir=f.pack_dir,
            )
            )

    new = Registry(facts=out,
        schema_version=reg.schema_version,
        tier=reg.tier,
        salt=s,
        repo_sha=sha,
        nonce_prefix=reg.nonce_prefix,
        source_path=reg.source_path,
            )
    ns = new.nonce_set(repo_dir=repo_dir)
    ns.assert_disjoint()
    if repo_dir is not None:
        ns.assert_absent_from_repo(baseline_sha or sha, repo_dir=repo_dir)
    return new


# ── leak check ───────────────────────────────────────────────────────────────
# Everything the harness itself puts in front of the model, or uses to grade it.
# names five: criteria.json, the task text, the accept text, the probe text,
# and the self-analysis prompt.
def frozen_protocol_texts() -> dict[str, str]:
    """The frozen probe/pacing/retry/resume/budget strings, by name.

    Imported lazily and defensively: protocol.py is a sibling contract owned
    elsewhere, and a leak check that cannot import it must say so rather than
    silently checking four sources instead of five.
    """
    try:
        try:
            from . import protocol  # type: ignore
        except ImportError:
            import protocol  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RegistryError(f"cannot import lib/wur/protocol.py for the leak check: {e}") from e
    return {f"protocol.{k}": v for k, v in protocol.FROZEN_STRINGS.items()}


def collect_leak_texts(job_dir: str | Path,
    *,
    repo_root: str | Path | None = None,
    extra_paths: Iterable[str | Path] = (),
) -> dict[str, str]:
    """Gather every text the nonce must NOT appear in, keyed by source name.

    Sources: job.yaml's `task` and `accept` (and every entry of `tasks[]`), every
    `grader/**/criteria.json`, the frozen protocol strings, and the self-analysis
    prompt (read as the whole of lib/self_analysis.sh, a strict superset of the
    prompt it builds). Missing sources are recorded as `__missing__` entries so a
    silently-skipped check is visible in the report instead of passing by default.
    """
    job = Path(job_dir)
    texts: dict[str, str] = {}
    missing: list[str] = []

    jy = job / "job.yaml"
    if jy.exists():
        try:
            spec = load_mapping(jy)
        except RegistryError:
            spec = {}
            texts["job.yaml(raw)"] = jy.read_text(encoding="utf-8", errors="replace")
        for key in ("task", "accept"):
            if spec.get(key):
                texts[f"job.yaml:{key}"] = str(spec[key])
        for t in spec.get("tasks") or []:
            if isinstance(t, Mapping):
                tid = t.get("id", "?")
                for key in ("task", "accept"):
                    if t.get(key):
                        texts[f"job.yaml:tasks[{tid}].{key}"] = str(t[key])
    else:
        missing.append(str(jy))


    grader_files = sorted((job / "grader").rglob("criteria.json")) if (job / "grader").exists() else []
    for c in grader_files:
        texts[f"grader:{c.relative_to(job)}"] = c.read_text(encoding="utf-8", errors="replace")
    if not grader_files:
        missing.append(str(job / "grader/**/criteria.json"))

    texts.update(frozen_protocol_texts())

    root = Path(repo_root) if repo_root else _HERE.parent.parent
    sa = root / "lib" / "self_analysis.sh"
    if sa.exists():
        texts["self_analysis.sh"] = sa.read_text(encoding="utf-8", errors="replace")
    else:
        missing.append(str(sa))

    for p in extra_paths:
        pp = Path(p)
        if pp.exists():
            texts[f"extra:{pp.name}"] = pp.read_text(encoding="utf-8", errors="replace")
        else:
            missing.append(str(pp))

    if missing:
        texts["__missing__"] = ""  # scanned harmlessly; carried for the report
        texts["__missing_list__"] = "\n".join(missing)
    return texts


def leak_check(reg: Registry, texts: Mapping[str, str]) -> dict:
    """Raise nonce.NonceLeak if any token occurs in the harness's own text.

    Note that `__missing_list__` is deliberately part of `texts`: it is a list of
    paths, so scanning it is harmless, and it keeps the fact that a source was
    absent inside the same report the operator reads.
    """
    ns = reg.nonce_set()
    rep = ns.assert_absent_from_texts(texts)
    rep["missing_sources"] = [s for s in (texts.get("__missing_list__") or "").splitlines() if s]
    rep["sources"] = [s for s in rep["sources"] if not s.startswith("__")]
    return rep


# ── the three-stage prior-check gate ──────────────────────────────────
def wilson_upper(k: int, n: int, z: float = Z_95) -> float:
    """Upper bound of the Wilson score interval for k fires in n trials.

    Reproduces the worked example exactly: wilson_upper(0, 10) == 0.2775…,
    which is the "0/10 gives 0.278, above its own 0.25 threshold" that killed the
    single-stage gate. (The spec also quotes 0/12 = 0.265; this computes 0.2425 —
    see the module docstring.)
    """
    if n <= 0:
        return 1.0
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return min(1.0, (centre + half) / denom)


@dataclass(frozen=True)
class GateResult:
    """One stage of the prior-check gate."""

    gate: str                 # gate1 | gate1b | gate2
    fact_id: str
    admitted: bool
    fires: int
    n: int
    agent_runs: int
    wilson_upper: float | None = None
    detail: str = ""
    evidence: tuple[dict, ...] = ()

    def to_dict(self) -> dict:
        return {
            "gate": self.gate,
            "fact_id": self.fact_id,
            "admitted": self.admitted,
            "fires": self.fires,
            "n": self.n,
            "agent_runs": self.agent_runs,
            "wilson_upper": self.wilson_upper,
            "detail": self.detail,
            "evidence": [dict(e) for e in self.evidence],
        }


# A gate's view of the detector registry: (binding, target) -> result mapping.
# `binding` is FactSpec.detector.to_dict(); `target` is a DetectorContext-shaped
# dict ({workspace, diff_text|diff_path, bash_commands, ...}) plus {kind, id}.
DetectorRunner = Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]


def _import_detectors():
    try:
        from . import detectors as mod  # type: ignore
        return mod
    except ImportError:
        pass
    try:
        import detectors as mod  # type: ignore
        return mod
    except ImportError as e:
        raise DetectorUnavailable(
            f"lib/wur/detectors.py is not importable — Gate 1 and Gate 1b cannot run ({e})"
            ) from e


def load_detector_runner() -> DetectorRunner:
    """Adapt lib/wur/detectors.py to the (binding, target) shape the gates use.

    detectors.py owns the closed 6-predicate registry and exposes
    `run_detector(name, params, DetectorContext, *, diff_only=False)`. It is a
    separately owned file, so it is imported lazily and adapted rather
    than depended on at module scope: Gate 2 consumes already-recorded control
    outcomes and must keep working even when the registry is absent.

    `diff_only` is forwarded from the target rather than read off the context —
    run_detector() re-derives the context from its own keyword, so passing it on
    the context alone would be silently ignored.
    """
    mod = _import_detectors()
    run = getattr(mod, "run_detector", None)
    ctx_cls = getattr(mod, "DetectorContext", None)
    if callable(run) and ctx_cls is not None and hasattr(ctx_cls, "from_dict"):

        def runner(binding: Mapping[str, Any], target: Mapping[str, Any]) -> Mapping[str, Any]:
            t = {k: v for k, v in dict(target).items() if k not in ("kind", "id")}
            return run(binding.get("name"),
                dict(binding.get("params") or {}),
                ctx_cls.from_dict(t),
                diff_only=bool(t.get("diff_only")),
            )

        return runner
    for name in ("run", "evaluate", "fire"):
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    raise DetectorUnavailable("lib/wur/detectors.py exposes neither run_detector(name, params, ctx) nor run"
            )


def _fire(runner: DetectorRunner, fact: FactSpec, target: Mapping[str, Any]) -> dict:
    """One detector evaluation, with a measurement error treated as an ERROR.

    detectors.run_detector() returns `error` non-null when a predicate faulted,
    and explicitly documents that this is NOT a non-fire. Swallowing it here
    would admit a fact through Gate 1 because its detector crashed, which is the
    worst possible way to pass a counter-prior check.
    """
    if fact.detector is None:
        raise RegistryError(f"{fact.fact_id}: no detector binding, cannot gate")
    res = dict(runner(fact.detector_binding(), target))
    if res.get("error"):
        raise RegistryError(
            f"{fact.fact_id}: detector {fact.detector.name!r} errored on target "
            f"{target.get('id') or target.get('kind')!r}: {res['error']}"
            )
    return {
        "target": target.get("id") or target.get("kind") or "?",
        "kind": target.get("kind"),
        "eligible": bool(res.get("eligible", True)),
        "fired": bool(res.get("fired", False)),
        "evidence": res.get("evidence"),
        "detail": res.get("detail"),
    }




def gate1(fact: FactSpec, runner: DetectorRunner, base_target: Mapping[str, Any]) -> GateResult:
    """Stage 1: the detector must NOT fire on the pristine base tree. 0 agent runs.

    This is the counter-prior requirement in its cheapest form: if the mandate is
    already satisfied by the untouched repo, `used` is 1 before the agent starts
    and the arm measures nothing.
    """
    ev = _fire(runner, fact, {"kind": "pristine_base", **dict(base_target)})
    return GateResult(gate="gate1",
        fact_id=fact.fact_id,
        admitted=not ev["fired"],
        fires=int(ev["fired"]),
        n=1,
        agent_runs=0,
        wilson_upper=None,
        detail="detector must not fire on the pristine base tree",
        evidence=(ev,),
            )


def gate1b(fact: FactSpec,
    runner: DetectorRunner,
    targets: Sequence[Mapping[str, Any]],
    *,
    min_near_miss: int = GATE1B_MIN_NEAR_MISS,
) -> GateResult:
    """Stage 1b: cross-task finished workspaces + >= 2 near-miss patches. 0 agent runs.

    Gate 1 only proves the mandate is not satisfied by the *starting* tree.
    Gate 1b asks the harder question: does a competent solution to some OTHER
    task, or a near-miss attempt at this one, satisfy it by accident? A detector
    that fires there is measuring competence, not uptake.

    Each target is a mapping with at least {kind, id} plus whatever the detector
    contract needs (workspace / diff / bash_commands). `kind` must be one of
    `finished_workspace` or `near_miss_patch`.
    """
    ts = [dict(t) for t in targets]
    near = [t for t in ts if t.get("kind") == "near_miss_patch"]
    ev = tuple(_fire(runner, fact, t) for t in ts)
    fires = sum(1 for e in ev if e["fired"])
    enough = len(near) >= min_near_miss
    detail = f"{len(ts)} targets ({len(near)} near-miss); need >= {min_near_miss} near-miss"
    if not enough:
        detail += " — INSUFFICIENT, gate cannot be passed"
    return GateResult(gate="gate1b",
        fact_id=fact.fact_id,
        admitted=(fires == 0 and enough),
        fires=fires,
        n=len(ts),
        agent_runs=0,
        wilson_upper=wilson_upper(fires, len(ts)) if ts else None,
        detail=detail,
        evidence=ev,
            )


def gate2(fact: FactSpec,
    ctrl_fires: Sequence[bool] | int,
    n: int | None = None,
    *,
    min_n: int = GATE2_MIN_N,
) -> GateResult:
    """Stage 2: >= 12 `ctrl` runs, admit at 0 fires. 12 agent runs.

    Accepts either a sequence of per-run booleans or (fires, n). D2: exactly one
    fire in >= min_n runs is `weak` — admitted, but pre-registered as excluded
    from the primary analysis, so keeping the fact does not create a
    forking-paths risk. Two or more fires reject the fact outright.

    `control_fire_rate` on the fact_trace row is fires/n from here.
    """
    if isinstance(ctrl_fires, int):
        fires, total = ctrl_fires, int(n or 0)
    else:
        seq = list(ctrl_fires)
        fires, total = sum(1 for x in seq if x), len(seq)
    if total < min_n:
        return GateResult(gate="gate2",
            fact_id=fact.fact_id,
            admitted=False,
            fires=fires,
            n=total,
            agent_runs=total,
            wilson_upper=wilson_upper(fires, total) if total else None,
            detail=f"only {total} control runs; Gate 2 needs >= {min_n}",
            )
    return GateResult(gate="gate2",
        fact_id=fact.fact_id,
        admitted=fires <= GATE2_WEAK_FIRES,
        fires=fires,
        n=total,
        agent_runs=total,
        wilson_upper=wilson_upper(fires, total),
        detail=("0 fires — admitted"
            if fires == 0
            else (f"{fires} fire in {total} control runs — WEAK (admitted, pre-registered "
                "as excluded from primary)"
                if fires == GATE2_WEAK_FIRES
                else f"{fires} fires in {total} control runs — rejected"
            )
                    ),
            )


def prior_check(fact: FactSpec,
    g1: GateResult | None = None,
    g1b: GateResult | None = None,
    g2: GateResult | None = None,
) -> dict:
    """Combine the three stages into the `prior_check_status` carried per row.

    status:
      pass  every run stage admitted with 0 fires
      weak  Gate 2 ran with exactly 1 fire in >= 12 controls
      fail  any stage rejected
      n_a   Gate 1/1b have not been run (nothing has been checked yet)
    """
    stages = {"gate1": g1, "gate1b": g1b, "gate2": g2}
    ran = {k: v for k, v in stages.items() if v is not None}
    if not ran or g1 is None or g1b is None:
        status = "n_a"
    elif any(not r.admitted for r in ran.values()):
        status = "fail"
    elif g2 is not None and g2.fires == GATE2_WEAK_FIRES:
        status = "weak"
    else:
        status = "pass"
    return {
        "schema_version": "1",
        "fact_id": fact.fact_id,
        "task_id": fact.task_id,
        "status": status,
        "control_fire_rate": (g2.fires / g2.n) if (g2 is not None and g2.n) else None,
        "agent_runs": sum(r.agent_runs for r in ran.values()),
        "stages": {k: v.to_dict() for k, v in ran.items()},
    }


def write_prior_check(job_dir: str | Path, payload: Mapping[str, Any]) -> Path:
    """Write `$JOB_DIR/.registry/prior_check/<task_id>.json` atomically."""
    task_id = str(payload.get("task_id") or payload.get("fact_id") or "unknown")
    d = registry_dir(job_dir) / "prior_check"
    d.mkdir(parents=True, exist_ok=True, mode=REGISTRY_DIR_MODE)
    p = d / f"{task_id}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    os.chmod(p, REGISTRY_FILE_MODE)
    return p


# ── CLI ──────────────────────────────────────────────────────────────────────
def _resolve_facts(a: argparse.Namespace) -> Registry:
    path = a.facts or (registry_path(a.job_dir) if getattr(a, "job_dir", None) else None)
    if not path:
        raise SystemExit("need --facts PATH or --job-dir DIR")
    return load(path)


def _cli_validate(a: argparse.Namespace) -> int:
    reg = _resolve_facts(a)
    errs, warns = validate(reg), lint(reg)
    for w in warns:
        print(f"WARN {w}", file=sys.stderr)
    for e in errs:
        print(f"ERROR {e}", file=sys.stderr)
    print(json.dumps({"facts": len(reg.facts), "errors": errs, "warnings": warns}, indent=2))
    return 1 if errs else 0


def _cli_mint(a: argparse.Namespace) -> int:
    reg = _resolve_facts(a)
    errs = validate(reg)
    if errs and not a.allow_invalid:
        for e in errs:
            print(f"ERROR {e}", file=sys.stderr)
        return 1
    reg = mint_nonces(reg,
        repo_sha=a.repo_sha,
        salt=a.salt,
        repo_dir=a.repo_dir,
        baseline_sha=a.baseline_sha,
        force=a.force,
            )
    out = Path(a.out) if a.out else (registry_path(a.job_dir, create=True) if a.job_dir else Path(a.facts))
    save(reg, out)
    print(json.dumps({"registry": str(out), "nonces": {f.fact_id: f.nonce for f in reg.facts}}, indent=2))
    return 0


def _cli_nonces(a: argparse.Namespace) -> int:
    reg = _resolve_facts(a)
    print(json.dumps({f.fact_id: f.nonce for f in reg.facts}, indent=2))
    return 0


def _cli_show(a: argparse.Namespace) -> int:
    reg = _resolve_facts(a)
    fact = reg.get(a.fact_id)
    print(json.dumps(fact.canonical(with_distractors=a.distractors).to_dict(), indent=2))
    return 0


def _cli_leak_check(a: argparse.Namespace) -> int:
    reg = _resolve_facts(a)
    texts = collect_leak_texts(a.job_dir, repo_root=a.repo_root, extra_paths=a.extra or [])
    try:
        rep = leak_check(reg, texts)
    except nonce_mod.NonceLeak as e:
        print(f"NONCE LEAK: {e}", file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return 1
    print(json.dumps(rep, indent=2))
    return 0


def _cli_gate1(a: argparse.Namespace) -> int:
    reg = _resolve_facts(a)
    res = gate1(reg.get(a.fact_id), load_detector_runner(), {"id": "base", "workspace": a.base_workspace})
    print(json.dumps(res.to_dict(), indent=2))
    return 0 if res.admitted else 1


def _cli_gate1b(a: argparse.Namespace) -> int:
    reg = _resolve_facts(a)
    targets: list[dict] = []
    for path in a.targets_json or []:
        loaded = json.loads(Path(path).read_text())
        targets += list(loaded) if isinstance(loaded, list) else [loaded]
    targets += [{"kind": "finished_workspace", "id": w, "workspace": w} for w in (a.workspace or [])]
    # A near-miss patch is judged on its DIFF: without a tree that has the patch
    # applied, a whole-workspace predicate would be reading the base tree and
    # answering Gate 1's question over again.
    for p in a.patch or []:
        if not a.base_workspace:
            raise SystemExit("--patch requires --base-workspace (the tree the diff applies to)")
        targets.append({
            "kind": "near_miss_patch", "id": p, "diff_path": p,
            "workspace": a.base_workspace, "diff_only": True,
        })
    res = gate1b(reg.get(a.fact_id), load_detector_runner(), targets)
    print(json.dumps(res.to_dict(), indent=2))
    return 0 if res.admitted else 1


def _cli_gate2(a: argparse.Namespace) -> int:
    reg = _resolve_facts(a)
    res = gate2(reg.get(a.fact_id), a.fires, a.n)
    print(json.dumps(res.to_dict(), indent=2))
    return 0 if res.admitted else 1


def _cli_prior_check(a: argparse.Namespace) -> int:
    reg = _resolve_facts(a)
    fact = reg.get(a.fact_id)
    loaded: dict[str, GateResult] = {}
    for path in a.gate_json or []:
        d = json.loads(Path(path).read_text())
        loaded[d["gate"]] = GateResult(
            gate=d["gate"], fact_id=d["fact_id"], admitted=d["admitted"], fires=d["fires"],
            n=d["n"], agent_runs=d.get("agent_runs", 0), wilson_upper=d.get("wilson_upper"),
            detail=d.get("detail", ""), evidence=tuple(d.get("evidence") or ()),
            )
    payload = prior_check(fact, loaded.get("gate1"), loaded.get("gate1b"), loaded.get("gate2"))
    p = write_prior_check(a.job_dir, payload)
    print(json.dumps({**payload, "path": str(p)}, indent=2))
    return 0 if payload["status"] in ("pass", "weak") else 1


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="WUR fact registry")
    p.add_argument("--facts", help="path to facts.yaml (or facts.json)")
    p.add_argument("--job-dir", help="job dir; the registry is <job-dir>/.registry/facts.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate").set_defaults(fn=_cli_validate)

    m = sub.add_parser("mint")
    m.add_argument("--repo-sha")
    m.add_argument("--salt")
    m.add_argument("--repo-dir", help="git dir to assert the nonces are absent from")
    m.add_argument("--baseline-sha", help="tree-ish to grep; defaults to --repo-sha")
    m.add_argument("--out")
    m.add_argument("--force", action="store_true", help="re-mint even where a nonce exists")
    m.add_argument("--allow-invalid", action="store_true")
    m.set_defaults(fn=_cli_mint)

    sub.add_parser("nonces").set_defaults(fn=_cli_nonces)

    s = sub.add_parser("show")
    s.add_argument("--fact-id", required=True)
    s.add_argument("--distractors", action="store_true")
    s.set_defaults(fn=_cli_show)

    lc = sub.add_parser("leak-check")
    lc.add_argument("--repo-root")
    lc.add_argument("--extra", action="append")
    lc.set_defaults(fn=_cli_leak_check)

    g1 = sub.add_parser("gate1")
    g1.add_argument("--fact-id", required=True)
    g1.add_argument("--base-workspace", required=True)
    g1.set_defaults(fn=_cli_gate1)

    g1b = sub.add_parser("gate1b")
    g1b.add_argument("--fact-id", required=True)
    g1b.add_argument("--workspace", action="append", help="a finished cross-task workspace")
    g1b.add_argument("--patch", action="append", help="a near-miss unified diff; needs --base-workspace")
    g1b.add_argument("--base-workspace", help="tree the --patch diffs apply to")
    g1b.add_argument("--targets-json", action="append",
                     help="a JSON file of DetectorContext-shaped targets, for anything richer")
    g1b.set_defaults(fn=_cli_gate1b)

    g2 = sub.add_parser("gate2")
    g2.add_argument("--fact-id", required=True)
    g2.add_argument("--fires", type=int, required=True)
    g2.add_argument("--n", type=int, required=True)
    g2.set_defaults(fn=_cli_gate2)

    pc = sub.add_parser("prior-check")
    pc.add_argument("--fact-id", required=True)
    pc.add_argument("--gate-json", action="append", help="a GateResult JSON written earlier")
    pc.set_defaults(fn=_cli_prior_check)

    a = p.parse_args(argv)
    if a.cmd in ("leak-check", "prior-check") and not a.job_dir:
        p.error(f"{a.cmd} needs --job-dir")
    try:
        return int(a.fn(a))
    except (RegistryError, nonce_mod.NonceError, render_mod.RenderError) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
