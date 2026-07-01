# HANDOFF — exp-runner

**You are picking up `exp-runner`. READ THIS ENTIRE FILE FIRST, then read every file it
points you to, BEFORE you touch anything.** This is not optional. The project is small but
every piece is load-bearing, and several behaviors are subtle (command-substitution I/O,
git-baseline diffing, codex-parallel/claude-serial, headless auth). If you change code
without reading the file that owns that behavior, you will break it.

---

## 0. Mandatory reading order (do all of it)

1. **This file (`HANDOFF.md`)** — top to bottom.
2. **`README.md`** — what it is + quickstart + the pipeline + limitations.
3. **`PLAN.md`** — the full design, every decision and why, and the build history (§8 = what's
   implemented, §8b = still-open). This is the design source of truth.
4. **`templates/job.example.yaml`** + **`schemas/job.schema.json`** — the job spec shape.
5. **The code, in this order** (each file has a docstring header — read it):
   - `run.sh` (entry point / orchestration)
   - `lib/jobspec.py` (the job model + accessors every shell script calls)
   - `lib/ladder.py` (the 7 environments E0..E6 + what each contains)
   - `lib/context_gen.py` (how environments are set up for a repo)
   - `lib/brew.sh`, `lib/detect_stack.py` (the "brew" step)
   - `lib/setup_run.sh`, `lib/run_agent.sh`, `lib/teardown_run.sh`, `lib/self_analysis.sh`
   - `lib/run_job.sh` (the task×env×rep matrix: codex-parallel / claude-serial + status)
   - `lib/judge.py`, `lib/battery.py`, `lib/agent.py` (grading)
   - `lib/telemetry.py` + `lib/extract/` (transcript → run_record.json)
   - `lib/report.py`, `lib/figures.py` (reports + the l1-doxed PNG figures)
   - `lib/wizard.py`, `install.sh`
6. **The origin experiment it mirrors:** `/home/coder/experiments/l1-doxed/` — its `CLAUDE.md`,
   `analysis/scripts/gen_figures.py` (the figure we clone), and `harness/envs/E0..E6/` (the
   context ladder we generalize). The figures and the 7-env ladder come straight from here.

Do not skip step 6 — the figures and environment semantics only make sense against l1-doxed.

---

## 1. What exp-runner is (one paragraph)

A generalized coding-agent runner. A person gives it **a repo, a task, a plain-English
acceptance description, and a model (codex|claude)**. It brews a hermetic environment, **sets
up the E0..E6 context ladder for that repo** (E0 bare → E6 DOX; the agent writes the context
docs), **runs the task in every environment** (codex cells in parallel, claude serial, with
live status), **grades each run** against a synthesized mechanical test battery, and emits the
**l1-doxed figures** (token-cost-by-environment bar chart + file-access heatmap, PNG, white
background). It is a generalization of the `experiments/l1-doxed` harness so anyone can run the
"does context help / what does it cost?" experiment on their own repo and task.

---

## 2. The end-to-end flow (what one `./run.sh` does)

1. **Author** → a validated `jobs/<job_id>/job.yaml` (wizard, flags, or `--job <existing>`).
2. **Brew** (`lib/brew.sh`) → bare-clone the repo, resolve `ref`→pinned SHA, detect the stack,
   build a hermetic `.venv` (Python-first; other stacks run as-is / via `build.command`).
3. **Set up environments** (`lib/context_gen.py`) → the agent reads the repo once and writes the
   content artifacts (README, AGENTS, PROJECT, OBJECTIVES, PLAN, PROGRESS, a memory fact file, a
   DOX child AGENTS); structural files (CLAUDE.md, `.xo/*`, memory skeleton) are templated; each
   environment's overlay is composed under `jobs/<id>/environments/<env>/`. Runs ONCE per job.
4. **Synthesize the grader** (`lib/judge.py --synthesize`, per task) → the agent turns the NL
   acceptance into a mechanical battery `grader/<task_id>/criteria.json`, then **floor-checks** it
   against the pristine base (new-behavior criteria must fail on base; invariants must pass).
5. **Run the matrix** (`lib/run_job.sh`) → for every (task, env, rep) cell: `setup_run.sh` →
   `run_agent.sh` → `teardown_run.sh`. **codex runs cells concurrently (`--jobs N`, sliding
   window); claude is forced serial** (it installs a global `~/.claude/settings.json` hook per
   run that races under parallelism). Live status prints per cell.
6. **Report + figures** → job-level `REPORT.md` (cost-by-env table) + two PNGs in
   `agent-analysis/` (`lib/figures.py`).

