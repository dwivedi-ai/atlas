#!/usr/bin/env python3
"""
events.py — the stream x gate join -> events.jsonl.

RESPONSIBILITY
  Produce one row per event of a run, joining the model's own record of a tool
  call (stream.jsonl) with the harness's authoritative record of the barrier
  that let it through (gate/tool_calls.jsonl), on `tool_use_id`. Report
  join_coverage; >= 0.99 is a pilot gate. Tag `is_probe_turn` — the field
  that keeps the instrument out of the measurement.

INPUTS
  regions.RegionSet    the canonical ordered records and their model-visible
                       regions; `seq` is assigned there and is authoritative.
  exposure rows        already scanned, so `nonce_hits` can be attached and the
                       tool result can THEN be digested (scan-before-truncate).
  gate/tool_calls.jsonl  {ts, barrier, tool_use_id, tool_name, tool_input}

OUTPUTS
  $RUN_DIR/events.jsonl   rows validating against schemas/events.schema.json
  EventsResult.summary    join_coverage, the token dedupe reconciliation, and
                          the per-run operations counters the gates read.
  EventsResult.probe_turns  the hand-off probes.py consumes, so the answer text
                          is extracted once, here, from the raw stream.

THREE MEASURED RULES THIS MODULE OBEYS
  V7/V17  Count by `message.id`, never by stream line. stream.jsonl splits one
          assistant message across lines exactly like the on-disk transcript, so
          a per-line count inflates tokens 1.0x-4.9x and message counts up to
          13.67x. Usage is carried on the FIRST row of a message_id group and is
          null on the rest; the deduped total is asserted against the terminal
          `result` event and the delta recorded.
  V14     A denied tool call costs TWO barrier fires, so gate ordinals are not
          tool-call ordinals: gate/tool_calls.jsonl is de-duplicated by
          tool_use_id and the fire count kept in `extra`.
  V13/  The probe is replayed as a `user` text block immediately AFTER the
          barriered call's tool_result. `is_probe_turn` therefore keys off the
          replayed probe_id TEXT, never off position.

CLI
  python3 lib/wur/events.py --run-dir DIR [--facts FILE] [--out PATH]
    exits 1 when join_coverage < 0.99.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

try:
    from . import exposure as exposure_mod, protocol, regions as regions_mod
except ImportError:  # flat context
    import exposure as exposure_mod  # type: ignore
    import protocol  # type: ignore
    import regions as regions_mod  # type: ignore

SCHEMA_VERSION = "1"
JOIN_COVERAGE_GATE = 0.99
TOKENS_ACCOUNTING_VERSION = "per_message_v2"


@dataclass
class ProbeTurn:
    """One realized probe: what was injected, and the reply that came back."""

    probe_id: str
    injection_kind: str  # probe | retry
    sent_seq: int
    sent_ts: str | None
    sent_at_barrier: int | None
    answer_seq: int | None = None
    answered_ts: str | None = None
    raw_response: str | None = None
    answer_message_id: str | None = None
    tokens_out: int | None = None
    seqs: list[int] = field(default_factory=list)


@dataclass
class EventsResult:
    rows: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    probe_turns: list[ProbeTurn] = field(default_factory=list)
    regionset: Any = None


# ── gate/tool_calls.jsonl ────────────────────────────────────────────────────
def load_gate(run_dir: str | os.PathLike) -> dict[str, dict]:
    """tool_use_id -> {barrier, fires, ts, tool, tool_input}, de-duplicated.

    V14: the model re-issues a DENIED tool call and it then succeeds, so the same
    tool_use_id can fire the barrier twice. The first barrier ordinal is kept;
    the fire count is preserved so nothing is lost.
    """
    path = Path(run_dir) / "gate" / "tool_calls.jsonl"
    out: dict[str, dict] = {}
    for row in regions_mod.read_jsonl(path):
        tid = row.get("tool_use_id") or row.get("toolUseId")
        if not tid:
            continue
        tid = str(tid)
        rec = out.get(tid)
        if rec is None:
            out[tid] = {
                "barrier": row.get("barrier"),
                "fires": 1,
                "ts": row.get("ts"),
                "tool": row.get("tool_name") or row.get("tool"),
                "tool_input": row.get("tool_input") if isinstance(row.get("tool_input"), dict) else None,
                "parent_tool_use_id": row.get("parent_tool_use_id"),
                "decision": row.get("decision"),
            }
        else:
            rec["fires"] += 1
            if rec.get("decision") in (None, "allow") and row.get("decision"):
                rec["decision"] = row.get("decision")
    return out


# ── token accounting ────────────────────────────────────────────────────
def _usage_totals(usage: Any) -> tuple[int, int]:
    """(input, output) where input FOLDS CACHE BACK IN.

    Measured identity (V7, S3 run0): result.usage 6 + 46,220 + 7,066 = 53,292 ==
    the message.id-deduped stream total. That equality is asserted per run.
    """
    if not isinstance(usage, dict):
        return 0, 0
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
    )


# ── the builder ──────────────────────────────────────────────────────────────
def build(regionset: Any, gate: dict[str, dict], exposure_rows: Sequence[dict],
          run_id: str) -> EventsResult:
    records = regionset.records
    hits = _hits_by_block(regionset, exposure_rows)
    trunc = _truncation_index(regionset)
    first_line_of_msg = _first_line_of_message(records)

    rows: list[dict] = []
    texts: list[str] = []          # parallel to rows; never written to disk
    seen_msg: set[str] = set()
    turn_idx = -1
    stream_tool_ids: set[str] = set()
    tool_uses_per_message: dict[str, int] = {}
    deduped_in = deduped_out = 0
    result_in = result_out = 0
    cost_usd = None
    permission_denials = 0
    result_seen = False

    def emit(rec, typ: str, *, text: str = "", **kw) -> dict:
        row = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "seq": rec.seq,
            "type": typ,
            "source": rec.source,
            "ts": rec.ts,
            "turn_idx": turn_idx if turn_idx >= 0 else None,
            "message_id": rec.message_id,
            "parent_tool_use_id": rec.parent_tool_use_id,
            "is_probe_turn": False,
            "nonce_hits": [],
        }
        row.update({k: v for k, v in kw.items() if v is not None or k in ("tool_input",)})
        rows.append(row)
        texts.append(text)
        return row

    for rec in records:
        if rec.kind == "assistant" and rec.message_id and rec.message_id not in seen_msg:
            seen_msg.add(rec.message_id)
            turn_idx += 1

        if rec.kind == "system_init":
            emit(rec, "system_init",
                 extra={"init_keys": sorted(k for k in rec.obj if k not in ("type", "subtype"))})
            continue

        if rec.kind == "hook":
            emit(rec, "hook", extra={"subtype": rec.obj.get("subtype")})
            continue

        if rec.kind == "result":
            result_seen = True
            usage = rec.obj.get("usage")
            result_in, result_out = _usage_totals(usage)
            cost_usd = rec.obj.get("total_cost_usd")
            denials = rec.obj.get("permission_denials")
            permission_denials = len(denials) if isinstance(denials, list) else 0
            emit(rec, "result", extra={
                "subtype": rec.obj.get("subtype"),
                "is_error": bool(rec.obj.get("is_error")),
                "num_turns": rec.obj.get("num_turns"),
                "result_input_tokens": result_in,
                "result_output_tokens": result_out,
                "total_cost_usd": cost_usd,
                "permission_denials": permission_denials,
            })
            continue

        if rec.kind == "assistant":
            msg = rec.obj.get("message") if isinstance(rec.obj.get("message"), dict) else {}
            first_of_msg = (rec.message_id is not None
                            and first_line_of_msg.get(rec.message_id) == rec.seq)
            t_in, t_out = _usage_totals(msg.get("usage")) if first_of_msg else (None, None)
            if first_of_msg:
                deduped_in += t_in or 0
                deduped_out += t_out or 0
            carried = False
            for i, blk in enumerate(regions_mod.blocks_of(msg)):
                btype = blk.get("type")
                tok = {} if carried or not first_of_msg else {"tokens_in": t_in, "tokens_out": t_out}
                if tok:
                    carried = True
                if btype == "tool_use":
                    tid = str(blk.get("id")) if blk.get("id") else None
                    if tid:
                        stream_tool_ids.add(tid)
                    if rec.message_id:
                        tool_uses_per_message[rec.message_id] = \
                            tool_uses_per_message.get(rec.message_id, 0) + 1
                    g = gate.get(tid or "", {})
                    emit(rec, "tool_use",
                         tool=blk.get("name"),
                         tool_input=blk.get("input") if isinstance(blk.get("input"), dict) else {},
                         tool_use_id=tid,
                         barrier=g.get("barrier"),
                         join_status="joined" if tid and tid in gate else "stream_only",
                         nonce_hits=hits.get((rec.seq, i), []),
                         extra=({"barrier_fires": g.get("fires")} if g else None),
                         **tok)
                elif btype in ("text", "thinking", "redacted_thinking"):
                    body = blk.get("text") if btype == "text" else (blk.get("thinking") or "")
                    emit(rec, "assistant", text=body or "",
                         nonce_hits=hits.get((rec.seq, i), []),
                         extra={"block": btype}, **tok)
                else:
                    emit(rec, "assistant", nonce_hits=hits.get((rec.seq, i), []),
                         extra={"block": str(btype)}, **tok)
            continue

        if rec.kind == "user":
            for i, blk in enumerate(regions_mod.blocks_of(rec.obj.get("message"))):
                btype = blk.get("type")
                if btype == "tool_result":
                    tid = blk.get("tool_use_id") or blk.get("toolUseId")
                    tid = str(tid) if tid else None
                    body = regions_mod.result_text(blk.get("content"))
                    tinfo = trunc.get(tid or "")
                    g = gate.get(tid or "", {})
                    emit(rec, "tool_result",
                         tool=g.get("tool"),
                         tool_use_id=tid,
                         barrier=g.get("barrier"),
                         is_error=bool(blk.get("is_error") or blk.get("isError")),
                         # digest computed AFTER the nonce scan
                         result_digest=protocol.sha256(body),
                         result_bytes=len(body.encode("utf-8", "replace")),
                         nonce_hits=hits.get((rec.seq, i), []),
                         truncated_by_cli=bool(tinfo),
                         truncation_source=(tinfo or {}).get("trigger"))
                elif btype == "text":
                    txt = blk.get("text") or ""
                    ch, kind, pid, sha_ok = regions_mod.classify_harness_text(txt)
                    typ = "probe_in" if kind in ("probe", "retry") else "harness_in"
                    emit(rec, typ, text=txt,
                         probe_id=pid,
                         injection_kind=kind,
                         nonce_hits=hits.get((rec.seq, i), []),
                         extra={"channel": ch, "sha_matched": sha_ok,
                                "is_replay": rec.is_replay})
                else:
                    emit(rec, "harness_in", nonce_hits=hits.get((rec.seq, i), []),
                         extra={"block": str(btype)})
            continue

    # gate rows the stream never showed
    max_seq = max((r["seq"] for r in rows), default=-1)
    gate_only = [tid for tid in gate if tid not in stream_tool_ids]
    for n, tid in enumerate(sorted(gate_only), start=1):
        g = gate[tid]
        rows.append({
            "schema_version": SCHEMA_VERSION, "run_id": run_id, "seq": max_seq + n,
            "type": "tool_use", "source": "gate", "ts": g.get("ts"), "turn_idx": None,
            "message_id": None, "tool_use_id": tid, "tool": g.get("tool"),
            "tool_input": g.get("tool_input"), "barrier": g.get("barrier"),
            "join_status": "gate_only", "is_probe_turn": False, "nonce_hits": [],
            "extra": {"barrier_fires": g.get("fires"), "synthetic_seq": True},
        })
        texts.append("")

    probe_turns = _tag_probe_turns(rows, texts, gate)
    _attach_probe_tokens(rows, probe_turns)

    n_joined = sum(1 for r in rows if r.get("join_status") == "joined")
    n_stream_only = sum(1 for r in rows if r.get("join_status") == "stream_only")
    n_gate_only = sum(1 for r in rows if r.get("join_status") == "gate_only")
    denom = n_joined + n_stream_only + n_gate_only
    coverage = (n_joined / denom) if denom else 1.0

    tool_rows = [r for r in rows if r["type"] == "tool_use"]
    summary = {
        "run_id": run_id,
        "n_events": len(rows),
        "join_coverage": round(coverage, 6),
        "join_coverage_gate": JOIN_COVERAGE_GATE,
        "join_coverage_pass": coverage >= JOIN_COVERAGE_GATE,
        "n_joined": n_joined, "n_stream_only": n_stream_only, "n_gate_only": n_gate_only,
        "turns_total": len(seen_msg),
        "tool_calls_total": len(tool_rows),
        "tool_calls_task": sum(1 for r in tool_rows if not r["is_probe_turn"]),
        "max_tool_uses_per_message": max(tool_uses_per_message.values(), default=0),
        "tool_uses_per_message": dict(sorted(tool_uses_per_message.items())),
        "pacing_ok": max(tool_uses_per_message.values(), default=0) <= 1,
        "barrier_fires_total": sum(g.get("fires", 0) for g in gate.values()),
        "probes_sent": sum(1 for p in probe_turns if p.injection_kind == "probe"),
        "retries_sent": sum(1 for p in probe_turns if p.injection_kind == "retry"),
        "probes_answered": sum(1 for p in probe_turns if p.raw_response is not None),
        "resumes_sent": sum(1 for r in rows if r.get("injection_kind") == "resume"),
        "permission_denials": permission_denials,
        "result_event_seen": result_seen,
        "n_unknown_visible": len(regionset.unknown_visible),
        "tokens": {
            "accounting_version": TOKENS_ACCOUNTING_VERSION,
            "deduped_input": deduped_in,
            "deduped_output": deduped_out,
            "result_input": result_in,
            "result_output": result_out,
            "dedupe_delta_input": deduped_in - result_in,
            "dedupe_delta_output": deduped_out - result_out,
            #'s `deduped_token_total == result.usage total` gate holds for
            # INPUT ONLY, and that is a property of the CLI, not a shortcut:
            # stream.jsonl's per-assistant-message usage.output_tokens is a
            # PLACEHOLDER (measured 1, 2 and 5 against real 772 and 499), so
            # output tokens are not computable from the stream at all. Comparing
            # them made this flag false on every healthy run and would have
            # retired a working correctness check as permanently red.
            "dedupe_equals_result_input": deduped_in == result_in,
            "stream_output_unreliable": deduped_out != result_out,
            "dedupe_equals_result": deduped_in == result_in,
            "cost_usd": cost_usd,
        },
    }
    return EventsResult(rows=rows, summary=summary, probe_turns=probe_turns, regionset=regionset)




def _first_line_of_message(records: Sequence[Any]) -> dict[str, int]:
    """message.id -> the seq of the FIRST stream line carrying it.

    Every line of the group repeats a byte-identical `usage`; carrying it once,
    on this line, is the whole fix.
    """
    out: dict[str, int] = {}
    for rec in records:
        if rec.kind == "assistant" and rec.message_id:
            out.setdefault(rec.message_id, rec.seq)
    return out






def _hits_by_block(regionset: Any, exposure_rows: Sequence[dict]) -> dict[tuple[int, int], list[dict]]:
    """exposure rows regrouped onto the (seq, block_idx) the event row carries."""
    block_of = {r.region_idx: r.block_idx for r in regionset.regions}
    out: dict[tuple[int, int], list[dict]] = {}
    for row in exposure_rows:
        blk = block_of.get(row.get("region_idx"))
        if blk is None:
            continue
        out.setdefault((row["seq"], blk), []).append({
            "fact_id": row["fact_id"],
            "channel": row["channel"],
            "offset": row["offset"],
            "match_form": row["match_form"],
            "model_visible": row["model_visible"],
            "inbound": row["inbound"],
            "bytes_before": row["bytes_before"],
        })
    return out


def _truncation_index(regionset: Any) -> dict[str, dict]:
    """tool_use_id -> the truncation/read_error detail, sourced from transcript.jsonl."""
    out: dict[str, dict] = {}
    for s in regionset.signals:
        if s.kind == "truncated" and s.tool_use_id:
            out.setdefault(str(s.tool_use_id), dict(s.detail))
        elif s.kind == "read_error" and s.tool_use_id:
            out[str(s.tool_use_id)] = dict(s.detail, trigger="read_error_256kb")
    return out






def _tag_probe_turns(rows: list[dict], texts: list[str], gate: dict[str, dict]) -> list[ProbeTurn]:
    """Mark is_probe_turn and lift the answer text out of the stream.

    The window opens on the replayed probe_in and closes on the reply (which is
    retyped `probe_out`) or, if the model never answers, on the next tool call —
    so a lost answer can never swallow the rest of the run.
    """
    turns: list[ProbeTurn] = []
    open_turn: ProbeTurn | None = None
    last_barrier: int | None = None
    for i, row in enumerate(rows):
        if row["type"] == "tool_use" and row.get("barrier") is not None:
            last_barrier = row["barrier"]

        if row["type"] == "probe_in":
            if open_turn is not None:
                open_turn = None  # superseded; probes.py scores it
            pid = row.get("probe_id")
            open_turn = ProbeTurn(
                probe_id=str(pid) if pid else "",
                injection_kind=str(row.get("injection_kind") or "probe"),
                sent_seq=row["seq"], sent_ts=row.get("ts"), sent_at_barrier=last_barrier,
            )
            turns.append(open_turn)
            row["is_probe_turn"] = True
            open_turn.seqs.append(row["seq"])
            continue

        if open_turn is None:
            continue

        if row["type"] == "tool_use":
            open_turn = None  # the model resumed the task without answering
            continue

        if row["type"] == "assistant":
            body = texts[i] or ""
            answers = (open_turn.probe_id and open_turn.probe_id in body) or \
                      bool(protocol.find_probe_ids(body)) or \
                      protocol.parse_answer(body, open_turn.probe_id).parse_ok
            row["is_probe_turn"] = True
            open_turn.seqs.append(row["seq"])
            if answers:
                row["type"] = "probe_out"
                row["probe_id"] = open_turn.probe_id or None
                open_turn.answer_seq = row["seq"]
                open_turn.answered_ts = row.get("ts")
                open_turn.answer_message_id = row.get("message_id")
                open_turn.raw_response = ((open_turn.raw_response or "") + body) or body
                open_turn = None
            else:
                open_turn.raw_response = (open_turn.raw_response or "") + body
            continue

        # tool_result / harness_in inside an open window still belong to it
        row["is_probe_turn"] = True
        open_turn.seqs.append(row["seq"])
    return turns


def _attach_probe_tokens(rows: Sequence[dict], turns: Sequence[ProbeTurn]) -> None:
    """The instrument's own output cost, deduped by message_id."""
    out_by_msg = {r["message_id"]: r.get("tokens_out") for r in rows
                  if r.get("message_id") and r.get("tokens_out") is not None}
    for t in turns:
        if t.answer_message_id:
            t.tokens_out = out_by_msg.get(t.answer_message_id)


