#!/usr/bin/env python3
"""
test_grader_proof.py — the grader invariants whose failure mode is a WRONG VERDICT.

RESPONSIBILITY
  Guard the three grading defects the Levanto collaboration found by running this
  harness against real repositories, plus the two capabilities added to close them.
  Every one of these produced a complete-looking judge.json carrying an answer that
  was quietly false — the class of bug that survives review because nothing crashes.

WHAT EACH TEST DEFENDS, AND WHY IT IS LOAD-BEARING
  SandboxScope        battery.py evaluated pass_conditions with its safe namespace in
      eval LOCALS. Python resolves a free name inside a lambda body or a
      comprehension through GLOBALS, so `str(x)` worked while
      `(lambda d: all(...))(x)` and `sum(float(r) for r in...)` raised NameError.
      It does not crash: the criterion scores None, "undecided". Measured on a
      VERIFIED-CORRECT reference solution: 3 of 7 criteria. Left in place, every
      completed run grades not-completed. The same test re-asserts the sandbox is
      still sealed, because "put the names in globals" is one careless edit away
      from "put __builtins__ in globals".
  OneSidedFloorCheckIsBlind / TwoSidedProof
      floor_check against the pristine base establishes "fails before" and
      ASSUMES "passes after". A criterion shaped `exit_code == 0 and <broken>`
      short-circuits there — the command exits non-zero, the broken half is never
      evaluated — so it looks cleanly discriminating and breaks only once a correct
      solution makes the command succeed. The first test PROVES the blindness (a
      known-broken criterion passes a one-sided check); the second proves that
      reference_patches catches it.
  CriteriaLint        the shipped smoke battery used `__import__('json')` in four of
      seven criteria although the synthesis prompt documents it as absent. Combined
      with the one-sided check, invisible. The linter names it.
  ReferenceMustApply  a reference solution that does not apply is a BROKEN proof,
      never a passing one — fail closed, the same way a missing answer key must.
  RegradeFromArchive  teardown removes the workspace unconditionally, so --grade can
      only ever run once. Re-deriving the verdict from
      refs/atlas/baseline-run/<RUN_ID> + git.patch is what makes a grader bugfix
      applicable to runs already collected.
  GitignoreMerged     the E6/DOX overlay used to REPLACE the repo's.gitignore with a
      one-line `.xo/`. teardown captures untracked files with
      `git ls-files --others --exclude-standard`, which honours.gitignore — so on
      any repo that ignores its own build output this pushed.pytest_cache/,
      *.egg-info/ and build/ into git.patch IN ONE ARM ONLY: an arm-correlated
      confound in the diff that the experiment measures.

CLI
  python3 tests/test_grader_proof.py [-v]        (stdlib unittest; no pytest needed)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
sys.path.insert(0, str(LIB))

import battery                       # noqa: E402
import context_gen                   # noqa: E402
import judge as judge_mod            # noqa: E402
import report as report_mod          # noqa: E402


def _git(args: list[str], cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def _init_repo(src: Path) -> str:
    """A one-commit repo with a module the criteria can exercise. Returns its SHA."""
    src.mkdir(parents=True, exist_ok=True)
    (src / "app.py").write_text("def total(rows):\n    return 0\n")
    _git(["init", "-q", "-b", "main"], src)
    _git(["config", "user.email", "t@example.com"], src)
    _git(["config", "user.name", "t"], src)
    _git(["add", "-A"], src)
    _git(["commit", "-q", "-m", "base"], src)
    return _git(["rev-parse", "HEAD"], src).strip()


def _make_job(tmp: Path) -> tuple[Path, str]:
    """jobs/<id>/ carrying only what the grader needs: repo.git and a pinned SHA."""
    src = tmp / "src"
    sha = _init_repo(src)
    job = tmp / "job"
    job.mkdir(parents=True, exist_ok=True)
    _git(["clone", "-q", "--bare", str(src), str(job / "repo.git")], tmp)
    return job, sha


def _reference_patch(tmp: Path, sha: str, name: str = "ref.patch") -> Path:
    """A real git patch implementing the task: make total actually sum its input."""
    work = tmp / f"work-{name}"
    _git(["clone", "-q", str(tmp / "src"), str(work)], tmp)
    _git(["checkout", "-q", sha], work)
    (work / "app.py").write_text("def total(rows):\n    return sum(rows)\n")
    (work / "feature.txt").write_text("done\n")
    _git(["add", "-A"], work)
    out = _git(["diff", "--cached", sha], work)
    patch = tmp / name
    patch.write_text(out)
    return patch


# The criterion at the centre of all of this. On the pristine base `cat feature.txt`
# exits non-zero, so `exit_code == 0 and...` short-circuits to False and never
# evaluates the right-hand side — which is broken. It therefore looks perfectly
# discriminating to a base-only check, and only misbehaves once a solution makes the
# command succeed, which is exactly when it matters.
TRAP = {
    "id": "C_trap",
    "description": "feature.txt records the work (broken expression behind a short-circuit)",
    "kind": "mechanical",
    "command": "cat feature.txt",
    "pass_condition": "exit_code == 0 and undefined_helper(stdout)",
    "expect_on_base": "fail",
}
SOUND = {
    "id": "C_sound",
    "description": "total sums its input",
    "kind": "mechanical",
    "command": "python3 -c \"import app; print(app.total([1,2,3]))\"",
    "pass_condition": "exit_code == 0 and int(stdout.strip()) == 6",
    "expect_on_base": "fail",
}
GUARD = {
    "id": "C_guard",
    "description": "the module still imports (regression guard)",
    "kind": "mechanical",
    "command": "python3 -c \"import app\"",
    "pass_condition": "exit_code == 0",
    "expect_on_base": "pass",
}


class SandboxScope(unittest.TestCase):
    """Free names inside a lambda/comprehension resolve — and the sandbox stays sealed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ws = Path(self.tmp)

    def _run(self, command: str, cond: str) -> dict:
        return battery.run_one_full({"id": "C", "kind": "mechanical", "command": command, "pass_condition": cond},
            self.ws,
)

    def test_scoped_expressions_resolve(self):
        cases = [
            ("echo '1 2 3'", "(lambda d: all(int(x) > 0 for x in d.split()))(stdout)"),
            ("echo '1.5 2.5'", "sum(float(r) for r in stdout.split()) == 4.0"),
            ("echo 'a b c'", "len([w for w in stdout.split() if len(w) == 1]) == 3"),
            ("echo '7 passed'", r"int(re.search(r'(\d+) passed', stdout).group(1)) >= 7"),
            ("echo '{\"a\": 1}'", "json.loads(stdout)['a'] == 1"),
        ]
        for command, cond in cases:
            with self.subTest(cond=cond):
                row = self._run(command, cond)
                self.assertIsNone(row["error"], f"{cond!r} raised: {row['error']}")
                self.assertIs(row["passed"], True)

    def test_run_variables_resolve_inside_a_nested_scope(self):
        """`exit_code`/`stdout` are as scope-sensitive as the builtins are.

        Moving only SAFE_BUILTINS into globals is a HALF fix that reads as a whole
        one. The shape a model actually writes is
        `(lambda d: exit_code == 0 and...)(json.loads(stdout))` — `stdout` is
        evaluated at the CALL SITE and resolved fine, while `exit_code` inside the
        lambda raised NameError. Observed on a live run: 3 of 7 criteria.
        """
        cases = [
            ("echo '{\"rows\": [[\"a\", 1]]}'",
             "(lambda d: exit_code == 0 and len(d['rows']) == 1)(json.loads(stdout))"),
            ("echo '1 2 3'",
             "(lambda d: exit_code == 0 and sum(int(x) for x in d.split()) == 6)(stdout)"),
            ("echo ok", "all(exit_code == 0 for _ in [1])"),
            ("echo ok", "[stdout for _ in [1]] == ['ok']"),
        ]
        for command, cond in cases:
            with self.subTest(cond=cond):
                row = self._run(command, cond)
                self.assertIsNone(row["error"], f"{cond!r} raised: {row['error']}")
                self.assertIs(row["passed"], True)

    def test_sandbox_still_sealed(self):
        for cond in ("__import__('os').system('true') == 0",
                     "open('/etc/passwd') is not None",
                     "eval('1+1') == 2",
                     "exec('x=1') is None"):
            with self.subTest(cond=cond):
                row = self._run("echo x", cond)
                self.assertIsNone(row["passed"], f"{cond!r} was NOT blocked")
                self.assertEqual(row["status"], battery.STATUS_ERROR)
                self.assertIn("NameError", row["error"])

    def test_eval_error_is_undecided_never_a_pass(self):
        """A broken expression must score None, never fall back to exit_code == 0."""
        row = self._run("true", "undefined_helper(stdout)")
        self.assertIsNone(row["passed"])
        self.assertEqual(row["exit_code"], 0)


