#!/usr/bin/env python3
"""
exposure.py — the nonce scan over model-visible regions -> exposure.jsonl.

RESPONSIBILITY
  Answer, for one run, the only question actually defines:

      did fact f's nonce occur in a model-visible text region of run r,
      and if so — where, through which channel, and in what form?

  One row per (fact x region-that-matched). Nothing here decides `read`;
  trace.py does that from these rows, because `read` is a union over channels
  and this module is deliberately blind to the union rule.

SCAN BEFORE TRUNCATING — ALWAYS
  This module runs BEFORE events.py digests anything. A digest computed first
  would drop a nonce past the cut, and the run would score "not read" for a fact
  that was sitting in the context window. `assert_scan_before_truncate()` exists
  so the ordering is a checked precondition, not a comment.

INPUTS
  A RegionSet from regions.py (regions.from_run($RUN_DIR)), plus the fact cards:
  a JSON/YAML facts file, or $JOB_DIR/.registry/_index/probe_key.json
  (fact_id -> {token, surface_forms, source_path, gist}). YAML is loaded only if
  PyYAML happens to be importable — the system python3 has neither yaml nor
  jsonschema, so JSON is the supported path.

OUTPUTS
  $RUN_DIR/exposure.jsonl — one JSON object per line, ROW_SCHEMA below.
  Only `exact` INBOUND hits are eligible to set first_exposure_seq: a
  lowercased or hyphen-stripped nonce in a tool result is almost certainly the
  agent's own prior text being re-read.

CLI
  python3 lib/wur/exposure.py --run-dir DIR [--facts FILE] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

try:
    from . import protocol, regions as regions_mod
except ImportError:  # flat context
    import protocol  # type: ignore
    import regions as regions_mod  # type: ignore

FactCard = protocol.FactCard

SCHEMA_VERSION = "1"

#: match_form, ordered strongest-first (protocol.MATCH_FORMS).
FORM_RANK = {form: i for i, form in enumerate(protocol.MATCH_FORMS)}

#: Only these forms, on an inbound region, may set first_exposure_seq.
EXPOSURE_FIRST_FORMS = protocol.EXPOSURE_FIRST_FORMS  # ("exact",)


# ── the row contract (there is no schemas/exposure.schema.json in the tree) ──
ROW_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "exposure.row.schema.json",
    "title": "WurExposure",
    "description": ("One row of $RUN_DIR/exposure.jsonl: one (fact x model-visible "
        "region) hit. `model_visible` and `channel_inbound` are fixed per channel; "
        "`inbound` is the EFFECTIVE value after regions.py forces it off for a tool "
        "error body (harness-authored text is not a workspace channel). Only exact "
        "inbound hits set first_exposure_seq."
    ),
    "type": "object",
    "required": ["schema_version", "run_id", "fact_id", "seq", "channel", "match_form",
                 "model_visible", "inbound", "counts_toward_read", "offset", "bytes_before"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "string", "const": "1"},
        "run_id": {"type": "string", "pattern": "^[a-zA-Z0-9._-]+$"},
        "fact_id": {"type": "string"},
        "seq": {"type": "integer", "minimum": 0},
        "region_idx": {"type": "integer", "minimum": 0},
        "channel": {"type": "string"},
        "source": {"type": "string", "enum": ["stream", "transcript", "gate", "driver", "manifest"]},
        "model_visible": {"type": "boolean"},
        "inbound": {"type": "boolean"},
        "channel_inbound": {"type": "boolean"},
        "counts_toward_read": {"type": "boolean"},
        "match_form": {"type": "string", "enum": ["exact", "lower", "nohyphen", "regex"]},
        "matched_token": {"type": ["string", "null"]},
        "matched_regex": {"type": ["string", "null"]},
        "matched_text": {"type": ["string", "null"]},
        "offset": {"type": "integer", "minimum": 0},
        "bytes_before": {"type": "integer", "minimum": 0},
        "n_hits": {"type": "integer", "minimum": 1},
        "region_bytes": {"type": "integer", "minimum": 0},
        "tool": {"type": ["string", "null"]},
        "tool_use_id": {"type": ["string", "null"]},
        "message_id": {"type": ["string", "null"]},
        "ts": {"type": ["string", "null"]},
        "is_error": {"type": ["boolean", "null"]},
        "sets_first_exposure": {"type": "boolean"},
        "extra": {"type": "object"},
    },
}

# schemas/exposure.schema.json is the single source of truth once it exists, so a
# hand-edit there cannot silently drift from what this module actually emits (the
# other three tables already validate against files; exposure was the only emitter
# validating against an in-module dict). The literal above remains the fallback so
# the module still works from a checkout with no schemas/ directory.
_SCHEMA_FILE = Path(__file__).resolve().parents[2] / "schemas" / "exposure.schema.json"
if _SCHEMA_FILE.is_file():
    try:
        ROW_SCHEMA = json.loads(_SCHEMA_FILE.read_text())
    except (OSError, ValueError):
        pass  # keep the literal; validate.py will report any real mismatch


# ── fact cards ───────────────────────────────────────────────────────────────
def _card_from_any(fact_id: str, d: Any) -> FactCard:
    """Accept both facts.yaml shape (`nonce`) and probe_key.json shape (`token`)."""
    if not isinstance(d, dict):
        return FactCard(fact_id=fact_id, nonce=str(d))
    merged = dict(d)
    merged.setdefault("fact_id", fact_id)
    if not merged.get("nonce") and merged.get("token"):
        merged["nonce"] = merged["token"]
    return FactCard.from_dict(merged)


def load_fact_cards(source: Any) -> list[FactCard]:
    """Fact cards from a path, a mapping, or a list of dicts.

    Accepted shapes:
      {"facts": {id: {...}}} | {"facts": [ {...}, ... ]} | {id: {...}} | [ {...} ]
    A path ending .yaml/.yml is parsed with PyYAML if importable; otherwise the
    caller must supply JSON (the system python3 has no yaml).
    """
    data: Any = source
    if isinstance(source, (str, os.PathLike)):
        p = Path(source)
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError(
                    f"{p} is YAML but PyYAML is not importable; run install.sh or pass JSON"
                ) from exc
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)

    if isinstance(data, dict) and "facts" in data:
        data = data["facts"]
    cards: list[FactCard] = []
    if isinstance(data, dict):
        for fid, body in data.items():
            cards.append(_card_from_any(str(fid), body))
    elif isinstance(data, list):
        for i, body in enumerate(data):
            fid = str((body or {}).get("fact_id") or (body or {}).get("id") or i) \
                if isinstance(body, dict) else str(i)
            cards.append(_card_from_any(fid, body))
    return [c for c in cards if c.fact_id]




def find_facts_file(run_dir: str | os.PathLike, job_dir: str | os.PathLike | None = None) -> Path | None:
    """Locate the fact registry for a run without guessing wildly.

    Order: $RUN_DIR/facts.json, $RUN_DIR/fact_card.json, then the job registry
    index $JOB_DIR/.registry/_index/probe_key.json, then .registry/facts.yaml.
    """
    rd = Path(run_dir)
    for cand in (rd / "facts.json", rd / "fact_card.json", rd / "facts.yaml"):
        if cand.exists():
            return cand
    roots = [Path(job_dir)] if job_dir else []
    roots.extend(rd.parents[:4])
    for root in roots:
        for rel in ("_index/probe_key.json", ".registry/_index/probe_key.json",
                    ".registry/facts.yaml", "facts.yaml"):
            cand = root / rel
            if cand.exists():
                return cand
    return None


# ── the scanner ──────────────────────────────────────────────────────────────
def _byte_offset(text: str, char_index: int) -> int:
    return len(text[:char_index].encode("utf-8", "replace"))


_SEP_RE = re.compile(r"[\s_\-]+")


def _strip_sep_with_map(text: str) -> tuple[str, list[int]]:
    """Separator-stripped lowercase text plus a per-character map back to `text`."""
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if _SEP_RE.match(ch):
            continue
        out.append(ch.lower())
        idx.append(i)
    return "".join(out), idx


@dataclass(frozen=True)
class Hit:
    form: str
    char_index: int
    length: int
    token: str | None
    regex: str | None


def scan_text(text: str, card: FactCard) -> list[Hit]:
    """Every hit of `card` in `text`, strongest form first, in order.

    exact    the nonce (or a surface form) verbatim
    lower    the same token, case-insensitively
    nohyphen the same token with whitespace/underscores/hyphens removed
    regex    a tier-(b) paraphrase pattern, FROZEN in facts.yaml before any data
    """
    if not text:
        return []
    tokens = [t for t in ([card.nonce] + list(card.surface_forms)) if t]
    hits: list[Hit] = []

    for tok in tokens:
        for m in re.finditer(re.escape(tok), text):
            hits.append(Hit("exact", m.start(), len(m.group(0)), tok, None))
    exact_spans = {(h.char_index, h.length) for h in hits}

    for tok in tokens:
        for m in re.finditer(re.escape(tok), text, re.IGNORECASE):
            if (m.start(), len(m.group(0))) in exact_spans:
                continue
            hits.append(Hit("lower", m.start(), len(m.group(0)), tok, None))
    covered = {(h.char_index, h.length) for h in hits}

    stripped, cmap = _strip_sep_with_map(text)
    for tok in tokens:
        needle, _ = _strip_sep_with_map(tok)
        if not needle:
            continue
        start = 0
        while True:
            j = stripped.find(needle, start)
            if j < 0:
                break
            start = j + 1
            ci = cmap[j] if j < len(cmap) else 0
            end = cmap[min(j + len(needle), len(cmap)) - 1] + 1 if cmap else ci
            if (ci, end - ci) in covered:
                continue
            hits.append(Hit("nohyphen", ci, max(1, end - ci), tok, None))

    for pat, src in zip(card.compiled_regexes(), card.regexes):
        for m in pat.finditer(text):
            hits.append(Hit("regex", m.start(), len(m.group(0)), None, src))

    hits.sort(key=lambda h: (FORM_RANK.get(h.form, 9), h.char_index))
    return hits


def assert_scan_before_truncate(regionset: "regions_mod.RegionSet") -> None:
    """Precondition: nothing has digested or truncated the regions yet.

    regions.py hands over full region text; events.py replaces it with a digest.
    If a caller ever reorders the chain, this raises instead of silently
    producing a run that scores 'not read' for a fact it was shown.
    """
    for r in regionset.regions:
        if r.model_visible and r.meta.get("digested"):
            raise RuntimeError(
                f"scan-before-truncate violated: region seq={r.seq} idx={r.region_idx} "
                "was already digested; exposure.py must run before events.py"
            )




def scan(regionset: "regions_mod.RegionSet", cards: Sequence[FactCard], run_id: str) -> list[dict]:
    """exposure.jsonl rows for one run. One row per (fact x matched region)."""
    assert_scan_before_truncate(regionset)
    rows: list[dict] = []
    for region in regionset.regions:
        if not region.model_visible or not region.text:
            continue  # sidecar_only / persisted_output_ondisk carry no bytes
        for card in cards:
            hits = scan_text(region.text, card)
            if not hits:
                continue
            best = hits[0]
            same = [h for h in hits if h.form == best.form]
            spec_inbound = regions_mod.channel_spec(region.channel).inbound
            sets_first = bool(region.inbound and best.form in EXPOSURE_FIRST_FORMS)
            rows.append({
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "fact_id": card.fact_id,
                "seq": region.seq,
                "region_idx": region.region_idx,
                "channel": region.channel,
                "source": region.source,
                "model_visible": region.model_visible,
                "inbound": bool(region.inbound),
                "channel_inbound": bool(spec_inbound),
                "counts_toward_read": bool(region.counts_toward_read),
                "match_form": best.form,
                "matched_token": best.token,
                "matched_regex": best.regex,
                "matched_text": region.text[best.char_index: best.char_index + best.length][:200],
                "offset": _byte_offset(region.text, best.char_index),
                "bytes_before": region.bytes_before + _byte_offset(region.text, best.char_index),
                "n_hits": len(same),
                "region_bytes": region.nbytes,
                "tool": region.tool,
                "tool_use_id": region.tool_use_id,
                "message_id": region.message_id,
                "ts": region.ts,
                "is_error": bool(region.is_error),
                "sets_first_exposure": sets_first,
            })
    rows.sort(key=lambda r: (r["seq"], r["region_idx"], r["fact_id"]))
    return rows


def first_exposure(rows: Sequence[dict], fact_id: str) -> dict | None:
    """The row that sets first_exposure_seq for `fact_id`, or None.

    Only EXACT INBOUND hits qualify. self_thinking is model-visible but not
    inbound, so a thinking-only fact has no first_exposure_seq — which is exactly
    what makes `unexplained_possession` detectable.
    """
    cand = [r for r in rows if r["fact_id"] == fact_id and r.get("sets_first_exposure")]
    return min(cand, key=lambda r: (r["seq"], r["region_idx"])) if cand else None




def run(run_dir: str | os.PathLike, facts: Any = None, out_path: str | os.PathLike | None = None,
        run_id: str | None = None) -> tuple[list[dict], dict]:
    """Scan one run dir and write exposure.jsonl. Returns (rows, summary)."""
    rd = Path(run_dir)
    rid = run_id or _run_id_of(rd)
    rs = regions_mod.from_run(rd)
    src = facts if facts is not None else find_facts_file(rd)
    cards = load_fact_cards(src) if src is not None else []
    rows = scan(rs, cards, rid)
    target = Path(out_path) if out_path else rd / "exposure.jsonl"
    regions_mod.write_jsonl_atomic(target, rows)
    summary = {
        "run_id": rid,
        "n_facts": len(cards),
        "n_rows": len(rows),
        "n_unknown_visible": len(rs.unknown_visible),
        "channels": rs.meta.get("channels", {}),
        "facts_source": str(src) if isinstance(src, (str, os.PathLike)) else "inline",
        "out": str(target),
    }
    return rows, summary


def _run_id_of(run_dir: Path) -> str:
    meta = regions_mod.read_json(run_dir / "run_meta.json", {}) or {}
    return str(meta.get("run_id") or run_dir.name)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="nonce scan over model-visible regions")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--facts", default=None, help="facts JSON/YAML or probe_key.json")
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)
    _rows, summary = run(a.run_dir, a.facts, a.out)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not summary["n_facts"]:
        print("no fact cards found — exposure.jsonl is empty by construction",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
