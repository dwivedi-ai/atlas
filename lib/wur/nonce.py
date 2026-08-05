#!/usr/bin/env python3
"""
nonce.py — mints the tracer token the whole instrument is built on, and scans for it.

RESPONSIBILITY
  Own the one string per fact whose presence in a byte the model provably saw IS
  the definition of `read` (IMPLEMENTATION.md §4.2). Three properties are
  load-bearing and every one of them is enforced here rather than assumed:

    1. DETERMINISTIC   — minted by blake2s from (salt, repo_sha, fact_id), so a
                         re-plant of the same job produces the same token and a
                         months-later re-derivation still matches the registry.
    2. DISJOINT        — no two nonces collide, not even after lowercasing and
                         separator stripping, because match_form `nohyphen`
                         (protocol.MATCH_FORMS) would otherwise attribute one
                         fact's hit to another fact.
    3. ABSENT          — the nonce does not already occur in the pinned baseline
                         tree. A nonce that is already in the repo makes every
                         run look like a read: it silently inflates read-rate,
                         which is the numerator of almost every metric in
                         STATUS.md §5. This check is NOT optional.

INPUTS
  fact_id + repo_sha + salt                 -> mint()
  {label: nonce} (+ optional surface forms) -> NonceSet
  a git dir + a tree-ish                    -> NonceSet.assert_absent_from_repo()
  arbitrary text                            -> NonceSet.find()

OUTPUTS
  Nonce strings of the shape `ZQ-4KM7TXP2` (prefix, hyphen, base-32 body).
  NonceHit records {label, nonce, form, start, end, text} whose `form` is drawn
  from protocol.MATCH_FORMS (exact | lower | nohyphen) — only `exact` inbound
  hits set first_exposure_seq (§4.5), so the form is reported, never collapsed.
  Report dicts from the assert_* helpers; the assert_* helpers RAISE on failure.

WHY THE ALPHABET LOOKS LIKE THAT
  Base-32 over A-Z minus I and O, plus 2-9: no character pair that survives an
  OCR-grade or model-grade transcription slip (0/O, 1/I/l), and no lowercase, so
  a `lower` match form is always evidence that something re-typed the token
  rather than copied it. The body is required to mix letters and digits so the
  nonce can never read as an English word, and `WURP` is banned outright — that
  is protocol.PROBE_ID_PREFIX, and a nonce containing it would make a probe id
  and a fact indistinguishable to a grep.

CLI
  python3 lib/wur/nonce.py mint --fact-id F --repo-sha SHA [--salt S]
  python3 lib/wur/nonce.py scan --nonce N [--nonce N ...] [FILE]     (stdin if no FILE)
  python3 lib/wur/nonce.py check-repo --repo-dir D --sha SHA --nonce N [--nonce N ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

# ── the minting alphabet ─────────────────────────────────────────────────────
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 24 letters (no I, no O) + 2..9
assert len(ALPHABET) == 32, "base-32 alphabet must have exactly 32 symbols"

DEFAULT_PREFIX = "ZQ"
DEFAULT_BODY_LEN = 8          # 8 * log2(32) = 40 bits
DEFAULT_SALT = "wur-v1"
PERSON = b"wurnonce"          # blake2s personalization is exactly 8 bytes
DIGEST_SIZE = 16
MAX_MINT_ATTEMPTS = 256

# `WURP` is protocol.PROBE_ID_PREFIX without its hyphen. A nonce carrying it
# would be indistinguishable from a probe id under a case-insensitive grep.
BANNED_SUBSTRINGS = ("WURP",)

NONCE_RE = re.compile(r"^[A-HJ-NP-Z]{2,4}-[A-HJ-NP-Z2-9]{6,16}$")

# Separators tolerated INSIDE a nonce when scanning. Deliberately bounded and
# newline-free: `nohyphen` must catch `ZQ 4KM7TXP2` and `ZQ4KM7TXP2` without
# letting a match sprawl across a paragraph.
_SEP_CLASS = r"[-_ ]{0,2}"
_STRIP_SEP_RE = re.compile(r"[\s_\-]+")

MATCH_FORMS = ("exact", "lower", "nohyphen")  # subset of protocol.MATCH_FORMS


class NonceError(Exception):
    """Base class for every failure this module raises."""


class NonceCollision(NonceError):
    """Two nonces are not disjoint under exact/lower/nohyphen normalization."""


class NonceInRepo(NonceError):
    """A nonce already occurs in the pinned baseline tree — read-rate would inflate."""


class NonceLeak(NonceError):
    """A nonce occurs in text the harness itself puts in front of the model."""


class MintFailed(NonceError):
    """No acceptable token after MAX_MINT_ATTEMPTS — widen body_len or change salt."""


# ── normalization ────────────────────────────────────────────────────────────
def strip_sep(s: str) -> str:
    """Lowercase with whitespace/underscore/hyphen removed — the `nohyphen` form.

    Mirrors protocol._strip_sep exactly; the two must agree or a slot judged
    `nohyphen` by protocol.match_slot would be judged `None` here.
    """
    return _STRIP_SEP_RE.sub("", (s or "")).lower()


def variants(nonce: str) -> dict[str, str]:
    """The three literal forms of `nonce`, keyed by match form."""
    return {"exact": nonce, "lower": nonce.lower(), "nohyphen": strip_sep(nonce)}


def match_form(matched: str, nonce: str) -> str | None:
    """Which of exact | lower | nohyphen `matched` is, relative to `nonce`."""
    if matched == nonce:
        return "exact"
    if matched.lower() == nonce.lower():
        return "lower"
    if strip_sep(matched) == strip_sep(nonce):
        return "nohyphen"
    return None


def loose_pattern(token: str) -> str:
    """Regex source matching `token` with optional short separators between chars.

    One pattern therefore finds all three match forms in a single pass, with real
    offsets, and the form is decided afterwards from the matched substring.
    """
    chars = [re.escape(c) for c in token if not _STRIP_SEP_RE.fullmatch(c)]
    if not chars:
        raise ValueError(f"token has no matchable characters: {token!r}")
    return _SEP_CLASS.join(chars)


# ── minting ──────────────────────────────────────────────────────────────────
def _encode(digest: bytes, n: int) -> str:
    v = int.from_bytes(digest, "big")
    out: list[str] = []
    for _ in range(n):
        v, r = divmod(v, len(ALPHABET))
        out.append(ALPHABET[r])
    return "".join(reversed(out))


def _acceptable(nonce: str, body: str) -> bool:
    if any(bad in nonce for bad in BANNED_SUBSTRINGS):
        return False
    if not any(c.isdigit() for c in body):
        return False       # never reads as an English word
    if not any(c.isalpha() for c in body):
        return False       # never reads as a bare number
    if re.search(r"(.)\1\1", body):
        return False       # no ZZZ runs: they invite transcription slips
    return bool(NONCE_RE.fullmatch(nonce))


def mint(
    fact_id: str,
    repo_sha: str,
    salt: str = DEFAULT_SALT,
    *,
    prefix: str = DEFAULT_PREFIX,
    body_len: int = DEFAULT_BODY_LEN,
    avoid: Iterable[str] = (),
) -> str:
    """Deterministically mint the nonce for one fact.

    material = f"{salt}|{repo_sha}|{fact_id}|{attempt}" through blake2s
    personalized with PERSON. `attempt` only advances when a candidate is
    rejected (banned substring, all-letter or all-digit body, triple run, or a
    loose collision with `avoid`), so the common case is attempt 0 and the token
    is a pure function of its inputs.

    §6.4: the fixture ships as a tree plus a deterministic builder precisely so
    `repo_sha` is stable — mint() is the reason that matters.
    """
    if not fact_id:
        raise ValueError("fact_id must be non-empty")
    if not repo_sha:
        raise ValueError("repo_sha must be non-empty (§6.4: the mint needs a stable repo_sha)")
    if not re.fullmatch(r"[A-HJ-NP-Z]{2,4}", prefix):
        raise ValueError(f"prefix must be 2-4 chars from the nonce alphabet: {prefix!r}")
    if not 6 <= body_len <= 16:
        raise ValueError(f"body_len out of range: {body_len}")

    blocked = [a for a in avoid if a]
    for attempt in range(MAX_MINT_ATTEMPTS):
        material = f"{salt}|{repo_sha}|{fact_id}|{attempt}".encode("utf-8")
        digest = hashlib.blake2s(material, digest_size=DIGEST_SIZE, person=PERSON).digest()
        body = _encode(digest, body_len)
        candidate = f"{prefix}-{body}"
        if not _acceptable(candidate, body):
            continue
        if any(_loosely_equal(candidate, other) for other in blocked):
            continue
        return candidate
    raise MintFailed(
        f"no acceptable nonce for {fact_id!r} after {MAX_MINT_ATTEMPTS} attempts"
    )


def is_wellformed(nonce: str) -> bool:
    """True iff `nonce` has the minted shape (shape only — says nothing about origin)."""
    return bool(nonce) and bool(NONCE_RE.fullmatch(nonce))


def _loosely_equal(a: str, b: str) -> bool:
    return strip_sep(a) == strip_sep(b)


def _loosely_contains(hay: str, needle: str) -> bool:
    h, n = strip_sep(hay), strip_sep(needle)
    return bool(n) and n in h


# ── hits ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class NonceHit:
    """One occurrence of one fact's token in one text region."""

    label: str        # fact_id (or `<fact_id>:distractor:<token>` for distractors)
    nonce: str        # the canonical form that was searched for
    form: str         # exact | lower | nohyphen  (protocol.MATCH_FORMS)
    start: int
    end: int
    text: str         # the literal substring found

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "nonce": self.nonce,
            "form": self.form,
            "start": self.start,
            "end": self.end,
            "text": self.text,
        }