class CriteriaLint(unittest.TestCase):
    def test_flags_the_three_shipped_failure_shapes(self):
        found = judge_mod.lint_criteria([
            TRAP,
            {"id": "C_imp", "kind": "mechanical", "command": "echo 1",
             "pass_condition": "int(__import__('re').search(r'(\\d+)', stdout).group(1)) > 0",
             "expect_on_base": "fail", "description": "x"},
            {"id": "C_pipe", "kind": "mechanical", "command": "pytest -q | tail -5",
             "pass_condition": "len(stdout) > 0", "expect_on_base": "fail", "description": "y"},
        ])
        by_id = {(f["id"], f["kind"]) for f in found}
        self.assertIn(("C_trap", "short_circuit"), by_id)
        self.assertIn(("C_imp", "sandbox"), by_id)
        self.assertIn(("C_pipe", "pipe"), by_id)

    def test_clean_criteria_produce_no_findings(self):
        self.assertEqual(judge_mod.lint_criteria([GUARD]), [])

    def test_a_quoted_pipe_character_is_not_a_pipeline(self):
        clean = {"id": "C", "kind": "mechanical", "command": "python3 -c \"print('a|b')\"",
                 "pass_condition": "exit_code == 0", "expect_on_base": "pass",
                 "description": "z"}
        self.assertEqual([f["kind"] for f in judge_mod.lint_criteria([clean])], [])

    def test_grepping_the_tree_is_flagged_but_grepping_a_diff_is_not(self):
        """"Does this word appear?" is satisfied by the pristine tree. "Did this
        change?" is a different question and the synthesis prompt recommends it."""
        tree = {"id": "C1", "kind": "mechanical", "command": "grep -rl cashflow tests/",
                "pass_condition": "exit_code == 0", "expect_on_base": "fail",
                "description": "z"}
        diff = {"id": "C2", "kind": "mechanical",
                "command": "git diff --name-only HEAD | grep -c '^ledgerline/'",
                "pass_condition": "exit_code == 0", "expect_on_base": "fail",
                "description": "z"}
        self.assertIn("tree_grep", [f["kind"] for f in judge_mod.lint_criteria([tree])])
        self.assertNotIn("tree_grep", [f["kind"] for f in judge_mod.lint_criteria([diff])])

    def test_the_guard_inside_a_lambda_is_flagged(self):
        """`(lambda d: exit_code == 0 and...)(json.loads(stdout))` parses BEFORE it
        checks the exit code. Measured: 3 of 7 criteria of one shipped battery."""
        bad = {"id": "C", "kind": "mechanical", "command": "cmd",
               "pass_condition": "(lambda d: exit_code == 0 and d['x'])(json.loads(stdout))",
               "expect_on_base": "fail", "description": "z"}
        good = {"id": "C", "kind": "mechanical", "command": "cmd",
                "pass_condition": "exit_code == 0 and (lambda d: d['x'])(json.loads(stdout))",
                "expect_on_base": "fail", "description": "z"}
        self.assertIn("guard_inside_lambda",
                      [f["kind"] for f in judge_mod.lint_criteria([bad], two_sided=True)])
        self.assertNotIn("guard_inside_lambda",
                         [f["kind"] for f in judge_mod.lint_criteria([good], two_sided=True)])

    def test_a_doubled_quiet_flag_is_flagged(self):
        """pytest.ini already sets -q; a second one suppresses the very listing the
        condition parses."""
        c = {"id": "C", "kind": "mechanical", "command": "python3 -m pytest -q --collect-only",
             "pass_condition": "exit_code == 0", "expect_on_base": "pass", "description": "z"}
        self.assertIn("double_quiet", [f["kind"] for f in judge_mod.lint_criteria([c])])

    def test_short_circuit_is_answered_once_the_proof_is_two_sided(self):
        """Its own remedy is "give the task reference_patches". A linter that keeps
        crying about a resolved finding trains people to skip the output — which is
        where the sandbox findings are."""
        c = {"id": "C", "kind": "mechanical", "command": "cmd",
             "pass_condition": "exit_code == 0 and len(stdout) > 0",
             "expect_on_base": "fail", "description": "z"}
        self.assertIn("short_circuit",
                      [f["kind"] for f in judge_mod.lint_criteria([c], two_sided=False)])
        self.assertNotIn("short_circuit",
                         [f["kind"] for f in judge_mod.lint_criteria([c], two_sided=True)])


