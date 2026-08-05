#!/usr/bin/env python3
"""
probes.py — probe answers -> probes.jsonl.

RESPONSIBILITY
  Turn every realized probe of a run into one row: what was injected, when, what
  came back verbatim, how it parsed, what each of the three slots is, and — the
  part no other module does — whether the `source` the model CLAIMED for a slot
  is a place it had actually looked by that point in the run.

  raw_response is ALWAYS retained verbatim. Every classifier above this line is
  a heuristic pending D7 (200 hand-labelled pilot slots); keeping the raw text
  means a re-classification never costs a new run.

INPUTS
  $RUN_DIR/probe_plan.json     the seeded cadence, written BEFORE the child
                               started, so intended-vs-realized is diffable even
                               when the run died.
  events.EventsResult          probe_turns (probe_in -> probe_out, lifted out of
                               the raw stream once, in events.py) and the event
                               rows, for barrier attribution and the tool that
                               was ACTUALLY called next.
  fact cards                   for the tier (a)/(b) mention ladder (§4.5).

OUTPUTS
  $RUN_DIR/probes.jsonl        rows validating against schemas/probes.schema.json

ATTRIBUTION IS BY ECHOED probe_id, NEVER BY POSITION
  probe_id = "WURP-" + sha256(run_id)[:8] + "-" + k:03d cannot occur in a repo
  and is echoed back inside the answer, so a superseded or late answer attaches
  to the probe that actually asked for it. `probe_idx` is read off the id's own
  ordinal, not off the order answers happened to arrive in.

OUTCOME LADDER (§6.2, schemas/probes.schema.json)
  answered   a reply arrived for this probe_id — even if the prose grumbles, as
             long as it parsed.
  refused    the reply reads as a refusal (protocol.looks_like_refusal). The
             pilot gate is refused == 0: a refusal means the trusted stream-json
             user channel broke (V1/V2).
  superseded probe k+1 fired while k was still pending. NEVER suppress a probe.
  unanswered sent, and the run ended first.

CLI
  python3 lib/wur/probes.py --run-dir DIR [--facts FILE] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Sequence

try:
    from . import events as events_mod, exposure as exposure_mod, protocol, regions as regions_mod
except ImportError:  # flat context
    import events as events_mod  # type: ignore
    import exposure as exposure_mod  # type: ignore
    import protocol  # type: ignore
    import regions as regions_mod  # type: ignore

SCHEMA_VERSION = "1"

#: A source string naming the task itself rather than a file on disk. These are
#: legitimate sources — the probe explicitly offers "task prompt" as one.
_PROMPT_SOURCE_RE = re.compile(
    r"\b(task|user|system)\s*(prompt|message|instruction|request)\b|\bprompt\b|"
    r"\bthe task\b|\buser'?s? request\b", re.IGNORECASE)
_TOOL_SOURCE_RE = re.compile(r"\b(bash|read|write|edit|glob|grep|tool call|tool result|"
                             r"command output|stdout)\b", re.IGNORECASE)
_PATHY_RE = re.compile(r"[\w.@-]*(?:/[\w.@-]+)+|[\w.-]+\.(?:md|py|txt|json|ya?ml|toml|cfg|ini|sh|rst)")


# ── probe plan ───────────────────────────────────────────────────────────────
def load_probe_plan(run_dir: str | os.PathLike) -> dict:
    plan = regions_mod.read_json(Path(run_dir) / "probe_plan.json", {}) or {}
    if isinstance(plan, dict) and isinstance(plan.get("plan"), dict):
        plan = {**plan, **plan["plan"]}
    return plan if isinstance(plan, dict) else {}


def probe_idx_of(probe_id: str) -> int | None:
    """The ordinal encoded in the id itself — attribution without position."""
    m = re.fullmatch(r"WURP-[0-9a-f]{8}-(\d{3})", probe_id or "")
    return int(m.group(1)) if m else None


# ── what the model had actually looked at, by a given seq ────────────────────
def opened_paths(rows: Sequence[dict], upto_seq: int | None = None) -> set[str]:
    """Every path the run had actually touched at or before `upto_seq`.

    Read/Write/Edit -> file_path; Glob/Grep -> path + pattern; Bash -> every
    path-shaped token of the command. This is the ground truth a claimed
    `source` is checked against.
    """
    out: set[str] = set()
    for r in rows:
        if r.get("type") != "tool_use":
            continue
        if upto_seq is not None and r.get("seq", 0) > upto_seq:
            continue
        ti = r.get("tool_input") or {}
        tool = (r.get("tool") or "")
        for key in ("file_path", "notebook_path", "path"):
            v = ti.get(key)
            if isinstance(v, str) and v:
                out.add(v)
        if tool == "Bash" and isinstance(ti.get("command"), str):
            for m in _PATHY_RE.finditer(ti["command"]):
                out.add(m.group(0))
        if tool in ("Glob", "Grep"):
            for key in ("pattern", "glob"):
                v = ti.get(key)
                if isinstance(v, str) and "/" in v:
                    out.add(v)
    return {p for p in out if p}


def tools_used(rows: Sequence[dict], upto_seq: int | None = None) -> set[str]:
    return {str(r.get("tool")) for r in rows
            if r.get("type") == "tool_use" and r.get("tool")
            and (upto_seq is None or r.get("seq", 0) <= upto_seq)}


def verify_source(source: str, paths: set[str], tools: set[str]) -> tuple[bool, str]:
    """(verified, source_class) for one claimed slot source.

    source_class ∈ {empty, path_opened, path_unopened, tool_used, tool_unused,
                    task_prompt, unresolved}. A claimed file path that the run
                    never opened is the interesting failure — the model citing a
                    provenance it does not have.
    """
    src = (source or "").strip().strip("`'\"")
    if not src or src.strip(".").lower() in ("none", "n/a", "na", "null", "-", ""):
        return False, "empty"
    if _PROMPT_SOURCE_RE.search(src):
        return True, "task_prompt"
    cands = [m.group(0) for m in _PATHY_RE.finditer(src)] or ([src] if "/" in src or "." in src else [])
    if cands:
        for c in cands:
            tail = c.lstrip("./")
            if any(p == c or p == tail or p.endswith("/" + tail) or p.endswith(tail)
                   for p in paths):
                return True, "path_opened"
        return False, "path_unopened"
    m = _TOOL_SOURCE_RE.search(src)
    if m:
        name = m.group(1).capitalize()
        return (name in tools), ("tool_used" if name in tools else "tool_unused")
    return False, "unresolved"


# ── probe fidelity ───────────────────────────────────────────────────────────
def _next_tool_after(rows: Sequence[dict], seq: int | None) -> dict | None:
    if seq is None:
        return None
    for r in rows:
        if r.get("type") == "tool_use" and r.get("seq", -1) > seq and not r.get("is_probe_turn"):
            return r
    return None


def fidelity(next_action: str | None, next_row: dict | None) -> bool | None:
    """Did the stated next move match the tool actually issued next?

    HEURISTIC: agreement is the tool NAME appearing in the stated next action, or
    the tool's primary path appearing in it. Null when no further tool call
    happened, because "no next call" is not a disagreement.
    """
    if next_row is None or not next_action:
        return None
    text = next_action.lower()
    tool = (next_row.get("tool") or "").lower()
    if tool and tool in text:
        return True
    ti = next_row.get("tool_input") or {}
    for key in ("file_path", "path", "pattern", "command"):
        v = ti.get(key)
        if isinstance(v, str) and v:
            base = os.path.basename(v.split()[0]) if key == "command" else os.path.basename(v)
            if base and len(base) > 2 and base.lower() in text:
                return True
    return False


# ── the builder ──────────────────────────────────────────────────────────────
def build(turns: Sequence[Any], rows: Sequence[dict], plan: dict,
          cards: Sequence[protocol.FactCard], run_id: str,
          identity: dict | None = None, task_text: str = "") -> list[dict]:
    ident = identity or {}
    card = cards[0] if cards else None
    fire_at = plan.get("fire_at") or []
    intervals = plan.get("intervals") or []

    # group realized turns by probe_id: first `probe`, then any `retry`
    by_pid: dict[str, list[Any]] = {}
    for t in turns:
        by_pid.setdefault(t.probe_id, []).append(t)

    sent_order = sorted({(t.sent_seq, t.probe_id) for t in turns})
    out: list[dict] = []
    for pid, group in sorted(by_pid.items(), key=lambda kv: min(t.sent_seq for t in kv[1])):
        first = min(group, key=lambda t: t.sent_seq)
        retry = next((t for t in group if t.injection_kind == "retry"), None)
        answered_turn = next((t for t in sorted(group, key=lambda t: t.sent_seq)
                              if t.raw_response is not None), None)
        k = probe_idx_of(pid)
        if k is None:
            k = sorted(by_pid).index(pid)

        raw = answered_turn.raw_response if answered_turn else None
        parsed = protocol.parse_answer(raw or "", expect_probe_id=pid or None)
        markers = protocol.refusal_markers(raw or "") if raw else []

        later_sent = any(s > first.sent_seq for s, _ in sent_order)
        if raw is None:
            outcome = "superseded" if later_sent else "unanswered"
        elif markers and not parsed.parse_ok:
            outcome = "refused"
        else:
            outcome = "answered"

        answer_seq = answered_turn.answer_seq if answered_turn else None
        paths = opened_paths(rows, first.sent_seq)
        tools = tools_used(rows, first.sent_seq)
        slots = protocol.annotate_slots(parsed.slots, card, task_text=task_text,
                                        probe_id_text=pid, known_sources=paths)
        audit: list[dict] = []
        for s in slots:
            ok, cls = verify_source(s.get("source", ""), paths, tools)
            s["source_verified"] = ok
            s["wrong_value"] = _wrong_value(s, card)
            audit.append({"slot_idx": s["slot_idx"], "source_class": cls,
                          "source": s.get("source", "")[:200]})

        nxt = _next_tool_after(rows, answer_seq if answer_seq is not None else first.sent_seq)
        seqs = sorted({s for t in group for s in t.seqs})

        # The two id lists lib/extract/core.py needs to compute `tool_calls_task`
        # (§4.4's difficulty-band metric = tool calls EXCLUDING probe turns).
        # Without them core.py falls back to tool_calls_task == tool_calls_total,
        # which is silently wrong on every probed run rather than absent.
        seqset = set(seqs)
        turn_message_ids = sorted({
            r["message_id"] for r in rows
            if r.get("message_id") and r.get("seq") in seqset
        })
        tool_use_ids = sorted({
            r["tool_use_id"] for r in rows
            if r.get("tool_use_id") and r.get("seq") in seqset
        })

        row = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "job_id": ident.get("job_id"),
            "task_id": ident.get("task_id"),
            "condition_id": ident.get("condition_id"),
            "rep": ident.get("rep"),
            "probe_idx": k,
            "probe_id": pid,
            "echoed_probe_id": parsed.probe_id,
            "probe_id_match": (None if parsed.probe_id is None else parsed.probe_id == pid),
            "sent_at_barrier": first.sent_at_barrier,
            "planned_at_barrier": fire_at[k] if 0 <= k < len(fire_at) else None,
            "sampled_interval": intervals[k] if 0 <= k < len(intervals) else None,
            "sent_seq": first.sent_seq,
            "answer_seq": answer_seq,
            "sent_ts": first.sent_ts,
            "answered_ts": answered_turn.answered_ts if answered_turn else None,
            "raw_response": raw,
            "parse_ok": bool(parsed.parse_ok),
            "parse_tier": parsed.parse_tier,
            "parse_errors": list(parsed.errors),
            "retry_sent": retry is not None,
            "answered_after_retry": bool(retry is not None and answered_turn is retry),
            "slots": slots if parsed.parse_ok else [],
            "next_action": parsed.next_action,
            "next_action_tool": (nxt or {}).get("tool"),
            "fidelity_agree": fidelity(parsed.next_action, nxt),
            "outcome": outcome,
            "refusal_markers": markers,
            "is_probe_turn_seq": seqs,
            "turn_message_ids": turn_message_ids,
            "tool_use_ids": tool_use_ids,
            "kind": "probe",
            "tokens_out": (answered_turn.tokens_out if answered_turn else None),
            "extra": {"source_audit": audit,
                      "n_turns_for_probe": len(group),
                      "injection_kinds": sorted({t.injection_kind for t in group})},
        }
        out.append(row)
    out.sort(key=lambda r: (r["probe_idx"], r["sent_seq"] or 0))
    return out


def _wrong_value(slot: dict, card: protocol.FactCard | None) -> bool | None:
    """d2-dist only: a distractor's token landed in this slot."""
    if card is None or not card.distractor_tokens:
        return None
    blob = f"{slot.get('fact', '')}\n{slot.get('source', '')}".lower()
    return any(str(d).lower() in blob for d in card.distractor_tokens if d)


