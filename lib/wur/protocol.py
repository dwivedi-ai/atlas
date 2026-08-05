#!/usr/bin/env python3
"""
protocol.py — the frozen wire protocol of the Workspace Uptake & Retention probe.

RESPONSIBILITY
  Own every byte of text the harness ever places in front of the model, and the
  pure functions that read the model's reply back out again. This module is the
  single source of truth for those strings: nothing else in the tree may inline
  probe, pacing, retry, resume or budget text. It touches no filesystem, no
  network and no subprocess, so it is import-safe from inside a hook.

INPUTS
  run_id + probe ordinal            -> probe_id() / render_probe()
  raw model text                    -> parse_answer(), looks_like_refusal()
  parsed slots + a FactCard         -> classify_slots(), annotate_slots()

OUTPUTS
  Rendered probe / retry / resume / budget-deny text (str)
  ParsedAnswer records (parse_ok, parse_tier, slots, next_action, errors)
  Slot classes drawn from SLOT_CLASSES
  sha256 stamps for run_meta.json   -> frozen_hashes()

FROZEN TEXT — DO NOT EDIT
  PROTOCOL_VERSION, PACING_PROMPT and PROBE_TEXT are copied verbatim from
  IMPLEMENTATION.md §6.2. PACING_PROMPT is independently pinned: its sha256 is
  0a687ddf… (338 chars), measured in SPIKES.md V4 against the runs that proved
  the one-tool-call-per-message invariant. Two clauses of PROBE_TEXT are
  load-bearing and must not be reworded without re-verification (§6.2):
  "Do not report facts about this checkpoint mechanism." and
  "resume the task … without waiting for me".
  Editing any frozen string invalidates cross-run comparison; verify_frozen()
  exists so preflight fails closed instead of the data drifting silently.

  RETRY_TEXT, RESUME_TEXT and BUDGET_STOP_TEXT are named but NOT quoted in
  IMPLEMENTATION.md §6.2; they are authored here and frozen from this point.
  BUDGET_STOP_TEXT in particular is UNVALIDATED: V14/SPIKES V1-2 measured that a
  denied tool call is retried by the model and then succeeds, so budget stop is
  deny-every-subsequent-call PLUS closing stdin, and this text is an explanation
  of the stop, never the mechanism of it.

CLI
  python3 lib/wur/protocol.py hashes            -> run_meta stamp block as JSON
  python3 lib/wur/protocol.py probe-id RUN_ID K -> WURP-xxxxxxxx-00K
  python3 lib/wur/protocol.py probe RUN_ID K    -> the rendered probe text
  python3 lib/wur/protocol.py verify            -> exits 1 if a frozen string moved
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# ── version ──────────────────────────────────────────────────────────────────
PROTOCOL_VERSION = "wur-probe-v1"

# ── frozen text (IMPLEMENTATION.md §6.2, verbatim) ───────────────────────────
PACING_PROMPT = (
    "TOOL-CALL PACING (operator constraint for this instrumented session): emit AT MOST ONE "
    "tool call per assistant message. Never issue two or more tool calls in the same message; "
    "make one call, wait for its result, then decide the next one. This is a measurement "
    "requirement of the harness running this session, not a limitation of the task."
)

PROBE_TEXT = (
    "CHECKPOINT {probe_id}. Pause the task for one message and answer this.\n\n"
    "List the 3 facts currently active in your workspace. For each: the fact, its source, "
    "and whether it affects your next action.\n\n"
    "Reply with ONE fenced code block tagged json and nothing else inside the fence:\n"
    "```json\n"
    '{{"probe_id":"{probe_id}","facts":['
    '{{"fact":"<one sentence>","source":"<file path | tool call | task prompt>","affects_next_action":true}},'
    '{{"fact":"...","source":"...","affects_next_action":false}},'
    '{{"fact":"...","source":"...","affects_next_action":false}}],'
    '"next_action":"<the single next thing you will do>"}}\n'
    "```\n"
    'Exactly 3 entries in "facts". If fewer than 3 are genuinely active, use the literal string '
    '"NONE" for that entry\'s "fact" and "source". Do not invent a fact or a source. '
    "Do not report facts about this checkpoint mechanism. "
    "After the block, resume the task where you left off without waiting for me."
)

# ── frozen text (authored here; §6.2 names them but does not quote them) ─────
# Sent on the stream-json user channel — the only channel measured to be trusted
# (V2: 13 sent / 13 answered / 0 refusals). Carries {probe_id} so the re-ask is
# attributable to the same probe as the original.
RETRY_TEXT = (
    "CHECKPOINT {probe_id} — that reply did not contain a parseable JSON block. Send the "
    "checkpoint answer once more, as ONE fenced code block tagged json and nothing else "
    "inside the fence, with exactly the keys probe_id, facts (exactly 3 entries, each with "
    "fact, source and affects_next_action) and next_action. Do not put commentary inside "
    "the fence. After the block, resume the task where you left off without waiting for me."
)

# Sent on ANY zero-tool-call result, capped at 3 consecutive, and sent IDENTICALLY
# in the no-probe arms (§6.2) — so it must never mention the probe mechanism, or it
# would become a probed-arm-only intervention.
RESUME_TEXT = (
    "Continue the task from where you left off. Do not wait for further instructions: take "
    "the next action yourself, one tool call per message, until the task is complete."
)

# The ONLY hook-authored text that ever reaches the model (§5.1(1)): the deny reason
# on a PreToolUse denial after the step budget is spent. UNVALIDATED wording (V14).
BUDGET_STOP_TEXT = (
    "Step budget for this instrumented session is exhausted; no further tool calls will be "
    "permitted. Stop calling tools and reply with a short summary of the work completed so far."
)

FROZEN_STRINGS: dict[str, str] = {
    "protocol_version": PROTOCOL_VERSION,
    "pacing_prompt": PACING_PROMPT,
    "probe_text": PROBE_TEXT,
    "retry_text": RETRY_TEXT,
    "resume_text": RESUME_TEXT,
    "budget_stop_text": BUDGET_STOP_TEXT,
}

# Measured in SPIKES.md V4 (338 chars, no trailing newline) against the runs that
# established the pacing invariant. A mismatch means the prompt was edited and no
# historical run is comparable to a new one.
PACING_PROMPT_SHA256_MEASURED = "0a687ddfc2f3374378188c2aacde2b5f5d2d97504a63e27dc88a8b9cfcbe249b"


# ── hashing ──────────────────────────────────────────────────────────────────
def sha256(text: str | bytes) -> str:
    """Hex sha256 of UTF-8 text (or raw bytes). The stamp used everywhere."""
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


PACING_PROMPT_SHA256 = sha256(PACING_PROMPT)
# NOTE: probe_text_sha256 hashes the TEMPLATE, with {probe_id} unresolved. The
# rendered instance differs per probe by construction; the template is what must
# be constant across runs.
PROBE_TEXT_SHA256 = sha256(PROBE_TEXT)


def frozen_hashes() -> dict[str, str]:
    """The run_meta.json stamp block for everything this module owns."""
    out: dict[str, str] = {"protocol_version": PROTOCOL_VERSION}
    for name, text in FROZEN_STRINGS.items():
        if name == "protocol_version":
            continue
        out[f"{name}_sha256"] = sha256(text)
    return out


def verify_frozen() -> list[str]:
    """Return a list of problems with the frozen strings; empty means intact.

    Called by preflight (fail-closed, no API call). Deliberately NOT an
    import-time assertion: gate.py runs inside a hook and hooks must always
    exit 0 (§5.1(1)).
    """
    problems: list[str] = []
    if PACING_PROMPT_SHA256 != PACING_PROMPT_SHA256_MEASURED:
        problems.append(
            f"PACING_PROMPT sha256 {PACING_PROMPT_SHA256} != measured "
            f"{PACING_PROMPT_SHA256_MEASURED} (SPIKES.md V4)"
        )
    if len(PACING_PROMPT) != 338:
        problems.append(f"PACING_PROMPT is {len(PACING_PROMPT)} chars, measured 338")
    for clause in (
        "Do not report facts about this checkpoint mechanism.",
        "resume the task where you left off without waiting for me",
    ):
        if clause not in PROBE_TEXT:
            problems.append(f"PROBE_TEXT lost a load-bearing clause: {clause!r}")
    if "{probe_id}" not in PROBE_TEXT or "{probe_id}" not in RETRY_TEXT:
        problems.append("probe templates must carry a {probe_id} placeholder")
    for banned in ("CHECKPOINT", "probe", "checkpoint"):
        if banned in RESUME_TEXT:
            problems.append(
                f"RESUME_TEXT mentions {banned!r}; it is sent identically in the "
                "no-probe arms and must stay probe-agnostic (§6.2)"
            )
    return problems


# ── probe ids ────────────────────────────────────────────────────────────────
PROBE_ID_PREFIX = "WURP-"
PROBE_ID_RE = re.compile(r"WURP-[0-9a-f]{8}-\d{3}")
MAX_PROBE_ORDINAL = 999


def probe_id(run_id: str, k: int) -> str:
    """`WURP-<sha256(run_id)[:8]>-<k:03d>` (§6.2).

    Cannot occur in a repo, and is echoed back inside the answer, so
    answer<->probe attribution is exact rather than positional.
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    k = int(k)
    if k < 0 or k > MAX_PROBE_ORDINAL:
        raise ValueError(f"probe ordinal out of range: {k}")
    return f"{PROBE_ID_PREFIX}{sha256(run_id)[:8]}-{k:03d}"


