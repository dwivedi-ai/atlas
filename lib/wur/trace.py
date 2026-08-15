#!/usr/bin/env python3
"""
trace.py — the headline table: one row per (run x fact) -> fact_trace.jsonl.

RESPONSIBILITY
  Place one run at one point in the funnel available -> read -> used -> retained,
  by joining exposure.jsonl, events.jsonl, probes.jsonl and
  use_detect.json. With one fact per task there is exactly one row per run,
  which is what makes rows independent strata for every significance test and
  the resampling unit for the bootstrap.

INPUTS
  $RUN_DIR/{exposure,events,probes}.jsonl   the derived layer
  $RUN_DIR/use_detect.json                  `used`, produced BEFORE grading
                                            touched the tree
  $RUN_DIR/judge.json                       `success` — deliberately independent
  $RUN_DIR/{run_meta,hygiene,run_record}.json   identity, factors, gates, cost
  fact cards                                fact_id, nonce, planted path

OUTPUTS
  $RUN_DIR/fact_trace.jsonl   rows validating against schemas/fact_trace.schema.json

THE FOUR RULES THAT ARE NOT NEGOTIABLE
  D4  read = inbound U self_thinking is PRIMARY. read_inbound_only is
      recorded alongside on every row so the mandatory sensitivity analysis
      needs no re-run. unexplained_possession = a self_thinking hit with NO
      PRIOR inbound hit; it is the compensating alarm for folding thinking into
      read, and any true row is quarantined and hand-audited.
  d0-push is ASSERTED, not scanned: exposure_basis = manifest_canary and
      EVERY seq/byte field is null. This module RAISES if a manifest_canary row
      would carry one — a measured seq is preserved in `extra` instead, so the
      assertion never costs information.
  Ordering invariant: for every row with read = 1 AND ever_mention = 1,
      first_exposure_seq < first_mention_seq. Violations are COUNTED and
      quarantine the run.
  A truncated or errored read on the fact file scores read = UNKNOWN,
      never read = 0. Wide searches truncate and deep facts are found by wide
      searches, so coercing unknown to false would make the bias run WITH the
      hypothesis.

CLI
  python3 lib/wur/trace.py --run-dir DIR [--facts FILE] [--out PATH]
    exits 1 when the run is quarantined or an invariant fired.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from . import exposure as exposure_mod, probes as probes_mod, regions as regions_mod
except ImportError:  # flat context
    import exposure as exposure_mod  # type: ignore
    import probes as probes_mod  # type: ignore
    import regions as regions_mod  # type: ignore

SCHEMA_VERSION = "1"
JOIN_COVERAGE_GATE = 0.99
PARSE_OK_GATE = 0.90

#: Channels whose hits are the model's own production, not exposure.
ECHO_CHANNELS = frozenset({"self_text", "tool_input", "probe_answer"})

#: Bash verbs that constitute "opening" a file for `opened`.
_OPEN_VERBS = ("cat", "sed", "head", "tail", "less", "more", "awk", "nl", "bat", "view")


class D0PushSeqViolation(RuntimeError):
    """A manifest_canary row carried a non-null seq/byte field."""


# ── inputs ───────────────────────────────────────────────────────────────────
def _run_meta(run_dir: Path) -> dict:
    return regions_mod.read_json(run_dir / "run_meta.json", {}) or {}


def _factors_of(meta: dict) -> dict:
    cond = meta.get("condition") if isinstance(meta.get("condition"), dict) else {}
    f = cond.get("factors") if isinstance(cond.get("factors"), dict) else meta.get("factors")
    keys = ("depth", "format", "channel", "distractors", "fact_present", "probe", "pointer_regime")
    f = f if isinstance(f, dict) else {}
    return {k: f.get(k) for k in keys}


def load_use_detect(run_dir: Path, fact_id: str) -> dict:
    """Normalize use_detect.json into {eligible, fired, fired_in_diff, first_used_seq, ...}.

    Accepts the shapes detect_use.py may reasonably emit: a flat object, a list
    of per-fact records under `facts` (what detect_use.py ACTUALLY writes) or
    under `results`, or a {fact_id: {...}} mapping.

    `facts` is not a hypothetical alias. detect_use.py emits `{"facts": [...]}`
    and this function used to look only for `results`, so every lookup fell
    through to the flat-object branch, found no `eligible`/`fired` at the top
    level, and returned all-None — leaving `used` and `eligible` NULL on every
    row of the headline table. The funnel's third boundary was silently unmeasured.
    """
    raw = regions_mod.read_json(run_dir / "use_detect.json", None)
    if raw is None:
        return {}
    node: Any = raw
    if isinstance(raw, dict):
        listed = next((raw[k] for k in ("facts", "results", "detectors")
                       if isinstance(raw.get(k), list)), None)
        if listed is not None:
            node = next((r for r in listed
                         if isinstance(r, dict) and str(r.get("fact_id")) == fact_id),
                        listed[0] if len(listed) == 1 else {})
        elif fact_id in raw and isinstance(raw[fact_id], dict):
            node = raw[fact_id]
    if not isinstance(node, dict):
        return {}
    detail = node.get("detail") if isinstance(node.get("detail"), dict) else {}
    # detect_use.py names these `used` / `used_in_diff` (matching the fact_trace
    # column they feed); older/other producers name them `fired` / `fired_in_diff`
    # (matching the detector predicate's own return). Accept BOTH. Reading only
    # `fired` left `used` null on every row while the detector was firing correctly
    # — the funnel's third boundary silently unmeasured, which is exactly the class
    # of defect that looks like a finding ("the fact is never used") instead of a bug.
    def _either(*keys, src=node):
        for k in keys:
            if k in src and src[k] is not None:
                return src[k]
        return None

    return {
        "eligible": _as_bool(node.get("eligible")),
        "fired": _as_bool(_either("fired", "used")),
        "fired_in_diff": _as_bool(_either("fired_in_diff", "used_in_diff")
            if _either("fired_in_diff", "used_in_diff") is not None
            else detail.get("fired_in_diff", detail.get("used_in_diff"))
        ),
        "first_used_seq": _as_int(node.get("first_used_seq", detail.get("first_used_seq"))),
        "evidence": node.get("evidence"),
        "detector": node.get("name") or node.get("detector"),
    }


def _as_bool(v: Any) -> bool | None:
    return v if isinstance(v, bool) else (None if v is None else bool(v))


def _evidence_text(v: Any, limit: int = 2000) -> str | None:
    """Detector evidence as one bounded string (it arrives as a list or a str)."""
    if v is None:
        return None
    s = "; ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
    s = s.strip()
    return (s[:limit] or None) if s else None


def _as_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def planted_path_of(card: Any, meta: dict) -> str | None:
    """Where THIS run's fact was actually planted.

    `run_meta.plant` is checked FIRST and is the only per-ARM source: setup_run.sh
    copies it from the arm manifest, so it says `NOTES.md` for d1/d1-ptr and
    `docs/NOTES.md` for d2. A fact card's `source_path` is per-FACT and names only
    the arm the card was authored against, so preferring the card would mis-resolve
    every other arm.

    This lookup previously searched neither `plant` nor anything else that exists:
    FactCard has no path field and run_meta has none at top level, so it returned
    None on every run, `opened` was structurally False, and the reported
    incidental-exposure rate (read - opened) always equalled read — i.e. it claimed
    100% of exposures were accidental even when the agent opened the file directly
    after being told to.
    """
    plant = meta.get("plant") if isinstance(meta.get("plant"), dict) else {}
    condition = meta.get("condition") if isinstance(meta.get("condition"), dict) else {}
    for src in (plant, getattr(card, "__dict__", {}) or {}, meta, condition):
        for key in ("notes_path", "source_path", "planted_path", "fact_path"):
            v = (src or {}).get(key)
            if isinstance(v, str) and v:
                return v
    return None


# ── targeting: which calls could have surfaced the fact file ─────────────────
def _basename(p: str) -> str:
    return os.path.basename(p.rstrip("/")) or p


def targets_fact_file(tool: str | None, tool_input: dict | None, planted: str | None) -> bool:
    """Could this call have delivered the fact file's bytes?

    Read/Write/Edit    the resolved target equals the planted path
    Glob/Grep          the search root is the planted path, an ancestor of it, or
                       unspecified (i.e. the workspace root)
    Bash               the command names the planted path or its basename

    Deliberately inclusive on the search channels: warns that wide
    searches truncate and deep facts are found by wide searches, so a narrow
    reading here would silently score `not_read` where the honest answer is
    `unknown`.
    """
    if not planted:
        return False
    ti = tool_input or {}
    base, tail = _basename(planted), planted.lstrip("./")
    if tool in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        fp = ti.get("file_path") or ti.get("notebook_path") or ""
        return bool(fp) and (fp.endswith(tail) or _basename(fp) == base)
    if tool in ("Glob", "Grep"):
        root = ti.get("path")
        if not root:
            return True  # workspace-wide search
        root = str(root).rstrip("/")
        return tail.startswith(root.lstrip("./") + "/") or root.lstrip("./") in ("", ".", tail)
    if tool == "Bash":
        cmd = str(ti.get("command") or "")
        return tail in cmd or base in cmd
    return False


def opened_fact_file(rows: Sequence[dict], planted: str | None) -> bool:
    """ `opened`: a Read/cat/sed whose resolved target IS the planted path.

    read - opened is the reported incidental-exposure rate: did the agent FIND
    the fact, or stumble into it via grep?
    """
    if not planted:
        return False
    base, tail = _basename(planted), planted.lstrip("./")
    for r in rows:
        if r.get("type") != "tool_use":
            continue
        ti = r.get("tool_input") or {}
        tool = r.get("tool")
        if tool in ("Read", "Edit", "MultiEdit", "NotebookEdit"):
            fp = str(ti.get("file_path") or ti.get("notebook_path") or "")
            if fp and (fp.endswith(tail) or _basename(fp) == base):
                return True
        elif tool == "Bash":
            cmd = str(ti.get("command") or "")
            if (tail in cmd or base in cmd) and any(
                    re.search(rf"(^|[|;&\s]){v}\b", cmd) for v in _OPEN_VERBS):
                return True
    return False


# ── the builder ──────────────────────────────────────────────────────────────
def build_row(run_dir: str | os.PathLike, card: Any, *, exposure_rows: Sequence[dict],
              event_rows: Sequence[dict], probe_rows: Sequence[dict],
              events_summary: dict | None = None) -> tuple[dict, dict]:
    """One fact_trace row plus a diagnostics dict. Raises on the d0-push rule."""
    rd = Path(run_dir)
    meta = _run_meta(rd)
    ident = probes_mod.identity_of(rd)
    esum = events_summary or {}
    fact_id = card.fact_id
    ex = [r for r in exposure_rows if r.get("fact_id") == fact_id]
    planted = planted_path_of(card, meta)
    factors = _factors_of(meta)

    # ── available ──
    available = meta.get("available")
    if available is None:
        available = factors.get("fact_present")
    if available is None:
        available = bool(card.nonce)
    available = bool(available)

    # ── exposure basis ──
    basis = meta.get("exposure_basis")
    if not basis:
        basis = "manifest_canary" if (ident.get("condition_id") == "d0-push"
                                      or factors.get("channel") == "push") else "event_stream"

    inbound = [r for r in ex if r.get("inbound") and r.get("counts_toward_read")]
    thinking = [r for r in ex if r.get("channel") == "self_thinking"]
    echoes = [r for r in ex if r.get("channel") in ECHO_CHANNELS]

    read_inbound_only = bool(inbound)
    thinking_echo = bool(thinking)
    read_primary = read_inbound_only or thinking_echo

    # D4 alarm: a thinking hit with NO PRIOR inbound hit.
    unexplained = False
    if thinking:
        first_think = min(r["seq"] for r in thinking)
        unexplained = not any(r["seq"] < first_think for r in inbound)

    # STRICT: only an EXACT INBOUND hit sets first_exposure_seq.
    first_ex = exposure_mod.first_exposure(ex, fact_id)
    # …but `read` counts ANY hit on a read-counting channel, in any match form, so
    # read=1 with no exact inbound hit is a spec-mandated state (a lower-cased
    # inbound hit, or a D4 thinking-only hit). fact_trace.schema.json's "A
    # MEASURED read has a position" allOf forbids exactly that combination, which
    # would make every unexplained_possession row unrecordable. Resolution: the
    # strict value is preserved verbatim in extra.first_inbound_exact_seq and
    # the emitted position falls back to the first read-counting hit, stamped with
    # extra.first_exposure_rule so no aggregate can confuse the two.
    read_hits = [r for r in ex if r.get("counts_toward_read")]
    first_read_hit = min(read_hits, key=lambda r: (r["seq"], r["region_idx"])) if read_hits else None


    # ── truncation / read_error on a call that could have surfaced the fact ──
    tool_by_id = {r.get("tool_use_id"): r for r in event_rows if r.get("type") == "tool_use"}
    censored = err = False
    for r in event_rows:
        if r.get("type") != "tool_result":
            continue
        src = r.get("truncation_source")
        if not r.get("truncated_by_cli") and src != "read_error_256kb":
            continue
        tu = tool_by_id.get(r.get("tool_use_id")) or {}
        if not targets_fact_file(tu.get("tool") or r.get("tool"), tu.get("tool_input"), planted):
            continue
        if src == "read_error_256kb":
            err = True
        else:
            censored = True

    # ── read / read_status ──
    if basis == "manifest_canary":
        read: bool | None = True
        read_status = "read"
        read_inbound_only = True  # autoload_claude_md is an inbound channel
        exposure_channel: str | None = "autoload_claude_md"
    elif read_primary:
        read, read_status = True, "read"
        exposure_channel = ((first_ex or first_read_hit) or {}).get("channel")
    elif not available:
        # No nonce in the tree: truncation cannot have hidden what is not there.
        read, read_status, exposure_channel = False, "not_read", None
    elif err:
        read, read_status, exposure_channel = None, "read_error", None
    elif censored:
        read, read_status, exposure_channel = None, "unknown", None
    else:
        read, read_status, exposure_channel = False, "not_read", None

    pos = first_ex if first_ex else (first_read_hit if read is True else None)
    exposure_rule = ("exact_inbound" if first_ex
                     else ("fallback_first_read_hit" if pos else None))

    # ── mentions and retention ──
    ment = probes_mod.mention_by_probe(probe_rows)
    observed = sorted(r["probe_idx"] for r in probe_rows if r.get("raw_response") is not None)
    mentions = sorted(i for i in observed if ment.get(i))
    ever_mention = bool(mentions)
    seq_by_probe = {r["probe_idx"]: r.get("answer_seq") for r in probe_rows}

    i0 = mentions[0] if mentions else None
    last_mention = mentions[-1] if mentions else None
    first_mention_seq = seq_by_probe.get(i0) if i0 is not None else None
    p_last = observed[-1] if observed else None

    run_len = 0
    if i0 is not None:
        for i in observed:
            if i < i0:
                continue
            if ment.get(i):
                run_len += 1
            else:
                break

    use = load_use_detect(rd, fact_id)
    first_used_seq = use.get("first_used_seq")
    first_use_probe_index = None
    if first_used_seq is not None:
        after = [r["probe_idx"] for r in probe_rows
                 if r.get("sent_seq") is not None and r["sent_seq"] > first_used_seq]
        first_use_probe_index = min(after) if after else None

    horizon = (p_last - i0) if (i0 is not None and p_last is not None) else None
    window = [i for i in observed
              if i0 is not None and i > i0
              and (first_use_probe_index is None or i < first_use_probe_index)]
    lapse = None
    for i in window:
        if not ment.get(i):
            lapse = i - i0
            break
    if i0 is None:
        censored_ret, reason = True, "never_mentioned"
    elif lapse is not None:
        censored_ret, reason = False, None
    elif first_use_probe_index is not None and any(
            i >= first_use_probe_index for i in observed):
        censored_ret, reason = True, "post_discharge"
    elif _run_completed(rd, meta, esum):
        censored_ret, reason = True, "administrative"
    else:
        censored_ret, reason = True, "truncated_run"

    # ── gates, alarms, analyzability ──
    hygiene = regions_mod.read_json(rd / "hygiene.json", {}) or {}
    judge = regions_mod.read_json(rd / "judge.json", {}) or {}
    coverage = esum.get("join_coverage")
    pacing_max = esum.get("max_tool_uses_per_message")
    parse_rate = (sum(1 for r in probe_rows if r.get("parse_ok")) / len(probe_rows)
                  if probe_rows else None)

    integrity = "ok"
    if any(r.get("parent_tool_use_id") for r in event_rows):
        integrity = "sidechain_barrier"
    elif any(r.get("outcome") == "superseded" for r in probe_rows):
        integrity = "superseded_answers"
    elif parse_rate is not None and parse_rate < PARSE_OK_GATE:
        integrity = "parse_degraded"

    #: for every row with read = 1 AND ever_mention = 1,
    # first_exposure_seq < first_mention_seq. Checked on the EMITTED position, so
    # the invariant covers the thinking-only fallback too.
    ordering_violation = (read is True and ever_mention
        and (pos or {}).get("seq") is not None and first_mention_seq is not None
        and not (pos["seq"] < first_mention_seq)
    )
    confabulation = (not available) and ever_mention
    breaks_control_invariant = (not available) and (
        bool(read) or read_status != "not_read" or read_inbound_only
        or unexplained or ever_mention)

    quarantine_reasons = []
    if unexplained:
        quarantine_reasons.append("unexplained_possession")
    if ordering_violation:
        quarantine_reasons.append("ordering_violation")
    if confabulation:
        quarantine_reasons.append("confabulation")
    if breaks_control_invariant and not confabulation:
        quarantine_reasons.append("control_invariant_broken")
    quarantined = bool(quarantine_reasons)

    exclusion = None
    if factors.get("fact_present") and not available:
        exclusion = "plant_missing"
    elif hygiene and hygiene.get("ok") is False:
        exclusion = "hygiene_violation"
    elif pacing_max is not None and pacing_max > 1:
        exclusion = "pacing_failed"
    elif coverage is not None and coverage < JOIN_COVERAGE_GATE:
        exclusion = "join_coverage_low"
    elif unexplained:
        exclusion = "unexplained_possession"
    elif ordering_violation:
        exclusion = "ordering_violation"
    elif confabulation:
        exclusion = "confabulation"
    elif integrity == "sidechain_barrier":
        exclusion = "probe_integrity"
    analyzable = exclusion is None

    tok = _tokens_of(rd, esum)
    row: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(ident.get("run_id") or rd.name),
        "job_id": ident.get("job_id"),
        "task_id": str(ident.get("task_id") or "unknown"),
        "condition_id": str(ident.get("condition_id") or "unknown"),
        "rep": _as_int(ident.get("rep")),
        "fact_id": fact_id,
        "tier": str(meta.get("tier") or "A"),
        "factors": factors,
        "backend": _dig(meta, "condition", "backend") or meta.get("backend"),
        "model": _dig(meta, "condition", "model") or meta.get("model"),
        "cli_version": _dig(meta, "condition", "cli_version") or meta.get("cli_version"),

        "available": available,
        "read": read,
        "read_status": read_status,
        "read_inbound_only": read_inbound_only,
        "unexplained_possession": unexplained,
        "opened": opened_fact_file(event_rows, planted),
        "read_censored": censored,
        "read_error": err,
        "echoed": bool(echoes),
        "thinking_echo": thinking_echo,

        "used": use.get("fired"),
        "used_in_diff": use.get("fired_in_diff"),
        "eligible": use.get("eligible"),
        # Carry the detector's own evidence onto the row. Without it `used` is a
        # bare boolean at analysis time and a surprising value cannot be audited
        # without re-opening every run's use_detect.json.
        "use_evidence": _evidence_text(use.get("evidence")),
        "use_detector": use.get("detector"),

        "exposure_basis": basis,
        "first_exposure_seq": (pos or {}).get("seq"),
        "first_exposure_bytes_before": (pos or {}).get("bytes_before"),
        "exposure_channel": exposure_channel,
        "first_used_seq": first_used_seq,
        "first_mention_seq": first_mention_seq,

        "ever_mention": ever_mention,
        "first_mention_probe": i0,
        "last_mention_probe": last_mention,
        "mention_run_length": run_len if i0 is not None else None,
        "n_reinjections": len(mentions),
        "first_use_probe_index": first_use_probe_index,
        "n_probes_observed": len(observed),
        "at_risk_horizon": horizon,
        "lapse_probe_index": lapse,
        "retention_censored": censored_ret,
        "censoring_reason": reason,

        "wrong_value_in_slot": _wrong_value_in_slot(probe_rows),
        "slot_precision": _slot_precision(probe_rows),

        "control_fire_rate": _as_float(meta.get("control_fire_rate")),
        "prior_check_status": meta.get("prior_check_status"),
        "success": _success_of(judge),
        "score_automated": _as_float(judge.get("score")),
        # THE DENOMINATOR TRAVELS WITH THE SCORE. `score` is computed over the criteria
        # that were actually GRADED — the right choice, because coercing an unevaluable
        # criterion to False would report a broken battery as a failed solution. But it
        # means a 1.0 over 4 of 7 criteria is indistinguishable, on this row, from a 1.0
        # over 7 of 7, and this row is what the parquet rollup and the notebook read.
        # Measured: a live run carried score_automated=1.0 with 3 of 7 unevaluable.
        "criteria_total": _as_int(judge.get("criteria_total")),
        "criteria_graded": _as_int(judge.get("criteria_graded")),
        "criteria_errored": _as_int(judge.get("criteria_errored")),
        "phi_used_success": None,
        "analyzable": analyzable,
        "exclusion_reason": exclusion,
        "quarantined": quarantined,
        "quarantine_reason": ("+".join(quarantine_reasons) or None),
        "probe_integrity": integrity,

        "tool_calls_total": esum.get("tool_calls_total"),
        "tool_calls_task": esum.get("tool_calls_task"),
        "turns_total": esum.get("turns_total"),
        "n_probes_sent": len(probe_rows),
        "tokens_input": tok.get("input"),
        "tokens_output": tok.get("output"),
        "tokens_cache_read": tok.get("cache_read"),
        "tokens_cache_write": tok.get("cache_write"),
        "tokens_effective": tok.get("effective"),
        "cost_usd": tok.get("cost_usd"),
        "tokens_accounting_version": tok.get("accounting_version"),
        "extra": {
            # The strict quantity, always, regardless of what the emitted
            # first_exposure_seq had to fall back to.
            "first_inbound_exact_seq": (first_ex or {}).get("seq"),
            "first_exposure_rule": exposure_rule,
        },
    }

    # ──: d0-push is asserted, not scanned ──
    if basis == "manifest_canary":
        measured = {k: row[k] for k in ("first_exposure_seq", "first_exposure_bytes_before")
                    if row[k] is not None}
        if measured:
            row["extra"]["measured_exposure_suppressed"] = measured
        row["first_exposure_seq"] = None
        row["first_exposure_bytes_before"] = None
    assert_d0_push_nulls(row)

    diag = {
        "run_id": row["run_id"],
        "fact_id": fact_id,
        "planted_path": planted,
        "ordering_violation": bool(ordering_violation),
        "confabulation": bool(confabulation),
        "unexplained_possession": bool(unexplained),
        "quarantined": quarantined,
        "exclusion_reason": exclusion,
        "read_status": read_status,
        "n_exposure_rows": len(ex),
        "n_inbound": len(inbound),
        "n_thinking": len(thinking),
    }
    return row, diag


def assert_d0_push_nulls(row: dict) -> None:
    """RAISE if a manifest_canary row carries a seq or byte position.

    Auto-loaded CLAUDE.md content appears in neither stream-json, nor the on-disk
    transcript, nor --debug api (S2d), so any seq here would be an artefact of a
    different channel and would silently pollute every seq-based aggregate.
    """
    if row.get("exposure_basis") != "manifest_canary":
        return
    bad = {k: row.get(k) for k in ("first_exposure_seq", "first_exposure_bytes_before")
           if row.get(k) is not None}
    if bad:
        raise D0PushSeqViolation(
            f"d0-push/manifest_canary row {row.get('run_id')} carries non-null "
            f"exposure position(s) {bad}; requires all seq/byte fields null"
    )


def _dig(d: dict, *keys: str) -> Any:
    node: Any = d
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    return node


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# judge.py's ACTUAL verdict vocabulary. This list used to be
# ("pass", "passed", "success", "ok") — which judge.py has never emitted. It writes
# exactly {accepted, partial, rejected, error, timeout}, so the two vocabularies were
# DISJOINT and `success` came out False on every row of every run, including runs the
# battery had certified `accepted`. That is a structural constant, not a measurement:
# the orthogonality gate |phi(used, success)| has zero variance in one variable and
# cannot be computed, and any "did the context help completion?" analysis reads zero.
# Verified against a real accepted run before this was changed.
_VERDICT_SUCCESS = {
    "accepted": True,       # every criterion met, none unevaluable
    "rejected": False,      # every graded criterion failed, none unevaluable
    "timeout": False,       # the agent ran out of wall clock; it did not complete
    "error": None,          # no criteria, or the battery could not run — not a fact
    #                         about the solution
    # "partial" is decided below: it means EITHER a genuine partial result OR a
    # battery that half-broke, and those are not the same claim.
}
# Foreign graders (not judge.py) may write these instead; keep accepting them so a
# hand-authored or external judge.json still resolves.
_FOREIGN_SUCCESS = {"pass": True, "passed": True, "success": True, "ok": True,
                    "fail": False, "failed": False}


def _success_of(judge: dict) -> bool | None:
    """The mechanical verdict as a TRI-STATE, matching `passed`/`met` elsewhere.

    None means "not established", never "no". A `partial` verdict with unevaluable
    criteria is exactly that: the battery could not decide, so neither can this. It
    is left null rather than being folded into False, because folding it in would
    report a broken grader as a failed solution — the failure mode this project has
    been punished for twice.
    """
    if not judge:
        return None
    v = judge.get("verdict")
    if isinstance(v, str):
        key = v.strip().lower()
        if key in _VERDICT_SUCCESS:
            return _VERDICT_SUCCESS[key]
        if key in _FOREIGN_SUCCESS:
            return _FOREIGN_SUCCESS[key]
        if key == "partial":
            errored = judge.get("criteria_errored")
            if isinstance(errored, int) and errored > 0:
                return None      # undecidable: the battery did not finish deciding
            return False         # a complete measurement that fell short
        return None              # an unknown verdict is not a verdict
    return _as_bool(judge.get("success"))


def _run_completed(run_dir: Path, meta: dict, esum: dict) -> bool:
    """Did the run reach a well-formed terminal `result`? (censoring disposition)"""
    if esum.get("result_event_seen"):
        return True
    rr = regions_mod.read_json(run_dir / "run_record.json", {}) or {}
    return str(_dig(rr, "outcome", "terminal_state") or meta.get("terminal_state") or "") \
        in ("pass", "fail", "completed", "success")


def _tokens_of(run_dir: Path, esum: dict) -> dict:
    rr = regions_mod.read_json(run_dir / "run_record.json", {}) or {}
    t = rr.get("tokens") if isinstance(rr.get("tokens"), dict) else {}
    et = esum.get("tokens") if isinstance(esum.get("tokens"), dict) else {}
    return {
        "input": t.get("total_input", et.get("result_input") or et.get("deduped_input")),
        "output": t.get("total_output", et.get("result_output") or et.get("deduped_output")),
        "cache_read": t.get("cache_read"),
        "cache_write": t.get("cache_write"),
        "effective": t.get("total_effective"),
        "cost_usd": t.get("cost_usd", et.get("cost_usd")),
        "accounting_version": t.get("accounting_version", et.get("accounting_version")),
    }


def _wrong_value_in_slot(probe_rows: Sequence[dict]) -> bool | None:
    seen = False
    for r in probe_rows:
        for s in r.get("slots") or []:
            if s.get("wrong_value") is True:
                return True
            if s.get("wrong_value") is False:
                seen = True
    return False if seen else None


def _slot_precision(probe_rows: Sequence[dict]) -> float | None:
    """critical_fact slots / non-empty slots, over every parsed probe."""
    crit = total = 0
    for r in probe_rows:
        for s in r.get("slots") or []:
            if s.get("slot_class") == "empty":
                continue
            total += 1
            if s.get("slot_class") == "critical_fact":
                crit += 1
    return round(crit / total, 6) if total else None


# ── entry point ──────────────────────────────────────────────────────────────
def fact_task_map(source: Any) -> dict[str, str]:
    """`fact_id -> task_id` straight off the registry, or `{}` if it cannot be read.

    protocol.FactCard deliberately carries only what MATCHING needs (nonce,
    surface forms, regexes), so the card alone cannot say which task a fact
    belongs to. This reads the registry a second time for that one column.
    """
    data: Any = source
    try:
        if isinstance(source, (str, os.PathLike)):
            p = Path(source)
            text = p.read_text(encoding="utf-8")
            if p.suffix.lower() in (".yaml", ".yml"):
                import yaml  # type: ignore
                data = yaml.safe_load(text)
            else:
                data = json.loads(text)
        if isinstance(data, Mapping) and "facts" in data:
            data = data["facts"]
        out: dict[str, str] = {}
        if isinstance(data, Mapping):
            for fid, body in data.items():
                tid = (body or {}).get("task_id") if isinstance(body, Mapping) else None
                if tid:
                    out[str(fid)] = str(tid)
        elif isinstance(data, list):
            for body in data:
                if not isinstance(body, Mapping):
                    continue
                fid, tid = body.get("fact_id") or body.get("id"), body.get("task_id")
                if fid and tid:
                    out[str(fid)] = str(tid)
        return out
    except Exception:
        return {}


def select_cards(cards: Sequence[Any], task_id: str | None, fact_to_task: Mapping[str, str]):
    """The facts this run is measuring: D1 says exactly the one owned by its task.

    A registry spans every task in the job. Tracing all of them per run was
    measurably wrong in three compounding ways: the other tasks' facts are NOT
    planted in this workspace, yet's `available` was reported true for them;
    `fact_trace` grew to one row per (run x REGISTRY fact) instead of D1's one row
    per run, which is the independence assumption the strata and the bootstrap
    resampling unit both rest on; and those extra never-planted rows land in the
    read-rate DENOMINATOR, deflating it by exactly the registry size.

    Falls back to every card when the task is unknown or the registry declares no
    task for any fact — a single-fact registry keeps working untouched.
    """
    if not task_id or not fact_to_task:
        return list(cards)
    mine = [c for c in cards if fact_to_task.get(getattr(c, "fact_id", "")) == task_id]
    return mine if mine else list(cards)




def run(run_dir: str | os.PathLike, facts: Any = None, out_path: str | os.PathLike | None = None,
        exposure_rows: Sequence[dict] | None = None, event_rows: Sequence[dict] | None = None,
        probe_rows: Sequence[dict] | None = None,
        events_summary: dict | None = None) -> tuple[list[dict], dict]:
    rd = Path(run_dir)
    src = facts if facts is not None else exposure_mod.find_facts_file(rd)
    cards = exposure_mod.load_fact_cards(src) if src is not None else []
    # D1: one fact per task, so one fact_trace row per run. exposure.jsonl keeps
    # scanning EVERY nonce in the registry on purpose — a cross-task nonce showing
    # up in this workspace is a plant leak, and dropping it here would hide it.
    task_id = str(probes_mod.identity_of(rd).get("task_id") or "") or None
    cards = select_cards(cards, task_id, fact_task_map(src) if src is not None else {})
    ex = list(exposure_rows) if exposure_rows is not None else regions_mod.read_jsonl(rd / "exposure.jsonl")
    ev = list(event_rows) if event_rows is not None else regions_mod.read_jsonl(rd / "events.jsonl")
    pr = list(probe_rows) if probe_rows is not None else regions_mod.read_jsonl(rd / "probes.jsonl")


    rows: list[dict] = []
    diags: list[dict] = []
    for card in cards:
        row, diag = build_row(rd, card, exposure_rows=ex, event_rows=ev, probe_rows=pr,
                              events_summary=events_summary)
        rows.append(row)
        diags.append(diag)
    target = Path(out_path) if out_path else rd / "fact_trace.jsonl"
    regions_mod.write_jsonl_atomic(target, rows)
    summary = {
        "run_id": str(probes_mod.identity_of(rd).get("run_id") or rd.name),
        "n_rows": len(rows),
        "ordering_violations": sum(1 for d in diags if d["ordering_violation"]),
        "confabulations": sum(1 for d in diags if d["confabulation"]),
        "unexplained_possession": sum(1 for d in diags if d["unexplained_possession"]),
        "quarantined": sum(1 for d in diags if d["quarantined"]),
        "analyzable": sum(1 for r in rows if r["analyzable"]),
        "read_status": {s: sum(1 for r in rows if r["read_status"] == s)
                        for s in ("read", "not_read", "unknown", "read_error")},
        "diagnostics": diags,
        "out": str(target),
    }
    return rows, summary


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="the fact_trace headline table")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--facts", default=None)
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)
    _rows, summary = run(a.run_dir, a.facts, a.out)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if (summary["quarantined"] or summary["ordering_violations"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
