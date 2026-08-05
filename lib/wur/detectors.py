#!/usr/bin/env python3
"""
detectors.py — the CLOSED registry of six `used` predicates (§4.3, §6.4).

RESPONSIBILITY
  Decide, mechanically and without an LLM, whether one planted fact crossed the
  third boundary of the funnel: available -> read -> **used** -> retained.

      used_rf = 1  iff  detector_f fires over
                        ( final workspace , git diff $BASELINE_SHA , ordered Bash commands )

  The registry is CLOSED. Tier-B fact generation may pick a `name` from
  DETECTOR_NAMES and fill `params`; it may never author a predicate, and every
  parameter is data (globs, regexes, booleans) that this module interprets — no
  parameter is ever executed as code or as a shell command. validate_params()
  rejects an unknown detector name, an unknown parameter key, and a parameter of
  the wrong type, so a generated pack fails loudly instead of silently degrading
  into "detector never fires".

  GRADE BEHAVIOUR, DETECT PROVENANCE (§4.3). The success battery may only test
  what the workspace *does*; this module may only test *how the agent got there*.
  If the battery tests the mandate, success == used and the funnel collapses to
  one measurement.

  `eligible` IS LOAD-BEARING. If the run never created the site the mandate
  applies to, `fired = 0` is CENSORED, not evidence of non-use — so both
  use_rate_uncond = fired/N and use_rate_cond = fired/eligible are computable
  from this output, and they are always reported together.

INPUTS
  DetectorContext — the final workspace tree, the unified diff against
  $BASELINE_SHA (tracked + untracked), the ordered Bash commands as recorded at
  the PreToolUse barrier, and the planted paths (excluded from content scans:
  the fact sitting in NOTES.md is `available`, never `used`).
  A detector name from DETECTOR_NAMES + a params dict.

OUTPUTS
  {eligible, fired, evidence, detail} — plus name / params_sha256 / error when
  called through run_detector(). Evidence is concrete and quotable, capped at
  EVIDENCE_MAX_ITEMS x EVIDENCE_MAX_CHARS so a criterion's stdout stays bounded.

EXECUTION MODEL
  A fact detector IS a mechanical criterion, so lib/battery.py is the execution
  engine and no new one is written (§6.4). detect_use.py compiles each binding
  into a battery criterion whose command is this module's `eval` subcommand and
  whose pass_condition is `exit_code == 0`.

CLI
  python3 lib/wur/detectors.py list
  python3 lib/wur/detectors.py describe NAME
  python3 lib/wur/detectors.py registry-hash
  python3 lib/wur/detectors.py validate --detector NAME --params-json '{...}'
  python3 lib/wur/detectors.py eval --detector NAME --params-b64 B64 \
                                    --context CTX.json [--diff-only]
      payload (the result JSON) on stdout, diagnostics on stderr;
      exit 0 = fired, 3 = eligible but not fired, 4 = not eligible, 5 = error.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

# ── version + limits ─────────────────────────────────────────────────────────
REGISTRY_VERSION = "wur-detectors-v1"

EVIDENCE_MAX_ITEMS = 40
EVIDENCE_MAX_CHARS = 240
MAX_FILE_BYTES = 2_000_000
MAX_PATTERNS = 24
MAX_PATTERN_CHARS = 512
MAX_GLOBS = 64

# Exit codes of the `eval` subcommand. battery.py's pass_condition is
# `exit_code == 0`, so only a fire is a pass; 3 and 4 are both "not a pass" and
# are told apart by the JSON on stdout, never by the exit code alone.
EXIT_FIRED = 0
EXIT_NOT_FIRED = 3
EXIT_NOT_ELIGIBLE = 4
EXIT_ERROR = 5

# Never walked, never scanned, never counted as a changed path.
DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = (
    ".git/**",
    "venv/**",
    ".venv/**",
    "node_modules/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/*.pyc",
    ".pytest_cache/**",
    "**/.pytest_cache/**",
    ".mypy_cache/**",
    "**/.egg-info/**",
)

# The carriers of §7.1: `NOTES.md` at every depth plus the `d0-push` import stub.
# A content detector that scanned these would fire on the plant itself — that is
# `available`, not `used`. detect_use.py unions the run's real planted paths on
# top of this; the constant is the floor so a missing manifest cannot silently
# turn the plant into evidence of use.
DEFAULT_PLANTED_GLOBS: tuple[str, ...] = (
    "NOTES.md",
    "docs/NOTES.md",
    "docs/internal/NOTES.md",
    "docs/internal/memory/NOTES.md",
    "CLAUDE.md",
    "**/CLAUDE.md",
)

BUCKETS = ("constraint", "method", "ordering", "hidden_cue")


class DetectorError(RuntimeError):
    """A detector could not be run at all (bad params, unknown name)."""


class UnknownDetector(DetectorError):
    """The registry is closed; this name is not in it."""


# ── glob matching ────────────────────────────────────────────────────────────
def glob_to_regex(pat: str) -> re.Pattern[str]:
    """Translate a POSIX-ish glob into a regex anchored on a relative path.

    `**` crosses directory separators, `*` and `?` do not. A pattern containing
    no `/` is additionally matched against the basename, so `*.py` means "any
    .py file anywhere" rather than "a top-level .py file" — the intuition every
    fact author has, made explicit here rather than discovered in review.
    """
    i, out = 0, []
    while i < len(pat):
        ch = pat[i]
        if ch == "*":
            if pat[i : i + 3] == "**/":
                out.append(r"(?:.*/)?")
                i += 3
                continue
            if pat[i : i + 2] == "**":
                out.append(r".*")
                i += 2
                continue
            out.append(r"[^/]*")
        elif ch == "?":
            out.append(r"[^/]")
        elif ch == "[":
            j = i + 1
            if j < len(pat) and pat[j] in "!^":
                j += 1
            if j < len(pat) and pat[j] == "]":
                j += 1
            while j < len(pat) and pat[j] != "]":
                j += 1
            if j >= len(pat):
                out.append(re.escape(ch))
            else:
                body = pat[i + 1 : j]
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append("[" + body + "]")
                i = j + 1
                continue
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


_GLOB_CACHE: dict[str, tuple[re.Pattern[str], bool]] = {}


def _glob(pat: str) -> tuple[re.Pattern[str], bool]:
    hit = _GLOB_CACHE.get(pat)
    if hit is None:
        hit = (glob_to_regex(pat), "/" not in pat)
        _GLOB_CACHE[pat] = hit
    return hit


def normalize_relpath(relpath: str) -> str:
    """POSIX, no leading './' or '/'. NOT str.lstrip('./') — that would eat the
    leading dot of a dotfile and turn `.gitignore` into `gitignore`."""
    rel = str(relpath).replace(os.sep, "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.lstrip("/")


def path_matches(relpath: str, patterns: Sequence[str]) -> bool:
    """True when `relpath` (POSIX, relative to the workspace) matches any glob."""
    if not patterns:
        return False
    rel = normalize_relpath(relpath)
    base = rel.rsplit("/", 1)[-1]
    for pat in patterns:
        rx, basename_too = _glob(pat)
        if rx.match(rel) or (basename_too and rx.match(base)):
            return True
    return False


# ── unified diff parsing ─────────────────────────────────────────────────────
@dataclass
class DiffFile:
    """One file's worth of a unified diff, with new-file line numbers kept."""

    path: str
    status: str = "modified"  # added | modified | deleted | renamed
    old_path: str | None = None
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[tuple[int, str]] = field(default_factory=list)
    binary: bool = False

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "old_path": self.old_path,
            "n_added": len(self.added),
            "n_removed": len(self.removed),
            "binary": self.binary,
        }


