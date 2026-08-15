#!/usr/bin/env python3
"""
render.py — one canonical fact, three presentations, identical content.

RESPONSIBILITY
  Turn ONE CanonicalFact into prose / checklist / table renderings whose
  *content is byte-identical modulo layout*, so the format contrast
  (`d2` vs `d2-check` vs `d2-table`) varies presentation and nothing else. If the
  three renderings said even slightly different things, "format sensitivity"
 would be measuring content, and the answer to "prose, checklist,
  or table?" would be an artifact of whoever wrote the fixture.

  Also owns the two length disciplines the measurement depends on:

    * BYTE CAP — every fact-bearing file is capped at MAX_PLANT_BYTES = 20,000.
      V16/S4 measured that `Read` truncates SILENTLY below the 256 KB ceiling,
      with a content-dependent cut point as low as 21,600 bytes and NO
      model-visible marker. A fact past the cut looks like "read but not used"
      when it is in fact "never exposed".
    * LINE CAP — MAX_PLANT_LINES = 200, kept as a structural discipline only.
      It is explicitly NOT the mitigation: a 200-byte-line file was cut at line
      108, so a line cap measurably mitigates nothing.
      The byte cap is the one that binds.

INPUTS
  CanonicalFact (fact_id, title, statement, clauses, nonce) + a format name,
  optional Distractors (the `d2-dist` arm), and for the control arms an optional
  hand-authored control block.

OUTPUTS
  Markdown documents (str) for NOTES.md and CLAUDE.md, deterministic filler for
  the skeleton directories, and length/content reports:
    length_report(text)                -> {bytes, chars, lines, words, sha256}
    render_report(fact, texts)         -> per-format lengths + word-count balance
  Every assert_* helper RAISES on failure; there is no soft mode, because a
  silently over-long or content-drifted plant is unrecoverable after the run.

DETERMINISM
  No clock, no RNG, no environment. The same fact renders to the same bytes
  forever, which is what makes plant.py's overlay_sha256 a meaningful stamp.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # flat import (lib/wur on sys.path, e.g. run by path)
    sys.path.insert(0, str(_HERE))

try:  # package import shares one module object with the rest of lib/wur
    from . import nonce as nonce_mod
except ImportError:  # pragma: no cover - exercised when run as a script
    import nonce as nonce_mod  # type: ignore[no-redef]

RENDERER_VERSION = "wur-render-v1"

# / V16. The byte cap binds; the line cap is discipline, not mitigation.
MAX_PLANT_BYTES = 20_000
MAX_PLANT_LINES = 200

FORMATS = ("prose", "checklist", "table")
POINTER_REGIMES = ("none", "prose", "import")
PROSE_WIDTH = 88

# Spread tolerated between the three renderings before assert_length_balanced()
# complains. CONTENT words (markdown scaffolding stripped) should be near
# identical — that is the whole claim the format contrast rests on — so the
# content tolerance is tight. Raw words legitimately differ: a pipe table costs
# more tokens than a paragraph, which is a real property of the arm and is
# reported rather than suppressed.
DEFAULT_LENGTH_TOL = 0.10
DEFAULT_RAW_LENGTH_TOL = 0.50


class RenderError(Exception):
    """Base class for every failure this module raises."""


class RenderTooLong(RenderError):
    """A planted file breached MAX_PLANT_BYTES or MAX_PLANT_LINES."""


class RenderDrift(RenderError):
    """The three renderings do not carry identical content."""


class ControlContaminated(RenderError):
    """A control document carries the nonce, a tier-(b) regex hit, or a distractor."""


# ── the canonical fact ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class Clause:
    """One label/text pair. The unit that is held constant across formats."""

    label: str
    text: str

    def __post_init__(self) -> None:
        for name, v in (("label", self.label), ("text", self.text)):
            if not str(v).strip():
                raise RenderError(f"clause {name} must be non-empty")
            if "|" in str(v):
                raise RenderError(
                    f"clause {name} may not contain '|': it would need escaping in the "
                    f"table rendering and the three formats would stop being identical"
                )
            if "\n" in str(v):
                raise RenderError(f"clause {name} must be a single line")

    def to_dict(self) -> dict:
        return {"label": self.label, "text": self.text}


@dataclass(frozen=True)
class Distractor:
    """A confusable-but-wrong convention, planted only in `d2-dist`."""

    token: str
    statement: str
    label: str = "Convention"

    def __post_init__(self) -> None:
        if not self.statement.strip():
            raise RenderError("distractor statement must be non-empty")
        if "|" in self.statement or "\n" in self.statement:
            raise RenderError("distractor statement must be one line and contain no '|'")

    def to_dict(self) -> dict:
        return {"token": self.token, "statement": self.statement, "label": self.label}


@dataclass(frozen=True)
class CanonicalFact:
    """The single source every rendering is produced from.

    `statement` is the mandate in one sentence and is where the nonce normally
    lives; `clauses` are the supporting label/text pairs. Nothing else may vary
    between formats, so nothing else is stored here.
    """

    fact_id: str
    title: str
    statement: str
    clauses: tuple[Clause, ...] = ()
    nonce: str | None = None
    task_id: str | None = None
    bucket: str = "constraint"
    distractors: tuple[Distractor, ...] = ()

    def __post_init__(self) -> None:
        if not self.fact_id:
            raise RenderError("fact_id must be non-empty")
        for name, v in (("title", self.title), ("statement", self.statement)):
            if not str(v).strip():
                raise RenderError(f"{name} must be non-empty")
            if "\n" in str(v):
                raise RenderError(f"{name} must be a single line")
        if "|" in self.statement:
            raise RenderError("statement may not contain '|' (see Clause.__post_init__)")
        if self.nonce and self.nonce not in self.statement and not any(
            self.nonce in c.text for c in self.clauses
        ):
            raise RenderError(
                f"nonce {self.nonce!r} appears in neither the statement nor any clause of "
                f"{self.fact_id!r}: the plant would be unfindable and `available` would be false"
            )

    @property
    def is_control(self) -> bool:
        return self.nonce is None

    def to_dict(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "title": self.title,
            "statement": self.statement,
            "clauses": [c.to_dict() for c in self.clauses],
            "nonce": self.nonce,
            "task_id": self.task_id,
            "bucket": self.bucket,
            "distractors": [d.to_dict() for d in self.distractors],
        }


# ── renderers ────────────────────────────────────────────────────────────────
def _para(sentences: Iterable[str]) -> str:
    body = " ".join(s for s in sentences if s)
    return textwrap.fill(body, width=PROSE_WIDTH, break_long_words=False, break_on_hyphens=False)




def _sentence(text: str) -> str:
    t = text.strip()
    return t if t.endswith((".", "!", "?", ":")) else t + "."


def _render_prose(fact: CanonicalFact, distractors: Sequence[Distractor]) -> list[str]:
    out = [f"# {fact.title}", "", _para([_sentence(fact.statement)])]
    if fact.clauses:
        out += ["", _para(_sentence(f"{c.label}: {c.text}") for c in fact.clauses)]
    if distractors:
        out += ["", "## Related conventions", ""]
        out.append(_para(_sentence(f"{d.label}: {d.statement}") for d in distractors))
    return out


def _render_checklist(fact: CanonicalFact, distractors: Sequence[Distractor]) -> list[str]:
    out = [f"# {fact.title}", "", _sentence(fact.statement), ""]
    for c in fact.clauses:
        out.append(f"- [ ] {c.label}: {c.text}")
    if distractors:
        out += ["", "## Related conventions", ""]
        for d in distractors:
            out.append(f"- [ ] {d.label}: {d.statement}")
    return out


def _render_table(fact: CanonicalFact, distractors: Sequence[Distractor]) -> list[str]:
    out = [f"# {fact.title}", "", _sentence(fact.statement), "", "| Field | Value |", "| --- | --- |"]
    for c in fact.clauses:
        out.append(f"| {c.label} | {c.text} |")
    if distractors:
        out += ["", "## Related conventions", "", "| Field | Value |", "| --- | --- |"]
        for d in distractors:
            out.append(f"| {d.label} | {d.statement} |")
    return out


_RENDERERS = {
    "prose": _render_prose,
    "checklist": _render_checklist,
    "table": _render_table,
}


def render(
    fact: CanonicalFact,
    fmt: str = "prose",
    *,
    distractors: Sequence[Distractor] | None = None,
    check_caps: bool = True,
) -> str:
    """Render `fact` as `fmt`. The returned text is the whole NOTES.md body.

    Distractors default to the fact's own list; pass an explicit sequence (or
    `()`) to override, which is how plant.py gives `d2-dist` three of them and
    every other arm zero.
    """
    if fmt not in _RENDERERS:
        raise RenderError(f"unknown format {fmt!r}; expected one of {FORMATS}")
    ds = tuple(fact.distractors if distractors is None else distractors)
    lines = _RENDERERS[fmt](fact, ds)
    text = "\n".join(lines).rstrip("\n") + "\n"
    if fact.nonce and fact.nonce not in text:
        raise RenderError(
            f"{fmt} rendering of {fact.fact_id!r} lost the nonce — the plant would be unfindable"
        )
    if check_caps:
        assert_within_caps(text, where=f"{fact.fact_id}:{fmt}")
    return text


def render_all(
    fact: CanonicalFact,
    *,
    distractors: Sequence[Distractor] | None = None,
    check_caps: bool = True,
) -> dict[str, str]:
    """All three formats of one fact, keyed by format name."""
    return {
        f: render(fact, f, distractors=distractors, check_caps=check_caps) for f in FORMATS
    }


# ── the CLAUDE.md carriers (d0-push and d1-ptr) ─────────────────────────
IMPORT_STUB_TITLE = "Project notes"
POINTER_TITLE = "Project notes"


def render_claude_md(regime: str, notes_rel: str) -> str:
    """The CLAUDE.md that accompanies a NOTES.md, per pointer regime.

    `import`  an `@NOTES.md` stub. This is the PUSH mechanism: Claude Code
              resolves the import and the content is auto-loaded, appearing in
              neither stream-json nor the on-disk transcript — which is why
              `d0-push` exposure is ASSERTED from the manifest and verified
              out-of-band by the autoload canary.
    `prose`   a pointer with no import: it names the file and says it matters,
              and deliberately carries NO `@` reference, because an import stub
              is simultaneously a pointer AND a push, and `d1-ptr` exists
              precisely to split that confound.
    `none`    no CLAUDE.md at all.

    Neither variant may carry the fact itself — plant.py re-checks that with the
    nonce scanner, since a pointer that quotes the fact would push it.
    """
    if regime not in POINTER_REGIMES:
        raise RenderError(f"unknown pointer regime {regime!r}; expected one of {POINTER_REGIMES}")
    if regime == "none":
        raise RenderError("pointer regime 'none' renders no CLAUDE.md")
    if regime == "import":
        text = f"# {IMPORT_STUB_TITLE}\n\n@{notes_rel}\n"
    else:
        text = (
            f"# {POINTER_TITLE}\n\n"
            + _para(
                [
                    f"Working conventions for this repository are recorded in {notes_rel}.",
                    "Read that file before changing code in this repository; it records the",
                    "conventions this project expects contributors to follow.",
                ]
            )
            + "\n"
        )
        if re.search(r"(^|\s)@\S", text):
            raise RenderError("prose pointer must not contain an @import reference")
    assert_within_caps(text, where=f"CLAUDE.md:{regime}")
    return text


# ── skeleton filler (skeleton matching) ─────────────────────────────────
_FILLER_LINES = (
    "This directory is part of the repository's documentation tree.",
    "Files here describe how the code is organised; they do not change behaviour.",
    "Keep entries short and update them when the corresponding module moves.",
)


def render_filler(rel_path: str) -> str:
    """Deterministic, nonce-free filler so git tracks a skeleton directory.

