#!/usr/bin/env python3
"""
test_detectors.py — the Phase-1 gate for the `used` half of the funnel.

RESPONSIBILITY
  Prove the closed detector registry does the two things a detector can silently
  fail to do: fire on real compliance, and NOT fire on everything else. Phase 1's
  gate (IMPLEMENTATION.md §9) is "23 base assertions + 7 misclassifications
  green; battery.py no longer false-PASSes on eval error", so this file is
  organised as exactly that:

    BASE          — registry closure, param validation, the primitives
                    (globs, diff parsing, shell segmentation, comment stripping),
                    a fire and a non-fire for each of the six predicates, the
                    eligibility/censoring rule, and the two battery.py fixes.
    MISCLASSIFY   — seven cases a plausible naive detector gets WRONG. Each one
                    is a bug that would have shipped: a match in a comment, a
                    match in the planted file itself, a compound `a && b`, a
                    rename, a removed line, a deep path, a re-run out of order.
    CORPUS        — a compliance corpus: several genuinely different correct
                    solutions and several genuinely different wrong ones, all
                    separated by one unchanged detector binding.
    ENDTOEND      — detect_use.py through lib/battery.py to use_detect.json, and
                    verify_pack.py's truth table + --prior-check.

INPUTS  none — every fixture is built in a temp dir with real git.
OUTPUTS unittest results.

RUN
  python3 -m unittest discover -s tests -v
  python3 tests/test_detectors.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "lib" / "wur"))

import battery  # noqa: E402
import detect_use  # noqa: E402
import detectors  # noqa: E402
import verify_pack  # noqa: E402
from detectors import (  # noqa: E402
    DetectorContext,
    UnknownDetector,
    parse_unified_diff,
    path_matches,
    run_detector,
    split_shell_segments,
    strip_comments,
    validate_params,
)


# ── fixture helpers ──────────────────────────────────────────────────────────
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, env=_GIT_ENV)


class Sandbox:
    """A real git worktree with a real baseline commit and a real diff.

    Nothing here is a mock: the diff the detectors read is produced by git and
    parsed by the same parse_unified_diff() the harness uses, so a diff-format
    surprise fails the test rather than the experiment.
    """

    def __init__(self, files: dict[str, str]):
        self.dir = Path(tempfile.mkdtemp(prefix="wur-test-"))
        self.write(files)
        _git(["init", "-q"], self.dir)
        _git(["add", "-A"], self.dir)
        _git(["commit", "-q", "-m", "base"], self.dir)
        self.baseline = _git(["rev-parse", "HEAD"], self.dir).stdout.strip()

    def write(self, files: dict[str, str | None]) -> "Sandbox":
        for rel, body in files.items():
            p = self.dir / rel
            if body is None:
                if p.exists():
                    p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        return self

    def rename(self, a: str, b: str) -> "Sandbox":
        (self.dir / b).parent.mkdir(parents=True, exist_ok=True)
        _git(["mv", a, b], self.dir)
        return self

    def diff(self) -> str:
        return detect_use.compute_diff(self.dir, self.baseline)

    def ctx(self, bash=(), planted=(), diff_only=False) -> DetectorContext:
        return DetectorContext(
            workspace=self.dir, diff_text=self.diff(), bash_commands=list(bash),
            planted_paths=list(planted), baseline_sha=self.baseline,
            scope="test", diff_only=diff_only,
        )

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


BASE_FILES = {
    "ledgerline/__init__.py": "VERSION = '0.1'\n",
    "ledgerline/core/rates.py": "# GENERATED — do not edit by hand\nRATES = {'usd': 1.0}\n",
    "ledgerline/search.py": "def search(db, q):\n    return db.execute('SELECT 1')\n",
    "docs/NOTES.md": "Some project notes.\n",
    "docs/internal/filler.md": "filler\n",
    "tests/test_search.py": "def test_ok():\n    assert True\n",
    "README.md": "ledgerline\n",
}


def fresh(extra: dict | None = None) -> Sandbox:
    return Sandbox({**BASE_FILES, **(extra or {})})


# Params reused across tests, so a "fires here / does not fire there" pair is
# demonstrably the SAME binding and not two differently-tuned ones.
P_UNTOUCHED = {"paths": ["ledgerline/core/**"]}
P_FTS5 = {"patterns": [r"\bfts5\b"], "paths": ["ledgerline/**/*.py"], "require_code": True}
P_NOSLEEP = {"patterns": [r"time\.sleep\s*\("], "paths": ["ledgerline/**/*.py"]}
P_ORDER = {"first": r"\bmigrate\b", "then": r"\bpytest\b"}
P_CMD = {"patterns": [r"scripts/reindex\.sh"]}
P_CONFINED = {"allowed": ["ledgerline/ext/**", "tests/**"], "required": ["ledgerline/ext/**"]}


def fire(name, params, sb, **kw) -> dict:
    return run_detector(name, params, sb.ctx(**kw))


# ═════════════════════════════════════════════════════════════════════════════
class TestRegistryClosure(unittest.TestCase):
    """BASE 1-9 — the registry is closed and its params are a contract."""

    def test_01_exactly_six_predicates(self):
        self.assertEqual(len(detectors.REGISTRY), 6)
        self.assertEqual(len(detectors.DETECTOR_NAMES), 6)

    def test_02_covers_the_four_fact_buckets(self):
        covered = {b for s in detectors.REGISTRY.values() for b in s.buckets}
        self.assertEqual(covered, {"constraint", "method", "ordering", "hidden_cue"})

    def test_03_unknown_detector_name_raises(self):
        with self.assertRaises(UnknownDetector):
            run_detector("llm_judges_it", {}, fresh().ctx())
        self.assertTrue(validate_params("llm_judges_it", {}))

    def test_04_unknown_param_key_rejected(self):
        problems = validate_params("content_present", {"patterns": ["x"], "command": "rm -rf /"})
        self.assertTrue(any("unknown param 'command'" in p for p in problems))

    def test_05_missing_required_param_rejected(self):
        self.assertTrue(any("patterns" in p for p in validate_params("content_present", {})))
        self.assertTrue(any("then" in p for p in validate_params("command_order", {"first": "a"})))

    def test_06_wrong_param_type_rejected(self):
        self.assertTrue(validate_params("content_present", {"patterns": "notalist"}))
        self.assertTrue(validate_params("content_present", {"patterns": ["x"], "min_count": "3"}))
        self.assertTrue(validate_params("path_untouched", {"paths": ["a"], "allow_new": "yes"}))

    def test_07_uncompilable_regex_rejected(self):
        problems = validate_params("content_present", {"patterns": ["("]})
        self.assertTrue(any("not a valid regex" in p for p in problems))

    def test_08_disabling_natural_eligibility_requires_a_replacement(self):
        bad = validate_params("content_present", {"patterns": ["x"], "natural_eligibility": False})
        self.assertTrue(any("eligible_when" in p for p in bad))
        ok = validate_params("content_present", {
            "patterns": ["x"], "natural_eligibility": False,
            "eligible_when": {"any_diff": True}})
        self.assertEqual(ok, [])

    def test_09_registry_hash_is_deterministic_and_params_hash_is_order_free(self):
        self.assertEqual(detectors.registry_sha256(), detectors.registry_sha256())
        a = detectors.params_sha256("content_present", {"patterns": ["x"], "mode": "any"})
        b = detectors.params_sha256("content_present", {"mode": "any", "patterns": ["x"]})
        self.assertEqual(a, b)


class TestPrimitives(unittest.TestCase):
    """BASE 10-14 — the four primitives every predicate rests on."""

    def test_10_glob_star_star_crosses_separators_single_star_does_not(self):
        self.assertTrue(path_matches("a/b/c.py", ["a/**"]))
        self.assertTrue(path_matches("a/b/c.py", ["a/**/*.py"]))
        self.assertFalse(path_matches("a/b/c.py", ["a/*.py"]))
        self.assertTrue(path_matches("a/c.py", ["a/*.py"]))

    def test_11_bare_pattern_matches_basename_and_dotfiles_survive(self):
        self.assertTrue(path_matches("deep/nested/NOTES.md", ["NOTES.md"]))
        self.assertFalse(path_matches("deep/nested/OTHER.md", ["NOTES.md"]))
        self.assertTrue(path_matches(".gitignore", [".gitignore"]))
        self.assertTrue(path_matches("./src/x.py", ["src/x.py"]))

    def test_12_diff_parser_reads_add_modify_delete_rename(self):
        sb = fresh()
        sb.write({"ledgerline/new.py": "x = 1\n", "README.md": "changed\n",
                  "docs/internal/filler.md": None})
        sb.rename("ledgerline/search.py", "ledgerline/find.py")
        by_path = {f.path: f for f in parse_unified_diff(sb.diff())}
        self.assertEqual(by_path["ledgerline/new.py"].status, "added")
        self.assertEqual(by_path["README.md"].status, "modified")
        self.assertEqual(by_path["docs/internal/filler.md"].status, "deleted")
        self.assertIn("ledgerline/find.py", by_path)
        self.assertEqual([t for _n, t in by_path["ledgerline/new.py"].added], ["x = 1"])
        sb.close()

    def test_13_shell_segmentation_splits_operators_and_respects_quotes(self):
        self.assertEqual(split_shell_segments("python migrate.py && pytest -q"),
                         ["python migrate.py", "pytest -q"])
        self.assertEqual(split_shell_segments("a; b | c || d"), ["a", "b", "c", "d"])
        self.assertEqual(split_shell_segments("echo 'a && b'"), ["echo 'a && b'"])
        self.assertEqual(split_shell_segments('echo "x;y" && ls'), ['echo "x;y"', "ls"])

    def test_14_comment_stripping_preserves_line_numbers(self):
        src = "import x\n# use fts5 here\ny = 1  # fts5\n'''\nfts5 docstring\n'''\nz = 'fts5'\n"
        out = strip_comments(src, ".py")
        self.assertEqual(len(out.split("\n")), len(src.split("\n")))
        self.assertNotIn("fts5 here", out)
        self.assertNotIn("fts5 docstring", out)
        self.assertIn("z = 'fts5'", out)
        self.assertIn("import x", out)


class TestEachPredicateFiresAndDoesNot(unittest.TestCase):
    """BASE 15-26 — one fire and one non-fire for each of the six predicates."""

    def test_15_path_untouched_fires_when_protected_path_is_untouched(self):
        sb = fresh()
        sb.write({"ledgerline/search.py": "def search(db, q):\n    return 'fts5'\n"})
        r = fire("path_untouched", P_UNTOUCHED, sb)
        self.assertTrue(r["eligible"])
        self.assertTrue(r["fired"])
        sb.close()

    def test_16_path_untouched_does_not_fire_when_protected_path_is_edited(self):
        sb = fresh()
        sb.write({"ledgerline/core/rates.py": "RATES = {'usd': 2.0}\n"})
        r = fire("path_untouched", P_UNTOUCHED, sb)
        self.assertTrue(r["eligible"])
        self.assertFalse(r["fired"])
        self.assertEqual(r["detail"]["violations"][0]["path"], "ledgerline/core/rates.py")
        sb.close()

    def test_17_diff_confined_fires_inside_the_allowed_set(self):
        sb = fresh()
        sb.write({"ledgerline/ext/search2.py": "def s():\n    pass\n",
                  "tests/test_ext.py": "def t():\n    pass\n"})
        r = fire("diff_confined", P_CONFINED, sb)
        self.assertTrue(r["eligible"])
        self.assertTrue(r["fired"])
        sb.close()

    def test_18_diff_confined_does_not_fire_when_work_lands_outside(self):
        sb = fresh()
        sb.write({"ledgerline/ext/search2.py": "def s():\n    pass\n",
                  "ledgerline/search.py": "def search():\n    pass\n"})
        r = fire("diff_confined", P_CONFINED, sb)
        self.assertTrue(r["eligible"])
        self.assertFalse(r["fired"])
        self.assertIn("ledgerline/search.py", r["detail"]["outside"])
        sb.close()

    def test_19_content_present_fires_on_the_mandated_construct(self):
        sb = fresh()
        sb.write({"ledgerline/search.py":
                  "def build(db):\n    db.execute('CREATE VIRTUAL TABLE t USING fts5(body)')\n"})
        r = fire("content_present", P_FTS5, sb)
        self.assertTrue(r["eligible"])
        self.assertTrue(r["fired"])
        self.assertTrue(any("fts5" in e for e in r["evidence"]))
        sb.close()

    def test_20_content_present_does_not_fire_on_the_default_solution(self):
        sb = fresh()
        sb.write({"ledgerline/search.py":
                  "def build(db):\n    db.execute('SELECT * FROM t WHERE body LIKE ?')\n"})
        r = fire("content_present", P_FTS5, sb)
        self.assertTrue(r["eligible"])
        self.assertFalse(r["fired"])
        sb.close()

    def test_21_content_absent_fires_when_the_forbidden_call_is_absent(self):
        sb = fresh()
        sb.write({"ledgerline/sched.py": "def tick():\n    return 1\n"})
        r = fire("content_absent", P_NOSLEEP, sb)
        self.assertTrue(r["eligible"])
        self.assertTrue(r["fired"])
        sb.close()

    def test_22_content_absent_does_not_fire_when_the_forbidden_call_is_present(self):
        sb = fresh()
        sb.write({"ledgerline/sched.py": "import time\ndef tick():\n    time.sleep(0.1)\n"})
        r = fire("content_absent", P_NOSLEEP, sb)
        self.assertTrue(r["eligible"])
        self.assertFalse(r["fired"])
        sb.close()

    def test_23_command_order_fires_when_migrate_precedes_pytest(self):
        sb = fresh()
        sb.write({"ledgerline/ext/x.py": "x = 1\n"})
        r = fire("command_order", P_ORDER, sb, bash=["python migrate.py", "pytest -q"])
        self.assertTrue(r["eligible"])
        self.assertTrue(r["fired"])
        sb.close()

    def test_24_command_order_does_not_fire_when_pytest_precedes_migrate(self):
        sb = fresh()
        sb.write({"ledgerline/ext/x.py": "x = 1\n"})
        r = fire("command_order", P_ORDER, sb, bash=["pytest -q", "python migrate.py"])
        self.assertTrue(r["eligible"])
        self.assertFalse(r["fired"])
        sb.close()

    def test_25_command_used_fires_on_the_mandated_command(self):
        sb = fresh()
        r = fire("command_used", P_CMD, sb, bash=["ls", "bash scripts/reindex.sh --full"])
        self.assertTrue(r["eligible"])
        self.assertTrue(r["fired"])
        sb.close()

    def test_26_command_used_does_not_fire_and_honours_forbidden(self):
        sb = fresh()
        r = fire("command_used", P_CMD, sb, bash=["ls", "python -m ledgerline.reindex"])
        self.assertTrue(r["eligible"])
        self.assertFalse(r["fired"])
        r2 = fire("command_used", {**P_CMD, "forbidden": [r"--force"]}, sb,
                  bash=["bash scripts/reindex.sh --force"])
        self.assertFalse(r2["fired"])
        self.assertEqual(r2["detail"]["breached_patterns"], ["--force"])
        sb.close()


class TestEligibilityIsCensoring(unittest.TestCase):
    """BASE 27-31 — `eligible` separates 'did not' from 'never got there'."""

    def test_27_empty_run_is_censored_not_a_miss_for_path_untouched(self):
        sb = fresh()
        r = fire("path_untouched", P_UNTOUCHED, sb)
        self.assertFalse(r["eligible"])
        self.assertFalse(r["fired"])
        self.assertFalse(r["detail"]["eligibility"]["natural"])
        sb.close()

    def test_28_content_absent_on_a_missing_site_is_censored(self):
        sb = fresh()
        sb.write({"README.md": "touched\n"})
        r = fire("content_absent", {**P_NOSLEEP, "paths": ["ledgerline/sched.py"]}, sb)
        self.assertFalse(r["eligible"])
        self.assertFalse(r["fired"])
        sb.close()

    def test_29_command_order_is_censored_when_the_second_command_never_ran(self):
        sb = fresh()
        sb.write({"ledgerline/ext/x.py": "x = 1\n"})
        r = fire("command_order", P_ORDER, sb, bash=["python migrate.py"])
        self.assertFalse(r["eligible"])
        self.assertFalse(r["fired"])
        sb.close()

    def test_30_eligible_when_only_narrows_and_a_fire_cannot_survive_it(self):
        sb = fresh()
        sb.write({"ledgerline/search.py": "q = 'fts5'\n"})
        base = fire("content_present", {**P_FTS5, "require_code": False}, sb)
        self.assertTrue(base["fired"])
        narrowed = fire("content_present",
                        {**P_FTS5, "require_code": False,
                         "eligible_when": {"diff_touches": ["ledgerline/ext/**"]}}, sb)
        self.assertFalse(narrowed["eligible"])
        self.assertFalse(narrowed["fired"])
        sb.close()

    def test_31_a_broken_predicate_returns_an_error_and_never_raises(self):
        sb = fresh()
        ctx = sb.ctx()
        ctx.workspace = Path("/nonexistent/definitely/not/here")
        out = run_detector("content_present", P_FTS5, ctx)
        self.assertIsNone(out["error"])          # a missing tree is not an exception
        self.assertFalse(out["eligible"])        # it is a censored measurement

        # A predicate that genuinely raises must come back as error != None with
        # used=null downstream — an unmeasured run, never a scored non-fire.
        spec = detectors.REGISTRY["content_present"]

        def boom(_ctx, _p):
            raise ZeroDivisionError("predicate exploded")

        detectors.REGISTRY["content_present"] = detectors.replace(spec, fn=boom)
        try:
            broken = run_detector("content_present", P_FTS5, sb.ctx())
        finally:
            detectors.REGISTRY["content_present"] = spec
        self.assertIn("ZeroDivisionError", broken["error"])
        self.assertFalse(broken["fired"])
        self.assertFalse(broken["eligible"])
        sb.close()


class TestBatteryFixes(unittest.TestCase):
    """BASE 32-36 — the two mandated battery.py fixes, plus the new return keys."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="wur-batt-"))

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_32_eval_error_returns_none_not_a_silent_false_pass(self):
        crit = {"id": "C1", "kind": "mechanical", "command": "true",
                "pass_condition": "this is not python((("}
        row = battery.run([crit], self.ws)["C1"]
        self.assertIsNone(row["passed"], "eval error must NOT fall back to exit_code == 0")
        self.assertEqual(row["status"], battery.STATUS_ERROR)
        self.assertIn("pass_condition eval error", row["error"])
        self.assertEqual(row["exit_code"], 0)

    def test_33_a_working_condition_still_decides_normally(self):
        rows = battery.run([
            {"id": "P", "kind": "mechanical", "command": "echo 7", "pass_condition": "int(stdout) == 7"},
            {"id": "F", "kind": "mechanical", "command": "echo 7", "pass_condition": "int(stdout) == 8"},
        ], self.ws)
        self.assertIs(rows["P"]["passed"], True)
        self.assertIs(rows["F"]["passed"], False)
        self.assertEqual(rows["P"]["status"], battery.STATUS_OK)

    def test_34_default_timeout_is_120(self):
        self.assertEqual(battery.AC_TIMEOUT_DEFAULT, 120)

    def test_35_per_criterion_timeout_override_applies(self):
        crit = {"id": "T", "kind": "mechanical", "command": "sleep 5",
                "pass_condition": "exit_code == 0", "timeout": 1}
        row = battery.run([crit], self.ws)["T"]
        self.assertTrue(row["timed_out"])
        self.assertEqual(row["status"], battery.STATUS_TIMEOUT)
        self.assertLess(row["duration_s"], 5)

    def test_36_exit_code_and_command_are_returned_and_run_one_shape_is_kept(self):
        crit = {"id": "E", "kind": "mechanical", "command": "exit 3",
                "pass_condition": "exit_code == 0"}
        row = battery.run([crit], self.ws)["E"]
        self.assertEqual(row["exit_code"], 3)
        self.assertEqual(row["command"], "exit 3")
        passed, output = battery.run_one(crit, self.ws)
        self.assertIs(passed, False)
        self.assertIsInstance(output, str)