_DIFF_GIT_RE = re.compile(r"^diff --git (?:a/)?(.+?) (?:b/)?(.+)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_ab(p: str) -> str:
    p = p.strip()
    if p.startswith(("a/", "b/")):
        p = p[2:]
    if p.startswith("./"):
        p = p[2:]
    return p


def parse_unified_diff(text: str) -> list[DiffFile]:
    """Parse `git diff` output (plus the `--no-index /dev/null <f>` form).

    teardown_run.sh builds git.patch as `git diff $BASELINE_SHA` followed by one
    `git diff --no-index /dev/null <untracked>` per untracked file, so both
    shapes must parse. Renames, deletions and binary markers are all preserved:
    a "do not modify X" mandate is violated by a delete or a rename just as much
    as by an edit.
    """
    files: list[DiffFile] = []
    cur: DiffFile | None = None
    new_ln = 0
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _DIFF_GIT_RE.match(line)
        if m:
            a, b = _strip_ab(m.group(1)), _strip_ab(m.group(2))
            cur = DiffFile(path=b if b != "dev/null" else a, old_path=a if a != b else None)
            files.append(cur)
            new_ln = 0
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        if line.startswith("new file mode"):
            cur.status = "added"
        elif line.startswith("deleted file mode"):
            cur.status = "deleted"
        elif line.startswith("rename from "):
            cur.status = "renamed"
            cur.old_path = _strip_ab(line[len("rename from ") :])
        elif line.startswith("rename to "):
            cur.status = "renamed"
            cur.path = _strip_ab(line[len("rename to ") :])
        elif line.startswith("Binary files"):
            cur.binary = True
        elif line.startswith("--- "):
            src = line[4:].strip()
            if src == "/dev/null":
                cur.status = "added"
            elif cur.old_path is None:
                cur.old_path = _strip_ab(src)
        elif line.startswith("+++ "):
            dst = line[4:].strip()
            if dst == "/dev/null":
                cur.status = "deleted"
            else:
                cur.path = _strip_ab(dst)
        else:
            h = _HUNK_RE.match(line)
            if h:
                new_ln = int(h.group(3))
            elif new_ln:
                if line.startswith("+"):
                    cur.added.append((new_ln, line[1:]))
                    new_ln += 1
                elif line.startswith("-"):
                    cur.removed.append((new_ln, line[1:]))
                elif line.startswith("\\"):
                    pass  # "\ No newline at end of file"
                elif line.startswith(" ") or line == "":
                    new_ln += 1
                else:
                    new_ln = 0  # left the hunk body
        i += 1
    return [f for f in files if f.path and f.path != "dev/null"]


# ── shell segmentation ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class BashSegment:
    """One simple command extracted from one Bash tool call, in issue order."""

    ordinal: int  # global position across every segment of the run
    call_idx: int  # which Bash tool call it came from
    seg_idx: int  # position inside that call
    text: str

    def to_dict(self) -> dict:
        return {"ordinal": self.ordinal, "call_idx": self.call_idx,
                "seg_idx": self.seg_idx, "text": self.text}


_SEPARATORS = ("&&", "||", ";;", ";", "|", "\n")


def split_shell_segments(command: str) -> list[str]:
    """Split one shell command into ordered simple commands.

    ORDERING DECISION, stated explicitly because §4.3 leaves it open: a compound
    `a && b` issued as ONE Bash call COUNTS as `a` before `b`. The mandate is
    "run the migration before the tests"; an agent that writes
    `python migrate.py && pytest` has complied — refusing it would score correct
    behaviour as non-use and would silently penalise the (very common) habit of
    chaining. Splitting on the shell operators makes compliance-by-compound fall
    out of ordinary segment ordering rather than needing a special case; a fact
    that genuinely requires two SEPARATE tool calls sets allow_compound=false.

    Quotes, backslash escapes and $( ) nesting are respected. Heredoc bodies are
    NOT parsed — a `<<EOF` body containing `&&` over-splits. Documented rather
    than hidden: no registry detector's semantics depend on heredoc interiors.
    """
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    i, n = 0, len(command or "")
    while i < n:
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command[i : i + 2] == "$(":
            depth += 1
            buf.append("$(")
            i += 2
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue
        if depth == 0:
            hit = next((s for s in _SEPARATORS if command.startswith(s, i)), None)
            if hit:
                out.append("".join(buf))
                buf = []
                i += len(hit)
                continue
            if ch == "&" and command[i : i + 2] != "&&":
                out.append("".join(buf))
                buf = []
                i += 1
                continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


# ── comment stripping (require_code) ─────────────────────────────────────────
_LINE_COMMENT_MARKERS = {
    ".py": ("#",), ".sh": ("#",), ".bash": ("#",), ".yaml": ("#",), ".yml": ("#",),
    ".toml": ("#",), ".cfg": ("#",), ".ini": ("#", ";"), ".rb": ("#",), ".pl": ("#",),
    ".js": ("//",), ".ts": ("//",), ".tsx": ("//",), ".jsx": ("//",), ".go": ("//",),
    ".java": ("//",), ".c": ("//",), ".h": ("//",), ".cpp": ("//",), ".rs": ("//",),
    ".sql": ("--",), ".lua": ("--",),
}
_BLOCK_COMMENT_SUFFIXES = {".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".c", ".h", ".cpp", ".rs", ".css"}
_TRIPLE_RE = re.compile(r"('''|\"\"\")")


def _blank(s: str) -> str:
    """Replace a span with spaces, preserving newlines so line numbers hold."""
    return "".join("\n" if c == "\n" else " " for c in s)


def strip_comments(text: str, suffix: str) -> str:
    """Blank out comments (and Python docstrings) while preserving line numbers.

    HEURISTIC, and deliberately conservative: it exists so `require_code` can
    tell "the agent wrote `# TODO: use FTS5`" from "the agent used FTS5". It is
    a lexer's approximation, not a parser — a `#` inside a regex literal in a
    .js file, for instance, is left alone because .js has no `#` comment.
    """
    suffix = (suffix or "").lower()
    if suffix == ".py":
        out, i, n = [], 0, len(text)
        while i < n:
            m = _TRIPLE_RE.search(text, i)
            if not m:
                out.append(text[i:])
                break
            out.append(text[i : m.start()])
            close = text.find(m.group(1), m.end())
            end = n if close < 0 else close + 3
            out.append(_blank(text[m.start() : end]))
            i = end
        text = "".join(out)
    if suffix in _BLOCK_COMMENT_SUFFIXES:
        text = re.sub(r"/\*.*?\*/", lambda m: _blank(m.group(0)), text, flags=re.DOTALL)
    markers = _LINE_COMMENT_MARKERS.get(suffix)
    if not markers:
        return text
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        lines[idx] = _strip_line_comment(line, markers)
    return "\n".join(lines)


def _strip_line_comment(line: str, markers: Sequence[str]) -> str:
    quote: str | None = None
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        for mk in markers:
            if line.startswith(mk, i):
                return line[:i] + " " * (n - i)
        i += 1
    return line


# ── the context ──────────────────────────────────────────────────────────────
@dataclass
class DetectorContext:
    """Everything a predicate is allowed to look at, and nothing else.

    Three sources, all MEASURED (STATUS.md §3): the final workspace tree, the
    unified diff against $BASELINE_SHA, and the ordered Bash commands recorded
    at the PreToolUse barrier. No transcript, no hook payload, no LLM. That is
    what makes `used` reproducible months later from raw artifacts alone.
    """

    workspace: Path
    diff_text: str = ""
    bash_commands: Sequence[str] = ()
    planted_paths: Sequence[str] = ()
    baseline_sha: str | None = None
    scope: str = "run"
    diff_only: bool = False

    _diff_cache: list[DiffFile] | None = field(default=None, repr=False, compare=False)
    _seg_cache: list[BashSegment] | None = field(default=None, repr=False, compare=False)
    _file_cache: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    # -- construction ---------------------------------------------------------
    @classmethod
    def from_dict(cls, d: dict) -> "DetectorContext":
        diff_text = d.get("diff_text") or ""
        if not diff_text and d.get("diff_path"):
            p = Path(d["diff_path"])
            if p.exists():
                diff_text = p.read_text(errors="replace")
        return cls(
            workspace=Path(d["workspace"]).resolve(),
            diff_text=diff_text,
            bash_commands=list(d.get("bash_commands") or ()),
            planted_paths=list(d.get("planted_paths") or ()),
            baseline_sha=d.get("baseline_sha"),
            scope=d.get("scope") or "run",
            diff_only=bool(d.get("diff_only")),
        )

    def to_dict(self) -> dict:
        return {
            "workspace": str(self.workspace),
            "diff_text": self.diff_text,
            "bash_commands": list(self.bash_commands),
            "planted_paths": list(self.planted_paths),
            "baseline_sha": self.baseline_sha,
            "scope": self.scope,
            "diff_only": self.diff_only,
        }

    def summary(self) -> dict:
        return {
            "workspace": str(self.workspace),
            "baseline_sha": self.baseline_sha,
            "scope": self.scope,
            "diff_bytes": len(self.diff_text),
            "n_changed_paths": len(self.changed_files()),
            "n_bash_calls": len(self.bash_commands),
            "n_bash_segments": len(self.bash_segments()),
            "planted_paths": list(self.planted_paths),
        }

    # -- diff -----------------------------------------------------------------
    def changed_files(self) -> list[DiffFile]:
        if self._diff_cache is None:
            files = parse_unified_diff(self.diff_text)
            self._diff_cache = [
                f for f in files if not path_matches(f.path, DEFAULT_EXCLUDE_GLOBS)
            ]
        return self._diff_cache

    def changed_paths(self) -> list[str]:
        return [f.path for f in self.changed_files()]

    # -- bash -----------------------------------------------------------------
    def bash_segments(self) -> list[BashSegment]:
        if self._seg_cache is None:
            segs: list[BashSegment] = []
            for call_idx, cmd in enumerate(self.bash_commands):
                for seg_idx, seg in enumerate(split_shell_segments(cmd)):
                    segs.append(
                        BashSegment(ordinal=len(segs), call_idx=call_idx, seg_idx=seg_idx, text=seg)
                    )
            self._seg_cache = segs
        return self._seg_cache

    # -- workspace ------------------------------------------------------------
    def iter_workspace_files(
        self, include: Sequence[str], exclude: Sequence[str]
    ) -> Iterator[str]:
        """Relative POSIX paths of readable, non-binary, non-symlinked files.

        os.walk with followlinks=False is deliberate: setup_run.sh symlinks the
        hermetic venv in as `workspace/venv`, and following it would walk a
        100 MB site-packages tree and could match a pattern in a dependency.
        """
        root = self.workspace
        if not root.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            rel_dir = os.path.relpath(dirpath, root)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            keep = []
            for d in sorted(dirnames):
                rd = f"{rel_dir}/{d}" if rel_dir else d
                full = Path(dirpath) / d
                if full.is_symlink():
                    continue
                if path_matches(rd + "/x", DEFAULT_EXCLUDE_GLOBS) or path_matches(rd, DEFAULT_EXCLUDE_GLOBS):
                    continue
                if path_matches(rd, exclude) or path_matches(rd + "/x", exclude):
                    continue
                keep.append(d)
            dirnames[:] = keep
            for fn in sorted(filenames):
                rel = f"{rel_dir}/{fn}" if rel_dir else fn
                full = Path(dirpath) / fn
                if full.is_symlink():
                    continue
                if path_matches(rel, DEFAULT_EXCLUDE_GLOBS) or path_matches(rel, exclude):
                    continue
                if include and not path_matches(rel, include):
                    continue
                yield rel

    def read_text(self, relpath: str) -> str | None:
        """File body, or None when it is missing, oversized or binary."""
        if relpath in self._file_cache:
            return self._file_cache[relpath]
        full = self.workspace / relpath
        try:
            if not full.is_file() or full.is_symlink():
                return None
            if full.stat().st_size > MAX_FILE_BYTES:
                return None
            raw = full.read_bytes()
        except OSError:
            return None
        if b"\x00" in raw[:8192]:
            return None
        text = raw.decode("utf-8", errors="replace")
        self._file_cache[relpath] = text
        return text

    def path_exists(self, globs: Sequence[str]) -> bool:
        return any(True for _ in self.iter_workspace_files(list(globs), ()))

    # -- the unit every content detector scans --------------------------------
    def iter_lines(
        self,
        include: Sequence[str],
        exclude: Sequence[str],
        *,
        require_code: bool = False,
    ) -> Iterator[tuple[str, int, str]]:
        """(path, 1-based line number, line text) over the searchable surface.

        diff_only restricts the surface to lines the run ADDED — that is what
        `used_in_diff` means (§4.4): provenance visible in what changed, rather
        than in a tree that already contained it.
        """
        if self.diff_only:
            for f in self.changed_files():
                if f.status == "deleted" or f.binary:
                    continue
                if path_matches(f.path, exclude):
                    continue
                if include and not path_matches(f.path, include):
                    continue
                suffix = Path(f.path).suffix.lower()
                markers = _LINE_COMMENT_MARKERS.get(suffix)
                for ln, text in f.added:
                    if require_code and markers:
                        text = _strip_line_comment(text, markers)
                    yield f.path, ln, text
            return
        for rel in self.iter_workspace_files(include, exclude):
            body = self.read_text(rel)
            if body is None:
                continue
            if require_code:
                body = strip_comments(body, Path(rel).suffix)
            for ln, text in enumerate(body.split("\n"), start=1):
                yield rel, ln, text

    def has_search_surface(self, include: Sequence[str], exclude: Sequence[str]) -> bool:
        """Does the site the mandate applies to exist at all? -> `eligible`."""
        if self.diff_only:
            for f in self.changed_files():
                if f.status == "deleted" or f.binary or not f.added:
                    continue
                if path_matches(f.path, exclude):
                    continue
                if not include or path_matches(f.path, include):
                    return True
            return False
        for rel in self.iter_workspace_files(include, exclude):
            if self.read_text(rel) is not None:
                return True
        return False


# ── parameter contract ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: str  # str | bool | int | list[str] | list[regex] | object | enum
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()
    doc: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name, "type": self.type, "required": self.required,
            "default": self.default, "choices": list(self.choices), "doc": self.doc,
        }


# Present on every detector. `eligible_when` only ever ADDS conditions to the
# detector's natural eligibility; it can never manufacture eligibility that the
# artifacts do not support, except through the explicit natural_eligibility
# escape hatch, which then REQUIRES an eligible_when to be supplied.
COMMON_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec(
        "eligible_when", "object", default=None,
        doc="Extra eligibility conditions, ANDed with the detector's own: "
            "{exists:[glob], diff_touches:[glob], any_diff:bool, "
            "bash_matches:[regex], min_bash_calls:int}.",
    ),
    ParamSpec(
        "natural_eligibility", "bool", default=True,
        doc="Disable the detector's built-in eligibility rule. Requires a "
            "non-empty eligible_when — eligibility must come from somewhere.",
    ),
    ParamSpec("note", "str", default="", doc="Free-text provenance note; never interpreted."),
)