: `docs/`, `docs/internal/` and `docs/internal/memory/` exist in EVERY
    arm including both controls. Git cannot track an empty directory, so each
    level carries one of these. The content is a pure function of the path, which
    means the filler is BYTE-IDENTICAL across arms — if it were not, the skeleton
    would itself be an uncontrolled treatment.
    """
    where = rel_path.rsplit("/", 1)[0] if "/" in rel_path else "."
    body = _para([f"Notes for `{where}`."] + list(_FILLER_LINES))
    text = f"# {where}\n\n{body}\n"
    assert_within_caps(text, where=rel_path)
    return text


# ── the control twin ( `ctrl`) ───────────────────────────────────────────
# Plausible, fact-free workspace conventions. Deliberately about house style
# rather than behaviour, so no `used` detector in the closed 6-predicate registry
# can fire on them — a control that trips a detector is a spurious fire, which is
# exactly the quantity Gate 2 is trying to bound at zero.
CONTROL_BANK: tuple[str, ...] = (
    "Keep module docstrings in the imperative mood, with a one-sentence summary on the first line.",
    "Prefer explicit imports over wildcard imports so the dependency graph stays readable.",
    "Name test files after the module they cover, so a failure points at one place.",
    "Group related helpers next to their caller rather than in a shared utilities module.",
    "Leave a blank line between logical sections of a long function.",
    "Write commit subjects in the present tense and keep them under seventy characters.",
    "Document a workaround with the reason it exists, not only what it does.",
    "Keep the public surface of a module small; helpers that only one caller uses stay private.",
    "When a constant is used twice, give it a name rather than repeating the literal.",
    "Prefer a small, named function over a comment explaining a block of inline code.",
    "Record follow-up work in the issue tracker rather than in a code comment.",
    "Keep line length consistent with the surrounding file rather than reformatting it.",
)
# Label/text pairs that cohere with each other. The control's clause LABELS are
# drawn from here rather than reused from the fact: "Marker: prefer explicit
# imports" is structurally matched and semantically incoherent, and an
# incoherent NOTES.md is its own treatment — it reads as a corrupted file, which
# is not what `ctrl` is supposed to hold constant.
CONTROL_CLAUSE_BANK: tuple[tuple[str, str], ...] = (
    ("Scope", "every module in this repository, including the tests and the helper scripts"),
    ("Imports", "prefer explicit imports; a wildcard import makes the dependency graph unreadable"),
    ("Naming", "test files are named after the module they cover so a failure points at one place"),
    ("Docstrings", "written in the imperative mood, with a one-sentence summary on the first line"),
    ("Comments", "a workaround is documented with the reason it exists, not only with what it does"),
    ("Commits", "commit subjects are written in the present tense and stay reasonably short"),
    ("Line length", "match the surrounding file rather than reformatting it in an unrelated change"),
    ("Follow-up", "recorded in the issue tracker rather than left behind in a code comment"),
    ("Helpers", "keep the public surface small; a helper used by one caller stays private"),
    ("Constants", "when a literal is used twice, give it a name rather than repeating the value"),
    ("Structure", "related helpers sit next to their caller rather than in a shared utility module"),
    ("Review", "a reviewer may ask for any of this before approving a change"),
)
CONTROL_STATEMENTS: tuple[str, ...] = (
    "This file records the house conventions contributors are expected to follow in this repository.",
    "The conventions below describe how code in this repository is written and reviewed.",
    "These notes cover the repository's house style and do not change program behaviour.",
    "What follows is the working style this repository has settled on over time.",
)
CONTROL_TITLES: tuple[str, ...] = (
    "Repository working notes",
    "House conventions",
    "Contributor notes",
    "Working agreements",
)
_CONTROL_TAIL = (
    "This applies to new code and to code you touch while making another change.",
    "Existing files are updated opportunistically rather than in a single sweep.",
    "Reviewers may ask for this before approving a change.",
)


def _pick(bank: Sequence[str], material: str, offset: int = 0) -> str:
    h = int.from_bytes(hashlib.blake2s(material.encode("utf-8"), digest_size=8).digest(), "big")
    return bank[(h + offset) % len(bank)]


def _pick_pair(
    bank: Sequence[tuple[str, str]], material: str, offset: int, used: set[str]
) -> tuple[str, str]:
    """Deterministically pick an unused (label, text) pair from a bank."""
    h = int.from_bytes(hashlib.blake2s(material.encode("utf-8"), digest_size=8).digest(), "big")
    for k in range(len(bank)):
        label, text = bank[(h + offset + k) % len(bank)]
        if label not in used:
            return label, text
    raise ControlContaminated(
        f"control clause bank exhausted: a fact with more than {len(bank)} clauses needs a "
        "hand-authored `control:` block"
    )


def _fit_words(text: str, target_words: int, material: str, offset: int) -> str:
    """Extend `text` toward `target_words` with neutral tails; never truncates mid-sentence."""
    out = text
    guard = 0
    while len(out.split()) + 3 < target_words and guard < len(_CONTROL_TAIL):
        out = out.rstrip() + " " + _pick(_CONTROL_TAIL, material, offset + guard)
        guard += 1
    return out


def control_fact(
    fact: CanonicalFact,
    *,
    control: Mapping | None = None,
    regexes: Sequence[str] = (),
    distractor_tokens: Sequence[str] = (),
    max_attempts: int = 32,
) -> CanonicalFact:
    """The fact-free twin planted in `ctrl` (and `ctrl-np`).

    `ctrl` isolates the fact's CONTENT from the mere existence of the file,
    so the twin must have the same shape — same title slot, same clause labels,
    comparable length — and none of the meaning. If a hand-authored `control:`
    block is present in facts.yaml it is used verbatim (strongly preferred); the
    generated default matches structure and approximate length only, and is
    re-drawn until it trips none of the fact's tier-(b) paraphrase regexes.

    A control document that matched a tier-(b) regex would register as a mention
    with `available = false` — i.e. it would manufacture exactly the
    confabulation that the `confab_rate <= 0.05` pilot gate exists to detect.
    """
    compiled = [re.compile(p, re.IGNORECASE) for p in regexes]
    tokens = tuple(t for t in distractor_tokens if t)

    if control:
        twin = CanonicalFact(
            fact_id=f"{fact.fact_id}__control",
            title=str(control.get("title") or _pick(CONTROL_TITLES, fact.fact_id)),
            statement=str(control["statement"]),
            clauses=tuple(
                Clause(label=str(c["label"]), text=str(c["text"]))
                for c in (control.get("clauses") or [])
            ),
            nonce=None,
            task_id=fact.task_id,
            bucket=fact.bucket,
        )
        assert_control_clean(
            "\n".join([twin.title, twin.statement, *(c.text for c in twin.clauses)]),
            nonce=fact.nonce,
            regexes=regexes,
            distractor_tokens=tokens,
            where=f"{fact.fact_id}:control(authored)",
        )
        return twin

    for attempt in range(max_attempts):
        statement = _fit_words(
            _pick(CONTROL_STATEMENTS, f"{fact.fact_id}|statement", attempt),
            len(fact.statement.split()),
            f"{fact.fact_id}|statement",
            attempt,
        )
        clauses = []
        used: set[str] = set()
        for i, c in enumerate(fact.clauses):
            material = f"{fact.fact_id}|clause|{i}"
            label, text = _pick_pair(CONTROL_CLAUSE_BANK, material, attempt + i, used)
            used.add(label)
            clauses.append(
                Clause(label=label, text=_fit_words(text, len(c.text.split()), material, attempt + i))
            )
        blob = "\n".join([statement, *(c.text for c in clauses)])
        if any(p.search(blob) for p in compiled):
            continue
        if fact.nonce and nonce_mod.NonceSet({fact.fact_id: fact.nonce}).occurs_in(blob):
            continue
        if any(t.lower() in blob.lower() for t in tokens):
            continue
        return CanonicalFact(
            fact_id=f"{fact.fact_id}__control",
            title=_pick(CONTROL_TITLES, fact.fact_id, attempt),
            statement=statement,
            clauses=tuple(clauses),
            nonce=None,
            task_id=fact.task_id,
            bucket=fact.bucket,
        )
    raise ControlContaminated(
        f"could not generate a clean control twin for {fact.fact_id!r} in {max_attempts} "
        "attempts — the tier-(b) regexes are almost certainly over-broad; author a "
        "`control:` block in facts.yaml instead"
    )


def assert_control_clean(
    text: str,
    *,
    nonce: str | None,
    regexes: Sequence[str] = (),
    distractor_tokens: Sequence[str] = (),
    where: str = "control",
) -> dict:
    """Raise ControlContaminated if a control document could pass for the fact."""
    problems: list[str] = []
    if nonce and nonce_mod.NonceSet({"fact": nonce}).occurs_in(text):
        problems.append("carries the nonce")
    for p in regexes:
        if re.search(p, text, re.IGNORECASE):
            problems.append(f"matches tier-(b) regex {p!r}")
    for t in distractor_tokens:
        if t and t.lower() in text.lower():
            problems.append(f"carries distractor token {t!r}")
    if problems:
        raise ControlContaminated(f"{where}: " + "; ".join(problems))
    return {"ok": True, "where": where}


# ── length + content discipline ──────────────────────────────────────────────
_TABLE_RULE_RE = re.compile(r"^\|?[\s:\-|]+\|?$")


def content_words(text: str) -> int:
    """Word count with markdown scaffolding removed.

    Headings markers, list bullets, checkboxes, table rules and cell pipes are
    presentation; everything left is the content that must be constant across
    the three formats. Counting raw whitespace tokens instead would make a pipe
    table look like it says ~30% more than the paragraph saying the same thing.
    """
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or _TABLE_RULE_RE.fullmatch(s):
            continue
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^[-*+]\s+", "", s)
        s = re.sub(r"^\[[ xX]\]\s*", "", s)
        out.append(s.replace("|", " "))
    return len(" ".join(out).split())


def length_report(text: str) -> dict:
    """Byte/char/line/word counts and the sha256 of one rendered document."""
    data = text.encode("utf-8")
    return {
        "bytes": len(data),
        "chars": len(text),
        "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        "words": len(text.split()),
        "content_words": content_words(text),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def assert_within_caps(
    text: str,
    *,
    where: str = "<doc>",
    max_bytes: int = MAX_PLANT_BYTES,
    max_lines: int = MAX_PLANT_LINES,
) -> dict:
    """Raise RenderTooLong unless the document fits both plant caps.

    The byte cap is the one that binds: below the 256 KB `Read` ceiling,
    truncation is completely silent — no ellipsis, no marker, nothing in
    stream.jsonl — and the observed cut point ran as low as 21,600 bytes.
    """
    rep = length_report(text)
    if rep["bytes"] > max_bytes:
        raise RenderTooLong(
            f"{where}: {rep['bytes']} bytes exceeds the {max_bytes}-byte plant cap; "
            "a silent Read truncation would make this look like 'read but not used'"
        )
    if rep["lines"] > max_lines:
        raise RenderTooLong(f"{where}: {rep['lines']} lines exceeds the {max_lines}-line cap")
    return rep


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def assert_content_constant(texts: Mapping[str, str], fact: CanonicalFact) -> dict:
    """Raise RenderDrift unless every format carries the same content.

    Checks the title, the statement and every clause label and text, after
    whitespace normalization (prose wraps, the others do not). This is what
    licenses the claim that `d2` vs `d2-check` vs `d2-table` is a pure
    presentation contrast.
    """
    needles = [fact.title, _norm(fact.statement)]
    for c in fact.clauses:
        needles += [_norm(c.label), _norm(c.text)]
    for d in fact.distractors:
        needles.append(_norm(d.statement))
    missing: list[str] = []
    for fmt, text in texts.items():
        hay = _norm(text)
        for n in needles:
            if n and n not in hay:
                missing.append(f"{fmt}: missing {n[:60]!r}")
    if missing:
        raise RenderDrift("; ".join(missing[:8]))
    return {"ok": True, "formats": sorted(texts), "checked": len(needles)}


def _spread(values: Sequence[int]) -> float | None:
    vals = [v for v in values]
    if not vals or min(vals) <= 0:
        return None
    return max(vals) / min(vals) - 1.0


def length_balance(texts: Mapping[str, str]) -> dict:
    """Length spread across formats, raw and content-only, plus per-format reports."""
    reps = {f: length_report(t) for f, t in texts.items()}
    words = [r["words"] for r in reps.values()]
    cwords = [r["content_words"] for r in reps.values()]
    return {
        "per_format": reps,
        "words_min": min(words) if words else 0,
        "words_max": max(words) if words else 0,
        "word_spread": _spread(words),
        "content_words_min": min(cwords) if cwords else 0,
        "content_words_max": max(cwords) if cwords else 0,
        "content_word_spread": _spread(cwords),
    }


def assert_length_balanced(
    texts: Mapping[str, str],
    tol: float = DEFAULT_LENGTH_TOL,
    *,
    raw_tol: float = DEFAULT_RAW_LENGTH_TOL,
) -> dict:
    """Raise RenderDrift if the formats differ in length by more than tolerance.

    Content is identical by construction, so a content-word spread means one
    layout is adding material — and "format sensitivity" would
    then be partly a length effect, which has no arm to separate. The raw
    bound is deliberately loose: a table really does cost more tokens than a
    paragraph, and that cost is part of the arm, not a defect in it.
    """
    bal = length_balance(texts)
    cs, ws = bal["content_word_spread"], bal["word_spread"]
    if cs is not None and cs > tol:
        raise RenderDrift(
            f"content-word spread {cs:.3f} exceeds tolerance {tol}: "
            f"{ {f: r['content_words'] for f, r in bal['per_format'].items()} }"
        )
    if ws is not None and ws > raw_tol:
        raise RenderDrift(
            f"raw word spread {ws:.3f} exceeds tolerance {raw_tol}: "
            f"{ {f: r['words'] for f, r in bal['per_format'].items()} }"
        )
    return bal


def render_report(
    fact: CanonicalFact,
    texts: Mapping[str, str] | None = None,
    *,
    distractors: Sequence[Distractor] | None = None,
) -> dict:
    """The `_index/render_report.json` entry for one fact: lengths + balance.

 requires length to be controlled AND reported; this is the reported half.
    """
    rendered = dict(texts) if texts is not None else render_all(fact, distractors=distractors)
    bal = length_balance(rendered)
    return {
        "renderer_version": RENDERER_VERSION,
        "fact_id": fact.fact_id,
        "task_id": fact.task_id,
        "is_control": fact.is_control,
        "nonce_present": bool(fact.nonce),
        "clause_count": len(fact.clauses),
        "distractor_count": len(fact.distractors if distractors is None else distractors),
        "max_plant_bytes": MAX_PLANT_BYTES,
        "max_plant_lines": MAX_PLANT_LINES,
        **bal,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────
def _fact_from_json(path: str) -> CanonicalFact:
    d = json.loads(Path(path).read_text())
    return CanonicalFact(
        fact_id=d["fact_id"],
        title=d["title"],
        statement=d["statement"],
        clauses=tuple(Clause(**c) for c in d.get("clauses", [])),
        nonce=d.get("nonce"),
        task_id=d.get("task_id"),
        bucket=d.get("bucket", "constraint"),
        distractors=tuple(Distractor(**x) for x in d.get("distractors", [])),
    )


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="WUR fact renderers (prose | checklist | table)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="render one fact (JSON on disk) in one format")
    r.add_argument("--fact-json", required=True, help="a CanonicalFact as JSON")
    r.add_argument("--format", default="prose", choices=list(FORMATS))

    rep = sub.add_parser("report", help="length + balance report over all three formats")
    rep.add_argument("--fact-json", required=True)

    a = p.parse_args(argv)
    fact = _fact_from_json(a.fact_json)
    if a.cmd == "render":
        sys.stdout.write(render(fact, a.format))
        return 0
    texts = render_all(fact)
    assert_content_constant(texts, fact)
    print(json.dumps(render_report(fact, texts), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