def mention_by_probe(rows: Sequence[dict]) -> dict[int, bool]:
    """probe_idx -> did ANY slot match the fact (§4.5 primary = tier a OR b)."""
    out: dict[int, bool] = {}
    for r in rows:
        hit = any(bool(s.get("match_nonce")) or bool(s.get("match_regex"))
                  for s in (r.get("slots") or []))
        out[r["probe_idx"]] = hit
    return out


# ── entry points ─────────────────────────────────────────────────────────────
def task_text_of(regionset: Any) -> str:
    """The task prompt as the model received it (harness_task_prompt regions)."""
    return "\n".join(r.text for r in regionset.regions
                     if r.channel == "harness_task_prompt")[:20000]


def identity_of(run_dir: str | os.PathLike) -> dict:
    meta = regions_mod.read_json(Path(run_dir) / "run_meta.json", {}) or {}
    run = meta.get("run") if isinstance(meta.get("run"), dict) else {}
    cond = meta.get("condition") if isinstance(meta.get("condition"), dict) else {}
    def pick(*keys, default=None):
        for k in keys:
            for src in (meta, run, cond):
                if isinstance(src, dict) and src.get(k) not in (None, ""):
                    return src[k]
        return default
    return {
        "run_id": pick("run_id", default=Path(run_dir).name),
        "job_id": pick("job_id"),
        "task_id": pick("task_id", "task"),
        "condition_id": pick("condition_id", "condition", "arm", "env_id"),
        "rep": pick("rep", "replicate"),
    }


