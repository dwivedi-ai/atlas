#!/usr/bin/env python3
"""
test_task_packs.py — the contract every `tasks/<task_id>/` pack must satisfy.

RESPONSIBILITY
  A task pack is the unit that decides what a run MEANS: the work order the agent
  sees, the acceptance the grader sees, the frozen battery that scores it, and the
  known-correct solutions that prove the battery discriminates. Every check here
  guards a way those four can silently disagree — and each one corresponds to a
  defect that shipped.

WHAT EACH TEST DEFENDS
  frozen battery present    Synthesis is one unseeded LLM call that produces a
      different battery every time. Two jobs that author their own graders are not
      comparable to each other, and the base-only floor check cannot tell a sound
      battery from a broken one. A pack must ship `criteria.json`.
  references present        Without known-correct solutions the floor check only
      ever proves "fails before" and ASSUMES "passes after". A criterion broken
      behind a short-circuit ships as discriminating.
  lint clean                The four shapes that have actually shipped: a name the
      sandbox does not provide, a guard inside a lambda whose argument parses, a
      piped command, a doubled `-q`.
  baseline pinned           The batteries carry a literal test count. Nothing tied
      it to the fixture, so a fixture change would silently under- or over-measure
      every run. This recomputes it from the fixture tree.
  task and acceptance agree The work orders say "add tests"; the acceptance text
      used to require only the PRE-CHANGE count, so a run that added none was
      graded complete — and every downstream claim about an agent misjudging its
      own work was contaminated.
  mechanical only           A `kind: "llm"` criterion cannot be the ground truth in
      an experiment about whether a model can adjudicate.
  tasks.yaml not stale      It is generated. Hand-editing it puts the prompt the
      agent sees out of step with the pack the detector was verified against, which
      is unrecoverable after the run.

Deliberately NOT here: whether a battery actually discriminates. That needs a
brewed repo and a worktree per reference, so it is the two-sided proof
(`judge.py --floor-check`), not a unit test.

CLI
  python3 tests/test_task_packs.py [-v]        (stdlib unittest; no pytest needed)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
TASKS = ROOT / "tasks"
FIXTURE_TESTS = ROOT / "fixtures" / "ledgerline" / "tree" / "tests"
sys.path.insert(0, str(LIB))

import judge as judge_mod  # noqa: E402

#: Packs are every directory under tasks/ carrying a work order.
PACKS = sorted(d for d in TASKS.iterdir() if d.is_dir() and (d / "task.md").is_file())

#: The fixture's own test count at its pinned SHA, which the batteries' regression
#: guards are expressed against. Recomputed from the tree by
#: `test_the_baseline_literal_still_matches_the_fixture` rather than trusted.
BASELINE_TESTS = 156


def _fixture_test_count() -> int:
    """Tests the fixture defines. Equal to pytest's collected count because the
    fixture uses no `parametrize` and no test classes — both asserted below, since
    either would break the equivalence this shortcut relies on."""
    return sum(len(re.findall(r"^def test_", p.read_text(), re.M))
               for p in sorted(FIXTURE_TESTS.glob("*.py")))


class PackContract(unittest.TestCase):
    def test_there_are_packs_at_all(self):
        self.assertTrue(PACKS, "no task packs found under tasks/")

    def test_every_pack_ships_a_frozen_battery(self):
        for pack in PACKS:
            with self.subTest(pack=pack.name):
                self.assertTrue((pack / "criteria.json").is_file(),
                                f"{pack.name} has no criteria.json — every job would "
                                f"synthesize its own and they would not be comparable")

    def test_every_pack_ships_known_correct_solutions(self):
        for pack in PACKS:
            with self.subTest(pack=pack.name):
                refs = sorted(pack.glob("ref_*.patch"))
                self.assertGreaterEqual(len(refs), 2,
                                        f"{pack.name}: needs >= 2 INDEPENDENT references; one "
                                        f"can be accidentally matched by a criterion that only "
                                        f"fits that implementation")

    def test_every_battery_is_well_formed_and_mechanical(self):
        for pack in PACKS:
            with self.subTest(pack=pack.name):
                crits = json.loads((pack / "criteria.json").read_text())["criteria"]
                self.assertTrue(crits)
                ids = [c["id"] for c in crits]
                self.assertEqual(len(ids), len(set(ids)), "duplicate criterion id")
                for c in crits:
                    self.assertEqual(c.get("kind"), "mechanical",
                                     f"{c['id']}: llm criteria cannot be ground truth here")
                    self.assertIn(c.get("expect_on_base"), ("pass", "fail"))
                    self.assertTrue(c.get("command"))
                    self.assertTrue(c.get("pass_condition"))
                    self.assertTrue(c.get("description"))

    def test_every_battery_is_lint_clean(self):
        """Every pack ships references, so the proof is two-sided and `short_circuit`
        is answered; anything else the linter finds is a real defect."""
        for pack in PACKS:
            with self.subTest(pack=pack.name):
                crits = json.loads((pack / "criteria.json").read_text())["criteria"]
                findings = judge_mod.lint_criteria(crits, two_sided=True)
                self.assertEqual(findings, [],
                    "\n".join(f"{f['id']} {f['kind']}: {f['detail'][:90]}" for f in findings))

    def test_at_least_one_criterion_must_fail_on_base(self):
        """A battery of pure regression guards cannot separate anything."""
        for pack in PACKS:
            with self.subTest(pack=pack.name):
                crits = json.loads((pack / "criteria.json").read_text())["criteria"]
                self.assertTrue(any(c["expect_on_base"] == "fail" for c in crits))


class NoAnswerKeyIsTracked(unittest.TestCase):
    """A pack must template its nonce, never hardcode a minted one.

    `tasks/` is tracked and published. A hardcoded nonce there is an answer key in
    git, and it also PINS the fact: `facts.py` leaves an authored nonce alone, so
    `mint --force` re-mints the registry while the pack keeps the old literal and
    the detector then measures a token nobody planted. Both halves are substituted
    at use — `detect_use.binding_from_entry` for a detector param,
    `detect_use.apply_case` for a reference patch — so nothing needs a literal.
    """

    #: `nonce.mint` emits <prefix>-<body>; the default prefix is ZQ.
    NONCE_RE = re.compile(r"\bZQ-[A-Z0-9]{8}\b")

    def test_no_pack_file_carries_a_minted_nonce(self):
        for pack in PACKS:
            for f in sorted(pack.iterdir()):
                if not f.is_file():
                    continue
                with self.subTest(path=str(f.relative_to(ROOT))):
                    hits = self.NONCE_RE.findall(f.read_text(errors="replace"))
                    self.assertEqual(hits, [],
                        f"{f.relative_to(ROOT)} hardcodes {hits} — template it as "
                        f"`{{nonce}}` instead")

    def test_a_pack_that_names_the_token_templates_it(self):
        """Sanity: the templating is actually used somewhere, so this suite would
        notice if `{nonce}` support silently stopped working."""
        blob = "\n".join(f.read_text(errors="replace")
                         for pack in PACKS for f in pack.iterdir() if f.is_file())
        self.assertIn("{nonce}", blob)


class BaselineIsPinnedToTheFixture(unittest.TestCase):
    """The batteries carry a literal test count; nothing tied it to the fixture."""

    def test_the_shortcut_is_valid(self):
        """`def test_` count == collected count only while there is no parametrize
        and no test class. If either appears, this file's arithmetic is wrong and
        the baseline must be measured with `pytest --collect-only` instead."""
        blob = "\n".join(p.read_text() for p in sorted(FIXTURE_TESTS.glob("*.py")))
        self.assertNotIn("parametrize", blob)
        self.assertNotIn("\nclass Test", blob)

    def test_the_baseline_literal_still_matches_the_fixture(self):
        self.assertEqual(_fixture_test_count(), BASELINE_TESTS,
                         "the fixture's test count changed; every battery's regression "
                         "guard and added-tests criterion is now measuring the wrong thing")

    def test_every_battery_uses_that_baseline_and_no_other_number(self):
        for pack in PACKS:
            with self.subTest(pack=pack.name):
                text = (pack / "criteria.json").read_text()
                for cond in re.findall(r'"pass_condition":\s*"(.*?)"(?=,\n)', text):
                    for n in re.findall(r"\bl\)\]\)\s*[<>]=?\s*(\d+)", cond):
                        self.assertEqual(int(n), BASELINE_TESTS,
                                         f"{pack.name}: a test-count threshold of {n} that is "
                                         f"not the fixture baseline {BASELINE_TESTS}")


class TaskAndAcceptanceAgree(unittest.TestCase):
    """The two documents must define 'done' the same way.

    A work order saying "add tests" against an acceptance requiring only the
    pre-change count grades an agent that honestly reports "I did not add the tests"
    as complete, and contaminates every claim about self-assessment downstream.
    """

    ASKS_FOR_TESTS = re.compile(r"add tests", re.I)
    REQUIRES_GROWTH = re.compile(r"(larger than before|more tests are collected)", re.I)

    def test_a_pack_that_asks_for_tests_grades_for_them(self):
        for pack in PACKS:
            task = (pack / "task.md").read_text()
            accept = (pack / "accept.md").read_text()
            if not self.ASKS_FOR_TESTS.search(task):
                continue
            with self.subTest(pack=pack.name):
                self.assertRegex(accept, self.REQUIRES_GROWTH,
                    f"{pack.name}/task.md asks the agent to add tests but accept.md does "
                    f"not require the suite to grow — nothing grades it")

    def test_a_pack_that_grades_for_growth_asks_for_it(self):
        """The mirror: do not require something the agent was never told to do."""
        for pack in PACKS:
            task = (pack / "task.md").read_text()
            accept = (pack / "accept.md").read_text()
            if not self.REQUIRES_GROWTH.search(accept):
                continue
            with self.subTest(pack=pack.name):
                self.assertRegex(task, self.ASKS_FOR_TESTS)


class GeneratedTasksFileIsCurrent(unittest.TestCase):
    def test_tasks_yaml_is_not_stale(self):
        r = subprocess.run([sys.executable, str(TASKS / "make_tasks_file.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0,
                         f"tasks.yaml is stale or a pack is incomplete:\n{r.stderr}")

    def test_every_declared_path_exists(self):
        import yaml  # noqa: PLC0415
        for t in yaml.safe_load((TASKS / "tasks.yaml").read_text()):
            with self.subTest(task=t["id"]):
                self.assertTrue((ROOT / t["criteria_file"]).is_file(), t["criteria_file"])
                for p in t["reference_patches"]:
                    self.assertTrue((ROOT / p).is_file(), p)


if __name__ == "__main__":
    unittest.main