class FloorProof(unittest.TestCase):
    """The two halves of "fails before, passes after", on a real repo."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.job, self.sha = _make_job(self.tmp)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_one_sided_check_cannot_see_the_broken_criterion(self):
        """THE DEFECT, encoded: base-only, the trap looks perfectly discriminating."""
        m = judge_mod.floor_check(self.job, "t1", [TRAP, SOUND, GUARD], sha=self.sha)
        self.assertTrue(m["floor_ok"])
        trap = next(c for c in m["criteria"] if c["id"] == "C_trap")
        self.assertTrue(trap["discriminating"])
        #...and the manifest says so out loud rather than implying a proof it skipped.
        self.assertFalse(m["proof"]["two_sided"])
        self.assertFalse(m["proof_ok"])

    def test_two_sided_proof_catches_it(self):
        ref = _reference_patch(self.tmp, self.sha)
        m = judge_mod.floor_check(self.job, "t1", [TRAP, SOUND, GUARD], sha=self.sha,
                                  references=[str(ref)])
        self.assertTrue(m["proof"]["two_sided"])
        self.assertTrue(m["proof"]["references"][0]["applied"])
        rows = {c["id"]: c for c in m["criteria"]}
        # The sound criterion and the guard survive both sides.
        self.assertIs(rows["C_sound"]["two_sided_ok"], True)
        self.assertIs(rows["C_guard"]["two_sided_ok"], True)
        # The trap fails on a KNOWN-CORRECT solution — undecided, not False, which is
        # precisely why it reads as "the agent did not finish" downstream.
        self.assertIs(rows["C_trap"]["passes_on_references"], False)
        self.assertIs(rows["C_trap"]["two_sided_ok"], False)
        self.assertFalse(m["proof_ok"])

    def test_a_reference_that_does_not_apply_fails_closed(self):
        bad = self.tmp / "bad.patch"
        bad.write_text("diff --git a/nope.py b/nope.py\n--- a/nope.py\n+++ b/nope.py\n"
                       "@@ -1 +1 @@\n-was\n+is\n")
        m = judge_mod.floor_check(self.job, "t1", [SOUND, GUARD], sha=self.sha,
                                  references=[str(bad)])
        self.assertFalse(m["proof"]["references"][0]["applied"])
        self.assertFalse(m["proof_ok"])

    def test_a_missing_reference_is_reported_not_ignored(self):
        m = judge_mod.floor_check(self.job, "t1", [SOUND], sha=self.sha,
                                  references=["does/not/exist.patch"])
        r = m["proof"]["references"][0]
        self.assertFalse(r["applied"])
        self.assertIn("not found", r["error"])
        self.assertFalse(m["proof_ok"])

    def test_manifest_and_log_are_written(self):
        ref = _reference_patch(self.tmp, self.sha)
        judge_mod.floor_check(self.job, "t1", [SOUND, GUARD], sha=self.sha,
                              references=[str(ref)])
        grader = self.job / "grader" / "t1"
        self.assertTrue((grader / "manifest.json").is_file())
        log = (grader / "floor.log").read_text()
        self.assertIn("Two-sided proof over 1 reference solution", log)


class RegradeFromArchive(unittest.TestCase):
    """Rebuild a finished run's tree from the artifacts that survive teardown."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.job, self.sha = _make_job(self.tmp)
        # A job.yaml minimal enough for jobspec.load, and the criteria pack.
        (self.job / "job.yaml").write_text(json.dumps({
            "schema_version": "1",
            "job_id": "regrade-test",
            "repo": {"path": str(self.tmp / "src"), "ref": "HEAD", "pinned_sha": self.sha},
            "tasks": [{"id": "t1", "task": "sum the rows", "accept": "total sums"}],
            "model": "claude",
            "environments": ["E0"],
        }))
        (self.job / "grader" / "t1").mkdir(parents=True)
        (self.job / "grader" / "t1" / "criteria.json").write_text(json.dumps({"criteria": [SOUND, GUARD]}))

        # Reproduce what setup_run.sh + teardown_run.sh leave behind: a per-run
        # baseline ref (post-overlay commit) and git.patch taken against it.
        self.run_id = "regrade-test-t1-E0-claude-r001"
        ws = self.tmp / "ws"
        _git(["--git-dir", str(self.job / "repo.git"), "worktree", "add", "--detach",
              str(ws), self.sha], self.tmp)
        (ws / "AGENTS.md").write_text("# overlay\n")
        _git(["add", "-A"], ws)
        _git(["-c", "user.email=t@e.com", "-c", "user.name=t",
              "commit", "-q", "-m", "env-baseline:E0"], ws)
        baseline = _git(["rev-parse", "HEAD"], ws).strip()
        _git(["update-ref", f"refs/atlas/baseline-run/{self.run_id}", baseline], ws)
        (ws / "app.py").write_text("def total(rows):\n    return sum(rows)\n")
        patch_text = _git(["diff", baseline], ws)

        self.run_dir = self.job / "runs" / self.run_id
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "git.patch").write_text(patch_text)
        (self.run_dir / "run_meta.json").write_text(json.dumps({"run_id": self.run_id, "task_id": "t1", "baseline_sha": baseline}))
        # The workspace is gone by the time anyone regrades — that is the whole point.
        _git(["--git-dir", str(self.job / "repo.git"), "worktree", "remove", "--force",
              str(ws)], self.tmp)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_regrade_rebuilds_the_tree_and_grades_it(self):
        j = judge_mod.regrade(self.job, self.run_id)
        self.assertEqual(j["verdict"], "accepted")
        self.assertEqual(j["score"], 1.0)
        self.assertFalse(j["regraded"]["llm_adjudicated"])
        # WHICH battery produced this verdict. A grader patched mid-collection is
        # otherwise undetectable after the fact.
        self.assertEqual(len(j["grader"]["criteria_sha256"]), 64)
        self.assertEqual(j["grader"]["criteria_sha256"], j["regraded"]["criteria_sha256"])
        self.assertTrue((self.run_dir / "judge.regrade.json").is_file())
        # Default is non-destructive: the original verdict is not silently rewritten.
        self.assertFalse((self.run_dir / "judge.json").exists())

    def test_in_place_writes_judge_json(self):
        judge_mod.regrade(self.job, self.run_id, in_place=True)
        self.assertTrue((self.run_dir / "judge.json").is_file())

    def test_missing_patch_fails_closed(self):
        (self.run_dir / "git.patch").unlink()
        with self.assertRaises(RuntimeError):
            judge_mod.regrade(self.job, self.run_id)

    def test_unresolvable_baseline_fails_closed(self):
        _git(["--git-dir", str(self.job / "repo.git"), "update-ref", "-d",
              f"refs/atlas/baseline-run/{self.run_id}"], self.tmp)
        (self.run_dir / "run_meta.json").write_text(json.dumps({"run_id": self.run_id, "task_id": "t1"}))
        with self.assertRaises(RuntimeError):
            judge_mod.regrade(self.job, self.run_id)