def run_token(run_id: str) -> str:
    """The 8-hex run component shared by every probe_id of a run."""
    return sha256(run_id)[:8]


def find_probe_ids(text: str) -> list[str]:
    """Every probe id occurring in `text`, in order, deduplicated.

    events.py keys `is_probe_turn` off the replayed probe_id text, never off
    position (the probe is replayed as a `user` block immediately AFTER the
    barriered call's tool_result — §6.2).
    """
    seen: list[str] = []
    for m in PROBE_ID_RE.finditer(text or ""):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    return seen


def render_probe(pid: str) -> str:
    """The exact bytes sent on stdin for probe `pid`."""
    return PROBE_TEXT.format(probe_id=pid)


def render_retry(pid: str) -> str:
    """The exact bytes sent on stdin when probe `pid` failed to parse."""
    return RETRY_TEXT.format(probe_id=pid)


# ── the answer contract (mirrors schemas/probe_answer.schema.json) ───────────
N_SLOTS = 3
NONE_LITERAL = "NONE"
ANSWER_REQUIRED_KEYS = ("probe_id", "facts", "next_action")
SLOT_REQUIRED_KEYS = ("fact", "source", "affects_next_action")
PARSE_TIERS = ("strict", "lenient", "failed")


def strict_answer_errors(obj: Any) -> list[str]:
    """Validate a decoded answer object exactly as probe_answer.schema.json does.

    Kept in Python (not jsonschema) because this runs inside the driver, and the
    system python3 has no jsonschema. Any change here must change that schema too.
    """
    errs: list[str] = []
    if not isinstance(obj, dict):
        return [f"answer is {type(obj).__name__}, expected object"]
    for key in ANSWER_REQUIRED_KEYS:
        if key not in obj:
            errs.append(f"missing key {key!r}")
    extra = sorted(set(obj) - set(ANSWER_REQUIRED_KEYS))
    if extra:
        errs.append(f"unexpected key(s): {extra}")
    pid = obj.get("probe_id")
    if not isinstance(pid, str) or not PROBE_ID_RE.fullmatch(pid):
        errs.append(f"probe_id is not a well-formed probe id: {pid!r}")
    if not isinstance(obj.get("next_action"), str):
        errs.append("next_action must be a string")
    facts = obj.get("facts")
    if not isinstance(facts, list):
        errs.append("facts must be an array")
        return errs
    if len(facts) != N_SLOTS:
        errs.append(f"facts has {len(facts)} entries, expected {N_SLOTS}")
    for i, f in enumerate(facts):
        if not isinstance(f, dict):
            errs.append(f"facts[{i}] is not an object")
            continue
        for key in SLOT_REQUIRED_KEYS:
            if key not in f:
                errs.append(f"facts[{i}] missing {key!r}")
        f_extra = sorted(set(f) - set(SLOT_REQUIRED_KEYS))
        if f_extra:
            errs.append(f"facts[{i}] unexpected key(s): {f_extra}")
        if "fact" in f and not isinstance(f["fact"], str):
            errs.append(f"facts[{i}].fact must be a string")
        if "source" in f and not isinstance(f["source"], str):
            errs.append(f"facts[{i}].source must be a string")
        if "affects_next_action" in f and not isinstance(f["affects_next_action"], bool):
            errs.append(f"facts[{i}].affects_next_action must be a boolean")
    return errs