def run(run_dir: str | os.PathLike, facts: Any = None, out_path: str | os.PathLike | None = None,
        events_result: Any = None) -> tuple[list[dict], dict]:
    rd = Path(run_dir)
    ident = identity_of(rd)
    rid = str(ident.get("run_id") or rd.name)
    res = events_result if events_result is not None else events_mod.run(rd, facts, run_id=rid)
    src = facts if facts is not None else exposure_mod.find_facts_file(rd)
    cards = exposure_mod.load_fact_cards(src) if src is not None else []
    rows = build(res.probe_turns, res.rows, load_probe_plan(rd), cards, rid, ident,
                 task_text=task_text_of(res.regionset) if res.regionset else "")
    target = Path(out_path) if out_path else rd / "probes.jsonl"
    regions_mod.write_jsonl_atomic(target, rows)
    n_parsed = sum(1 for r in rows if r["parse_ok"])
    summary = {
        "run_id": rid,
        "n_probes": len(rows),
        "n_answered": sum(1 for r in rows if r["outcome"] == "answered"),
        "n_refused": sum(1 for r in rows if r["outcome"] == "refused"),
        "n_superseded": sum(1 for r in rows if r["outcome"] == "superseded"),
        "n_unanswered": sum(1 for r in rows if r["outcome"] == "unanswered"),
        "parse_ok_rate": round(n_parsed / len(rows), 6) if rows else None,
        "n_strict": sum(1 for r in rows if r["parse_tier"] == "strict"),
        "n_lenient": sum(1 for r in rows if r["parse_tier"] == "lenient"),
        "n_mentions": sum(1 for v in mention_by_probe(rows).values() if v),
        "out": str(target),
    }
    return rows, summary


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="probe answers -> probes.jsonl (§4.3)")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--facts", default=None)
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)
    _rows, summary = run(a.run_dir, a.facts, a.out)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if summary["n_refused"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
