# exp-runner — design & build plan

**A generalized, single-repo coding-agent runner.** A person gives it (1) their repo, (2) a task,
(3) a plain-English description of what an accepted solution looks like, and (4) a model (codex or
claude). It *brews* an isolated, hermetic environment, runs the agent headless against the task in an
isolated git worktree, has an **LLM judge** grade the result against the acceptance description, and
writes everything into one organized job folder.

This is a generalization of the research harness in `/home/coder/experiments/` (the L1–L4 arms). That
harness ran a *fixed* repo and a *fixed* task catalog across a *matrix* of context conditions to measure
cost/conformance. `exp-runner` keeps the harness's hermetic, worktree-per-run, capture-everything
machinery but turns the three fixed inputs into user-supplied prompts and collapses the matrix to a
single "repo as-is" condition.

---

## 0. Decisions (locked)

- **Acceptance = natural language → judge-synthesized mechanical tests.** The user describes the accepted
  solution in prose. The judge does **not** grade from prose alone — it first *authors a small battery of
  mechanical tests/helpers* from the acceptance text (pytest functions, shell/curl probes), runs them
  against the workspace, and adjudicates from their pass/fail results. This makes grading largely
  deterministic and re-runnable while keeping plain-English input. (See §5.)
- **Auth is a documented prerequisite, not handled by the tool.** Users `codex login` / `claude login`
  before running; `run.sh` just fails fast with a clear message if a CLI isn't authenticated.
- **Brew = Python-first + generic fallback.** Auto-detect & build Python repos (venv from
  `requirements.txt` / `pyproject.toml`). Other stacks clone + run; the user may supply a build command.
- **Run mode = single model, optional `--reps N`.** One of codex / claude per job; reps repeat the same
  cell. (Head-to-head compare is a trivial later extension — run the job twice with different models into
  the same job folder.)
- **Self-analysis end step (the "reflection pass").** After a run is graded, the *same* agent is invoked
  one more time with a final prompt asking it to honestly analyze its own result — what it did, whether it
  thinks it met the acceptance, its confidence, and what it would do differently — and write it to a
  job-level **`agent-analysis/<run-id>.md`** folder. A separate invocation (keeps the graded solution
  pure), on by default, skippable with `--no-analyze`. This is the research payoff: one folder of every
  agent's self-assessment, side by side. (See §5b.)

---

## 1. What we inherit from the experiments harness (and how it maps)

| experiments/ (L4 harness)                              | exp-runner                                                        |
|--------------------------------------------------------|-------------------------------------------------------------------|
| Fixed target repo, SHA baked into `bootstrap.sh`       | **User repo URL/path**, pinned at clone (HEAD or `--ref`)         |
| `harness/tasks/*.yaml` fixed catalog                   | **One `job.yaml`** authored by the wizard                         |
| `harness/envs/E0..CDOX` overlays + context stripping   | **Dropped.** Repo runs as-is (one condition)                      |
| `acceptance_criteria.automated[]` (shell/pytest/regex) | **`judge.py`** — NL acceptance → structured verdict              |
| `setup_run.sh` (worktree + overlay + venv + hooks)     | **`setup_run.sh`** (worktree + venv + hooks; *no* overlay)        |
| codex/claude headless invocation block                 | **`run_agent.sh`** (same flags, parameterized by model)           |
| `teardown_run.sh` (diff, transcript, grade, telemetry) | **`teardown_run.sh`** (same, grader = judge)                      |
| `extract_telemetry.py` / `validate_schema.py`          | **ported ~verbatim**                                              |
| `run_experiment.sh` matrix loop (task×env×rep)         | **`run.sh`** wizard + single-cell (× reps) loop                   |
| `results/raw/*.jsonl` aggregate                         | per-job `report.md` + `run_record.json` (+ optional jobs index)   |