class GraderProvenanceIsOnTheScorecard(unittest.TestCase):
    """Two facts a scorecard must not leave to trust.

    "We patched battery.py mid-collection, so the harness grader was inconsistent
    across the dataset" is a real, published-claim-costing event. After the fact it
    is undetectable unless each verdict records WHICH battery produced it, and
    unless a re-derivation that disagrees is surfaced rather than filed beside it.
    """

    def _job(self, tmp: Path, judges: list[dict], regrades: list[dict | None]) -> Path:
        job = tmp / "job"
        (job / "runs").mkdir(parents=True)
        (job / "job.yaml").write_text("job_id: prov\nexperiment: ladder\nreps: 1\n")
        for i, (j, rg) in enumerate(zip(judges, regrades)):
            rd = job / "runs" / f"r{i}"
            rd.mkdir()
            (rd / "judge.json").write_text(json.dumps(j))
            if rg is not None:
                (rd / "judge.regrade.json").write_text(json.dumps(rg))
        return job

    def _judge(self, verdict: str, sha: str) -> dict:
        return {"verdict": verdict, "score": 1.0, "criteria": [], "criteria_errored": 0,
                "grader": {"criteria_sha256": sha}}

    def test_two_batteries_in_one_job_are_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            job = self._job(Path(td),
                            [self._judge("accepted", "a" * 64), self._judge("accepted", "b" * 64)],
                            [None, None])
            report_mod.job_report(job).read_text()
            text = (job / "REPORT.md").read_text()
            self.assertIn("NOT all scored by the same battery", text)
            self.assertIn("not comparable", text)

    def test_one_battery_is_stated_plainly(self):
        with tempfile.TemporaryDirectory() as td:
            job = self._job(Path(td),
                            [self._judge("accepted", "a" * 64), self._judge("rejected", "a" * 64)],
                            [None, None])
            report_mod.job_report(job)
            self.assertIn("scored by one battery", (job / "REPORT.md").read_text())

    def test_a_regrade_that_disagrees_is_surfaced(self):
        with tempfile.TemporaryDirectory() as td:
            job = self._job(Path(td), [self._judge("partial", "a" * 64)],
                            [{"verdict": "accepted"}])
            report_mod.job_report(job)
            text = (job / "REPORT.md").read_text()
            self.assertIn("disagree with the recorded verdict", text)
            self.assertIn("| partial | accepted |", text)

    def test_a_regrade_that_agrees_says_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            job = self._job(Path(td), [self._judge("accepted", "a" * 64)],
                            [{"verdict": "accepted"}])
            report_mod.job_report(job)
            text = (job / "REPORT.md").read_text()
            self.assertNotIn("Grader provenance", text)
            self.assertIn("scored by one battery", text)


