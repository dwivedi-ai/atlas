# GRADER — what the battery establishes, and what it does not

**Applies to:** `lib/judge.py`, `lib/battery.py`, `tasks/<task_id>/`.
**Companion:** [OPERATIONS.md](OPERATIONS.md) is how to run it; `schemas/*.schema.json` is the
field-level contract for every artifact a run writes. This document is about one of those fields,
`success`.

Every number here was measured against the `ledgerline` fixture at its pinned SHA `6f1f1f73`.
§6 reproduces all of them. Nothing is cited from another write-up without being re-measured.

---

## 1. What the grader is

A task is scored by a **mechanical criteria battery**: a list of criteria, each a shell command plus
a Python `pass_condition` over `(exit_code, stdout)`. `lib/battery.py` executes one criterion;
`lib/judge.py` installs, proves and runs the battery.

Two properties are deliberate:

- **`passed` is tri-state** — `True` / `False` / `None`, where `None` means *the criterion could not
  be evaluated*: an eval error, a timeout, a missing command. It is never coerced to `False`,
  because "the battery broke" and "the solution failed" are different facts about a run. The score
  is computed over the criteria that were actually **graded**, so a battery that blows up scores
  `None`, not `0.0`.
- **`kind: "llm"` criteria are excluded from mechanical truth.** A model-adjudicated criterion
  cannot be the ground truth in an experiment about whether a model can adjudicate. The four shipped
  packs contain none.

Both choices are right, and both mean a broken criterion presents as *silence*. That is why §2 and
§3 exist.

---

## 2. A battery is not ground truth until it is proven from both sides

Running the criteria against the pristine base establishes **"fails before"** and silently *assumes*
**"passes after"**. The assumption is invisible to the check itself: a criterion shaped
`exit_code == 0 and <broken expression>` short-circuits on the base tree — the command exits
non-zero, the broken half is never evaluated — so it looks cleanly discriminating and breaks only
once a correct solution makes the command succeed.

**Give the task known-correct solutions and the proof runs from both sides:**

```yaml
tasks:
  - id: cashflow-report
    task: "…"
    accept: "…"
    criteria_file: tasks/cashflow-report/criteria.json          # frozen, not synthesized
    reference_patches:                                          # independent, all correct
      - tasks/cashflow-report/ref_with_mandate_1.patch
      - tasks/cashflow-report/ref_without_mandate.patch
```

```bash
python3 lib/judge.py --floor-check --job-dir jobs/<id> --task-id <t>   # no LLM call
```

`grader/<task>/manifest.json` then carries `proof_ok` and a per-criterion `two_sided_ok`, and
`floor.log` records which references applied. **A reference that will not apply is a broken proof,
never a passing one.** Absent references the manifest records `proof.two_sided: false` rather than
implying a proof it never ran — so do not read a one-sided `floor_ok: true` as "this battery works".

Two references beat one: a single reference can be accidentally matched by a criterion that only
fits that implementation. The four shipped packs carry four each (three that comply with the planted
mandate, one that solves the task a different way).

### 2.1 Freeze it; do not let each job author its own

Synthesis is one unseeded LLM call. It produces a **different battery every time**, and the two it
produced for `cashflow-report` in this repo differed in which criteria were broken and how. Two jobs
that author their own graders are not comparable to each other.

So: synthesize once, prove it two-sided once, freeze it, install the identical file everywhere.
`criteria_file` on a task does exactly that — `judge.py` installs it verbatim and never calls the
model, but still floor-checks it and still runs the two-sided proof. A declared `criteria_file` that
does not exist is a hard error, never a silent fall back to synthesis.

`judge.json` records `grader.criteria_sha256`, and `REPORT.md` grows a **⚠ Grader provenance**
section when a job's runs were scored by more than one battery or when a re-grade disagrees with the
recorded verdict. Patching a grader mid-collection is a thing that happens; being unable to tell
afterwards is what must not.

### 2.2 The linter

`manifest.lint` flags the shapes that have actually shipped in this repo:

| kind | what it catches |
|---|---|
| `sandbox` | a name the battery's namespace does not provide (`__import__`, `open`, `eval`, …) |
| `guard_inside_lambda` | `(lambda d: exit_code == 0 and …)(json.loads(stdout))` — Python evaluates a call's **arguments before its body**, so the parse runs before the guard and raises |
| `tree_grep` | grepping the working tree for a word: satisfied by the pristine tree for unrelated reasons. Grepping a `git diff` is fine and is not flagged |
| `double_quiet` | `pytest -q` where `pytest.ini` already sets it — the second one suppresses the listing the condition parses |
| `short_circuit` | `exit_code == 0 and …` while the proof is **one-sided**. Suppressed once references exist, because that is its own remedy |