**Key invocation facts to preserve (verified in the harness):**
- codex: `codex exec -C <ws> --sandbox workspace-write --dangerously-bypass-approvals-and-sandbox
  --ephemeral --json "<prompt>" > transcript.jsonl` — the JSONL stdout *is* the transcript.
- claude: `env -u ANTHROPIC_API_KEY claude --model <id> --output-format json --print
  --permission-mode bypassPermissions "<prompt>"` — strip `ANTHROPIC_API_KEY` so headless uses the
  *subscription* login; `bypassPermissions` because `--print` can't approve tool calls; transcript is
  fished out of `~/.claude/projects/<workspace-slug>/*.jsonl` in teardown.
- claude needs a per-run logging hook merged into `~/.claude/settings.json` (backed up + restored) for
  timing/telemetry; codex needs none. → claude runs **serial**, codex may run reps in parallel.
- worktree add/remove is guarded by a repo-wide `flock` (concurrent reps would race the bare repo).
- The agent is given **only the task prompt — never the acceptance text** (no leaking the grader).

---

## 2. Folder layout

```
exp-runner/
├── README.md                 # what it is, quickstart, prerequisites
├── PLAN.md                   # this file
├── run.sh                    # entry point: interactive wizard + flag mode
├── requirements.txt          # runner deps: pyyaml, jsonschema (+ judge via codex/claude CLI)
├── lib/
│   ├── wizard.py             # interactive prompts -> validated job.yaml
│   ├── brew.sh               # clone (bare) + pin SHA + detect stack + build env   ["brew"]
│   ├── detect_stack.py       # python/node/other detection -> build plan
│   ├── setup_run.sh          # per-run worktree + venv symlink + claude hooks + run_meta
│   ├── run_agent.sh          # headless codex/claude invocation
│   ├── teardown_run.sh       # capture diff+transcript, call judge, telemetry, cleanup
│   ├── judge.py              # LLM-judge grader: NL acceptance -> judge.json
│   ├── extract_telemetry.py  # ported: tokens/timing/navigation -> run_record.json
│   ├── validate_schema.py    # ported
│   └── report.py             # run_record + judge -> report.md
├── schemas/
│   ├── job.schema.json       # validates a job spec
│   └── run_record.schema.json# ported, generalized (relax task_id/env_id patterns)
├── templates/
│   └── job.example.yaml
└── jobs/                     # ALL output; one folder per job
    └── <job-slug>/
        ├── job.yaml          # frozen spec: repo, ref(pinned SHA), task, acceptance, model
        ├── repo.git/         # bare clone (the brew) — shared by all reps in this job
        ├── .venv/            # hermetic env built from the repo
        ├── grader/           # judge-synthesized test battery (authored ONCE, shared by all reps)
        │   ├── criteria.json #   decomposed acceptance criteria
        │   ├── test_accept.py#   generated pytest battery (and/or checks.sh)
        │   ├── manifest.json #   criterion -> test mapping + floor-check results
        │   └── floor.log     #   sanity run against pristine base (expect failures)
        ├── brew.log
        ├── agent-analysis/   # the research payoff: one self-assessment per run
        │   ├── <run-id>.md   #   the agent's honest reflection on its own result
        │   └── ...
        └── runs/
            └── <run-id>/     # one per (model, rep), id = <slug>-<model>-rNNN
                ├── workspace/        # worktree (removed after grade; patch kept)
                ├── run_meta.json
                ├── transcript.jsonl
                ├── agent_stdout.txt / agent_stderr.txt
                ├── git.patch         # the agent's solution as a unified diff
                ├── judge.json         # verdict + score + per-criterion evidence
                ├── analysis.md        # copy of this run's self-analysis
                ├── run_record.json    # canonical telemetry (ported schema)
                ├── event_log.jsonl
                └── report.md
```

Bare clone + venv are **per job** (one repo per job), shared across that job's reps via worktrees +
a `venv` symlink — exactly the experiment's "bootstrap once, worktree per cell" pattern.

---

## 3. The job spec (`job.yaml`)