@dataclass(frozen=True)
class Slot:
    """One of the three answer slots."""

    slot_idx: int
    fact: str
    source: str
    affects_next_action: bool | None = None

    def to_dict(self) -> dict:
        return {
            "slot_idx": self.slot_idx,
            "fact": self.fact,
            "source": self.source,
            "affects_next_action": self.affects_next_action,
        }

    @property
    def is_empty(self) -> bool:
        return _is_empty_text(self.fact) and _is_empty_text(self.source)


@dataclass(frozen=True)
class ParsedAnswer:
    """Result of the parse ladder. `raw` is never discarded by the caller."""

    parse_ok: bool
    parse_tier: str
    probe_id: str | None
    slots: tuple[Slot, ...]
    next_action: str | None
    errors: tuple[str, ...] = ()
    payload: dict | None = None

    def to_dict(self) -> dict:
        """The parse half of a probes.jsonl row.

        `slots` here are UN-annotated (no slot_class / match_* / source_verified),
        so probes.py must overwrite them with annotate_slots() before the row
        validates against schemas/probes.schema.json.
        """
        return {
            "parse_ok": self.parse_ok,
            "parse_tier": self.parse_tier,
            "echoed_probe_id": self.probe_id,
            "slots": [s.to_dict() for s in self.slots],
            "next_action": self.next_action,
            "parse_errors": list(self.errors),
        }