# ═════════════════════════════════════════════════════════════════════════════
class TestMisclassifications(unittest.TestCase):
    """MISCLASSIFY 1-7 — seven ways a plausible detector gets it wrong.

    Each of these is a bug that a regex-over-the-repo implementation ships with,
    and each would push `used` in a specific direction: 1, 2, 3 and 5 inflate it
    (the fact looks used when it is not), 4, 6 and 7 deflate it (real compliance
    scores as non-use). Inflation is the worse failure — it runs WITH the
    hypothesis — but a deflating detector is just as unusable for lift.
    """

    def test_m1_a_match_in_a_comment_is_not_use(self):
        sb = fresh()
        sb.write({"ledgerline/search.py":
                  "def build(db):\n"
                  "    # TODO: switch this to fts5 when there is time\n"
                  "    db.execute('SELECT * FROM t WHERE body LIKE ?')\n"})
        strict = fire("content_present", P_FTS5, sb)
        naive = fire("content_present", {**P_FTS5, "require_code": False}, sb)
        self.assertFalse(strict["fired"], "a comment is an intention, not a use")
        self.assertTrue(naive["fired"], "the naive reading is what require_code exists to stop")
        sb.close()

    def test_m2_a_match_in_the_planted_file_is_availability_not_use(self):
        sb = fresh()
        sb.write({"docs/NOTES.md": "Use fts5 for the search index. Token QX-77.\n"})
        r = fire("content_present", {**P_FTS5, "paths": ["**/*"], "require_code": False},
                 sb, planted=["docs/NOTES.md"])
        self.assertFalse(r["fired"], "the plant must never be its own evidence of use")
        self.assertIn("docs/NOTES.md", r["detail"]["exclude_globs"])
        leaky = fire("content_present",
                     {**P_FTS5, "paths": ["**/*"], "require_code": False,
                      "exclude_planted": False}, sb, planted=["docs/NOTES.md"])
        self.assertTrue(leaky["fired"], "opting out is what makes the default load-bearing")
        sb.close()

    def test_m3_a_match_in_a_docstring_is_not_use(self):
        sb = fresh()
        sb.write({"ledgerline/search.py":
                  'def build(db):\n'
                  '    """Build the index.\n\n    Later we may move to fts5.\n    """\n'
                  "    db.execute('SELECT 1')\n"})
        self.assertFalse(fire("content_present", P_FTS5, sb)["fired"])
        sb.close()

    def test_m4_a_compound_call_satisfies_the_ordering_mandate(self):
        sb = fresh()
        sb.write({"ledgerline/ext/x.py": "x = 1\n"})
        compound = fire("command_order", P_ORDER, sb,
                        bash=["python migrate.py && pytest -q"])
        self.assertTrue(compound["fired"],
                        "`migrate && pytest` in one Bash call IS compliance (documented decision)")
        self.assertTrue(compound["detail"]["compound_compliance"])
        strict = fire("command_order", {**P_ORDER, "allow_compound": False}, sb,
                      bash=["python migrate.py && pytest -q"])
        self.assertFalse(strict["fired"], "and the opt-out must actually be stricter")
        sb.close()

    def test_m5_a_later_correct_run_does_not_excuse_an_earlier_wrong_one(self):
        sb = fresh()
        sb.write({"ledgerline/ext/x.py": "x = 1\n"})
        bash = ["pytest -q", "python migrate.py", "pytest -q"]
        self.assertFalse(fire("command_order", P_ORDER, sb, bash=bash)["fired"])
        lenient = fire("command_order", {**P_ORDER, "all_occurrences": False}, sb, bash=bash)
        self.assertFalse(lenient["fired"], "the FIRST pytest is still unordered")
        sb.close()

    def test_m6_a_rename_of_a_protected_path_is_a_modification(self):
        sb = fresh()
        sb.write({"ledgerline/ext/x.py": "x = 1\n"})
        sb.rename("ledgerline/core/rates.py", "ledgerline/core/rates_v2.py")
        r = fire("path_untouched", P_UNTOUCHED, sb)
        self.assertTrue(r["eligible"])
        self.assertFalse(r["fired"], "moving a file you were told not to modify is modifying it")
        sb.close()

    def test_m7_a_removed_line_is_not_evidence_of_use_in_the_diff(self):
        sb = fresh({"ledgerline/legacy.py":
                    "def old(db):\n    db.execute('USING fts5(body)')\n"})
        sb.write({"ledgerline/legacy.py": "def old(db):\n    db.execute('SELECT 1')\n"})
        params = {**P_FTS5, "paths": ["ledgerline/**/*.py"], "require_code": False}
        in_diff = run_detector("content_present", params, sb.ctx(), diff_only=True)
        self.assertTrue(in_diff["eligible"])
        self.assertFalse(in_diff["fired"], "deleting the construct is the opposite of using it")
        whole = run_detector("content_present", params, sb.ctx(), diff_only=False)
        self.assertFalse(whole["fired"])
        sb.close()

    def test_m8_a_deep_protected_path_is_still_protected(self):
        sb = fresh({"ledgerline/core/vendor/deep/gen.py": "GEN = 1\n"})
        sb.write({"ledgerline/core/vendor/deep/gen.py": "GEN = 2\n"})
        r = fire("path_untouched", P_UNTOUCHED, sb)
        self.assertFalse(r["fired"], "`core/**` must reach every depth, not just one level")
        sb.close()