# ── entry points ─────────────────────────────────────────────────────────────
def run(run_dir: str | os.PathLike, facts: Any = None, out_path: str | os.PathLike | None = None,
        run_id: str | None = None, exposure_rows: Sequence[dict] | None = None,
        regionset: Any = None) -> EventsResult:
    """Build and write events.jsonl for one run dir."""
    rd = Path(run_dir)
    rid = run_id or _run_id_of(rd)
    rs = regionset if regionset is not None else regions_mod.from_run(rd)
    if exposure_rows is None:
        src = facts if facts is not None else exposure_mod.find_facts_file(rd)
        cards = exposure_mod.load_fact_cards(src) if src is not None else []
        exposure_rows = exposure_mod.scan(rs, cards, rid)
    res = build(rs, load_gate(rd), exposure_rows, rid)
    target = Path(out_path) if out_path else rd / "events.jsonl"
    regions_mod.write_jsonl_atomic(target, res.rows)
    res.summary["out"] = str(target)
    return res


def _run_id_of(run_dir: Path) -> str:
    meta = regions_mod.read_json(run_dir / "run_meta.json", {}) or {}
    return str(meta.get("run_id") or run_dir.name)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="stream x gate join")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--facts", default=None)
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)
    res = run(a.run_dir, a.facts, a.out)
    print(json.dumps(res.summary, indent=2, sort_keys=True))
    if not res.summary["join_coverage_pass"]:
        print(f"join_coverage {res.summary['join_coverage']} < {JOIN_COVERAGE_GATE}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