_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)


def _fences(text: str) -> list[tuple[str, str]]:
    return [(m.group(1).strip().lower(), m.group(2)) for m in _FENCE_RE.finditer(text)]


def strip_fences(text: str) -> str:
    """`text` with every fenced block removed — the model's prose, alone."""
    return _FENCE_RE.sub(" ", text or "")


def _balanced_objects(text: str) -> list[str]:
    """Every top-level {...} substring, brace-balanced outside of strings."""
    out, depth, start, in_str, esc = [], 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    out.append(text[start : i + 1])
                    start = -1
    return out


_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_SMART_QUOTES = {"“": '"', "”": '"', "‘": "'", "’": "'"}


def _repair(blob: str) -> str:
    for bad, good in _SMART_QUOTES.items():
        blob = blob.replace(bad, good)
    blob = _TRAILING_COMMA_RE.sub(r"\1", blob)
    blob = re.sub(r"\bTrue\b", "true", blob)
    blob = re.sub(r"\bFalse\b", "false", blob)
    blob = re.sub(r"\bNone\b", "null", blob)
    return blob


def _loads(blob: str) -> Any:
    try:
        return json.loads(blob)
    except Exception:
        pass
    try:
        return json.loads(_repair(blob))
    except Exception:
        return None


def _as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "y", "1"):
            return True
        if s in ("false", "no", "n", "0"):
            return False
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    return None


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _slots_from(facts: Any) -> list[Slot]:
    """Coerce anything list-shaped into exactly N_SLOTS slots (padded, truncated)."""
    slots: list[Slot] = []
    items = facts if isinstance(facts, list) else []
    for i, f in enumerate(items[:N_SLOTS]):
        if isinstance(f, dict):
            slots.append(
                Slot(
                    slot_idx=i,
                    fact=_as_text(f.get("fact", f.get("Fact", ""))).strip(),
                    source=_as_text(f.get("source", f.get("Source", ""))).strip(),
                    affects_next_action=_as_bool(
                        f.get("affects_next_action", f.get("affectsNextAction"))
                    ),
                )
            )
        else:
            slots.append(Slot(slot_idx=i, fact=_as_text(f).strip(), source="", affects_next_action=None))
    while len(slots) < N_SLOTS:
        slots.append(Slot(slot_idx=len(slots), fact="", source="", affects_next_action=None))
    return slots