_ELIGIBLE_WHEN_KEYS = {"exists", "diff_touches", "any_diff", "bash_matches", "min_bash_calls"}


@dataclass(frozen=True)
class DetectorSpec:
    name: str
    buckets: tuple[str, ...]
    doc: str
    params: tuple[ParamSpec, ...]
    fn: Callable[[DetectorContext, dict], "DetectorResult"]
    diff_native: bool = False  # already reads only the diff / the command log

    @property
    def bucket(self) -> str:
        """The primary fact bucket; `buckets` carries every bucket it serves."""
        return self.buckets[0]

    def all_params(self) -> tuple[ParamSpec, ...]:
        return self.params + COMMON_PARAMS

    def to_dict(self) -> dict:
        return {
            "name": self.name, "bucket": self.bucket, "buckets": list(self.buckets),
            "doc": self.doc.strip(), "diff_native": self.diff_native,
            "params": [p.to_dict() for p in self.all_params()],
        }


@dataclass
class DetectorResult:
    """The four-field contract of §4.3."""

    eligible: bool
    fired: bool
    evidence: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "eligible": bool(self.eligible),
            "fired": bool(self.fired),
            "evidence": [_ev(e) for e in self.evidence[:EVIDENCE_MAX_ITEMS]],
            "detail": self.detail,
        }