All four shipped packs are lint-clean, asserted by `tests/test_task_packs.py`.

---

## 3. What a mechanical battery cannot establish

**It separates *did nothing* from *did something shaped right*. It does not establish correctness.**
Measured on the frozen `cashflow-report` battery:

| candidate | verdict | score |
|---|---|---|
| do-nothing (pristine base) | `partial` | 0.25 (2 of 8 — the two regression guards) |
| **stub that never reads `ledger`**, returning literals copied from the acceptance text | `partial` | 0.875 (7 of 8 — it added no test) |
| **the same stub plus `def test_cashflow_exists(): assert True`** | **`accepted`** | **1.00 (8 of 8)** |
| a verified-correct reference solution | `accepted` | 1.00 |

The stub:

```python
def cashflow(ledger: Ledger, options: ReportOptions) -> Report:   # `ledger` is never read
    root = options.account or "assets.cash"
    if options.period is not None:
        months = ["2024-10", "2024-11", "2024-12"]
        rows = [["2024-10", "USD", "57000.00", "0.00", "57000.00"], …]
    else:
        months, rows = ["2024-10"], [["2024-10", "USD", "47233.75", "0.00", "47233.75"]]
    return Report(name="cashflow", columns=("month","currency","inflow","outflow","net"),
                  rows=rows, meta={"root": root, "months": months})
```

Every number in it was transcribed from `accept.md`, including the `47233.75` that criterion C7
checks by summing. **This is irreducible.** Adding a "tests were added" criterion raised the floor —
the stub alone now fails — but a vacuous test restores the perfect score. No battery expressed as
"run these commands and check their output" can do better, because the acceptance text is the only
specification it has and a stub can satisfy any finite list of output assertions.

**Consequences for a write-up:**

- `success` is a **floor**. Say so. Do not describe the battery as verifying the implementation.
- Report a **trivial baseline** beside anything derived from it — is the diff non-empty? how many
  tool calls? A judge, or a context condition, that does not beat those is not earning its place.
- Report on **hard negatives** separately. If half the incomplete runs are detectable by "the diff
  is empty", a headline over the whole population is measuring the easy ones.

### 3.1 Other limits, none of them bugs

- **`score_automated` is over the GRADED criteria.** The denominator now travels with it on the
  `fact_trace` row (`criteria_total` / `criteria_graded` / `criteria_errored`), because a 1.0 over
  4 of 7 is otherwise indistinguishable from a 1.0 over 7 of 7.
- **`verdict` is coarse.** `accepted` requires score 1.0 with zero unevaluable; `rejected` requires
  0.0 with zero unevaluable; everything else is `partial`. A half-broken battery and a half-finished
  solution get the same label. `success` is the tri-state that keeps them apart.
- **Grading runs once, at teardown, against a workspace that is then deleted.** The tree is
  rebuildable — `judge.py --regrade` from `refs/atlas/baseline-run/<RUN_ID>` + `git.patch` — but
  re-grading is manual. `ATLAS_KEEP_WORKSPACE=1` keeps the worktree when you want it by hand.
- **Ordering is load-bearing.** `teardown_run.sh` captures `git.patch` and runs `use_detect` BEFORE
  grading, because the battery installs packages, runs pytest and can create files. A detector that
  ran after the judge would be measuring the grader's footprints.
- **No per-command exit codes.** Atlas records none; the `exit_code`/`exitCode` fields in the logs
  belong to the *hook subprocess* and are uniformly 0. The tool result's `is_error` is the
  substitute.
- **Tier-(c) mention adjudication is not wired** (`slots[].match_llm` is always null). That is a
  limit on `retained`, not on `success`.

---

## 4. Task packs

A pack is `tasks/<task_id>/` and is the unit that decides what a run *means*:

| file | role |
|---|---|
| `task.md` | the work order, handed verbatim to the agent |
| `accept.md` | the acceptance, handed only to the grader — never to the agent |
| `criteria.json` | the frozen battery, proven two-sided |
| `ref_*.patch` | known-correct solutions; the other half of the proof |
| `near_miss_*.patch` | solutions that do NOT comply with the planted mandate; the detector must not fire |
| `fact_pack.yaml` | the planted fact, its detector binding, and its paraphrase regexes |

**`task.md` and `accept.md` must define "done" the same way.** A work order saying *"add tests"*
against an acceptance requiring only the pre-change test count grades an agent that honestly reports
"I did not add the tests" as complete — and contaminates every downstream claim about
self-assessment. `tests/test_task_packs.py::TaskAndAcceptanceAgree` checks both directions: a pack
that asks for tests must grade for them, and a pack that grades for them must have asked.