def parse_answer(raw: str, expect_probe_id: str | None = None) -> ParsedAnswer:
    """The parse ladder: strict fenced json -> lenient -> failed.

    strict  exactly one ```json fence, whose body is pure JSON and satisfies
            probe_answer.schema.json (3 slots, well-formed probe_id, next_action).
    lenient any fence, or any brace-balanced object in the prose, that decodes
            (after a conservative repair pass) into something with `facts`;
            slots are padded/truncated to exactly 3.
    failed   nothing decodable — parse_ok is False and slots is empty.

    A probe_id that does not match `expect_probe_id` is recorded as an error but
    does NOT demote the tier: attribution is reported, not silently repaired.
    """
    text = raw or ""
    json_fences = [body for tag, body in _fences(text) if tag.startswith("json")]

    # ── strict ──
    if len(json_fences) == 1:
        obj = None
        try:
            obj = json.loads(json_fences[0])
        except Exception:
            obj = None
        if obj is not None:
            errs = strict_answer_errors(obj)
            if not errs:
                pid = obj.get("probe_id")
                mismatch = _probe_id_mismatch(pid, expect_probe_id)
                return ParsedAnswer(
                    parse_ok=True,
                    parse_tier="strict",
                    probe_id=pid,
                    slots=tuple(_slots_from(obj.get("facts"))),
                    next_action=_as_text(obj.get("next_action")),
                    errors=tuple(mismatch),
                    payload=obj,
                )

    # ── lenient ──
    candidates: list[str] = [body for _tag, body in _fences(text)]
    candidates.extend(_balanced_objects(text))
    for blob in candidates:
        obj = _loads(blob)
        if not isinstance(obj, dict):
            continue
        if "facts" not in obj and "Facts" not in obj:
            continue
        facts = obj.get("facts", obj.get("Facts"))
        pid = obj.get("probe_id") or obj.get("probeId")
        pid = pid if isinstance(pid, str) else None
        errs = strict_answer_errors(obj)
        errs.extend(_probe_id_mismatch(pid, expect_probe_id))
        return ParsedAnswer(
            parse_ok=True,
            parse_tier="lenient",
            probe_id=pid,
            slots=tuple(_slots_from(facts)),
            next_action=_as_text(obj.get("next_action", obj.get("nextAction"))),
            errors=tuple(errs),
            payload=obj,
        )

    # ── failed ──
    return ParsedAnswer(
        parse_ok=False,
        parse_tier="failed",
        probe_id=(find_probe_ids(text) or [None])[0],
        slots=(),
        next_action=None,
        errors=("no decodable json object in the reply",),
        payload=None,
    )


def _probe_id_mismatch(pid: Any, expect: str | None) -> list[str]:
    if expect is None or pid is None:
        return []
    return [] if pid == expect else [f"probe_id mismatch: echoed {pid!r}, expected {expect!r}"]


# ── refusal detection ────────────────────────────────────────────────────────
# V1: probe text delivered through a hook is refused as prompt injection 6/6, and
# the refusal propagates into result.result. The stream-json user channel is not
# expected to produce these — a hit means the trusted channel broke (pilot gate
# `probe refused == 0`).
_REFUSAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pat, re.IGNORECASE))
    for name, pat in (
        ("prompt_injection", r"\bprompt[- ]injection\b"),
        ("injected", r"\binject(ed|ion)\s+(instruction|prompt|probe|message|text|checkpoint)"),
        ("not_complying", r"\bnot\s+(going\s+to\s+)?compl(y|ying)\b"),
        ("refuse", r"\bI\s*(?:'m|am|will)?\s*(?:going to\s+)?refus(e|ing)\b"),
        ("disregard", r"\bdisregard(ing)?\s+(it|this|that|the)\b"),
        ("flagging", r"\bflag(ging)?\s+(it|this)\b"),
        ("not_from_you", r"\b(did\s*n[o']t|wasn'?t|was not)\s+come\s+from\s+you\b"),
        ("fake_checkpoint", r"\bfake\s+\"?checkpoint\"?\b"),
        ("hallmarks", r"\bhallmarks of an? (injected|embedded)\b"),
        ("ignoring_embedded", r"\bignor(e|ing)\s+(the\s+)?embedded\s+instruction"),
        ("not_part_of_request", r"\b(this|that)\s+(wasn'?t|was not|is not|isn'?t)\s+part of "
                               r"(your|the)\s+(original\s+)?(request|task)\b"),
    )
)


