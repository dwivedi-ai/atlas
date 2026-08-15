#!/usr/bin/env python3
"""
regions.py — model-visible region extraction against the CLOSED channel enum.

RESPONSIBILITY
  Turn the two raw transcripts of a run into an ordered list of *regions*: spans
  of text that provably entered the model's context window, each tagged with one
  member of the closed 18-channel enum of  Nothing in
  this module scans for a nonce, decides `read`, or writes a derived table — it
  only says "these bytes, on this channel, at this position".

  "Model-visible" is the load-bearing word. A nonce that exists only
  in a sidecar field, a hook payload, or a persistedOutputPath file on disk was
  never in the context window; scanning those manufactures exposure. So this
  module NEVER reads hook payloads, never reads watch/persisted/* content, and
  emits `sidecar_only` / `persisted_output_ondisk` as ZERO-LENGTH marker regions
  (model_visible=false, empty text) that carry the diagnostic metadata and can
  never contribute a byte to the scan.

INPUTS
  $RUN_DIR/stream.jsonl      verbatim child stdout (may be .gz) — PRIMARY
  $RUN_DIR/transcript.jsonl  the on-disk session file, copied by --session-id.
                             LOAD-BEARING, not a convenience copy: it is
                             the only source of truncation information, and the
                             only source of `attachment` blocks and isSidechain.

  CAMEL vs SNAKE: the on-disk transcript uses `toolUseResult`; the stream uses
  `tool_use_result`. They are NOT interchangeable and this module reads each
  from its own file.

OUTPUTS
  RegionSet(records, regions, signals, unknown_visible, meta)
    records         canonical ordered StreamRecord list — `seq` is assigned HERE
                    and every downstream table (events/exposure/trace) uses it,
                    which is what makes the ordering invariant
                    first_exposure_seq < first_mention_seq comparable at all.
    regions         Region rows, in seq order, with cumulative bytes_before.
    signals         per-tool_use_id truncation / read_error / sidecar facts,
                    sourced from transcript.jsonl (stream.jsonl carries none).
    unknown_visible list of Regions whose channel could not be mapped. NON-EMPTY
                    FAILS CI — the enum is not allowed to drift silently.

CLI
  python3 lib/wur/regions.py --run-dir DIR [--summary|--json]
    exits 1 when any unknown_visible region was produced.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

try:  # package context (`from wur import regions`)
    from . import protocol
except ImportError:  # flat context (lib/wur on sys.path, or run as a script)
    import protocol  # type: ignore


# ── the closed channel enum ───────────────────────
@dataclass(frozen=True)
class ChannelSpec:
    """Fixed properties of one channel. `inbound` and `model_visible` are

    properties of the CHANNEL, never of the hit — which is what makes the
    definition of `read` auditable rather than a per-row judgement call.
    """

    name: str
    model_visible: bool
    inbound: bool
    counts_toward_read: bool
    note: str = ""


def _c(name: str, mv: bool, inb: bool, read: bool, note: str = "") -> ChannelSpec:
    return ChannelSpec(name=name, model_visible=mv, inbound=inb, counts_toward_read=read, note=note)




CHANNELS: dict[str, ChannelSpec] = {
    s.name: s
    for s in (
        # ── inbound: bytes the harness/tools put in front of the model ──
        _c("autoload_claude_md", True, True, True, "asserted from plant_manifest.json, never scanned"),
        _c("tool_read", True, True, True, "tool_result.content of a Read"),
        _c("tool_grep_content", True, True, True, "tool_result.content of a Grep, output_mode=content"),
        _c("tool_glob_filenames", True, True, True, "a filename listing (Glob, or Grep in a non-content mode)"),
        _c("bash_stdout", True, True, True, "Bash tool_result, offset < the 30,000-char sidecar cap"),
        _c("bash_unattributed", True, True, True, "past the sidecar cap or inside a <persisted-output> preview"),
        _c("tool_write_echo", True, True, True, "Write/Edit confirmation string — a hit means the nonce is in a PATH"),
        # ── model-visible but NOT inbound ──
        _c("self_thinking", True, False, True, "D4: sets read, leaves read_inbound_only alone; also sets thinking_echo"),
        _c("harness_task_prompt", True, False, False, "user + isReplay, sha-matched; asserted nonce-free"),
        _c("harness_probe", True, False, False, "the replayed CHECKPOINT text; asserted nonce-free"),
        _c("harness_resume", True, False, False, "RESUME_TEXT; sent identically in the no-probe arms"),
        _c("system_reminder", True, False, False, "audit only"),
        _c("system_init_listing", True, False, False, "system/init; audit only"),
        _c("self_text", True, False, False, "model output -> echoed, never exposure"),
        _c("tool_input", True, False, False, "model output -> echoed, never exposure"),
        _c("probe_answer", True, False, False, "model output -> echoed, never exposure"),
        # ── the CI tripwire ──
        _c("unknown_visible", True, True, True, "any model-visible region not mapped above; FAILS CI"),
        # ── not in context: diagnostic only, emitted with EMPTY text ──
        _c("sidecar_only", False, False, False, "hook/transcript sidecar bytes the model never saw"),
        _c("persisted_output_ondisk", False, False, False, "persistedOutputPath file; never read by this module"),
    )
}

ATTACHMENT_PREFIX = "attachment_"
_ATTACHMENT_RE = re.compile(r"^attachment_[A-Za-z0-9_.-]+$")

#: Channels that are asserted rather than observed; regions.py never emits them.
ASSERTED_CHANNELS = frozenset({"autoload_claude_md"})

#: V6: a Bash tool_result is truncated into a sidecar past this many characters.
SIDECAR_CHAR_CAP = 30_000

#: V16: the hard 256 KB Read ceiling. is_error:true, NO content, NO sidecar. The
#: 197-char body must never be classified as a tool_read region.
READ_CEILING_RE = re.compile(
    r"File content \([^)]*\) exceeds maximum allowed size \([^)]*\)", re.IGNORECASE
)
_PERSISTED_RE = re.compile(r"<persisted-output\b[^>]*>(.*?)</persisted-output>", re.DOTALL | re.IGNORECASE)
_PERSISTED_OPEN_RE = re.compile(r"<persisted-output\b", re.IGNORECASE)
_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>(.*?)</system-reminder>", re.DOTALL | re.IGNORECASE)



#: The frozen six. A tool_result from anything else is a real drift, not noise.
FROZEN_TOOLS = ("Bash", "Read", "Write", "Edit", "Glob", "Grep")

# Distinctive fixed substrings of the frozen harness texts, derived from
# protocol.py so an edit there cannot desynchronize the classifier.
_PROBE_MARKER = protocol.PROBE_TEXT.split("{probe_id}")[1][:40]
_RETRY_MARKER = protocol.RETRY_TEXT.split("{probe_id}")[1][:48]
_RESUME_SHA = protocol.sha256(protocol.RESUME_TEXT)


def channel_spec(channel: str) -> ChannelSpec:
    """The fixed spec of `channel`, including the attachment_<type> family."""
    if channel in CHANNELS:
        return CHANNELS[channel]
    if _ATTACHMENT_RE.match(channel or ""):
        return _c(channel, True, True, True, "transcript attachment.attachment.type")
    raise KeyError(f"channel not in the closed enum: {channel!r}")


def is_valid_channel(channel: str) -> bool:
    return channel in CHANNELS or bool(_ATTACHMENT_RE.match(channel or ""))


def counts_toward_read(channel: str) -> bool:
    return channel_spec(channel).counts_toward_read


# ── records and regions ──────────────────────────────────────────────────────
@dataclass
class StreamRecord:
    """One raw line of stream.jsonl, with the canonical `seq` stamped on it."""

    seq: int
    line_no: int
    source: str  # "stream" | "transcript"
    kind: str  # system_init | assistant | user | result | hook | other
    obj: dict
    ts: str | None = None
    message_id: str | None = None
    parent_tool_use_id: str | None = None
    is_replay: bool = False
    is_sidechain: bool = False


@dataclass
class Region:
    """One span of text with a channel. `text` is EMPTY for non-visible channels."""

    seq: int
    region_idx: int
    channel: str
    text: str
    source: str  # stream | transcript
    model_visible: bool
    inbound: bool
    counts_toward_read: bool
    tool: str | None = None
    tool_use_id: str | None = None
    message_id: str | None = None
    block_idx: int | None = None
    ts: str | None = None
    is_error: bool = False
    bytes_before: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def nbytes(self) -> int:
        return len(self.text.encode("utf-8", "replace"))

    def to_dict(self) -> dict:
        d = {
            "seq": self.seq,
            "region_idx": self.region_idx,
            "channel": self.channel,
            "source": self.source,
            "model_visible": self.model_visible,
            "inbound": self.inbound,
            "counts_toward_read": self.counts_toward_read,
            "tool": self.tool,
            "tool_use_id": self.tool_use_id,
            "message_id": self.message_id,
            "block_idx": self.block_idx,
            "ts": self.ts,
            "is_error": self.is_error,
            "bytes_before": self.bytes_before,
            "nbytes": self.nbytes,
            "text_sha256": protocol.sha256(self.text) if self.text else None,
            "meta": self.meta,
        }
        return d


@dataclass
class Signal:
    """A per-tool_use_id fact sourced from transcript.jsonl.

    kind ∈ {truncated, read_error, persisted_output, sidecar_only, sidechain}
    """

    kind: str
    tool_use_id: str | None
    tool: str | None = None
    detail: dict = field(default_factory=dict)
    source: str = "transcript"


@dataclass
class RegionSet:
    records: list[StreamRecord] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    unknown_visible: list[Region] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def by_seq(self) -> dict[int, list[Region]]:
        out: dict[int, list[Region]] = {}
        for r in self.regions:
            out.setdefault(r.seq, []).append(r)
        return out

    def signals_for(self, tool_use_id: str | None) -> list[Signal]:
        return [s for s in self.signals if s.tool_use_id == tool_use_id]


# ── tiny IO helpers, shared by the whole derivation chain ────────────────────
def open_maybe_gz(path: str | os.PathLike) -> io.TextIOBase:
    """Open a text file, transparently handling a .gz sibling (teardown gzips)."""
    p = Path(path)
    if p.exists():
        if p.suffix == ".gz":
            return gzip.open(p, "rt", encoding="utf-8", errors="replace")
        return p.open("r", encoding="utf-8", errors="replace")
    gz = Path(str(p) + ".gz")
    if gz.exists():
        return gzip.open(gz, "rt", encoding="utf-8", errors="replace")
    raise FileNotFoundError(str(p))


def jsonl_objects(path: str | os.PathLike) -> Iterator[tuple[int, dict]]:
    """Yield (1-based line number, object) for every parseable line.

    Unparseable lines are skipped, not fatal: V8 measured >PIPE_BUF appends
    corrupting 5/12 lines of a concurrently-written hook log, and a corrupt tail
    must not destroy a run's whole derivation.
    """
    with open_maybe_gz(path) as fh:
        for i, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield i, obj


def read_jsonl(path: str | os.PathLike) -> list[dict]:
    try:
        return [o for _n, o in jsonl_objects(path)]
    except FileNotFoundError:
        return []


def write_jsonl_atomic(path: str | os.PathLike, rows: Iterable[dict]) -> int:
    """Write rows as JSONL via a .tmp + os.replace, so a reader never sees a

    half-written table and a crashed reconcile leaves the previous one intact
    (idempotent, re-runnable months later).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    n = 0
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
            n += 1
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
    return n