```yaml
schema_version: "1"
job_id: add-health-endpoint            # slug; folder name
repo:
  url: https://github.com/acme/widgets.git   # or local path:
  # path: /home/coder/widgets
  ref: HEAD                            # branch/tag/SHA; resolved + pinned at brew
  pinned_sha: null                     # filled by brew.sh
build:
  stack: auto                          # auto | python | node | none
  command: null                        # optional override, e.g. "make deps"
task: |                                # handed verbatim to the agent
  Add a GET /health endpoint that returns 200 with body {"status":"ok"}.
accept: |                              # handed ONLY to the judge
  A GET /health route returns 200 and JSON {"status":"ok"}, wired into the app's
  router. Existing tests still pass. No unrelated files changed.
model: codex                           # codex | claude(-sonnet-4-6)
reps: 1
max_seconds: 0                         # 0 = no timeout
```

`schemas/job.schema.json` validates this before any work starts (wizard and flag mode both go through it).

---

## 4. Flow (end to end)

1. **Author** — `run.sh` with no args runs `wizard.py`: prompts for repo, ref, task (multiline /
   `$EDITOR`), acceptance (multiline), model, reps → writes + validates `jobs/<slug>/job.yaml`, echoes a
   confirmation.
2. **Brew** (`brew.sh`, once per job) —
   a. `git clone --bare <url> repo.git` (or from local path); drop `objects/info/alternates`.
   b. Resolve `ref` → concrete SHA, verify it exists, write `pinned_sha` back into `job.yaml`.
   c. `detect_stack.py`: requirements.txt / pyproject / setup.py → python; package.json → node; else none.
   d. Build env: **python** → `python3 -m venv .venv`; `pip install -r requirements.txt` or `pip install
      -e .` + `pytest`. **node** (fallback) → `npm ci`/`pnpm i`. **none / override** → run `build.command`
      if given, else skip. All output → `brew.log`. On failure: clear message + "supply build.command".
3. **Synthesize grader** (`judge.py --synthesize`, once per job) — turn the NL `accept` into the test
   battery in `jobs/<slug>/grader/`, then **floor-check** it against a pristine base worktree (§5 A/A′).
   Done before any run so every rep is graded by the same battery.
4. **Setup run** (`setup_run.sh`, per rep) — `git worktree add --detach workspace <pinned_sha>` (flock);
   symlink `../../.venv` → `workspace/venv`; if claude, back up + merge logging hook into
   `~/.claude/settings.json`; write `run_meta.json`. **No stripping, no overlay.**
5. **Run agent** (`run_agent.sh`) — invoke codex/claude headless (flags in §1), cwd = workspace, capture
   transcript + stdout/stderr, record exit code (124 = timeout).
6. **Teardown + grade** (`teardown_run.sh`) — `git diff HEAD` + untracked → `git.patch` (captured *before*
   any later step so the graded solution stays pure); locate/copy transcript; **grade** by running the
   battery + adjudication (`judge.py --grade`) → `judge.json`.
7. **Self-analysis** (`self_analysis.sh`, unless `--no-analyze`) — invoke the *same* agent once more in the
   still-present workspace with a reflection prompt (original task + its own diff + the grade if present);
   it writes `agent-analysis/ANALYSIS.md`, which teardown copies out to job-level
   `agent-analysis/<run-id>.md` + run-dir `analysis.md`. Then `extract_telemetry.py` → `run_record.json`;
   validate schema; restore `~/.claude/settings.json`; `git worktree remove` (flock). Patch is kept
   (`git apply git.patch`).
8. **Report** (`report.py`) — `report.md`: task, model, verdict + score, per-criterion table with
   evidence, tokens/timing, the self-analysis, patch location + apply instructions. Reps → an aggregate
   scorecard.

Resume-safe: a run whose `run_record.json` exists is skipped (ported behavior). `run.sh --job <slug>`
re-runs against an already-brewed job (skips clone/build).