def refusal_markers(text: str) -> list[str]:
    """Names of the refusal patterns matching `text` outside fenced blocks."""
    prose = strip_fences(text or "")
    return [name for name, pat in _REFUSAL_PATTERNS if pat.search(prose)]


def looks_like_refusal(text: str) -> bool:
    """True when the reply reads as a refusal of the probe rather than an answer.

    Matched on prose only (fenced blocks are stripped) so a slot that quotes a
    refusal-shaped sentence cannot flip the classification. Callers apply the
    ladder answered -> refused -> unanswered: a reply that parses is `answered`
    even if the prose around it grumbles.
    """
    return bool(refusal_markers(text))


# ── slot classification ──────────────────────────────────────────────────────
SLOT_CLASSES = (
    "critical_fact",
    "task_restatement",
    "generic_workspace",
    "filler",
    "empty",
    "distrusted",
)

# match_form is ordered strongest-first. Only `exact` inbound hits set
# first_exposure_seq (§4.5); mention tier (a) accepts exact + lower (the tier is
# defined case-insensitively and whitespace-normalized); `nohyphen` is recorded
# but is NOT tier (a) — a hyphen-stripped nonce is usually the agent's own text
# being re-read.
MATCH_FORMS = ("exact", "lower", "nohyphen", "regex")
MENTION_TIER_A_FORMS = ("exact", "lower")
EXPOSURE_FIRST_FORMS = ("exact",)

_EMPTY_TOKENS = {
    "", "none", "n/a", "na", "null", "nil", "-", "--", "—", "unknown",
    "<none>", "no fact", "nothing", "(none)", "none.",
}

_MECHANISM_RE = re.compile(
    r"(checkpoint|WURP-[0-9a-f]{8}|probe\b|pacing|instrumented session|operator constraint|"
    r"measurement requirement|one tool call per|harness|tool[- ]call pacing|"
    r"this (mechanism|session's? instrumentation))",
    re.IGNORECASE,
)
_TEMPLATE_ECHO_RE = re.compile(
    r"(<one sentence>|<file path|<the single next thing|^\.\.\.$)", re.IGNORECASE
)
_PATHY_RE = re.compile(r"(^|[\s\"'(])[\w.-]*/[\w./-]+|\b[\w-]+\.(md|py|txt|json|ya?ml|toml|cfg|ini|sh)\b")
_GENERIC_TERMS = {
    "repo", "repository", "workspace", "codebase", "directory", "folder", "file", "files",
    "test", "tests", "pytest", "readme", "notes", "docs", "documentation", "git", "commit",
    "branch", "module", "package", "function", "class", "script", "config", "configuration",
    "venv", "dependencies", "requirements", "task", "prompt",
}
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "that", "this", "these",
    "those", "is", "are", "was", "were", "be", "been", "being", "to", "of", "in", "on",
    "for", "with", "as", "by", "at", "from", "it", "its", "must", "should", "will",
    "not", "no", "you", "your", "i", "my", "we", "our", "they", "their", "there",
}
TASK_RESTATEMENT_MIN_OVERLAP = 0.60
TASK_RESTATEMENT_MIN_TOKENS = 3