# ── the set ──────────────────────────────────────────────────────────────────
class NonceSet:
    """Every token a run scans for, compiled once into a single alternation.

    `surface_forms` are extra literals declared in facts.yaml that count as the
    same fact (§4.5 tier (a) accepts them). They are searched with the same loose
    pattern and reported under the same label, but `nonce` on the hit is always
    the canonical token, so downstream never has to re-map.
    """

    def __init__(
        self,
        nonces: Mapping[str, str] | Iterable[tuple[str, str]],
        *,
        repo_dir: str | Path | None = None,
        surface_forms: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        items = list(nonces.items()) if isinstance(nonces, Mapping) else list(nonces)
        self._by_label: dict[str, str] = {}
        for label, nonce in items:
            if not label or not nonce:
                raise ValueError(f"empty label or nonce in NonceSet: {label!r} -> {nonce!r}")
            if label in self._by_label:
                raise NonceCollision(f"duplicate label in NonceSet: {label!r}")
            self._by_label[str(label)] = str(nonce)
        self._forms: dict[str, tuple[str, ...]] = {}
        sf = dict(surface_forms or {})
        for label, nonce in self._by_label.items():
            extra = tuple(str(s) for s in sf.get(label, ()) if s)
            self._forms[label] = (nonce, *extra)
        self.repo_dir = Path(repo_dir) if repo_dir is not None else None
        self._pattern, self._group_to_label = self._compile()

    # ── construction helpers ──
    def _compile(self) -> tuple[re.Pattern[str] | None, dict[str, str]]:
        alts: list[tuple[str, str, str]] = []  # (form_literal, label, group_name)
        for label, forms in self._forms.items():
            for form in forms:
                alts.append((form, label, ""))
        if not alts:
            return None, {}
        # Longest literal first so a token that is a prefix of another cannot
        # shadow it. assert_disjoint() forbids containment among nonces, but a
        # declared surface form may legitimately be a prefix of the nonce.
        alts.sort(key=lambda t: (-len(strip_sep(t[0])), t[0]))
        parts: list[str] = []
        mapping: dict[str, str] = {}
        for i, (form, label, _g) in enumerate(alts):
            g = f"n{i}"
            mapping[g] = label
            parts.append(f"(?P<{g}>{loose_pattern(form)})")
        return re.compile("|".join(parts), re.IGNORECASE), mapping

    # ── read-only views ──
    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(self._by_label)

    @property
    def nonces(self) -> tuple[str, ...]:
        return tuple(self._by_label.values())

    @property
    def pattern(self) -> re.Pattern[str] | None:
        """The compiled alternation. None when the set is empty (a control job)."""
        return self._pattern

    def forms_of(self, label: str) -> tuple[str, ...]:
        return self._forms[label]

    def nonce_of(self, label: str) -> str:
        return self._by_label[label]

    def to_dict(self) -> dict:
        return {
            "nonces": dict(self._by_label),
            "surface_forms": {k: list(v[1:]) for k, v in self._forms.items() if len(v) > 1},
        }

    def __len__(self) -> int:
        return len(self._by_label)

    def __contains__(self, label: object) -> bool:
        return label in self._by_label

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"NonceSet({self._by_label!r})"

    # ── scanning ──
    def find(self, text: str) -> list[NonceHit]:
        """Every hit in `text`, in offset order, with its match form resolved.

        This is the single scanning primitive: exposure.py runs it over
        model-visible regions, plant.py runs it over rendered overlay files, and
        facts.py runs it over the harness's own prompts. One implementation
        means a leak and an exposure can never disagree about what a hit is.
        """
        if not text or self._pattern is None:
            return []
        hits: list[NonceHit] = []
        for m in self._pattern.finditer(text):
            g = m.lastgroup
            if g is None:  # defensive: alternation always fills exactly one group
                g = next((k for k, v in m.groupdict().items() if v is not None), None)
            if g is None:
                continue
            label = self._group_to_label.get(g)
            if label is None:
                continue
            matched = m.group(g)
            canonical = self._by_label[label]
            form = match_form(matched, canonical)
            if form is None:
                # Matched a declared surface form rather than the nonce itself;
                # grade it against that form and report it as the fact's hit.
                for f in self._forms[label]:
                    form = match_form(matched, f)
                    if form is not None:
                        break
            if form is None:
                form = "nohyphen"
            hits.append(
                NonceHit(
                    label=label,
                    nonce=canonical,
                    form=form,
                    start=m.start(),
                    end=m.end(),
                    text=matched,
                )
            )
        return hits

    def occurs_in(self, text: str) -> bool:
        return bool(self.find(text))

    # ── invariant 2: disjointness ──
    def assert_disjoint(self) -> dict:
        """Raise NonceCollision unless the tokens are pairwise disjoint.

        Disjoint means more than "not equal": under match form `nohyphen` the
        comparison is lowercase with separators removed, so `ZQ-4KM7TXP2` and
        `zq4km7txp2` are the same token, and a token CONTAINED in another would
        make every hit on the longer one also a hit on the shorter one. Both are
        rejected, in both directions.
        """
        problems: list[str] = []
        labels = list(self._by_label)
        for i, a in enumerate(labels):
            for b in labels[i + 1 :]:
                na, nb = self._by_label[a], self._by_label[b]
                if _loosely_equal(na, nb):
                    problems.append(f"{a} and {b} mint the same token ({na!r} ~ {nb!r})")
                elif _loosely_contains(na, nb):
                    problems.append(f"{b}'s token {nb!r} is contained in {a}'s {na!r}")
                elif _loosely_contains(nb, na):
                    problems.append(f"{a}'s token {na!r} is contained in {b}'s {nb!r}")
        # Surface forms must not steal another fact's hits either.
        for label, forms in self._forms.items():
            for form in forms[1:]:
                for other, other_nonce in self._by_label.items():
                    if other == label:
                        continue
                    if _loosely_contains(form, other_nonce) or _loosely_contains(other_nonce, form):
                        problems.append(
                            f"{label}'s surface form {form!r} overlaps {other}'s token {other_nonce!r}"
                        )
        if problems:
            raise NonceCollision("; ".join(problems))
        return {"ok": True, "checked": len(self._by_label), "problems": []}

    # ── invariant 3: absence from the pinned tree ──
    def grep_repo(
        self,
        baseline_sha: str = "HEAD",
        *,
        repo_dir: str | Path | None = None,
        pathspec: str = ".",
    ) -> dict[str, bool]:
        """`git grep -F -I -i` each label's tokens at `baseline_sha` (§4.1).

        Returns {label: found}. Searches the exact token AND its `nohyphen`
        form, because a de-hyphenated occurrence in the tree would produce
        `nohyphen` hits that look like the agent re-typing the fact.
        """
        d = Path(repo_dir) if repo_dir is not None else self.repo_dir
        if d is None:
            raise ValueError("no repo_dir: pass repo_dir= or construct NonceSet(repo_dir=...)")
        _assert_tree_exists(d, baseline_sha)
        out: dict[str, bool] = {}
        for label, forms in self._forms.items():
            patterns: list[str] = []
            for form in forms:
                for v in (form, strip_sep(form)):
                    if v and v not in patterns:
                        patterns.append(v)
            argv = ["git", "-C", str(d), "grep", "-F", "-I", "-i", "-q"]
            for p in patterns:
                argv += ["-e", p]
            argv += [baseline_sha, "--", pathspec]
            proc = subprocess.run(argv, capture_output=True, text=True)
            if proc.returncode == 0:
                out[label] = True
            elif proc.returncode == 1:
                out[label] = False
            else:
                raise NonceError(
                    f"git grep failed ({proc.returncode}) in {d} at {baseline_sha}: "
                    f"{proc.stderr.strip()}"
                )
        return out

    def assert_absent_from_repo(
        self,
        baseline_sha: str = "HEAD",
        *,
        repo_dir: str | Path | None = None,
        pathspec: str = ".",
    ) -> dict:
        """Raise NonceInRepo if any token already occurs in the pinned tree.

        A nonce that is already in the repo makes `read` fire on runs where the
        planted file was never opened — read-rate inflates silently and every
        exposure-conditioned metric in STATUS.md §5 is wrong in the same
        direction. There is no safe way to detect this after the fact, so it is
        checked before a single agent run.
        """
        found = self.grep_repo(baseline_sha, repo_dir=repo_dir, pathspec=pathspec)
        hits = sorted(label for label, ok in found.items() if ok)
        if hits:
            detail = ", ".join(f"{h}={self._by_label[h]}" for h in hits)
            raise NonceInRepo(
                f"nonce already present in {baseline_sha}: {detail} — re-mint with a "
                "different salt; leaving it would inflate read-rate on every run"
            )
        return {"ok": True, "baseline_sha": baseline_sha, "checked": sorted(found)}

    # ── leak surface ──
    def assert_absent_from_texts(self, texts: Mapping[str, str]) -> dict:
        """Raise NonceLeak if any token occurs in text the harness itself sends.

        `texts` is {source_name: text}: criteria.json, the task prompt, the
        accept text, the probe text and the self-analysis prompt (§5.2). A nonce
        in any of them means the model can name the fact without ever having read
        the workspace — which is exactly the failure the confab_rate <= 0.05
        pilot gate is built to catch, arriving through a channel the gate cannot
        see.
        """
        leaks: list[dict] = []
        for source, text in (texts or {}).items():
            for hit in self.find(text or ""):
                leaks.append({"source": source, **hit.to_dict()})
        if leaks:
            where = ", ".join(sorted({f"{d['source']}:{d['label']}" for d in leaks}))
            raise NonceLeak(f"nonce leaked into harness text: {where}")
        return {"ok": True, "sources": sorted(texts or {}), "leaks": []}


def _assert_tree_exists(repo_dir: Path, rev: str) -> None:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--verify", "--quiet", f"{rev}^{{tree}}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise NonceError(f"{rev!r} does not name a tree in {repo_dir}")


# ── CLI ──────────────────────────────────────────────────────────────────────
def _cli_mint(a: argparse.Namespace) -> int:
    n = mint(a.fact_id, a.repo_sha, a.salt, prefix=a.prefix, body_len=a.len)
    print(n)
    return 0


def _cli_scan(a: argparse.Namespace) -> int:
    text = Path(a.file).read_text(encoding="utf-8", errors="replace") if a.file else sys.stdin.read()
    ns = NonceSet({f"n{i}": v for i, v in enumerate(a.nonce)})
    hits = [h.to_dict() for h in ns.find(text)]
    print(json.dumps({"hits": hits, "count": len(hits)}, indent=2))
    return 0 if not hits else 2


def _cli_check_repo(a: argparse.Namespace) -> int:
    ns = NonceSet({f"n{i}": v for i, v in enumerate(a.nonce)}, repo_dir=a.repo_dir)
    try:
        rep = ns.assert_absent_from_repo(a.sha)
    except NonceInRepo as e:
        print(f"NONCE IN REPO: {e}", file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return 1
    print(json.dumps(rep, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="WUR nonce minting and scanning")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mint", help="deterministically mint one nonce")
    m.add_argument("--fact-id", required=True)
    m.add_argument("--repo-sha", required=True)
    m.add_argument("--salt", default=DEFAULT_SALT)
    m.add_argument("--prefix", default=DEFAULT_PREFIX)
    m.add_argument("--len", type=int, default=DEFAULT_BODY_LEN)
    m.set_defaults(fn=_cli_mint)

    s = sub.add_parser("scan", help="find nonces in a file or stdin (exit 2 on a hit)")
    s.add_argument("--nonce", action="append", required=True)
    s.add_argument("file", nargs="?")
    s.set_defaults(fn=_cli_scan)

    c = sub.add_parser("check-repo", help="assert nonces are absent from a tree-ish")
    c.add_argument("--repo-dir", required=True)
    c.add_argument("--sha", default="HEAD")
    c.add_argument("--nonce", action="append", required=True)
    c.set_defaults(fn=_cli_check_repo)

    a = p.parse_args(argv)
    return int(a.fn(a))


if __name__ == "__main__":
    raise SystemExit(main())