Per-cell teardown: capture `git.patch` + transcript → **grade** (run the battery + LLM-adjudicate
the `llm` criteria) → **self-analysis** (ON by default; `--no-analyze` skips it; runs post-hoc from
`git.patch` if the workspace is already gone) → **telemetry** (`run_record.json`) → per-run `report.md`
→ restore claude settings → remove worktree.

---

## 3. The 7 environments (from l1-doxed)

`lib/ladder.py` owns this. Default for every job is **all 7**.

| env | label | overlay (composed for the repo) |
|-----|-------|---------------------------------|
| E0  | bare | nothing — repo as-is (orienting docs stripped in multi-env mode) |
| E1  | +README | `README.md` |
| E2  | +AGENTS | `AGENTS.md` |
| E3  | +PROJECT | `AGENTS.md` + `PROJECT.md` |
| E4  | full-XO | AGENTS/PROJECT/OBJECTIVES/PLAN/PROGRESS + CLAUDE.md + memory/ skeleton |
| E5  | +memory | E4 + seeded `memory/` (facts, constraints, preferences, episodic) |
| E6  | DOX | root AGENTS rail + CLAUDE.md + child `<dir>/AGENTS.md` + `.xo/` + episodic memory |

**Diff purity:** in multi-env mode `setup_run.sh` strips the repo's own orienting docs so E0 is
truly bare, drops the env overlay in, and **commits it as the run's baseline** — so the agent's
solution `git.patch` is taken relative to the env context and never contains the injected docs.
(Verified: injected README/AGENTS never appear in `git.patch`.)

---

## 4. Job folder layout

```
jobs/<job_id>/
├── job.yaml                  frozen spec (repo, pinned SHA, tasks, model, environments, reps)
├── repo.git/  .venv/  brew.log  .brew_done         the brew
├── environments/
│   ├── _artifacts/…          agent-generated content docs (once per job)
│   └── E0/ … E6/             composed per-env overlays
├── grader/<task_id>/         criteria.json + floor.log + manifest.json  (per task)
├── agent-analysis/
│   ├── fig_token_cost_by_env.png     ← the l1-doxed headline figure (PNG, white bg)
│   ├── fig_file_access_by_env.png    ← file×env access heatmap (PNG)
│   └── <run-id>.md           per-run self-analysis text (unless --no-analyze)
├── REPORT.md                 job scorecard (cost by environment, runs table)
└── runs/<run-id>/            one per (task, env, rep); run-id = <job>-<task>-<env>-<agent>-rNNN
    ├── run_meta.json  transcript.jsonl  git.patch  judge.json
    ├── run_record.json  event_log.jsonl  report.md  analysis.md
    └── (workspace/ is removed after grading; the patch is kept)
```

`jobs/*` is **gitignored** (generated output). Only `jobs/.gitkeep` is tracked.

---

## 5. How to run it

**Prerequisite (not handled by the tool): log in to your agent CLI first.**
```
codex login          # or:  claude login   (exp-runner ignores any ANTHROPIC_API_KEY)
```

```
# interactive wizard (asks the questions; runs the whole thing):
./run.sh                       # or, if installed: exp-runner

# non-interactive (a normal user / CI):
./run.sh --repo <url> --task-file t.txt --accept-file a.txt --model codex --jobs 7

# knobs:
--envs E0,E4,E6      # subset of environments (default: all 7 E0..E6)
--jobs N             # concurrent codex cells (default 4; claude is forced to 1)
--reps N             # runs per (task × env)  (default 1)
--tasks-file f.yaml  # multiple tasks in one job (list of {id?, task, accept})
--no-analyze         # skip the per-run self-analysis text (ON by default)
--brew-only          # stop after brew
--job <id>           # re-run an already-authored job (resume-safe)
```

**Install for a teammate:** `git clone <url> && cd exp-runner && ./install.sh` — checks prereqs
(git, python3≥3.9, warns if no codex/claude), installs deps, puts `exp-runner` on PATH.

---

## 6. GOTCHAS (read before editing — these have all bitten already)

- **The interactive wizard writes prompts to STDERR, and prints ONLY the job dir to STDOUT.**
  `run.sh` reads the wizard via `JOB_DIR="$(python3 lib/wizard.py)"` — command substitution
  captures stdout. If you make the wizard print a prompt to stdout, it gets swallowed and the
  wizard looks hung. (This was a real bug; fixed. Keep all `_p()`/`ask*` output on stderr.)
- **Auth is a hard prerequisite.** Headless codex/claude must be logged in. The harness unsets
  `ANTHROPIC_API_KEY` so claude uses the subscription login, not a stray key.
- **codex = parallel, claude = serial.** `run_job.sh` forces `JOBS=1` for claude (global settings
  hook races under concurrency). Do not "fix" this.