def _is_empty_text(s: str) -> bool:
    return (s or "").strip().strip(".").lower() in _EMPTY_TOKENS


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _strip_sep(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", (s or "").lower())


def _content_tokens(s: str) -> set[str]:
    toks = re.findall(r"[a-z0-9]{3,}", (s or "").lower())
    return {t for t in toks if t not in _STOPWORDS}


@dataclass(frozen=True)
class FactCard:
    """The frozen identity of one planted fact, as facts.yaml carries it.

    `regexes` are the tier-(b) paraphrase patterns; §4.5 requires them frozen in
    facts.yaml before any data is collected.
    """

    fact_id: str
    nonce: str | None = None
    surface_forms: tuple[str, ...] = ()
    regexes: tuple[str, ...] = ()
    gist: str = ""
    distractor_tokens: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict) -> "FactCard":
        return cls(
            fact_id=str(d.get("fact_id") or d.get("id") or ""),
            nonce=d.get("nonce"),
            surface_forms=tuple(d.get("surface_forms") or ()),
            regexes=tuple(d.get("regexes") or d.get("paraphrase_regexes") or ()),
            gist=str(d.get("gist") or ""),
            distractor_tokens=tuple(d.get("distractor_tokens") or d.get("distractors") or ()),
        )

    def compiled_regexes(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.regexes]


def match_nonce_form(text: str, nonce: str | None) -> str | None:
    """Strongest match form of `nonce` in `text`: exact | lower | nohyphen | None."""
    if not nonce or not text:
        return None
    hay = _norm_ws(text)
    needle = _norm_ws(nonce)
    if needle and needle in hay:
        return "exact"
    if needle and needle.lower() in hay.lower():
        return "lower"
    if needle and _strip_sep(needle) and _strip_sep(needle) in _strip_sep(hay):
        return "nohyphen"
    return None


def match_slot(slot: Slot | dict, card: FactCard | None) -> dict:
    """Tier (a)/(b) match of one slot against a fact card (§4.5).

    Returns {match_nonce, match_regex, match_form, matched_regex}. Tier (c),
    `match_llm`, is adjudicated elsewhere and is always None here.
    """
    s = _as_slot(slot)
    blob = f"{s.fact}\n{s.source}"
    out = {"match_nonce": False, "match_regex": False, "match_form": None, "matched_regex": None}
    if card is None:
        return out
    form = match_nonce_form(blob, card.nonce)
    if form is None:
        for sf in card.surface_forms:
            form = match_nonce_form(blob, sf)
            if form:
                break
    if form is not None:
        out["match_form"] = form
        out["match_nonce"] = form in MENTION_TIER_A_FORMS
    for pat, src in zip(card.compiled_regexes(), card.regexes):
        if pat.search(blob):
            out["match_regex"] = True
            out["matched_regex"] = src
            if out["match_form"] is None:
                out["match_form"] = "regex"
            break
    return out


def _as_slot(slot: Slot | dict, idx: int = 0) -> Slot:
    if isinstance(slot, Slot):
        return slot
    return Slot(
        slot_idx=int(slot.get("slot_idx", idx)),
        fact=_as_text(slot.get("fact")).strip(),
        source=_as_text(slot.get("source")).strip(),
        affects_next_action=_as_bool(slot.get("affects_next_action")),
    )