def write_json_atomic(path: str | os.PathLike, obj: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, sort_keys=True, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


def read_json(path: str | os.PathLike, default: Any = None) -> Any:
    try:
        with open_maybe_gz(path) as fh:
            return json.load(fh)
    except Exception:
        return default


# ── content helpers ──────────────────────────────────────────────────────────
def blocks_of(message: Any) -> list[dict]:
    """Normalize `message.content` into a list of block dicts."""
    if isinstance(message, str):
        return [{"type": "text", "text": message}]
    if isinstance(message, list):
        return [b if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in message]
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in content]
    return []


def result_text(content: Any) -> str:
    """The model-visible text of a tool_result body (string or block list)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if isinstance(b.get("text"), str):
                    parts.append(b["text"])
                elif b.get("type") in (None, "text"):
                    parts.append(json.dumps(b, ensure_ascii=False, sort_keys=True))
                else:
                    # image / document blocks carry no scannable text
                    parts.append("")
            else:
                parts.append(str(b))
        return "".join(parts)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    return str(content)


def _strings_in(obj: Any, out: list[str], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _strings_in(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _strings_in(v, out, depth + 1)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ── harness text classification ("user + isReplay:true, sha-matched") ──
def classify_harness_text(text: str) -> tuple[str, str, str | None, bool]:
    """(channel, injection_kind, probe_id, sha_matched) for a user text block.

    `sha_matched` is the honest half: the text is compared against the exact
    bytes protocol.py would have rendered. A False here on a harness_* region
    means something reached the model that this harness did not author, which is
    a leak check, not a formatting nicety.
    """
    ids = protocol.find_probe_ids(text or "")
    if ids:
        pid = ids[0]
        if _RETRY_MARKER and _RETRY_MARKER in text:
            return "harness_probe", "retry", pid, text.strip() == protocol.render_retry(pid).strip()
        if _PROBE_MARKER and _PROBE_MARKER in text:
            return "harness_probe", "probe", pid, text.strip() == protocol.render_probe(pid).strip()
        return "harness_probe", "probe", pid, False
    if protocol.sha256(text.strip()) == protocol.sha256(protocol.RESUME_TEXT.strip()):
        return "harness_resume", "resume", None, True
    if _norm_ws(text) == _norm_ws(protocol.RESUME_TEXT):
        return "harness_resume", "resume", None, False
    return "harness_task_prompt", "task_prompt", None, False


# ── tool -> channel mapping ──────────────────────────────────────────────────
def channel_for_tool_result(tool: str | None, tool_input: dict | None) -> str:
    """Map a tool_result body to its channel.

    DOCUMENTED MAPPING DECISION: defines `tool_grep_content` as "Grep,
    output_mode=content". A Grep in files_with_matches / count mode returns a
    FILENAME LISTING, which is semantically the tool_glob_filenames channel
    ("a glob listing whose path carries the nonce",) — so it is mapped
    there, with the real tool name kept on the region. Routing it to
    unknown_visible instead would fail CI on an ordinary search.
    """
    t = (tool or "").strip()
    if t == "Read":
        return "tool_read"
    if t == "Grep":
        mode = ((tool_input or {}).get("output_mode") or "").strip().lower()
        return "tool_grep_content" if mode in ("", "content") else "tool_glob_filenames"
    if t == "Glob":
        return "tool_glob_filenames"
    if t == "Bash":
        return "bash_stdout"
    if t in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return "tool_write_echo"
    return "unknown_visible"


# ── the extractor ────────────────────────────────────────────────────────────
class _Builder:
    def __init__(self) -> None:
        self.regions: list[Region] = []
        self.signals: list[Signal] = []
        self.records: list[StreamRecord] = []
        self._idx = 0

    def add(self, **kw: Any) -> Region:
        spec = channel_spec(kw["channel"])
        text = kw.pop("text", "") or ""
        if not spec.model_visible:
            text = ""  # never scan bytes the model did not see
        r = Region(
            region_idx=self._idx,
            text=text,
            model_visible=spec.model_visible,
            inbound=spec.inbound,
            counts_toward_read=spec.counts_toward_read,
            **kw,
        )
        self._idx += 1
        self.regions.append(r)
        return r


def _tool_index(records: Sequence[StreamRecord]) -> dict[str, dict]:
    """tool_use_id -> {tool, input, seq, message_id} from assistant tool_use blocks."""
    idx: dict[str, dict] = {}
    for rec in records:
        if rec.kind != "assistant":
            continue
        for b in blocks_of(rec.obj.get("message")):
            if b.get("type") == "tool_use" and b.get("id"):
                idx[str(b["id"])] = {
                    "tool": b.get("name"),
                    "input": b.get("input") if isinstance(b.get("input"), dict) else {},
                    "seq": rec.seq,
                    "message_id": rec.message_id,
                }
    return idx


def _record_from(line_no: int, obj: dict, seq: int, source: str) -> StreamRecord:
    typ = obj.get("type")
    sub = obj.get("subtype")
    if typ == "system" and sub == "init":
        kind = "system_init"
    elif typ == "system":
        kind = "hook" if str(sub or "").startswith("hook") else "other"
    elif typ in ("assistant", "user", "result"):
        kind = typ
    elif typ == "attachment":
        kind = "attachment"
    else:
        kind = "other"
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    return StreamRecord(
        seq=seq,
        line_no=line_no,
        source=source,
        kind=kind,
        obj=obj,
        ts=obj.get("timestamp") or obj.get("ts"),
        message_id=(msg.get("id") if kind == "assistant" else None),
        parent_tool_use_id=obj.get("parent_tool_use_id") or obj.get("parentToolUseId"),
        is_replay=bool(obj.get("isReplay") or obj.get("is_replay")),
        is_sidechain=bool(obj.get("isSidechain") or obj.get("is_sidechain")),
    )


def _emit_text_block(b: _Builder, rec: StreamRecord, block_idx: int, text: str, channel: str,
                     **meta: Any) -> None:
    """Emit a text block, splitting out any <system-reminder> spans it carries."""
    spans = list(_SYSTEM_REMINDER_RE.finditer(text or ""))
    if not spans:
        b.add(seq=rec.seq, channel=channel, text=text, source=rec.source,
              message_id=rec.message_id, block_idx=block_idx, ts=rec.ts, meta=dict(meta))
        return
    cursor = 0
    for m in spans:
        if m.start() > cursor:
            chunk = text[cursor:m.start()]
            if chunk.strip():
                b.add(seq=rec.seq, channel=channel, text=chunk, source=rec.source,
                      message_id=rec.message_id, block_idx=block_idx, ts=rec.ts,
                      meta=dict(meta, split="pre_system_reminder"))
        b.add(seq=rec.seq, channel="system_reminder", text=m.group(0), source=rec.source,
              message_id=rec.message_id, block_idx=block_idx, ts=rec.ts, meta=dict(meta))
        cursor = m.end()
    tail = text[cursor:]
    if tail.strip():
        b.add(seq=rec.seq, channel=channel, text=tail, source=rec.source,
              message_id=rec.message_id, block_idx=block_idx, ts=rec.ts,
              meta=dict(meta, split="post_system_reminder"))


def _emit_tool_result(b: _Builder, rec: StreamRecord, block_idx: int, block: dict,
                      tools: dict[str, dict]) -> None:
    tid = block.get("tool_use_id") or block.get("toolUseId")
    tid = str(tid) if tid else None
    info = tools.get(tid or "", {})
    tool = info.get("tool")
    tinput = info.get("input") or {}
    is_error = bool(block.get("is_error") or block.get("isError"))
    body = result_text(block.get("content"))

    common = dict(seq=rec.seq, source=rec.source, tool=tool, tool_use_id=tid,
                  message_id=rec.message_id, block_idx=block_idx, ts=rec.ts, is_error=is_error)

    # V16: the hard 256 KB Read ceiling — is_error, no content, no sidecar. It is
    # NOT a tool_read region: the 197-char body carries no file bytes at all.
    if is_error and READ_CEILING_RE.search(body or ""):
        b.signals.append(Signal(kind="read_error", tool_use_id=tid, tool=tool,
                                source="stream",
                                detail={"body": body[:400], "path": tinput.get("file_path")}))
        return

    channel = channel_for_tool_result(tool, tinput)
    if channel == "unknown_visible":
        b.add(text=body, channel="unknown_visible",
              meta={"reason": "tool_result from a tool outside the frozen six",
                    "tool": tool, "unjoined": tool is None}, **common)
        return

    if is_error:
        # A harness-authored error (a deny reason, a missing path) is bytes the
        # model saw, but it is derived from the model's own input or from the
        # harness — never from the workspace. Recorded on its channel, with
        # inbound forced off so it can never set read / first_exposure_seq.
        r = b.add(text=body, channel=channel,
                  meta={"error_body": True,
                        "budget_deny": protocol.BUDGET_STOP_TEXT[:40] in (body or "")},
                  **common)
        if channel != "bash_stdout":
            # Bash error output IS real command output and stays inbound.
            r.inbound = False
            r.counts_toward_read = False
        return

    if channel == "bash_stdout":
        _emit_bash(b, body, common)
        return

    b.add(text=body, channel=channel, meta={}, **common)


def _emit_bash(b: _Builder, body: str, common: dict) -> None:
    """Split a Bash result into bash_stdout and bash_unattributed."""
    cursor = 0
    visible_budget = SIDECAR_CHAR_CAP
    for m in _PERSISTED_RE.finditer(body or ""):
        pre = body[cursor:m.start()]
        if pre:
            visible_budget = _emit_bash_chunk(b, pre, common, visible_budget)
        b.add(text=m.group(0), channel="bash_unattributed",
              meta={"reason": "persisted_output_preview"}, **common)
        cursor = m.end()
    tail = body[cursor:]
    if tail:
        _emit_bash_chunk(b, tail, common, visible_budget)
    if not body:
        b.add(text="", channel="bash_stdout", meta={}, **common)


def _emit_bash_chunk(b: _Builder, chunk: str, common: dict, budget: int) -> int:
    if budget <= 0:
        b.add(text=chunk, channel="bash_unattributed",
              meta={"reason": "sidecar_capped"}, **common)
        return 0
    head, tail = chunk[:budget], chunk[budget:]
    if head:
        b.add(text=head, channel="bash_stdout", meta={}, **common)
    if tail:
        b.add(text=tail, channel="bash_unattributed",
              meta={"reason": "sidecar_capped"}, **common)
    return max(0, budget - len(chunk))


def _extract_stream(b: _Builder, records: Sequence[StreamRecord]) -> None:
    tools = _tool_index(records)
    for rec in records:
        if rec.kind == "system_init":
            listing = {k: rec.obj.get(k) for k in
                       ("tools", "mcp_servers", "slash_commands", "skills", "plugins", "agents",
                        "model", "permissionMode", "output_style")
                       if k in rec.obj}
            b.add(seq=rec.seq, channel="system_init_listing",
                  text=json.dumps(listing, ensure_ascii=False, sort_keys=True),
                  source=rec.source, ts=rec.ts, meta={"subtype": "init"})
            continue

        if rec.kind == "assistant":
            for i, blk in enumerate(blocks_of(rec.obj.get("message"))):
                btype = blk.get("type")
                if btype == "thinking":
                    b.add(seq=rec.seq, channel="self_thinking",
                          text=blk.get("thinking") or blk.get("text") or "",
                          source=rec.source, message_id=rec.message_id, block_idx=i, ts=rec.ts,
                          meta={})
                elif btype == "redacted_thinking":
                    b.add(seq=rec.seq, channel="self_thinking", text="",
                          source=rec.source, message_id=rec.message_id, block_idx=i, ts=rec.ts,
                          meta={"redacted": True})
                elif btype == "text":
                    txt = blk.get("text") or ""
                    ch = "probe_answer" if protocol.find_probe_ids(txt) else "self_text"
                    b.add(seq=rec.seq, channel=ch, text=txt, source=rec.source,
                          message_id=rec.message_id, block_idx=i, ts=rec.ts, meta={})
                elif btype == "tool_use":
                    b.add(seq=rec.seq, channel="tool_input",
                          text=json.dumps(blk.get("input") or {}, ensure_ascii=False, sort_keys=True),
                          source=rec.source, tool=blk.get("name"),
                          tool_use_id=str(blk.get("id")) if blk.get("id") else None,
                          message_id=rec.message_id, block_idx=i, ts=rec.ts, meta={})
                else:
                    b.add(seq=rec.seq, channel="unknown_visible",
                          text=json.dumps(blk, ensure_ascii=False, sort_keys=True)[:20000],
                          source=rec.source, message_id=rec.message_id, block_idx=i, ts=rec.ts,
                          meta={"reason": f"unmapped assistant content block type {btype!r}"})
            continue

        if rec.kind == "user":
            for i, blk in enumerate(blocks_of(rec.obj.get("message"))):
                btype = blk.get("type")
                if btype == "tool_result":
                    _emit_tool_result(b, rec, i, blk, tools)
                elif btype == "text":
                    txt = blk.get("text") or ""
                    ch, kind, pid, sha_ok = classify_harness_text(txt)
                    _emit_text_block(b, rec, i, txt, ch,
                                     injection_kind=kind, probe_id=pid, sha_matched=sha_ok,
                                     is_replay=rec.is_replay)
                else:
                    b.add(seq=rec.seq, channel="unknown_visible",
                          text=json.dumps(blk, ensure_ascii=False, sort_keys=True)[:20000],
                          source=rec.source, message_id=rec.message_id, block_idx=i, ts=rec.ts,
                          meta={"reason": f"unmapped user content block type {btype!r}"})
            continue

        # result / hook / rate_limit_event / anything else: no model-visible
        # region. Hook payloads are DELIBERATELY never scanned.


# ── transcript.jsonl: truncation, sidecars, attachments ──────────────────────
def _first_tool_use_id(obj: dict) -> str | None:
    for b in blocks_of(obj.get("message")):
        tid = b.get("tool_use_id") or b.get("toolUseId")
        if tid:
            return str(tid)
        if b.get("type") == "tool_use" and b.get("id"):
            return str(b["id"])
    return None


def _extract_transcript(b: _Builder, lines: Sequence[tuple[int, dict]],
                        stream_records: Sequence[StreamRecord]) -> None:
    """Harvest the three things ONLY the transcript has (V16, ).

    1. truncation:      toolUseResult.file.numLines < totalLines  (camelCase!)
    2. sidecars:        persistedOutputPath, and sidecar bodies the model did
                        not see — emitted as ZERO-LENGTH marker regions.
    3. attachments:     attachment.attachment.type -> attachment_<type>, and
                        <system-reminder> blocks the stream does not replay.

    Transcript-derived regions are mapped onto the stream's `seq` by tool_use_id
    or assistant message id; when neither resolves, the last resolved seq is
    carried forward and the region is stamped meta.seq_approx = true.
    """
    seq_by_tool: dict[str, int] = {}
    seq_by_msg: dict[str, int] = {}
    for rec in stream_records:
        if rec.message_id:
            seq_by_msg.setdefault(rec.message_id, rec.seq)
        for blk in blocks_of(rec.obj.get("message")):
            tid = blk.get("tool_use_id") or blk.get("toolUseId") or (
                blk.get("id") if blk.get("type") == "tool_use" else None)
            if tid:
                seq_by_tool.setdefault(str(tid), rec.seq)

    # Dedupe against what the stream already carries, so nothing is counted twice.
    seen = {(r.channel, protocol.sha256(r.text)) for r in b.regions if r.text}

    last_seq = 0
    for line_no, obj in lines:
        tid = _first_tool_use_id(obj)
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        mid = msg.get("id")
        approx = True
        if tid and tid in seq_by_tool:
            last_seq, approx = seq_by_tool[tid], False
        elif mid and mid in seq_by_msg:
            last_seq, approx = seq_by_msg[mid], False
        seq = last_seq

        if obj.get("isSidechain") or obj.get("is_sidechain"):
            b.signals.append(Signal(kind="sidechain", tool_use_id=tid,
                                    detail={"line_no": line_no}))

        tur = obj.get("toolUseResult")
        if isinstance(tur, dict):
            _transcript_sidecar(b, tur, tid, seq, line_no)

        if obj.get("type") == "attachment" or isinstance(obj.get("attachment"), dict):
            att = obj.get("attachment") if isinstance(obj.get("attachment"), dict) else {}
            atype = str(att.get("type") or obj.get("attachmentType") or "unknown")
            atype = re.sub(r"[^A-Za-z0-9_.-]", "_", atype) or "unknown"
            parts: list[str] = []
            _strings_in(att, parts)
            text = "\n".join(parts)
            key = (ATTACHMENT_PREFIX + atype, protocol.sha256(text))
            if key not in seen:
                seen.add(key)
                b.add(seq=seq, channel=ATTACHMENT_PREFIX + atype, text=text, source="transcript",
                      tool_use_id=tid, message_id=mid, ts=obj.get("timestamp"),
                      meta={"seq_approx": approx, "line_no": line_no})
            continue

        # <system-reminder> blocks that the stream never replayed
        for blk in blocks_of(msg):
            if blk.get("type") != "text":
                continue
            for m in _SYSTEM_REMINDER_RE.finditer(blk.get("text") or ""):
                key = ("system_reminder", protocol.sha256(m.group(0)))
                if key in seen:
                    continue
                seen.add(key)
                b.add(seq=seq, channel="system_reminder", text=m.group(0), source="transcript",
                      message_id=mid, ts=obj.get("timestamp"),
                      meta={"seq_approx": approx, "line_no": line_no})






def _transcript_sidecar(b: _Builder, tur: dict, tid: str | None, seq: int, line_no: int) -> None:
    finfo = tur.get("file") if isinstance(tur.get("file"), dict) else None
    if finfo:
        num, total = finfo.get("numLines"), finfo.get("totalLines")
        if isinstance(num, int) and isinstance(total, int) and num < total:
            b.signals.append(Signal(
                kind="truncated", tool_use_id=tid,
                detail={"trigger": "numlines_lt_totallines", "numLines": num,
                        "totalLines": total, "filePath": finfo.get("filePath"),
                        "startLine": finfo.get("startLine")}))
        # NOTE: `truncated` is None on EVERY read. It is deliberately not
        # consulted; consulting it would silently disable truncation detection.

    ppath = tur.get("persistedOutputPath") or tur.get("persisted_output_path")
    if ppath:
        b.signals.append(Signal(kind="truncated", tool_use_id=tid,
                                detail={"trigger": "persisted_output_path", "path": ppath}))
        b.add(seq=seq, channel="persisted_output_ondisk", text="", source="transcript",
              tool_use_id=tid, meta={"path": ppath, "line_no": line_no,
                                     "never_scanned": True})

    parts: list[str] = []
    _strings_in({k: v for k, v in tur.items() if k not in ("file",)}, parts)
    sidecar_chars = sum(len(p) for p in parts)
    if sidecar_chars:
        b.signals.append(Signal(kind="sidecar_only", tool_use_id=tid,
                                detail={"sidecar_chars": sidecar_chars}))
        b.add(seq=seq, channel="sidecar_only", text="", source="transcript",
              tool_use_id=tid, meta={"sidecar_chars": sidecar_chars, "line_no": line_no,
                                     "never_scanned": True})


# ── public entry points ──────────────────────────────────────────────────────
def extract(stream_lines: Sequence[tuple[int, dict]],
            transcript_lines: Sequence[tuple[int, dict]] | None = None) -> RegionSet:
    """Build the RegionSet from already-parsed (line_no, obj) pairs."""
    records = [_record_from(n, o, seq, "stream") for seq, (n, o) in enumerate(stream_lines)]
    b = _Builder()
    b.records = records
    _extract_stream(b, records)
    if transcript_lines:
        _extract_transcript(b, transcript_lines, records)

    # Stable order, then cumulative model-visible byte position.
    b.regions.sort(key=lambda r: (r.seq, r.region_idx))
    running = 0
    for r in b.regions:
        r.bytes_before = running
        if r.model_visible:
            running += r.nbytes

    unknown = [r for r in b.regions if r.channel == "unknown_visible"]
    meta = {
        "n_records": len(records),
        "n_regions": len(b.regions),
        "n_signals": len(b.signals),
        "n_unknown_visible": len(unknown),
        "visible_bytes": running,
        "channels": _channel_histogram(b.regions),
    }
    return RegionSet(records=records, regions=b.regions, signals=b.signals,
                     unknown_visible=unknown, meta=meta)


def _channel_histogram(regions: Sequence[Region]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for r in regions:
        hist[r.channel] = hist.get(r.channel, 0) + 1
    return dict(sorted(hist.items()))


def from_run(run_dir: str | os.PathLike) -> RegionSet:
    """Build the RegionSet for one $RUN_DIR (stream.jsonl [+ transcript.jsonl])."""
    rd = Path(run_dir)
    stream = list(jsonl_objects(rd / "stream.jsonl"))
    tpath = rd / "transcript.jsonl"
    transcript: list[tuple[int, dict]] = []
    if tpath.exists() or Path(str(tpath) + ".gz").exists():
        transcript = list(jsonl_objects(tpath))
    rs = extract(stream, transcript)
    rs.meta["run_dir"] = str(rd)
    rs.meta["has_transcript"] = bool(transcript)
    if not transcript:
        # read_censored is computable ONLY from the transcript. Say so
        # loudly rather than letting every row silently score truncated=false.
        rs.meta["truncation_unavailable"] = True
    return rs


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="model-visible region extraction")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--json", action="store_true", help="dump every region as JSONL on stdout")
    a = p.parse_args(argv)
    rs = from_run(a.run_dir)
    if a.json:
        for r in rs.regions:
            print(json.dumps(r.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(rs.meta, indent=2, sort_keys=True))
    for r in rs.unknown_visible:
        print(f"UNKNOWN_VISIBLE seq={r.seq} {r.meta.get('reason')!r}", file=sys.stderr)
    return 1 if rs.unknown_visible else 0


if __name__ == "__main__":
    raise SystemExit(main())
