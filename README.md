# exp-runner

A generalized coding-agent runner. Give it **your repo**, a **task**, a plain-English description of **what
an accepted solution looks like**, and a **model** (`codex` or `claude`). It brews a hermetic environment,
**sets up the E0..E6 context ladder for your repo** (bare → fully scaffolded — the agent writes the context
docs), **runs the task in every environment** (codex cells in parallel, claude serial, with live status),
grades each run against an LLM-synthesized test battery, and emits the **l1-doxed figures**
(token-cost-by-environment bar chart + file-access heatmap, PNG). Everything lands in one job folder.

It generalizes the `experiments/l1-doxed` harness so anyone can run the "does context help / what does it
cost?" experiment on their own repo and task. See **`PLAN.md`** for the design and **`HANDOFF.md`** if you're
picking the project up.

## Prerequisites

- **Log in to your agent CLI first** — this tool does not handle auth:
  - Codex: `codex login`
  - Claude: `claude login` (uses your subscription; exp-runner deliberately ignores any `ANTHROPIC_API_KEY`)
- `python3 ≥ 3.9`, `git`, and deps `PyYAML jsonschema matplotlib numpy` — `./install.sh` sets all this up and
  puts an `exp-runner` command on your PATH (or `pip install -r requirements.txt` and use `./run.sh`).

## Quickstart

```bash
# Interactive — asks the questions, then runs the whole thing:
./run.sh                       # or, if installed: exp-runner

# Non-interactive: run the task across all 7 environments, 7 codex cells at once:
./run.sh --repo https://github.com/acme/widgets.git \
         --task-file task.txt --accept-file accept.txt --model codex --jobs 7

# Knobs:
#   --envs E0,E4,E6   subset of environments (default: all 7, E0..E6)
#   --jobs N          concurrent codex cells (default 4; claude forced to 1)
#   --reps N          runs per (task × env) (default 1)
#   --no-analyze      skip the per-run self-analysis text (keeps a big matrix lean)
#   --brew-only       stop after cloning + building the env
#   --job <id>        re-run an already-authored job (resume-safe)
```

Everything lands in `jobs/<job_id>/`:

```
jobs/<job_id>/
├── job.yaml                  frozen spec (repo, pinned SHA, tasks, model, environments, reps)
├── repo.git/  .venv/  brew.log       the brew
├── environments/E0..E6/      per-env context overlays (agent-written for your repo)
├── grader/<task_id>/         synthesized test battery + floor-check
├── agent-analysis/
│   ├── fig_token_cost_by_env.png     token cost per environment (l1-doxed bar chart)
│   ├── fig_file_access_by_env.png    file × environment access heatmap
│   └── <run-id>.md           per-run agent self-analysis (text)
├── REPORT.md                 scorecard + cost-by-environment table
└── runs/<run-id>/            one per (task, env, rep): git.patch, transcript, judge.json,
                              run_record.json, report.md, analysis.md
```

## Status — complete

The full pipeline works end-to-end for **both** codex and claude, verified on real runs (clean
`accepted` / `partial` verdicts, schema-valid telemetry, accurate floor-checks):

- **Brew** — clone + pin SHA + detect stack + build hermetic venv (`requirements.txt` / `pyproject`;
  Node + other stacks fall back to as-is / `build.command`).
- **Run** — codex/claude headless in an isolated worktree; captures `git.patch` + transcript; resume-safe.
- **Context environments (default: all 7)** — every job runs the task across the l1-doxed context ladder:
  **E0 bare → E1 +README → E2 +AGENTS → E3 +PROJECT → E4 full-XO → E5 +memory → E6 DOX**. The runner sets
  each environment up *for your repo* (the agent writes README/AGENTS/PROJECT/scaffold/DOX once per job).
  E0 strips the repo's own orienting docs and each environment commits its context as a baseline, so the
  agent's solution diff never mixes in the injected docs. Runs = task × environment × rep. `--envs` overrides.
- **codex parallel / claude serial + live status** — codex cells run concurrently (`--jobs N`, sliding
  window); claude is forced serial (global settings-hook race). Status prints per cell `[k/N] <run> verdict`.