def classify_slot(
    slot: Slot | dict,
    card: FactCard | None = None,
    *,
    task_text: str = "",
    probe_id_text: str = "",
) -> str:
    """One of SLOT_CLASSES, by a fixed precedence.

        empty            -> the slot is NONE / blank in both fact and source
        critical_fact    -> tier (a) or tier (b) match against the fact card
        distrusted       -> the slot reports the checkpoint mechanism itself, or
                            echoes the probe template / probe_id (§6.2: without
                            the "do not report facts about this checkpoint
                            mechanism" clause the harness ate a fact slot)
        task_restatement -> mostly the task prompt, restated
        generic_workspace-> names a workspace artifact (a path, a filename, a
                            generic repo noun) but not the fact
        filler           -> anything else

    HEURISTIC, pending D7: 200 pilot slots are hand-labelled before
    slots_distrusted / slots_filler are reported. `raw_response` is retained
    verbatim, so a re-classification never needs new runs.
    """
    s = _as_slot(slot)
    if _is_empty_text(s.fact):
        return "empty"

    m = match_slot(s, card)
    if m["match_nonce"] or m["match_regex"] or m["match_form"] is not None:
        return "critical_fact"

    blob = f"{s.fact}\n{s.source}"
    if probe_id_text and probe_id_text in blob:
        return "distrusted"
    if find_probe_ids(blob):
        return "distrusted"
    if _MECHANISM_RE.search(blob) or _TEMPLATE_ECHO_RE.search(blob.strip()):
        return "distrusted"

    if task_text:
        st, tt = _content_tokens(s.fact), _content_tokens(task_text)
        if st:
            shared = st & tt
            if len(shared) >= TASK_RESTATEMENT_MIN_TOKENS and (
                len(shared) / len(st) >= TASK_RESTATEMENT_MIN_OVERLAP
            ):
                return "task_restatement"

    if _PATHY_RE.search(blob) or (_content_tokens(blob) & _GENERIC_TERMS):
        return "generic_workspace"
    return "filler"


def classify_slots(
    slots: Sequence[Slot | dict],
    card: FactCard | None = None,
    *,
    task_text: str = "",
    probe_id_text: str = "",
) -> list[str]:
    """classify_slot() over an answer's slots, in order."""
    return [
        classify_slot(_as_slot(s, i), card, task_text=task_text, probe_id_text=probe_id_text)
        for i, s in enumerate(slots)
    ]


def annotate_slots(
    slots: Sequence[Slot | dict],
    card: FactCard | None = None,
    *,
    task_text: str = "",
    probe_id_text: str = "",
    known_sources: Iterable[str] | None = None,
) -> list[dict]:
    """Schema-shaped slot rows for probes.jsonl (schemas/probes.schema.json).

    `known_sources` is the set of paths that exist in the run's workspace; when
    supplied, `source_verified` is True/False, otherwise None (not adjudicated).
    `match_llm` is always None here — tier (c) is a separate, pre-registered pass.
    """
    known = {str(p) for p in known_sources} if known_sources is not None else None
    out: list[dict] = []
    for i, raw in enumerate(slots):
        s = _as_slot(raw, i)
        m = match_slot(s, card)
        row = s.to_dict()
        row.update(
            {
                "slot_class": classify_slot(
                    s, card, task_text=task_text, probe_id_text=probe_id_text
                ),
                "match_nonce": bool(m["match_nonce"]),
                "match_regex": bool(m["match_regex"]),
                "match_form": m["match_form"],
                "matched_regex": m["matched_regex"],
                "match_llm": None,
                "source_verified": None if known is None else _source_verified(s.source, known),
            }
        )
        out.append(row)
    return out


def _source_verified(source: str, known: set[str]) -> bool:
    src = (source or "").strip().strip("`'\"")
    if not src or _is_empty_text(src):
        return False
    if src in known:
        return True
    tail = src.lstrip("./")
    return any(k == tail or k.endswith("/" + tail) or k.endswith(tail) for k in known)


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="frozen WUR probe protocol")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hashes", help="print the run_meta stamp block as JSON")
    sub.add_parser("verify", help="check the frozen strings against their measured hashes")
    pid = sub.add_parser("probe-id")
    pid.add_argument("run_id")
    pid.add_argument("k", type=int)
    pr = sub.add_parser("probe", help="render the probe text for RUN_ID/K")
    pr.add_argument("run_id")
    pr.add_argument("k", type=int)
    args = p.parse_args(argv)

    if args.cmd == "hashes":
        print(json.dumps(frozen_hashes(), indent=2, sort_keys=True))
        return 0
    if args.cmd == "verify":
        problems = verify_frozen()
        for prob in problems:
            print(f"FROZEN-STRING DRIFT: {prob}", file=sys.stderr)
        print("ok" if not problems else "drift")
        return 1 if problems else 0
    if args.cmd == "probe-id":
        print(probe_id(args.run_id, args.k))
        return 0
    print(render_probe(probe_id(args.run_id, args.k)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