---

## 5. The judge (`judge.py`) — the one genuinely new component

The judge is a **test author + runner + adjudicator**. Instead of opining over prose (high variance), it
turns the NL acceptance into a concrete, re-runnable test battery — recovering most of the determinism of
the harness's hand-written `acceptance_criteria.automated[]` while keeping plain-English input. It runs in
two phases: **synthesize once per job**, then **grade per run**.

### Phase A — synthesize (once per job, before any run)
`judge.py --synthesize` — inputs: the NL `accept` text + repo structure (file tree, detected framework,
existing test layout). One headless `claude`/`codex` call emits a grader bundle into `jobs/<slug>/grader/`:
- `criteria.json` — the acceptance decomposed into discrete, checkable criteria.
- `test_accept.py` (and/or `checks.sh`) — a **small battery of mechanical tests** that assert those
  criteria. Prompted to prefer in-process checks (e.g. FastAPI `TestClient`, direct imports, AST/grep on
  the diff) over spinning up servers, and to keep each test mapped to one criterion.
- `manifest.json` — criterion → test(s) mapping.

The battery is authored against the **task/acceptance only**, never against any agent's solution, so it is
unbiased and **identical for every rep** — that is what makes reps comparable and re-grading stable. The
human can inspect/edit `grader/` before runs (a trust feature).

### Phase A′ — floor-check (validate the battery, inherited from the experiments)
The harness validated every AC as *floor 0.0 / oracle 1.0* (fails on the bare repo, passes on the
reference solution). We borrow the cheap half: run the synthesized battery against a **pristine base
worktree** (the unmodified `pinned_sha`). The new-behavior tests should **fail** there — a test that
already passes on base isn't discriminating and is flagged/regenerated. Result logged to `grader/floor.log`
and folded into `manifest.json`. (We can't auto-check the oracle side without a reference solution, so
that half stays advisory.)

### Phase B — grade (per run)
`judge.py --grade --run <id>`:
1. Run the battery against the **post-run workspace** via the job venv; capture each test's pass/fail +
   output. Also run the repo's own pre-existing test suite for the "existing tests still pass" signal.
2. A final lightweight LLM **adjudication** pass handles only what isn't mechanically decided (e.g. "no
   unrelated files changed", subjective quality) and sanity-checks that a failing test reflects the
   solution, not a broken generated test. Most criteria are already settled by Phase B.1.
3. Emit `judge.json`:
   ```json
   {
     "verdict": "accepted | partial | rejected",
     "score": 0.0,
     "criteria": [{"criterion": "...", "met": true,
                   "source": "mechanical | llm", "evidence": "test name + output / diff hunk"}],
     "battery": {"total": 5, "passed": 4, "details": "pytest -q output"},
     "existing_tests": {"ran": true, "passed": true, "summary": "12 passed"},
     "reasoning": "..."
   }
   ```
   `score` = fraction of criteria met. JSON enforced via `--output-format json` + schema-checked retry.

### Variance control
- The battery is the primary, deterministic signal; the LLM only adjudicates the residue.
- `--judge-votes N` (default 1): re-run the adjudication N times and majority-vote the LLM-only criteria.
- Battery is persisted, so a re-grade of the same run is reproducible.

Honest caveat (README): the *synthesis* step is still an LLM and can write an imperfect test — the
floor-check + adjudication pass catch most of it; the §8 explicit-checks hatch (user supplies the battery
directly) is the answer when hard guarantees are required.

---

## 5b. The self-analysis end step (`self_analysis.sh`)

The research payoff, kept deliberately simple: after grading, the *same* runner the user chose reflects on
its own work. One extra headless invocation per run, in the still-present workspace (so the agent can
`git diff` its own changes directly), with a model-agnostic prompt:

> You just attempted this task: «task». Your changes are in the current working directory (run `git diff`
> to review them). [If graded:] An automated judge scored your solution: «judge.json summary». Honestly
> analyze your own result: what you did and why, whether you believe you met the acceptance criteria, your
> confidence (low/med/high), anything you got wrong or would do differently, and any risks a reviewer
> should check. Write it as Markdown to `agent-analysis/ANALYSIS.md`. Do not modify any other files.

Mechanics:
- Runs **after** `git.patch` is captured and **after** grading → the graded solution and saved patch are
  never contaminated by the analysis file or any reflection-time edits.
- The agent writes a **relative** path inside its workspace (sandbox-safe). Teardown copies it to the
  job-level rollup `agent-analysis/<run-id>.md` (the folder the user opens) and to the run dir as
  `analysis.md`. **Fallback:** if the agent didn't create the file, synthesize it from the agent's final
  message (claude: `.result` from `agent_stdout`; codex: last `agent_message` in the analysis transcript),
  so an analysis always exists.
- **On by default**; `--no-analyze` (or `analyze: false` in `job.yaml`) skips it. Works with or without the
  judge (Phase 3) being wired — the grade section is simply omitted when `judge.json` is absent.
- Cost note: it is a second agent call per run; surfaced in `report.md` token totals.

---

## 6. What we port vs. write new

- **Port ~verbatim:** `setup_run.sh` (minus the stripping/overlay blocks), the codex/claude invocation
  block, `teardown_run.sh` (diff + transcript capture + claude-settings backup/restore + worktree
  removal), `extract_telemetry.py`, `validate_schema.py`, `run_record.schema.json` (relax the `task_id`
  `^T[0-9]{2}$` and `env_id` enum to free strings; add `model`/`repo_url`).
- **Write new:** `wizard.py`, `brew.sh` + `detect_stack.py` (generalized `bootstrap.sh`), `judge.py`
  (`--synthesize` authors the battery + floor-check; `--grade` runs it + adjudicates — replaces the AC
  loop), `report.py`, `job.schema.json`, the new `run.sh`. `grade_run.py` is reused inside `judge.py` as
  the battery *runner* (its `run_ac` shell/pytest executor + `pass_condition` eval are exactly what runs
  the synthesized tests).
- **Drop:** `envs/` overlays + context stripping, the task×env matrix, `e5_seeds/`, the cross-arm analysis.

---

## 7. Build phases (incremental, each independently testable)

- **Phase 0 — Scaffold.** Create the tree above, `PLAN.md`, `README.md`, schemas, `job.example.yaml`.
- **Phase 1 — Brew.** `brew.sh` + `detect_stack.py`: clone any repo, pin SHA, build a Python venv.
  Verify: `run.sh --repo <small python repo> --brew-only` produces `repo.git` + `.venv` + `brew.log`.
- **Phase 2 — Run.** ✅ Port setup/run_agent/teardown (no overlays). Verified on a throwaway repo + trivial
  task with **both** codex and claude: workspace runs, `git.patch` + `transcript.jsonl` captured, worktree
  cleaned, claude settings restored byte-identical, resume-skip works.
- **Self-analysis end step.** ✅ `self_analysis.sh` wired into teardown after grading: the same agent writes
  an honest self-assessment to job-level `agent-analysis/<run-id>.md` (+ run-dir `analysis.md`); grade-
  optional (works before Phase 3); `--no-analyze` skips; patch verified pure (analysis never leaks into the
  graded diff). See §5b.
- **Phase 3 — Judge.** ✅ `judge.py --synthesize` (NL → battery in `grader/` + floor-check against base) and
  `judge.py --grade` (run battery + LLM-adjudicate → `judge.json`). Synthesis wired into `run.sh` (once per
  job, resume-safe, aborts on empty battery); grade wired into teardown. Verified: battery fails on base
  (floor-check), grade yields accepted/partial/rejected with per-criterion evidence. Helpers: `agent.py`
  (headless codex/claude), `battery.py` (AC runner, `PYTHONDONTWRITEBYTECODE` so grading leaves no `.pyc`).
  Synthesis prompt calibrated (example values illustrative; ignore byte-cache/venv in file-scope checks).