- **Judge** — synthesizes a mechanical battery from the NL acceptance, floor-checks it against the pristine
  base, runs it against the solution, LLM-adjudicates subjective criteria → `judge.json`.
- **Self-analysis (on by default)** — after grading, the same agent writes an honest self-assessment (text)
  to `agent-analysis/<run-id>.md`. `--no-analyze` skips it (one extra agent call per cell).
- **Figures** — two PNGs (matplotlib, white background — a direct port of the l1-doxed figures) into
  `agent-analysis/`: **`fig_token_cost_by_env.png`** (per-task-normalized input tokens by environment,
  E0=1.00, E6/DOX red) and **`fig_file_access_by_env.png`** (file × environment access heatmap).
- **Telemetry + report** — schema-valid `run_record.json`, per-run `report.md`, job `REPORT.md` scorecard.
- **Multiple tasks & reps** — a job can also carry a `tasks:` list (each graded by its own battery) and run
  each `--reps N` times; reps give the token bars bootstrapped error bars and the heatmap its per-run means.

## How a job is specified (`job.yaml`)

See `templates/job.example.yaml`. The **task** is handed verbatim to the agent; the **accept** text is
handed only to the judge (never the agent), which turns it into a mechanical test battery, floor-checks it,
runs it against the solution, then adjudicates. Python-first stack detection; other stacks run as-is or via
an explicit `build.command`.

After grading, the **same agent** does a reflection pass and writes an honest self-analysis of its own
result into the job-level `agent-analysis/` folder — one Markdown file per run, so a researcher can open one
folder and read every agent's self-assessment side by side. On by default; `--no-analyze` skips it.

## The pipeline (what one `./run.sh` does)

1. **Author** — wizard / flags → a validated `jobs/<id>/job.yaml`.
2. **Brew** — bare-clone the repo, pin the ref to a SHA, detect the stack, build a hermetic `.venv`.
3. **Set up environments** (once per job) — the agent reads the repo and writes the context artifacts
   (README, AGENTS, PROJECT, OBJECTIVES, PLAN, PROGRESS, a memory fact file, a DOX child AGENTS); structural
   files (CLAUDE.md, `.xo/*`, memory skeleton) are templated; each env's overlay is composed under
   `environments/<env>/`.
4. **Synthesize the grader** (per task) — the model reads only the acceptance text + repo layout and writes
   `grader/<task_id>/criteria.json`. Then **floor-check** against the pristine base — new-behavior criteria
   must *fail* there, invariants must *pass*; result in `grader/<task_id>/floor.log` (`manifest.json` →
   `floor_ok`).
5. **Run the matrix** — every (task × env × rep) cell: the agent solves the task in an isolated worktree
   (it sees only the task, plus that env's context). codex cells run in parallel (`--jobs`), claude serial;
   live status per cell.
6. **Grade** — run the battery against the solution; LLM-adjudicate the `llm` criteria (`--judge-votes` for
   majority); write `judge.json` (verdict ∈ accepted/partial/rejected + score + per-criterion evidence).
7. **Self-analysis** (unless `--no-analyze`) — the same agent reflects on its own result →
   `agent-analysis/<run-id>.md`.
8. **Telemetry + report + figures** — `run_record.json` (tokens/nav), per-run `report.md`, job `REPORT.md`,
   and the two PNG figures in `agent-analysis/`.

## Limitations & notes (for researchers)

- **The grader is model-authored.** Synthesis is an LLM step and can write an imperfect criterion. The
  floor-check catches the common failure modes (a "new behavior" test that already passes on base, or an
  invariant that fails on base) — always skim `grader/floor.log` / `floor_ok` before trusting a verdict,
  and feel free to hand-edit `grader/criteria.json` (then re-grade). Bump `judge_votes` for the subjective
  criteria on high-stakes jobs.
- **The judge uses the same model you chose** for the job (one logged-in CLI). Synthesis and grading are
  separate invocations from the task run, and the grader is authored from the acceptance text *only* — it
  never sees any solution, so it is unbiased and identical across reps.
- **Agents run with approvals bypassed** against an isolated worktree + per-job venv — never your working
  copy. The solution is saved as `git.patch` (apply with `git apply`), and the worktree is removed after.