class TestComplianceCorpus(unittest.TestCase):
    """CORPUS — one binding, many genuinely different right and wrong answers."""

    COMPLIANT = {
        "raw sql": "def build(db):\n    db.execute('CREATE VIRTUAL TABLE idx USING fts5(body)')\n",
        "helper": "SCHEMA = 'USING FTS5(body, tokenize=porter)'\n"
                  "def build(db):\n    db.execute('CREATE VIRTUAL TABLE idx ' + SCHEMA)\n",
        "f-string": "def build(db, cols):\n    db.execute(f'CREATE VIRTUAL TABLE idx USING fts5({cols})')\n",
        "constant": "FTS5 = 'fts5'\ndef build(db):\n    db.execute('USING ' + FTS5)\n",
    }
    NONCOMPLIANT = {
        "like": "def build(db):\n    db.execute('SELECT * FROM t WHERE body LIKE ?')\n",
        "fts4": "def build(db):\n    db.execute('CREATE VIRTUAL TABLE idx USING fts4(body)')\n",
        "comment only": "def build(db):\n    # fts5 would be nicer\n    db.execute('SELECT 1')\n",
        "python index": "def build(db):\n    return {w: 1 for w in ['a']}\n",
    }

    def test_corpus_all_compliant_fire(self):
        for label, body in self.COMPLIANT.items():
            with self.subTest(solution=label):
                sb = fresh()
                sb.write({"ledgerline/search.py": body})
                r = fire("content_present", P_FTS5, sb)
                self.assertTrue(r["eligible"])
                self.assertTrue(r["fired"], f"missed a real solution: {label}")
                sb.close()

    def test_corpus_no_noncompliant_fires(self):
        for label, body in self.NONCOMPLIANT.items():
            with self.subTest(solution=label):
                sb = fresh()
                sb.write({"ledgerline/search.py": body})
                r = fire("content_present", P_FTS5, sb)
                self.assertTrue(r["eligible"], f"{label} must be eligible to be a real miss")
                self.assertFalse(r["fired"], f"over-matched: {label}")
                sb.close()

    def test_corpus_used_in_diff_tracks_used_for_a_fresh_solution(self):
        sb = fresh()
        sb.write({"ledgerline/search.py": self.COMPLIANT["raw sql"]})
        whole = run_detector("content_present", P_FTS5, sb.ctx(), diff_only=False)
        in_diff = run_detector("content_present", P_FTS5, sb.ctx(), diff_only=True)
        self.assertTrue(whole["fired"])
        self.assertTrue(in_diff["fired"])
        sb.close()