class GitignoreMerged(unittest.TestCase):
    """The DOX overlay appends `.xo/`; it never replaces the repo's own ignores."""

    def test_repo_ignores_are_preserved(self):
        base = "*.pyc\n.pytest_cache/\nbuild/\n"
        merged = context_gen._merged_gitignore(base)
        for line in base.splitlines():
            self.assertIn(line, merged.splitlines())
        self.assertIn(".xo/", merged.splitlines())

    def test_empty_base_still_ignores_xo(self):
        self.assertEqual(context_gen._merged_gitignore(""), ".xo/\n")

    def test_idempotent_and_newline_safe(self):
        once = context_gen._merged_gitignore("*.pyc")
        self.assertEqual(once, "*.pyc\n.xo/\n")
        self.assertEqual(context_gen._merged_gitignore(once), once)

    def test_overlay_carries_the_merged_file(self):
        files = context_gen._xo_files("*.egg-info/\n")
        self.assertEqual(files[".gitignore"], "*.egg-info/\n.xo/\n")

    def test_base_gitignore_is_read_from_the_pinned_tree(self):
        """The plumbing half: read the repo's OWN.gitignore out of repo.git."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "src"
            src.mkdir()
            (src / "app.py").write_text("x = 1\n")
            (src / ".gitignore").write_text("*.pyc\nbuild/\n")
            _git(["init", "-q", "-b", "main"], src)
            _git(["config", "user.email", "t@example.com"], src)
            _git(["config", "user.name", "t"], src)
            _git(["add", "-A"], src)
            _git(["commit", "-q", "-m", "base"], src)
            sha = _git(["rev-parse", "HEAD"], src).strip()
            job = tmp / "job"
            job.mkdir()
            _git(["clone", "-q", "--bare", str(src), str(job / "repo.git")], tmp)

            self.assertEqual(context_gen._base_gitignore(job, sha), "*.pyc\nbuild/\n")
            merged = context_gen._xo_files(context_gen._base_gitignore(job, sha))[".gitignore"]
            self.assertEqual(merged, "*.pyc\nbuild/\n.xo/\n")

    def test_a_repo_with_no_gitignore_yields_just_xo(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            job, sha = _make_job(tmp)
            self.assertEqual(context_gen._base_gitignore(job, sha), "")
            self.assertEqual(context_gen._xo_files("")[".gitignore"], ".xo/\n")


if __name__ == "__main__":
    unittest.main