def _ev(text: str) -> str:
    s = re.sub(r"\s+", " ", str(text)).strip()
    return s if len(s) <= EVIDENCE_MAX_CHARS else s[: EVIDENCE_MAX_CHARS - 1] + "…"


def _cap(seq: Sequence[Any], n: int = EVIDENCE_MAX_ITEMS) -> list:
    return list(seq)[:n]


# ── param validation / normalization ─────────────────────────────────────────
def _type_errors(spec: ParamSpec, value: Any) -> list[str]:
    errs: list[str] = []
    t = spec.type
    if t == "str":
        if not isinstance(value, str):
            errs.append(f"{spec.name}: expected string, got {type(value).__name__}")
    elif t == "bool":
        if not isinstance(value, bool):
            errs.append(f"{spec.name}: expected boolean, got {type(value).__name__}")
    elif t == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            errs.append(f"{spec.name}: expected integer, got {type(value).__name__}")
    elif t == "enum":
        if value not in spec.choices:
            errs.append(f"{spec.name}: expected one of {list(spec.choices)}, got {value!r}")
    elif t in ("list[str]", "list[regex]"):
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            errs.append(f"{spec.name}: expected a list of strings, got {type(value).__name__}")
            return errs
        if len(value) > (MAX_PATTERNS if t == "list[regex]" else MAX_GLOBS):
            errs.append(f"{spec.name}: too many entries ({len(value)})")
        for i, v in enumerate(value):
            if not isinstance(v, str) or not v:
                errs.append(f"{spec.name}[{i}]: expected a non-empty string")
                continue
            if len(v) > MAX_PATTERN_CHARS:
                errs.append(f"{spec.name}[{i}]: longer than {MAX_PATTERN_CHARS} chars")
            if t == "list[regex]":
                try:
                    re.compile(v)
                except re.error as e:
                    errs.append(f"{spec.name}[{i}]: not a valid regex ({e})")
    elif t == "object":
        if not isinstance(value, dict):
            errs.append(f"{spec.name}: expected an object, got {type(value).__name__}")
    return errs


