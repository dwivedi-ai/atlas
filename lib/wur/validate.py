#!/usr/bin/env python3
"""
validate.py — schema validation for every derived table, at teardown.

RESPONSIBILITY
  Refuse to let a malformed row reach the parquet. Every line of events.jsonl,
  exposure.jsonl, probes.jsonl and fact_trace.jsonl is validated against its
  schema before the run is marked done, and the closed channel enum is
  cross-checked against the enum regions.py actually implements — a silent drift
  between the two would defeat the `unknown_visible` tripwire.

WHY THERE IS A VALIDATOR IN HERE AT ALL
  The system python3 has NEITHER yaml NOR jsonschema (measured), and this runs
  at teardown on every one of 840 runs. So: `jsonschema` is used when it is
  importable, and otherwise a stdlib validator covers exactly the vocabulary the
  four schemas use — type, enum, const, required, properties,
  additionalProperties, items, minItems/maxItems, minimum/maximum, pattern,
  anyOf, allOf, if/then/else and $ref into $defs. Anything outside that
  vocabulary is reported as an UNSUPPORTED KEYWORD rather than silently passing,
  so the fallback can never quietly become a no-op.

INPUTS
  $RUN_DIR/{events,exposure,probes,fact_trace}.jsonl
  schemas/{events,probes,fact_trace}.schema.json  (+ exposure.ROW_SCHEMA, which
  has no file in schemas/ — see `needs_from_others`).

OUTPUTS
  A report dict {ok, tables:{name:{rows, errors:[...]}}, channel_enum, engine},
  written to $RUN_DIR/validate.json by the CLI.

CLI
  python3 lib/wur/validate.py --run-dir DIR [--max-errors N]
    exits 1 on any validation error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    from . import exposure as exposure_mod, regions as regions_mod
except ImportError:  # flat context
    import exposure as exposure_mod  # type: ignore
    import regions as regions_mod  # type: ignore

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"

#: keywords the stdlib fallback understands; anything else is reported, loudly.
_SUPPORTED = {
    "$schema", "$id", "$defs", "$ref", "title", "description", "default", "examples",
    "type", "enum", "const", "required", "properties", "additionalProperties",
    "items", "minItems", "maxItems", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "pattern", "anyOf", "allOf", "oneOf", "not", "if", "then",
    "else", "format", "deprecated",
}

_TYPES: dict[str, tuple[type, ...] | str] = {
    "object": (dict,), "array": (list,), "string": (str,), "number": (int, float),
    "integer": (int,), "boolean": (bool,), "null": (type(None),),
}


# ── the stdlib fallback validator ────────────────────────────────────────────
def _type_ok(value: Any, typename: str) -> bool:
    if typename == "boolean":
        return isinstance(value, bool)
    if typename == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typename == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    py = _TYPES.get(typename)
    return isinstance(value, py) if isinstance(py, tuple) else False


def _resolve(ref: str, root: dict) -> dict:
    if not ref.startswith("#/"):
        raise ValueError(f"only local $ref is supported, got {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node


def _json_eq(a: Any, b: Any) -> bool:
    """JSON equality: `true` is not `1`, unlike Python's ==."""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def _matches(instance: Any, schema: Any, root: dict) -> bool:
    return not validate_instance(instance, schema, root, "")


