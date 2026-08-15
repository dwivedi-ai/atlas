#!/usr/bin/env python3
"""
agy.py — shared helpers for the Antigravity CLI (`agy`) backend.

Centralizes the three things multiple exp-runner components need (see AGY_DOCS.md):
  1. AGY_MODELS  — exp-runner model slug -> the human-readable `--model` string agy expects.
  2. locate/*    — find a finished run's transcript_full.jsonl + conversation .db. agy never
                   prints the conversation id, so we recover it from the --log-file, else the
                   cache/last_conversations.json map (abs-cwd -> uuid), else the newest brain dir.
  3. extract_tokens() — per-call token usage from the conversation SQLite DB (agy stores NO
                   token fields in the transcript; the DB `gen_metadata` protobuf is the source).

CLI (used by the shell seams):
  python3 agy.py cli-model <agent_id>                         -> the `--model` string
  python3 agy.py locate --log L --cwd C --gemini-dir G [--what transcript|db|cid]
  python3 agy.py tokens <conversation.db>                     -> JSON usage
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import struct
from pathlib import Path

# exp-runner model slug -> agy `--model` human string (exactly as `agy models` prints them)
AGY_MODELS = {
    "agy-flash-low":  "Gemini 3.5 Flash (Low)",
    "agy-flash-med":  "Gemini 3.5 Flash (Medium)",
    "agy-flash-high": "Gemini 3.5 Flash (High)",
    "agy-pro-low":    "Gemini 3.1 Pro (Low)",
    "agy-pro-high":   "Gemini 3.1 Pro (High)",
    "agy-sonnet":     "Claude Sonnet 4.6 (Thinking)",
    "agy-opus":       "Claude Opus 4.6 (Thinking)",
    "agy-gpt-oss":    "GPT-OSS 120B (Medium)",
}
DEFAULT_AGY = "agy-flash-low"   # cheap, verified-reliable; `model: agy` resolves here


def cli_model(agent_id: str) -> str:
    return AGY_MODELS.get(agent_id, AGY_MODELS[DEFAULT_AGY])


# ── transcript / db location ─────────────────────────────────────────────────
def appdata_dir(gemini_dir: str | None = None) -> Path:
    base = Path(gemini_dir) if gemini_dir else Path(os.environ.get("HOME", "~")).expanduser() / ".gemini"
    return base / "antigravity-cli"


def conversation_id(log_path: str | None = None, cwd: str | None = None,
                    gemini_dir: str | None = None) -> str | None:
    # 1. the CLI log names it: "Print mode: conversation=<uuid>"
    if log_path and Path(log_path).exists():
        m = re.search(r"conversation=([0-9a-f-]{36})", Path(log_path).read_text(errors="replace"))
        if m:
            return m.group(1)
    ad = appdata_dir(gemini_dir)
    # 2. cache/last_conversations.json maps abs-cwd -> uuid
    if cwd:
        lc = ad / "cache" / "last_conversations.json"
        if lc.exists():
            try:
                d = json.loads(lc.read_text())
                key = str(Path(cwd).resolve())
                if key in d:
                    return d[key]
            except Exception:
                pass
    # 3. newest brain dir
    brains = sorted(glob.glob(str(ad / "brain" / "*")), key=os.path.getmtime, reverse=True)
    if brains:
        return Path(brains[0]).name
    return None


def transcript_path(conv_id: str, gemini_dir: str | None = None) -> Path:
    return appdata_dir(gemini_dir) / "brain" / conv_id / ".system_generated" / "logs" / "transcript_full.jsonl"



def db_path(conv_id: str, gemini_dir: str | None = None) -> Path:
    return appdata_dir(gemini_dir) / "conversations" / f"{conv_id}.db"


def final_answer(transcript_file: str | os.PathLike) -> str:
    """The last MODEL PLANNER_RESPONSE that carries content and no tool_calls (= the answer)."""
    ans = ""
    try:
        for line in Path(transcript_file).read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if (d.get("source") == "MODEL" and d.get("type") == "PLANNER_RESPONSE"
                    and d.get("content") and not d.get("tool_calls")):
                ans = d["content"]
    except Exception:
        pass
    return ans


# ── token extractor (validated; AGY_DOCS.md Appendix A) ──────────────────────
def _varint(b, i):
    s = r = 0
    while True:
        x = b[i]; i += 1; r |= (x & 0x7f) << s
        if not x & 0x80:
            return r, i
        s += 7


def _decode(b):
    out = []; i = 0; n = len(b)
    while i < n:
        k, i = _varint(b, i); f, wt = k >> 3, k & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v = struct.unpack("<Q", b[i:i + 8])[0]; i += 8
        elif wt == 2:
            ln, i = _varint(b, i); v = b[i:i + ln]; i += ln
        elif wt == 5:
            v = struct.unpack("<I", b[i:i + 4])[0]; i += 4
        else:
            break
        out.append((f, wt, v))
    return out


def _sub(d, fn):  return next((v for f, wt, v in d if f == fn and wt == 2), None)
def _var(d, fn):  return next((v for f, wt, v in d if f == fn and wt == 0), None)
def _str(d, fn):  return next((v.decode("utf-8", "replace") for f, wt, v in d if f == fn and wt == 2), None)



def snapshot(src: str | os.PathLike, dst: str | os.PathLike) -> bool:
    """Checkpoint the WAL into the main DB, then back up a clean single-file snapshot.
    A plain `cp` of a live WAL-mode SQLite DB loses rows still in the -wal (agy writes
    gen_metadata there); this captures them. agy has exited by teardown, so RW-open is safe."""
    try:
        con = sqlite3.connect(str(src))
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dstcon = sqlite3.connect(str(dst))
            con.backup(dstcon)
            dstcon.close()
        finally:
            con.close()
        return True
    except Exception:
        return False


def extract_tokens(db_file: str | os.PathLike) -> dict:
    """Per-call token usage from gen_metadata (client-side ESTIMATES; no cache field).
    input = f1.f9.f10.f1, output = f1.f4.f3, model = f1.f21. See AGY_DOCS.md"""
    try:
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    except Exception:
        return {}
    calls = []
    try:
        for idx, data in con.execute("SELECT idx,data FROM gen_metadata ORDER BY idx"):
            m1 = _decode(_sub(_decode(data), 1) or b"")
            model = _str(m1, 21) or _str(m1, 19)
            out_t = _var(_decode(_sub(m1, 4) or b""), 3)
            in_t = _var(_decode(_sub(_decode(_sub(m1, 9) or b""), 10) or b""), 1)
            calls.append((idx, model, in_t or 0, out_t or 0))
    except Exception:
        pass
    finally:
        con.close()
    return {
        "num_calls": len(calls),
        "total_input": sum(c[2] for c in calls),    # Σ per-call = billed-input upper bound
        "total_output": sum(c[3] for c in calls),
        "context_peak": max((c[2] for c in calls), default=0),
        "model": calls[0][1] if calls else None,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────
def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="agy backend helper")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("cli-model"); m.add_argument("agent_id")

    lt = sub.add_parser("locate")
    lt.add_argument("--log"); lt.add_argument("--cwd"); lt.add_argument("--gemini-dir")
    lt.add_argument("--what", choices=["transcript", "db", "cid"], default="transcript")

    tk = sub.add_parser("tokens"); tk.add_argument("db")
    sn = sub.add_parser("snapshot"); sn.add_argument("src"); sn.add_argument("dst")
    a = p.parse_args()

    if a.cmd == "snapshot":
        import sys as _sys
        _sys.exit(0 if snapshot(a.src, a.dst) else 1)
    if a.cmd == "cli-model":
        print(cli_model(a.agent_id))
    elif a.cmd == "locate":
        cid = conversation_id(a.log, a.cwd, a.gemini_dir)
        if not cid:
            return
        if a.what == "cid":
            print(cid)
        elif a.what == "db":
            print(db_path(cid, a.gemini_dir))
        else:
            print(transcript_path(cid, a.gemini_dir))
    elif a.cmd == "tokens":
        print(json.dumps(extract_tokens(a.db)))


if __name__ == "__main__":
    main()