def _eligible_when_errors(value: Any) -> list[str]:
    if value in (None, {}):
        return []
    if not isinstance(value, dict):
        return ["eligible_when: expected an object"]
    errs = [f"eligible_when: unknown key {k!r}" for k in sorted(set(value) - _ELIGIBLE_WHEN_KEYS)]
    for key in ("exists", "diff_touches", "bash_matches"):
        v = value.get(key)
        if v is None:
            continue
        if isinstance(v, str) or not isinstance(v, (list, tuple)):
            errs.append(f"eligible_when.{key}: expected a list of strings")
            continue
        for i, s in enumerate(v):
            if not isinstance(s, str) or not s:
                errs.append(f"eligible_when.{key}[{i}]: expected a non-empty string")
            elif key == "bash_matches":
                try:
                    re.compile(s)
                except re.error as e:
                    errs.append(f"eligible_when.bash_matches[{i}]: not a valid regex ({e})")
    if "any_diff" in value and not isinstance(value["any_diff"], bool):
        errs.append("eligible_when.any_diff: expected a boolean")
    if "min_bash_calls" in value:
        v = value["min_bash_calls"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            errs.append("eligible_when.min_bash_calls: expected a non-negative integer")
    return errs


def validate_params(name: str, params: dict | None) -> list[str]:
    """Every problem with (name, params); empty means the binding is runnable.

    The registry is closed in BOTH directions: an unknown detector name and an
    unknown parameter key are equally fatal. Tier-B generation that hallucinates
    a parameter therefore fails at pack-verification time, not silently at
    measurement time.
    """
    spec = REGISTRY.get(name)
    if spec is None:
        return [f"unknown detector {name!r}; the registry is closed: {list(DETECTOR_NAMES)}"]
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return [f"params must be an object, got {type(params).__name__}"]
    by_name = {p.name: p for p in spec.all_params()}
    errs = [f"unknown param {k!r} for {name}" for k in sorted(set(params) - set(by_name))]
    for p in spec.all_params():
        if p.name not in params:
            if p.required:
                errs.append(f"missing required param {p.name!r}")
            continue
        value = params[p.name]
        if p.name == "eligible_when":
            errs.extend(_eligible_when_errors(value))
            continue
        if value is None and not p.required:
            continue
        errs.extend(_type_errors(p, value))
    if params.get("natural_eligibility") is False and not (params.get("eligible_when") or {}):
        errs.append(
            "natural_eligibility=false requires a non-empty eligible_when: "
            "disabling the built-in rule without replacing it would make every "
            "run eligible and destroy the censoring distinction (§4.3)"
        )
    for p in spec.all_params():
        if p.required and p.type in ("list[str]", "list[regex]") and not params.get(p.name):
            if f"missing required param {p.name!r}" not in errs:
                errs.append(f"{p.name}: must be a non-empty list")
    return errs


def normalize_params(name: str, params: dict | None) -> dict:
    """Validated params with defaults filled in. Raises DetectorError on any problem."""
    problems = validate_params(name, params)
    if problems:
        raise DetectorError(f"{name}: " + "; ".join(problems))
    spec = REGISTRY[name]
    out: dict[str, Any] = {}
    src = params or {}
    for p in spec.all_params():
        value = src.get(p.name, p.default)
        if value is None and p.type in ("list[str]", "list[regex]"):
            value = []
        if p.name == "eligible_when" and value is None:
            value = {}
        out[p.name] = value
    return out


def params_sha256(name: str, params: dict) -> str:
    blob = json.dumps({"detector": name, "params": params}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── eligibility ──────────────────────────────────────────────────────────────
def _eval_eligible_when(ctx: DetectorContext, spec: dict) -> tuple[bool, dict]:
    detail: dict[str, Any] = {}
    ok = True
    if not spec:
        return True, detail
    for glob in spec.get("exists") or []:
        hit = ctx.path_exists([glob])
        detail.setdefault("exists", {})[glob] = hit
        ok = ok and hit
    if spec.get("diff_touches"):
        changed = ctx.changed_paths()
        for glob in spec["diff_touches"]:
            hit = any(path_matches(c, [glob]) for c in changed)
            detail.setdefault("diff_touches", {})[glob] = hit
            ok = ok and hit
    if spec.get("any_diff"):
        hit = bool(ctx.changed_files())
        detail["any_diff"] = hit
        ok = ok and hit
    for rx in spec.get("bash_matches") or []:
        pat = re.compile(rx)
        hit = any(pat.search(s.text) for s in ctx.bash_segments())
        detail.setdefault("bash_matches", {})[rx] = hit
        ok = ok and hit
    if spec.get("min_bash_calls"):
        hit = len(ctx.bash_commands) >= int(spec["min_bash_calls"])
        detail["min_bash_calls"] = {"required": int(spec["min_bash_calls"]),
                                    "actual": len(ctx.bash_commands), "ok": hit}
        ok = ok and hit
    return ok, detail


def _finish(
    ctx: DetectorContext,
    params: dict,
    natural_eligible: bool,
    fired: bool,
    evidence: Sequence[str],
    detail: dict,
) -> DetectorResult:
    """Combine natural eligibility with eligible_when and enforce the censoring rule."""
    use_natural = params.get("natural_eligibility", True)
    extra_ok, extra_detail = _eval_eligible_when(ctx, params.get("eligible_when") or {})
    eligible = (natural_eligible if use_natural else True) and extra_ok
    detail = dict(detail)
    detail["eligibility"] = {
        "natural": natural_eligible,
        "natural_applied": bool(use_natural),
        "eligible_when": extra_detail or None,
        "resolved": eligible,
    }
    # A detector may not fire on a site that does not exist. fired=0 under
    # eligible=0 is CENSORED, and trace.py reads it that way; letting a fire
    # through would put an uncensorable 1 in the numerator.
    if not eligible:
        fired = False
    return DetectorResult(eligible=eligible, fired=fired, evidence=list(evidence), detail=detail)


# ═════════════════════════════════════════════════════════════════════════════
# THE SIX PREDICATES
# ═════════════════════════════════════════════════════════════════════════════

# ── 1. path_untouched — bucket: constraint ───────────────────────────────────
def _det_path_untouched(ctx: DetectorContext, p: dict) -> DetectorResult:
    protected = p["paths"]
    allow_new = bool(p["allow_new"])
    changed = ctx.changed_files()
    violations: list[DiffFile] = []
    for f in changed:
        touched = path_matches(f.path, protected) or (
            f.old_path is not None and path_matches(f.old_path, protected)
        )
        if not touched:
            continue
        if allow_new and f.status == "added":
            continue
        violations.append(f)

    # The mandate only applies if the protected site was there to be protected,
    # AND the run actually changed something. "Did not modify X" is vacuously
    # true for a run that produced an empty diff — that is censored, not use.
    protected_present = ctx.path_exists(protected) or any(
        path_matches(f.path, protected) or (f.old_path and path_matches(f.old_path, protected))
        for f in changed
    )
    natural = bool(changed) and bool(protected_present)

    fired = not violations
    evidence = (
        [f"violation: {f.status} {f.path}" for f in _cap(violations)]
        if violations
        else [f"changed (none protected): {f.status} {f.path}" for f in _cap(changed)]
    )
    detail = {
        "protected_globs": list(protected),
        "allow_new": allow_new,
        "n_changed": len(changed),
        "changed_paths": _cap(ctx.changed_paths()),
        "violations": [f.to_dict() for f in _cap(violations)],
        "protected_present": bool(protected_present),
    }
    return _finish(ctx, p, natural, fired, evidence, detail)


# ── 2. diff_confined — bucket: constraint ────────────────────────────────────
def _det_diff_confined(ctx: DetectorContext, p: dict) -> DetectorResult:
    allowed = p["allowed"]
    required = p["required"]
    changed = ctx.changed_files()
    paths = [f.path for f in changed]
    outside = [c for c in paths if allowed and not path_matches(c, allowed)]
    missing = [g for g in required if not any(path_matches(c, [g]) for c in paths)]

    # The agent changed SOMETHING: the "where" mandate is now testable. A
    # required glob that was never touched is non-compliance, not censoring —
    # the site was reachable, the agent put its work elsewhere.
    natural = bool(changed)
    fired = not outside and not missing
    evidence = [f"outside allowed: {c}" for c in _cap(outside)] + [
        f"required but untouched: {g}" for g in _cap(missing)
    ]
    if fired:
        evidence = [f"changed: {c}" for c in _cap(paths)]
    detail = {
        "allowed_globs": list(allowed),
        "required_globs": list(required),
        "changed_paths": _cap(paths),
        "outside": _cap(outside),
        "missing_required": _cap(missing),
        "n_changed": len(changed),
    }
    return _finish(ctx, p, natural, fired, evidence, detail)


# ── content scanning shared by 3 and 4 ───────────────────────────────────────
def _content_scan(ctx: DetectorContext, p: dict) -> tuple[dict[str, list[str]], list[str], bool]:
    include = list(p["paths"]) or ["**/*"]
    exclude = list(p["exclude_paths"])
    if p.get("exclude_planted", True):
        exclude = exclude + list(ctx.planted_paths or ()) + list(DEFAULT_PLANTED_GLOBS)
    patterns = list(p["patterns"])
    compiled = [(pat, re.compile(pat, re.IGNORECASE if p.get("ignore_case", True) else 0))
                for pat in patterns]
    hits: dict[str, list[str]] = {pat: [] for pat in patterns}
    for path, ln, text in ctx.iter_lines(include, exclude, require_code=bool(p["require_code"])):
        if not text.strip():
            continue
        for pat, rx in compiled:
            if len(hits[pat]) >= EVIDENCE_MAX_ITEMS:
                continue
            if rx.search(text):
                hits[pat].append(f"{path}:{ln}: {text.strip()}")
    surface = ctx.has_search_surface(include, exclude)
    return hits, exclude, surface


# ── 3. content_present — buckets: method, hidden_cue ─────────────────────────
def _det_content_present(ctx: DetectorContext, p: dict) -> DetectorResult:
    hits, exclude, surface = _content_scan(ctx, p)
    patterns = list(p["patterns"])
    min_count = max(1, int(p["min_count"]))
    matched = [pat for pat in patterns if len(hits[pat]) >= min_count]
    fired = (len(matched) == len(patterns)) if p["mode"] == "all" else bool(matched)

    natural = surface
    evidence: list[str] = []
    for pat in patterns:
        for h in hits[pat][:5]:
            evidence.append(f"{pat} -> {h}")
    if not evidence:
        evidence = [f"no match for {pat!r}" for pat in _cap(patterns)]
    detail = {
        "patterns": patterns,
        "mode": p["mode"],
        "min_count": min_count,
        "require_code": bool(p["require_code"]),
        "matched_patterns": matched,
        "counts": {pat: len(hits[pat]) for pat in patterns},
        "include_globs": list(p["paths"]) or ["**/*"],
        "exclude_globs": _cap(exclude, MAX_GLOBS),
        "diff_only": ctx.diff_only,
        "site_exists": surface,
    }
    return _finish(ctx, p, natural, fired, evidence, detail)


# ── 4. content_absent — buckets: constraint, method ──────────────────────────
def _det_content_absent(ctx: DetectorContext, p: dict) -> DetectorResult:
    hits, exclude, surface = _content_scan(ctx, p)
    patterns = list(p["patterns"])
    offending = [pat for pat in patterns if hits[pat]]
    fired = not offending

    # Absence is only evidence of compliance where the code exists to be checked
    # and the agent actually wrote something. An empty workspace satisfies every
    # "never call X" mandate trivially, which is precisely a censored run.
    natural = surface and (bool(ctx.changed_files()) if p["require_diff"] else True)
    evidence = [f"{pat} -> {h}" for pat in offending for h in hits[pat][:5]]
    if not evidence:
        evidence = [f"absent: {pat!r}" for pat in _cap(patterns)]
    detail = {
        "patterns": patterns,
        "offending_patterns": offending,
        "counts": {pat: len(hits[pat]) for pat in patterns},
        "require_code": bool(p["require_code"]),
        "require_diff": bool(p["require_diff"]),
        "include_globs": list(p["paths"]) or ["**/*"],
        "exclude_globs": _cap(exclude, MAX_GLOBS),
        "diff_only": ctx.diff_only,
        "site_exists": surface,
    }
    return _finish(ctx, p, natural, fired, evidence, detail)


# ── 5. command_order — bucket: ordering ──────────────────────────────────────
def _det_command_order(ctx: DetectorContext, p: dict) -> DetectorResult:
    segs = ctx.bash_segments()
    first_rx = re.compile(p["first"], re.IGNORECASE)
    then_rx = re.compile(p["then"], re.IGNORECASE)
    allow_compound = bool(p["allow_compound"])

    firsts = [s for s in segs if first_rx.search(s.text)]
    thens = [s for s in segs if then_rx.search(s.text)]

    # `then` is the site: the ordering claim is only observable once the second
    # command ran. A run that never got as far as the tests cannot be scored on
    # "migrate before tests" — that is censoring, not disobedience.
    natural = bool(thens)

    checked = thens if p["all_occurrences"] else thens[:1]
    bad: list[BashSegment] = []
    for t in checked:
        if allow_compound:
            ok = any(f.ordinal < t.ordinal for f in firsts)
        else:
            ok = any(f.call_idx < t.call_idx for f in firsts)
        if not ok:
            bad.append(t)
    fired = bool(checked) and not bad and (bool(firsts) if p["require_first"] else True)

    evidence = [f"#{s.ordinal} [call {s.call_idx}] {s.text}" for s in _cap(segs)]
    detail = {
        "first": p["first"],
        "then": p["then"],
        "allow_compound": allow_compound,
        "all_occurrences": bool(p["all_occurrences"]),
        "require_first": bool(p["require_first"]),
        "first_hits": [s.to_dict() for s in _cap(firsts)],
        "then_hits": [s.to_dict() for s in _cap(thens)],
        "unordered_then": [s.to_dict() for s in _cap(bad)],
        "n_segments": len(segs),
        "n_bash_calls": len(ctx.bash_commands),
        "compound_compliance": bool(
            allow_compound
            and any(f.call_idx == t.call_idx and f.seg_idx < t.seg_idx for f in firsts for t in checked)
        ),
    }
    return _finish(ctx, p, natural, fired, evidence, detail)


# ── 6. command_used — buckets: method, hidden_cue ────────────────────────────
def _det_command_used(ctx: DetectorContext, p: dict) -> DetectorResult:
    segs = ctx.bash_segments()
    patterns = list(p["patterns"])
    forbidden = list(p["forbidden"])
    unit = (
        [BashSegment(i, i, 0, c) for i, c in enumerate(ctx.bash_commands)]
        if p["scope"] == "command"
        else segs
    )
    hits = {pat: [s.text for s in unit if re.search(pat, s.text, re.IGNORECASE)][:8]
            for pat in patterns}
    forb = {pat: [s.text for s in unit if re.search(pat, s.text, re.IGNORECASE)][:8]
            for pat in forbidden}
    matched = [pat for pat, v in hits.items() if v]
    breached = [pat for pat, v in forb.items() if v]
    ok = (len(matched) == len(patterns)) if p["mode"] == "all" else bool(matched)
    fired = ok and not breached

    # The site is "the agent ran a shell command at all". No Bash calls means
    # the command-shaped mandate never became observable.
    natural = bool(ctx.bash_commands)
    evidence = [f"{pat} -> {t}" for pat, v in hits.items() for t in v[:3]] + [
        f"FORBIDDEN {pat} -> {t}" for pat, v in forb.items() for t in v[:3]
    ]
    if not evidence:
        evidence = [f"no command matched {pat!r}" for pat in _cap(patterns)]
    detail = {
        "patterns": patterns,
        "forbidden": forbidden,
        "mode": p["mode"],
        "scope": p["scope"],
        "matched_patterns": matched,
        "breached_patterns": breached,
        "n_segments": len(segs),
        "n_bash_calls": len(ctx.bash_commands),
    }
    return _finish(ctx, p, natural, fired, evidence, detail)


# ── the registry (CLOSED — exactly six) ──────────────────────────────────────
_SPECS: tuple[DetectorSpec, ...] = (
    DetectorSpec(
        name="path_untouched",
        buckets=("constraint",),
        diff_native=True,
        doc="Fires when NO protected path appears in the diff. The 'do not "
            "modify X' mandate: X is generated / vendored / owned elsewhere. "
            "Deletes and renames count as modification.",
        params=(
            ParamSpec("paths", "list[str]", required=True,
                      doc="Globs of the protected paths."),
            ParamSpec("allow_new", "bool", default=False,
                      doc="Creating a NEW file under the glob is permitted; only "
                          "edits/deletes/renames of pre-existing files violate."),
        ),
        fn=_det_path_untouched,
    ),
    DetectorSpec(
        name="diff_confined",
        buckets=("constraint",),
        diff_native=True,
        doc="Fires when every changed path matches `allowed` and every "
            "`required` glob was touched. The 'put it here, not there' mandate: "
            "extend via a new module rather than editing core.",
        params=(
            ParamSpec("allowed", "list[str]", default=[],
                      doc="Allowlist of globs; empty means unrestricted."),
            ParamSpec("required", "list[str]", default=[],
                      doc="Globs that must each be touched by the diff."),
        ),
        fn=_det_diff_confined,
    ),
    DetectorSpec(
        name="content_present",
        buckets=("method", "hidden_cue"),
        doc="Fires when the mandated construct appears in the workspace (or, "
            "under diff_only, in the added lines). The 'use FTS5' mandate and "
            "the hidden-cue mandate ('the canonical key is X').",
        params=(
            ParamSpec("patterns", "list[regex]", required=True,
                      doc="Regexes, matched per line, case-insensitive by default."),
            ParamSpec("mode", "enum", default="any", choices=("any", "all")),
            ParamSpec("paths", "list[str]", default=[],
                      doc="Where to look; empty means the whole workspace."),
            ParamSpec("exclude_paths", "list[str]", default=[],
                      doc="Extra globs to skip, on top of the build/vcs defaults."),
            ParamSpec("exclude_planted", "bool", default=True,
                      doc="Skip the planted carrier files. ON by default: a fact "
                          "sitting in NOTES.md is `available`, never `used`."),
            ParamSpec("require_code", "bool", default=False,
                      doc="Ignore matches inside comments and Python docstrings, "
                          "so '# TODO: use FTS5' is not scored as use."),
            ParamSpec("min_count", "int", default=1,
                      doc="Matching lines needed before a pattern counts."),
            ParamSpec("ignore_case", "bool", default=True),
        ),
        fn=_det_content_present,
    ),
    DetectorSpec(
        name="content_absent",
        buckets=("constraint", "method"),
        doc="Fires when a forbidden construct appears NOWHERE in the site. The "
            "'never call time.sleep in the scheduler' mandate. Eligibility is "
            "the whole point here: an empty workspace satisfies it trivially.",
        params=(
            ParamSpec("patterns", "list[regex]", required=True,
                      doc="Regexes that must not match anywhere in the site."),
            ParamSpec("paths", "list[str]", default=[],
                      doc="The site; empty means the whole workspace."),
            ParamSpec("exclude_paths", "list[str]", default=[]),
            ParamSpec("exclude_planted", "bool", default=True),
            ParamSpec("require_code", "bool", default=False),
            ParamSpec("require_diff", "bool", default=True,
                      doc="Also require a non-empty diff, so a run that changed "
                          "nothing is censored rather than scored compliant."),
            ParamSpec("ignore_case", "bool", default=True),
        ),
        fn=_det_content_absent,
    ),
    DetectorSpec(
        name="command_order",
        buckets=("ordering",),
        diff_native=True,
        doc="Fires when every `then` command was preceded by a `first` command. "
            "The 'run the migration before the tests' mandate. A compound "
            "`first && then` in ONE Bash call counts as compliance unless "
            "allow_compound is false.",
        params=(
            ParamSpec("first", "str", required=True,
                      doc="Regex identifying the command that must come first."),
            ParamSpec("then", "str", required=True,
                      doc="Regex identifying the command that must come second."),
            ParamSpec("allow_compound", "bool", default=True,
                      doc="`a && b` in a single Bash call satisfies the order."),
            ParamSpec("all_occurrences", "bool", default=True,
                      doc="Check EVERY `then`, not just the first one."),
            ParamSpec("require_first", "bool", default=True,
                      doc="The `first` command must have been issued at all."),
        ),
        fn=_det_command_order,
    ),
    DetectorSpec(
        name="command_used",
        buckets=("method", "hidden_cue"),
        diff_native=True,
        doc="Fires when the mandated command/flag was invoked and no forbidden "
            "alternative was. The 'run it through scripts/reindex.sh' and "
            "'pass -p no:randomly' mandates.",
        params=(
            ParamSpec("patterns", "list[regex]", required=True,
                      doc="Regexes over the ordered Bash commands."),
            ParamSpec("mode", "enum", default="any", choices=("any", "all")),
            ParamSpec("forbidden", "list[regex]", default=[],
                      doc="If any matches, the detector does not fire even when "
                          "`patterns` did — 'use X INSTEAD OF Y'."),
            ParamSpec("scope", "enum", default="segment", choices=("segment", "command"),
                      doc="segment: match per simple command (compound-split). "
                          "command: match the whole Bash tool input."),
        ),
        fn=_det_command_used,
    ),
)

REGISTRY: dict[str, DetectorSpec] = {s.name: s for s in _SPECS}
DETECTOR_NAMES: tuple[str, ...] = tuple(REGISTRY)
N_DETECTORS = 6

assert len(REGISTRY) == N_DETECTORS, (
    f"the registry is CLOSED at {N_DETECTORS} predicates (§4.3); found {len(REGISTRY)}"
)
assert {b for s in _SPECS for b in s.buckets} == set(BUCKETS), (
    "the six predicates must cover all four fact buckets (§6.4)"
)


def registry_manifest() -> dict:
    """The structural description of the registry — hashed into use_detect.json."""
    return {
        "registry_version": REGISTRY_VERSION,
        "n_detectors": len(REGISTRY),
        "detectors": [REGISTRY[n].to_dict() for n in DETECTOR_NAMES],
    }


def registry_sha256() -> str:
    blob = json.dumps(registry_manifest(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def module_sha256() -> str:
    """sha256 of this file's bytes — a silent predicate edit invalidates it."""
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        return ""


# ── the one entry point ──────────────────────────────────────────────────────
def run_detector(
    name: str, params: dict | None, ctx: DetectorContext, *, diff_only: bool = False
) -> dict:
    """Run one registry predicate. Never raises for a predicate-internal fault.

    Returns {name, params, params_sha256, eligible, fired, evidence, detail,
    error, diff_only}. `error` non-null means the measurement failed and the
    caller must record used=null with exclusion_reason="detector_error" — it is
    NOT a non-fire.
    """
    spec = REGISTRY.get(name)
    if spec is None:
        raise UnknownDetector(
            f"unknown detector {name!r}; the registry is closed: {list(DETECTOR_NAMES)}"
        )
    norm = normalize_params(name, params)
    run_ctx = ctx if ctx.diff_only == diff_only else replace(
        ctx, diff_only=diff_only, _diff_cache=None, _seg_cache=None, _file_cache={}
    )
    base = {
        "name": name,
        "bucket": spec.bucket,
        "params": norm,
        "params_sha256": params_sha256(name, norm),
        "diff_only": diff_only,
        "registry_version": REGISTRY_VERSION,
    }
    try:
        res = spec.fn(run_ctx, norm)
    except Exception as e:  # noqa: BLE001 — a broken predicate must not kill the run
        return {
            **base, "eligible": False, "fired": False, "evidence": [],
            "detail": {"exception": type(e).__name__},
            "error": f"{type(e).__name__}: {e}",
        }
    return {**base, **res.to_dict(), "error": None}


# ── CLI ──────────────────────────────────────────────────────────────────────
def _b64json(s: str) -> Any:
    return json.loads(base64.b64decode(s.encode("ascii")).decode("utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="the closed WUR `used` detector registry")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="detector names, one per line")
    sub.add_parser("registry-hash", help="the registry sha256 + module sha256")
    d = sub.add_parser("describe", help="full spec of one detector (or all)")
    d.add_argument("name", nargs="?")
    v = sub.add_parser("validate", help="check a (detector, params) binding")
    v.add_argument("--detector", required=True)
    v.add_argument("--params-json", default="{}")
    e = sub.add_parser("eval", help="run one detector against a serialized context")
    e.add_argument("--detector", required=True)
    e.add_argument("--params-b64", default=None)
    e.add_argument("--params-json", default=None)
    e.add_argument("--context", required=True, help="path to the context JSON")
    e.add_argument("--diff-only", action="store_true")
    a = p.parse_args(argv)

    if a.cmd == "list":
        for n in DETECTOR_NAMES:
            print(f"{n}\t{REGISTRY[n].bucket}")
        return 0
    if a.cmd == "registry-hash":
        print(json.dumps(
            {"registry_version": REGISTRY_VERSION,
             "detector_registry_sha256": registry_sha256(),
             "detectors_module_sha256": module_sha256()},
            indent=2, sort_keys=True))
        return 0
    if a.cmd == "describe":
        if a.name:
            if a.name not in REGISTRY:
                print(f"unknown detector {a.name!r}", file=sys.stderr)
                return 2
            print(json.dumps(REGISTRY[a.name].to_dict(), indent=2))
        else:
            print(json.dumps(registry_manifest(), indent=2))
        return 0
    if a.cmd == "validate":
        problems = validate_params(a.detector, json.loads(a.params_json))
        print(json.dumps({"detector": a.detector, "ok": not problems,
                          "problems": problems}, indent=2))
        return 0 if not problems else 1

    # eval
    try:
        params = _b64json(a.params_b64) if a.params_b64 else json.loads(a.params_json or "{}")
        ctx = DetectorContext.from_dict(json.loads(Path(a.context).read_text()))
        out = run_detector(a.detector, params, ctx, diff_only=bool(a.diff_only))
    except Exception as ex:  # noqa: BLE001
        print(json.dumps({"name": a.detector, "eligible": False, "fired": False,
                          "evidence": [], "detail": {},
                          "error": f"{type(ex).__name__}: {ex}"}, indent=2))
        print(f"detector eval failed: {type(ex).__name__}: {ex}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(out, indent=2, sort_keys=True))
    if out.get("error"):
        return EXIT_ERROR
    if not out["eligible"]:
        return EXIT_NOT_ELIGIBLE
    return EXIT_FIRED if out["fired"] else EXIT_NOT_FIRED


if __name__ == "__main__":
    raise SystemExit(main())