- **Figures need `matplotlib` + `numpy`.** They are in `requirements.txt` and `install.sh` checks
  for them. `figures.py` is a straight port of `l1-doxed/analysis/scripts/gen_figures.py` fig1
  (normalized bar, E6 red `#c44e52`, others blue `#4c72b0`, dashed E0 line, PNG white bg).
- **Env baseline commit** (setup_run.sh, multi-env only): the solution diff is relative to the
  committed env context. Don't capture `git diff` against the original SHA in multi-env mode.
- **Grader battery eval** (`battery.py`) runs `pass_condition` with a curated safe builtin set and
  `PYTHONDONTWRITEBYTECODE=1` (so grading never litters `__pycache__` that a "no unrelated files"
  criterion would flag). Paths are resolved absolute (relative venv/bin on PATH breaks pytest).
- **Floor-check** validates the synthesized battery on a pristine base worktree with the same
  `/venv` git-exclude the run workspace uses — else file-scope checks false-mismatch.
- **Codex nav extraction** sometimes captures shell-command fragments as "files"; `figures.py`
  filters non-path entries (`_is_path`). If a heatmap row looks like code, extend that filter.
- **Resume-safe:** a cell with `.run_done` is skipped; brew/context/grader skip if already present.
  To force a clean re-run, delete the relevant `jobs/<id>/runs/*` (or the whole job).
- **Passing env vars into a script from an expansion does NOT work** (`${X:+FOO=$X} bash …` runs
  `FOO=..` as a command). Use `export FOO=..; bash …`. (Real bug that was fixed in run.sh --jobs.)
- **The `[k/N]` status counter counts FINISHED cells, not started ones** (`run_job.sh` `_status_line`
  touches a `$DONE_DIR` marker then counts it). Don't revert it to counting `$STATUS_DIR` — under
  parallelism every cell writes a pessimistic status file up front, so that would print `[N/N]` repeatedly.
- **Self-analysis is ON by default for every job** and works POST-HOC: if the workspace is already gone it
  runs in a temp dir with the saved `git.patch` embedded in the prompt. To backfill a finished job, call
  `lib/self_analysis.sh` per run with `JOB_DIR/RUN_ID/AGENT_ID/TASK_PROMPT` set.
- **Figures are PNG** (`fig_token_cost_by_env.png`, `fig_file_access_by_env.png`) — `report.py` embeds
  `agent-analysis/fig_*.png` and the run.sh footer names them. Don't reintroduce the old `.svg` names.

---

## 7. Current state (as of this handoff)

- **Complete and committed.** `exp-runner/` is a clean git repo (initial commit made, **not
  pushed** — add a remote and push to distribute). 34 source files tracked; no job output / venv
  / bytecode committed. Working tree clean.
- **Validated end-to-end** on `github.com/dwivedi-ai/TicTacToe-AI` (task: reduce the end-of-game
  delay), codex, 7 environments × 1 rep, `--jobs 7`: **7/7 accepted**, both PNG figures generated
  and confirmed to match l1-doxed's look. The run reproduced the l1-doxed thesis on that repo:
  **E4 full-XO scaffold most expensive (1.42×)**, **E6 DOX cheap (0.69×)**.
- **All Python compiles, all bash syntax clean, deps importable, `exp-runner --help` works.**

---

## 8. Open items / where to go next

- **Naming:** the project is still called `exp-runner`. Candidates proposed: **Crucible** (brand),
  **Strata** (descriptive — layers of context), Ladder, Primer, Onboard. Renaming touches the dir,
  the `exp-runner` command in `install.sh`, and README/PLAN headers.
- **Figures with distributions:** at 1 rep the token bars have no error bars. With `--reps ≥3`,
  `figures.py` already draws bootstrapped CIs — add the l1-doxed significance brackets (KW/Dunn)
  only if you also add `scipy`/`scikit-posthocs` (currently intentionally omitted).
- **Still-open extensions (PLAN §8b):** explicit-checks acceptance hatch; multi-model head-to-head
  (models become the grouped series in the figures); user-supplied custom env overlays;
  Docker/devcontainer brew.
- **Cost awareness:** default is 7 envs × reps × context-generation (~8 agent calls) per job. A
  multi-task, multi-rep, 7-env job is a real token spend — surface/scale it deliberately.

---

## 9. One-paragraph orientation

exp-runner turns the l1-doxed context experiment into a tool anyone can point at their own repo:
give it a repo + task + acceptance + model, it brews a hermetic env, has the agent write the E0..E6
context ladder for that repo, runs the task in all 7 environments (codex parallel / claude serial,
live status), grades each run with a synthesized+floor-checked test battery, and prints the
l1-doxed PNG figures (token cost by environment + file-access heatmap). Everything lands under
`jobs/<id>/`. It is complete, committed, and validated; read every file above before changing it.