# ═════════════════════════════════════════════════════════════════════════════
class TestEndToEnd(unittest.TestCase):
    """ENDTOEND — detect_use through battery to use_detect.json, and verify_pack."""

    def _facts(self, root: Path, verification: dict | None = None) -> Path:
        entry = {
            "fact_id": "F-fts5",
            "task_id": "t-search",
            "planted_paths": ["docs/NOTES.md"],
            "detector": {"name": "content_present", "params": P_FTS5},
        }
        if verification:
            entry["verification"] = verification
        p = root / "facts.json"
        p.write_text(json.dumps({"facts": [entry]}, indent=2))
        return p

    def test_e1_detect_use_writes_use_detect_json_through_the_battery(self):
        sb = fresh()
        sb.write({"ledgerline/search.py":
                  "def build(db):\n    db.execute('USING fts5(body)')\n"})
        run_dir = Path(tempfile.mkdtemp(prefix="wur-run-"))
        shutil.move(str(sb.dir), str(run_dir / "workspace"))
        ws = run_dir / "workspace"
        _git(["update-ref", "refs/atlas/baseline", sb.baseline], ws)
        (run_dir / "gate").mkdir()
        (run_dir / "gate" / "tool_calls.jsonl").write_text(
            json.dumps({"barrier": 1, "tool_use_id": "t1", "tool_name": "Bash",
                        "tool_input": {"command": "pytest -q"}}) + "\n"
            # V14: a denied call fires the barrier twice under the SAME id.
            + json.dumps({"barrier": 2, "tool_use_id": "t1", "tool_name": "Bash",
                          "tool_input": {"command": "pytest -q"}}) + "\n"
        )
        facts = self._facts(run_dir)
        rc = detect_use.main(["--scope", "run", "--run-dir", str(run_dir),
                              "--facts", str(facts), "--task-id", "t-search"])
        self.assertEqual(rc, 0)
        doc = json.loads((run_dir / "use_detect.json").read_text())
        self.assertEqual(doc["schema_version"], "1")
        self.assertEqual(len(doc["facts"]), 1)
        row = doc["facts"][0]
        self.assertIs(row["used"], True)
        self.assertIs(row["eligible"], True)
        self.assertIs(row["used_in_diff"], True)
        self.assertIsNone(row["error"])
        self.assertEqual(row["battery"]["exit_code"], detectors.EXIT_FIRED)
        self.assertEqual(doc["context"]["n_bash_calls"], 1, "gate rows dedupe by tool_use_id")
        self.assertEqual(doc["detector_registry_sha256"], detectors.registry_sha256())
        self.assertIn("docs/NOTES.md", row["planted_paths"])
        shutil.rmtree(run_dir, ignore_errors=True)

    def test_e2_detect_use_records_a_non_fire_and_leaves_the_workspace_untouched(self):
        sb = fresh()
        sb.write({"ledgerline/search.py":
                  "def build(db):\n    db.execute('SELECT 1')\n"})
        run_dir = Path(tempfile.mkdtemp(prefix="wur-run-"))
        shutil.move(str(sb.dir), str(run_dir / "workspace"))
        ws = run_dir / "workspace"
        _git(["update-ref", "refs/atlas/baseline", sb.baseline], ws)
        before = sorted(p.relative_to(ws).as_posix()
                        for p in ws.rglob("*") if ".git" not in p.parts)
        facts = self._facts(run_dir)
        self.assertEqual(detect_use.main(
            ["--scope", "run", "--run-dir", str(run_dir), "--facts", str(facts)]), 0)
        row = json.loads((run_dir / "use_detect.json").read_text())["facts"][0]
        self.assertIs(row["used"], False)
        self.assertIs(row["eligible"], True)
        after = sorted(p.relative_to(ws).as_posix()
                       for p in ws.rglob("*") if ".git" not in p.parts)
        self.assertEqual(before, after, "detection must not touch the tree before grading")
        shutil.rmtree(run_dir, ignore_errors=True)

    def test_e3_verify_pack_truth_table_separates_a_good_pack(self):
        root = Path(tempfile.mkdtemp(prefix="wur-pack-"))
        base = root / "tree"
        for rel, body in BASE_FILES.items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            (base / rel).write_text(body)
        cases = {}
        for name, body in list(TestComplianceCorpus.COMPLIANT.items())[:3]:
            d = root / f"ref-{name.replace(' ', '_')}"
            (d / "ledgerline").mkdir(parents=True)
            (d / "ledgerline" / "search.py").write_text(body)
            cases.setdefault("reference", []).append(
                {"id": f"ref-{name}", "overlay": str(d.relative_to(root))})
        for name, body in list(TestComplianceCorpus.NONCOMPLIANT.items())[:2]:
            d = root / f"near-{name.replace(' ', '_')}"
            (d / "ledgerline").mkdir(parents=True)
            (d / "ledgerline" / "search.py").write_text(body)
            cases.setdefault("near_miss", []).append(
                {"id": f"near-{name}", "overlay": str(d.relative_to(root))})
        facts = self._facts(root, verification=cases)
        report_path = root / "report.json"
        rc = verify_pack.main(["--facts", str(facts), "--base-tree", str(base),
                               "--out", str(report_path), "--quiet"])
        report = json.loads(report_path.read_text())
        self.assertEqual(rc, 0, report["facts"][0]["problems"])
        f = report["facts"][0]
        self.assertTrue(f["separates"])
        self.assertEqual(f["n_reference"], 3)
        self.assertEqual(f["n_near_miss"], 2)
        self.assertTrue(all(r["ok"] for r in f["truth_table"]))
        shutil.rmtree(root, ignore_errors=True)

    def test_e4_verify_pack_fails_an_overmatching_detector(self):
        root = Path(tempfile.mkdtemp(prefix="wur-pack2-"))
        base = root / "tree"
        for rel, body in BASE_FILES.items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            (base / rel).write_text(body)
        cases = {"reference": [], "near_miss": []}
        for i, body in enumerate(list(TestComplianceCorpus.COMPLIANT.values())[:3], 1):
            d = root / f"r{i}"
            (d / "ledgerline").mkdir(parents=True)
            (d / "ledgerline" / "search.py").write_text(body)
            cases["reference"].append({"id": f"r{i}", "overlay": f"r{i}"})
        # The classic over-match: `require_code` off, so the comment-only
        # near-miss fires and the pack must be rejected.
        for i, body in enumerate(["def build(db):\n    # fts5 later\n    db.execute('SELECT 1')\n",
                                  "def build(db):\n    db.execute('USING fts4(x)')\n"], 1):
            d = root / f"n{i}"
            (d / "ledgerline").mkdir(parents=True)
            (d / "ledgerline" / "search.py").write_text(body)
            cases["near_miss"].append({"id": f"n{i}", "overlay": f"n{i}"})
        entry = {"fact_id": "F-loose", "task_id": "t-search",
                 "detector": {"name": "content_present",
                              "params": {**P_FTS5, "require_code": False}},
                 "verification": cases}
        facts = root / "facts.json"
        facts.write_text(json.dumps({"facts": [entry]}))
        out = root / "r.json"
        rc = verify_pack.main(["--facts", str(facts), "--base-tree", str(base),
                               "--out", str(out), "--quiet"])
        self.assertEqual(rc, 1)
        f = json.loads(out.read_text())["facts"][0]
        self.assertFalse(f["separates"])
        self.assertTrue(any("over-matches" in p for p in f["problems"]))
        shutil.rmtree(root, ignore_errors=True)

    def test_e5_prior_check_passes_on_a_counter_prior_fact(self):
        root = Path(tempfile.mkdtemp(prefix="wur-prior-"))
        base = root / "tree"
        for rel, body in BASE_FILES.items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            (base / rel).write_text(body)
        cases = {"reference": [], "near_miss": []}
        for i, body in enumerate(list(TestComplianceCorpus.COMPLIANT.values())[:3], 1):
            d = root / f"r{i}"
            (d / "ledgerline").mkdir(parents=True)
            (d / "ledgerline" / "search.py").write_text(body)
            cases["reference"].append({"id": f"r{i}", "overlay": f"r{i}"})
        for i, body in enumerate(list(TestComplianceCorpus.NONCOMPLIANT.values())[:2], 1):
            d = root / f"n{i}"
            (d / "ledgerline").mkdir(parents=True)
            (d / "ledgerline" / "search.py").write_text(body)
            cases["near_miss"].append({"id": f"n{i}", "overlay": f"n{i}",
                                       "allow_ineligible": True})
        facts = self._facts(root, verification=cases)
        outdir = root / "prior_check"
        rc = verify_pack.main(["--prior-check", "--facts", str(facts),
                               "--base-tree", str(base), "--out-dir", str(outdir), "--quiet"])
        self.assertEqual(rc, 0)
        doc = json.loads((outdir / "t-search.json").read_text())
        self.assertTrue(doc["gate1_ok"], "the pristine base must not fire (Gate 1)")
        self.assertTrue(doc["gate1b_ok"])
        self.assertEqual(doc["n_fires"], 0)
        self.assertEqual(doc["control_fire_rate"], 0.0)
        self.assertEqual(doc["prior_check_status"], "pass")
        self.assertEqual(doc["disposition"], "admit")
        shutil.rmtree(root, ignore_errors=True)

    def test_e6_prior_check_rejects_a_fact_the_base_tree_already_satisfies(self):
        root = Path(tempfile.mkdtemp(prefix="wur-prior2-"))
        base = root / "tree"
        for rel, body in BASE_FILES.items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            (base / rel).write_text(body)
        # `path_untouched` on a path nobody would touch fires trivially — but the
        # pristine base has an EMPTY diff, so it is censored, not a fire. The
        # prior-check failure we care about is a content fact already satisfied.
        (base / "ledgerline" / "search.py").write_text(
            "def build(db):\n    db.execute('USING fts5(body)')\n")
        cases = {"reference": [], "near_miss": []}
        for i in (1, 2, 3):
            d = root / f"r{i}"
            (d / "ledgerline").mkdir(parents=True)
            (d / "ledgerline" / "search.py").write_text(
                f"def build(db):\n    db.execute('USING fts5(c{i})')\n")
            cases["reference"].append({"id": f"r{i}", "overlay": f"r{i}"})
        for i in (1, 2):
            d = root / f"n{i}"
            (d / "ledgerline").mkdir(parents=True)
            (d / "ledgerline" / "search.py").write_text(
                f"def build(db):\n    db.execute('LIKE ?{i}')\n")
            cases["near_miss"].append({"id": f"n{i}", "overlay": f"n{i}"})
        facts = self._facts(root, verification=cases)
        out = root / "prior.json"
        rc = verify_pack.main(["--prior-check", "--facts", str(facts),
                               "--base-tree", str(base), "--out", str(out), "--quiet"])
        self.assertEqual(rc, 1, "a fact the base tree already satisfies is not counter-prior")
        doc = json.loads(out.read_text())["facts"][0]
        self.assertFalse(doc["gate1_ok"])
        self.assertGreaterEqual(doc["n_fires"], 1)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