def validate_instance(instance: Any, schema: Any, root: dict | None = None,
                      path: str = "") -> list[str]:
    """Errors for `instance` against `schema`. Empty list means valid."""
    if schema is True or schema == {}:
        return []
    if schema is False:
        return [f"{path or '<root>'}: schema is false"]
    if not isinstance(schema, dict):
        return [f"{path or '<root>'}: schema is not an object"]
    root = root if root is not None else schema
    errs: list[str] = []
    here = path or "<root>"

    unsupported = sorted(set(schema) - _SUPPORTED)
    if unsupported:
        errs.append(f"{here}: UNSUPPORTED SCHEMA KEYWORD(S) {unsupported} — "
                    "the stdlib fallback cannot check these; install jsonschema")

    if "$ref" in schema:
        return errs + validate_instance(instance, _resolve(schema["$ref"], root), root, path)

    t = schema.get("type")
    if t is not None:
        names = t if isinstance(t, list) else [t]
        if not any(_type_ok(instance, n) for n in names):
            got = "boolean" if isinstance(instance, bool) else type(instance).__name__
            return errs + [f"{here}: type {names} expected, got {got}"]

    if "const" in schema and not _json_eq(instance, schema["const"]):
        errs.append(f"{here}: const {schema['const']!r} expected, got {instance!r}")
    if "enum" in schema and not any(_json_eq(instance, e) for e in schema["enum"]):
        errs.append(f"{here}: {instance!r} not in enum {schema['enum']}")

    if isinstance(instance, str) and "pattern" in schema:
        if not re.search(schema["pattern"], instance):
            errs.append(f"{here}: {instance!r} does not match /{schema['pattern']}/")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errs.append(f"{here}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errs.append(f"{here}: {instance} > maximum {schema['maximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errs.append(f"{here}: {len(instance)} items < minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errs.append(f"{here}: {len(instance)} items > maxItems {schema['maxItems']}")
        if "items" in schema:
            for i, item in enumerate(instance):
                errs.extend(validate_instance(item, schema["items"], root, f"{path}[{i}]"))

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errs.append(f"{here}: missing required property {key!r}")
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in instance:
                errs.extend(validate_instance(instance[key], sub, root,
                                              f"{path}.{key}" if path else key))
        addl = schema.get("additionalProperties")
        if addl is False:
            extra = sorted(set(instance) - set(props))
            if extra:
                errs.append(f"{here}: additional properties not allowed: {extra}")
        elif isinstance(addl, dict):
            for key in sorted(set(instance) - set(props)):
                errs.extend(validate_instance(instance[key], addl, root,
                                              f"{path}.{key}" if path else key))

    for sub in schema.get("allOf", []):
        errs.extend(validate_instance(instance, sub, root, path))
    if "anyOf" in schema and not any(_matches(instance, s, root) for s in schema["anyOf"]):
        errs.append(f"{here}: matches none of anyOf")
    if "oneOf" in schema:
        n = sum(1 for s in schema["oneOf"] if _matches(instance, s, root))
        if n != 1:
            errs.append(f"{here}: matches {n} of oneOf, expected exactly 1")
    if "not" in schema and _matches(instance, schema["not"], root):
        errs.append(f"{here}: matches `not` schema")

    if "if" in schema:
        branch = "then" if _matches(instance, schema["if"], root) else "else"
        if branch in schema:
            errs.extend(validate_instance(instance, schema[branch], root, path))
    return errs


def _jsonschema_validator(schema: dict):
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return None
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)






ENGINE = "jsonschema" if _jsonschema_validator({"type": "object"}) is not None else "stdlib-subset"


def validate_rows(rows: Iterable[dict], schema: dict, label: str = "",
                  max_errors: int = 25) -> list[dict]:
    """[{line, errors:[...]}] for every row that failed. Empty means all valid."""
    v = _jsonschema_validator(schema)
    out: list[dict] = []
    for i, row in enumerate(rows, 1):
        if v is not None:
            errs = [f"{'.'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
                    for e in v.iter_errors(row)]
        else:
            errs = validate_instance(row, schema, schema, "")
        if errs:
            out.append({"table": label, "line": i, "errors": errs[:max_errors]})
        if len(out) >= max_errors:
            break
    return out


# ── schemas ──────────────────────────────────────────────────────────────────
def load_schema(name: str, schemas_dir: str | os.PathLike | None = None) -> dict:
    """The schema for one table. `exposure` has no file and is owned by exposure.py."""
    if name == "exposure":
        return exposure_mod.ROW_SCHEMA
    d = Path(schemas_dir) if schemas_dir else SCHEMAS_DIR
    return json.loads((d / f"{name}.schema.json").read_text(encoding="utf-8"))


TABLES = (
    ("events", "events.jsonl"),
    ("exposure", "exposure.jsonl"),
    ("probes", "probes.jsonl"),
    ("fact_trace", "fact_trace.jsonl"),
)


def check_channel_enum(schemas_dir: str | os.PathLike | None = None) -> list[str]:
    """The closed enum must be identical in regions.py and both schemas.

    A drift here would leave a channel that regions.py can emit but no schema
    admits (or worse, one both admit that nothing can ever produce), and the
    `unknown_visible` tripwire would stop meaning anything.
    """
    problems: list[str] = []
    impl = {n for n in regions_mod.CHANNELS}
    for name in ("events", "fact_trace"):
        try:
            schema = load_schema(name, schemas_dir)
        except Exception as exc:  # pragma: no cover - missing file
            problems.append(f"{name}.schema.json unreadable: {exc}")
            continue
        node = schema.get("$defs", {}).get("channel", {})
        listed: set[str] = set()
        for alt in node.get("anyOf", []):
            listed |= set(alt.get("enum") or [])
        if not listed:
            problems.append(f"{name}.schema.json has no $defs.channel enum")
            continue
        missing = sorted(listed - impl)
        extra = sorted(impl - listed)
        if missing:
            problems.append(f"{name}.schema.json declares channels regions.py cannot emit: {missing}")
        if extra:
            problems.append(f"regions.py emits channels {name}.schema.json rejects: {extra}")
    if "unknown_visible" not in impl:
        problems.append("regions.py lost the unknown_visible tripwire")
    return problems


# ── run-level entry point ────────────────────────────────────────────────────
def validate_run(run_dir: str | os.PathLike, schemas_dir: str | os.PathLike | None = None,
                 max_errors: int = 25, require: Sequence[str] = ()) -> dict:
    """Validate every derived table present in `run_dir`."""
    rd = Path(run_dir)
    report: dict[str, Any] = {"run_dir": str(rd), "engine": ENGINE, "tables": {}, "ok": True}
    for name, fname in TABLES:
        path = rd / fname
        if not path.exists():
            report["tables"][name] = {"present": False, "rows": 0, "errors": []}
            if name in require:
                report["tables"][name]["errors"] = [f"{fname} is missing"]
                report["ok"] = False
            continue
        rows = regions_mod.read_jsonl(path)
        errors = validate_rows(rows, load_schema(name, schemas_dir), name, max_errors)
        report["tables"][name] = {"present": True, "rows": len(rows), "errors": errors}
        if errors:
            report["ok"] = False
    report["channel_enum"] = check_channel_enum(schemas_dir)
    if report["channel_enum"]:
        report["ok"] = False
    unknown = sum(1 for r in regions_mod.read_jsonl(rd / "exposure.jsonl")
                  if r.get("channel") == "unknown_visible")
    report["unknown_visible_exposures"] = unknown
    if unknown:
        report["ok"] = False
    return report


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="validate every derived table")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--schemas-dir", default=None)
    p.add_argument("--max-errors", type=int, default=25)
    p.add_argument("--require", nargs="*", default=[],
                   help="table names that MUST be present, e.g. events fact_trace")
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)
    report = validate_run(a.run_dir, a.schemas_dir, a.max_errors, a.require)
    regions_mod.write_json_atomic(a.out or (Path(a.run_dir) / "validate.json"), report)
    print(json.dumps({k: v for k, v in report.items() if k != "tables"}, indent=2, sort_keys=True))
    for name, info in report["tables"].items():
        for e in info.get("errors", []):
            print(f"INVALID {name} line {e.get('line', '?')}: {e.get('errors', e)}", file=sys.stderr)
    for prob in report["channel_enum"]:
        print(f"CHANNEL ENUM DRIFT: {prob}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
