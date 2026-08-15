# Atlas documentation

Atlas (`exp-runner` in the code) points a headless coding agent at a repository and a task, records
everything it does, grades the result, and writes the whole thing down. It runs two experiments over
that machinery:

- **Ladder** — the same task across seven escalating context environments (E0 bare → E6 fully
  scaffolded), measuring what richer context costs and buys.
- **WUR** (Workspace Uptake & Retention) — one counter-prior fact planted in the workspace, tracked
  across four boundaries: **available → read → used → retained**.

The root [README](../README.md) is the tool's front page: install it, run a job, read the output.
These documents are for changing it, or for trusting a number that came out of it.

---

## Read in this order

| | | |
|---|---|---|
| 1 | **[GRADER.md](GRADER.md)** | What the mechanical battery establishes, and what it does not. **Read this before quoting `success` or any score.** A stub that never reads its input scores 8/8 `accepted`; §5 is the checklist. |
| 2 | [OPERATIONS.md](OPERATIONS.md) | Running it: prerequisites, environment knobs, the traps that cost real time, and the commands. |
| 3 | [RESULTS.md](RESULTS.md) | What has actually been measured, what is still open, and what is deliberately not built. |
| 4 | [CHANGELOG.md](CHANGELOG.md) | Dated record of every change that alters a recorded number. |

**`schemas/*.schema.json` is the authoritative field-level documentation.** When a document and the
code disagree, the code and the schemas win: several statements in the spec were falsified by
measurement and amended, so assume drift.

---

## The one thing to carry into any change

Atlas's dangerous defects do not crash. They produce a complete-looking `judge.json` or
`fact_trace.jsonl` carrying a number that is quietly false — and that number reads as a **finding**,
not as a bug. Two columns in this repository were structural constants for their entire existence:
`retained` (the paraphrase matchers were absent from the file the reconciler is handed) and
`success` (the grader and the tracer did not share a verdict vocabulary). Both looked like results.

So, when you add or change a measurement, ask: **what would this look like if the mechanism were
dead?** If the answer is "a plausible zero", it needs a test that fails when it is. Every entry in
[GRADER.md](GRADER.md) Appendix A is an instance of that question not being asked in time.

## Testing

```bash
python3 -m pytest tests/ -q          # needs pytest; not in requirements.txt
python3 tests/test_wur_lib.py -v     # or run any file directly — stdlib unittest, no pytest
```

| file | guards |
|---|---|
| `tests/test_wur_lib.py` | the instrument's invariants — anything whose failure is a wrong number rather than a crash |
| `tests/test_grader_proof.py` | the grading defects: sandbox scope, the two-sided proof, re-grading, provenance |
| `tests/test_task_packs.py` | the task-pack contract: frozen battery, references, lint, the baseline, task/acceptance agreement |
| `tests/test_detectors.py` | the closed detector registry |