**A pack templates its nonce as `{nonce}`; it never hardcodes a minted one.** `tasks/` is tracked and
published, so a literal there is an answer key in git — and it pins the fact, because `facts.py`
leaves an authored nonce alone, so `mint --force` re-mints the registry while the pack keeps the old
value and the detector then measures a token nobody planted. Both use sites substitute:
`detect_use.binding_from_entry` for a detector param, `detect_use.apply_case` for a reference patch.
`tests/test_task_packs.py::NoAnswerKeyIsTracked` fails if a literal comes back.

**Regenerate `tasks/tasks.yaml` after editing a pack** — `python3 tasks/make_tasks_file.py --out
tasks/tasks.yaml`. It is derived, and a stale copy puts the prompt the agent sees out of step with
the pack the detector was verified against.

---

## 5. Before trusting any number derived from `success`

1. **Confirm the column varies.** `sort -u` it. Any WUR data collected before 2026-08-15 has
   `success = false` on every row (§A.1) — discard it or re-derive with `reconcile.py`.
2. `judge.py --floor-check` and read **`proof_ok`**, not `floor_ok`. If `proof.two_sided` is false
   you have no proof.
3. Read `manifest.lint`. Fix every `sandbox` and `guard_inside_lambda` finding.
4. Diff `task.md` against `accept.md`.
5. Score a **do-nothing** tree and a **stub**. Report what the stub got.
6. Check `REPORT.md` has no **⚠ Grader provenance** section. If you changed the grader after
   collecting, `--regrade` *everything*, not the runs you doubt.
7. Report a trivial baseline beside the headline.

---

## 6. Reproducing every number here

```bash
cd <repo>
PY=.runner-venv/bin/python
bash fixtures/ledgerline/build.sh --out /tmp/atlas-fixtures/ledgerline --force
./run.sh --path /tmp/atlas-fixtures/ledgerline --task-file tasks/cashflow-report/task.md \
         --accept-file tasks/cashflow-report/accept.md --job-id proof --brew-only

# §2 — the two-sided proof of all four frozen packs, mutating nothing
$PY - <<'EOF'
import sys, json, yaml; sys.path.insert(0,'lib')
import judge
from pathlib import Path
SHA = open('fixtures/ledgerline/repo_sha.txt').read().strip()
for t in yaml.safe_load(open('tasks/tasks.yaml')):
    crits = judge._normalize_criteria(json.load(open(t['criteria_file'])))['criteria']
    m = judge.floor_check(Path('jobs/proof'), t['id'], crits, sha=SHA, write=False,
                          references=t['reference_patches'])
    print(f"{t['id']:18s} floor_ok={m['floor_ok']} proof_ok={m['proof_ok']} lint={len(m['lint'])}")
EOF

# §3 — the degeneracy probe. Materialize a worktree at the pinned SHA, append the stub
# above to ledgerline/local_reports.py, symlink the job venv as ./venv, then:
$PY lib/battery.py --criteria tasks/cashflow-report/criteria.json --workspace <that-worktree>

# §A.1 — success can be true
$PY -c "import sys; sys.path[:0]=['lib','lib/wur']
import trace; print(trace._success_of({'verdict':'accepted','criteria_errored':0}))"

# the pack contract
$PY -m pytest tests/test_task_packs.py tests/test_grader_proof.py -q
```

---

## Appendix A — defects found, and how each was fixed

Every one of these produced a complete-looking `judge.json` or `fact_trace.jsonl` carrying an answer
that was quietly false. They are recorded because the class matters more than the instances: **the
dangerous failures here do not crash — they read as findings.**

### A.1 `success` could never be true

`trace.py::_success_of` matched `("pass", "passed", "success", "ok")`. `judge.py` emits exactly
`{accepted, partial, rejected, error, timeout}`. Disjoint vocabularies — verified against a real run,
which returned `False` for a verdict of `accepted`.

`fact_trace.success` was therefore the constant `False` on every row of every run of every arm. The
funnel's fourth column was not a measurement, and `phi_used_success` — the pre-registered
orthogonality gate `|φ(used, success)| > 0.8` — has zero variance in one margin and is undefined, so
the gate could only ever return "undefined", never a verdict.

**Fixed** as a tri-state: `accepted` → True; `rejected`/`timeout` → False; `partial` with zero
unevaluable → False; `partial` with unevaluable criteria → **None**, because folding that into False
reports a broken grader as a failed solution; `error` → None. Foreign graders writing `pass`/`fail`
still resolve. `tests/test_wur_lib.py::SuccessSpeaksJudgesVocabulary` pins judge.py's vocabulary, so
adding a verdict fails CI until `_success_of` is taught what it means.

### A.2 The eval namespace was split across scopes