- **Phase 4 — Telemetry + report.** ✅ Ported `extract/` (adapters + core) → `telemetry.py` (run_record,
  schema-validated, both agents); `report.py` → per-run `report.md` + job-level `REPORT.md` scorecard.
- **Phase 5 — Wizard + UX.** ✅ Interactive `run.sh`, flag mode, `--reps`, `--job <existing>`, resume-skip,
  `--no-analyze`.
- **Phase 6 — Polish.** ✅ README + prerequisites; brew-failure UX; generic (non-Python) fallback path;
  `--judge-votes` (LLM-adjudication majority); cleanup of throwaway test jobs.

---

## 8. Implemented since v1

- **Multiple tasks per job + reps.** A job carries a `tasks:` list (each `{id, task, accept}`); single
  `task`/`accept` still works (becomes task `t1`). Synthesis authors a battery per task under
  `grader/<task_id>/`; the run loop is tasks × reps; each run is graded against its own task's battery.
  Authored via the wizard (loops "add another task?"), `--tasks-file <yaml>`, or single `--task/--accept`.
- **Context environments / the ladder** (`--ladder`, `lib/ladder.py` + `lib/context_gen.py`). The job carries
  an `environments` list (default `[E0]`). `--ladder` = E0 bare → E1 +README → E2 +AGENTS → E3 +full
  scaffold; each context artifact is **agent-generated for the repo once** (`context_gen.py`) and
  materialized into per-env overlays. The run loop becomes task × env × rep. `setup_run.sh` strips the
  repo's own orienting docs (so E0 is bare), drops the env overlay in, and **commits an env baseline** so the
  agent's solution diff stays pure (verified: injected README/AGENTS never appear in `git.patch`). Multi-env
  jobs default `analyze:false` (cost experiments stay lean). `env_id` flows through run_meta → run_record.
- **l1-style by-environment figures** (`lib/figures.py`, stdlib inline SVG) into `agent-analysis/`:
  **token cost by env** (box min/Q1/median/Q3/max + per-run dots, log₂ y) and **file access by env**
  (file×env access heatmap). Reproduce the l1 finding on the tiny repo (E0 84k → E2/E3 63k input tokens).
- **Packaging — `install.sh`** (git-clone + install): checks prereqs (git, python3 ≥ 3.9; warns if no
  codex/claude), ensures deps (system → `pip --user` → private `.runner-venv` fallback), and drops an
  `exp-runner` command on PATH.

## 8b. Still-open extensions

- **Explicit-checks hatch:** allow `accept.checks[]` (shell/pytest + pass_condition) alongside the NL text.
- **Head-to-head compare:** multiple models into one job → models become the grouped series in the figures.
- **Custom environments:** user-supplied overlay dirs to extend/override the built-in E0..E3 ladder.
- **Docker/devcontainer brew** for language-agnostic isolation.

---

## 9. Risks / prerequisites

- **Auth (settled):** users `codex login` / `claude login` first — a one-line prerequisite in the README.
  `run.sh` checks and **fails fast** with that message if a CLI isn't authenticated. (The harness also
  unsets `ANTHROPIC_API_KEY` so headless claude uses the subscription login, not a stray API key.)
- **Safety:** agents run with approvals bypassed against the user's repo — contained to an isolated
  worktree + per-job venv, never the user's working copy. Same model as the experiments. State it plainly.
- **Cost:** each run + judge consumes tokens; `--reps`/`--judge-votes` multiply it. Surface token totals
  in `report.md`.
- **Judge variance (largely closed):** grading is now driven by a persisted, floor-checked mechanical
  battery; the LLM only adjudicates the residue (and only that is majority-voted via `--judge-votes`). The
  remaining soft spot is *synthesis* quality — mitigated by the floor-check and the §8 hatch.
</content>
</invoke>