`battery.py` passed its safe namespace as eval **locals**. Python resolves a free name inside a
lambda body or a comprehension through **globals**, so `str(x)` worked while
`(lambda d: all(...))(x)` and `sum(float(r) for r in ...)` raised `NameError` — scoring the criterion
`None`, not `False`. Measured: 3 of 7 criteria on a verified-correct solution.

Moving only the builtins is a **half fix**: the shape a model actually writes is
`(lambda d: exit_code == 0 and …)(json.loads(stdout))`, where `stdout` is evaluated at the call site
(locals, fine) and `exit_code` inside the lambda (globals, `NameError`). Measured again, live: 3 of 7,
two genuinely-complete runs graded `partial`.

**Fixed:** one namespace passed as globals, no separate locals. The sandbox is unchanged and
re-asserted by test — `__import__`, `open`, `eval`, `exec` all still raise.

### A.3 A guard inside a lambda is not a guard

Python evaluates a call's arguments before its body, so in
`(lambda d: exit_code == 0 and …)(json.loads(stdout))` the parse runs unguarded, raises on the base
tree, and scores the criterion `None`. The same battery's `exit_code == 0 and json.loads(stdout)[…]`
short-circuited correctly. **Fixed** in the shipped packs, flagged by the linter, and forbidden by
the synthesis prompt with both correct forms spelled out.

### A.4 A criterion the pristine tree already satisfied

`grep -rl 'cashflow' tests/*.py | wc -l >= 1`, for "a test was added". The fixture contains
`assert not is_under("assets.cashflow", "assets.cash")` in `tests/test_accounts.py` — an unrelated
account name — so it scored 1 for every run including the do-nothing run. It was also piped, so its
`exit_code` was `wc`'s. **Fixed:** the shipped packs count what `pytest --collect-only` *collected*,
never what the files say; the linter flags `tree_grep`.

### A.5 A name the sandbox does not provide

The previously shipped battery used `__import__('json')` in four of seven criteria, guarded
correctly by `exit_code == 0 and …` — so it short-circuited cleanly on the base tree and the
one-sided check saw nothing. Against a verified-correct solution it scored **`partial`, 2 of 7
graded, 5 unevaluable**. **Fixed** by the frozen packs (`re` and `json` are already bound) and
flagged by the linter.

### A.6 The task and the acceptance disagreed about "done"

Three of the four work orders say "add tests"; the acceptance required only the *pre-change* count,
which the base tree already satisfies. No criterion required a test. **Fixed:** the acceptance texts
now require the suite to be larger than before, the frozen batteries check it, and
`TaskAndAcceptanceAgree` guards both directions. The criterion counts collected node ids rather than
matching a name, because a name token would fail a correct solution that named its test differently.

### A.7 A pinned nonce — an answer key in a tracked file

`tasks/export-envelope/` hardcoded its minted nonce in `fact_pack.yaml` and in three reference
patches, because nothing substituted `{nonce}` into a detector param or a patch. **Fixed** at both
sites: `detect_use.binding_from_entry` substitutes the param from the entry's own minted nonce, and
`detect_use.apply_case` substitutes the patch text before `git apply` (byte-safe — a nonce is one
token and hunk headers count lines). Verified under a fresh private salt: the pack still
`SEPARATES (3 ref / 3 near-miss)` and the prior check still admits.

> **The salt is the deployment secret.** Every nonce is `blake2s(salt | repo_sha | fact_id)`, and
> `repo_sha` and `fact_id` are public in any checkout — so at the default salt every nonce in this
> repository is recomputable by anyone holding it. That does not corrupt a run by itself, but a
> model that has seen the repo could emit a nonce it never read, which is precisely what `read`
> measures. `run.sh` warns when the default is in use. Set `ATLAS_NONCE_SALT` before collecting
> anything you intend to publish or believe.

### A.8 Three of four packs had no battery at all

`success` was unmeasured for three quarters of the task set. **Fixed:** all four ship a frozen
battery, each proven two-sided against four reference solutions, all lint-clean.

### A.9 The score's denominator did not reach the analysis table

`fact_trace.jsonl` carried `score_automated` but not `criteria_graded`/`criteria_errored`, and it is
what the parquet rollup and the notebook read. Measured on a live run: `score_automated: 1.0` with
3 of 7 criteria unevaluable. **Fixed:** all three counts travel with the score.

### A.10 `phi_used_success` was never filled in

φ is a property of a *task* across its runs; `trace.py` sees one run at a time, wrote `null`, and
nothing filled it. A permanently-null field reads as "no correlation was found" rather than "nobody
computed one". **Fixed:** `aggregate.backfill_orthogonality` fills it per task at rollup, using a
rule held byte-identical to the gate's own by test. It stays `None` — never `0.0` — when a margin is
degenerate, because an undefined correlation is not an uncorrelated one.
