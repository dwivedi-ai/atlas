# exp-runner

A generalized, single-repo coding-agent runner. Give it **your repo**, a **task**, a plain-English
description of **what an accepted solution looks like**, and a **model** (`codex` or `claude`). It brews an
isolated environment, runs the agent against the task in an isolated git worktree, has an LLM judge grade
the result, and writes everything into one organized job folder.

It's a generalization of the research harness in `../experiments/` — same hermetic, worktree-per-run,
capture-everything machinery, but the repo/task/acceptance are *your* inputs instead of a fixed catalog.

See **`PLAN.md`** for the full design and build phases.

## Prerequisites

- **Log in to your agent CLI first** — this tool does not handle auth:
  - Codex: `codex login`
  - Claude: `claude login` (uses your subscription; exp-runner deliberately ignores any `ANTHROPIC_API_KEY`)
- `python3` with `PyYAML` + `jsonschema` (`pip install -r requirements.txt`), `git`.

## Quickstart

```bash
# Interactive — asks the four questions:
./run.sh

# Or non-interactively:
./run.sh --repo https://github.com/acme/widgets.git \
         --task-file task.txt --accept-file accept.txt --model codex

# Just brew (clone + pin SHA + build env), don't run the agent yet:
./run.sh --repo https://github.com/acme/widgets.git --brew-only

# Compare the task across the context ladder (E0..E3), 3 runs per environment,
# producing the by-environment token-cost + file-access figures:
./run.sh --repo https://github.com/acme/widgets.git \
         --task-file task.txt --accept-file accept.txt \
         --model codex --ladder --reps 3
```

Everything lands in `jobs/<job_id>/`:

```
jobs/<job_id>/
├── job.yaml      # the frozen spec (repo, pinned SHA, task, acceptance, model)
├── repo.git/     # self-contained bare clone (source of every run's worktree)
├── .venv/        # hermetic env built from the repo
├── brew.log
└── runs/<run-id>/   # one per (model, rep): workspace, git.patch, transcript,
                     # judge.json, run_record.json, report.md   (Phases 2-4)
```

## Status — complete

The full pipeline works end-to-end for **both** codex and claude, verified on real runs (clean
`accepted` / `partial` verdicts, schema-valid telemetry, accurate floor-checks):

- **Brew** — clone + pin SHA + detect stack + build hermetic venv (`requirements.txt` / `pyproject`;
  Node + other stacks fall back to as-is / `build.command`).
- **Run** — codex/claude headless in an isolated worktree; captures `git.patch` + transcript; resume-safe.
- **Judge** — synthesizes a mechanical battery from the NL acceptance, floor-checks it against the pristine
  base, runs it against the solution, LLM-adjudicates subjective criteria → `judge.json`.
- **Self-analysis** — the same agent writes an honest self-assessment to `agent-analysis/<run-id>.md`.
- **Telemetry + report** — schema-valid `run_record.json`, per-run `report.md`, job `REPORT.md` scorecard.
- **Context environments (`--ladder`)** — compare the task across levels of project context:
  **E0 bare → E1 +README → E2 +AGENTS → E3 +full scaffold**, where each context file is *agent-written for
  your repo* (once per job). Runs become task × environment × rep. To keep the comparison clean, E0 strips
  the repo's own orienting docs and each environment commits its context as a baseline, so the agent's
  solution diff never mixes in the injected docs.
- **Figures** — two portable SVGs auto-generated into `agent-analysis/` (stdlib-only inline SVG, no charting
  deps): **`fig_token_cost_by_env.svg`** (box min/Q1/median/Q3/max + per-run dots, log₂ tokens, x = env) and
  **`fig_file_access_by_env.svg`** (file × environment access heatmap) — the l1 headline figures.
- **Multiple tasks & reps** — a job can also carry a `tasks:` list (each graded by its own battery) and run
  each `--reps N` times; reps give the box-plot distributions and the heatmap its per-run means.

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
3. **Synthesize the grader** (once per job) — the chosen model reads only the acceptance text + repo
   layout and writes `grader/criteria.json`: a small battery of concrete, checkable criteria (mechanical
   shell checks + a few `llm` ones for subjective things). Then **floor-check**: run the battery against
   the pristine base — "new behavior" criteria must *fail* there, invariants must *pass*; mismatches are
   logged to `grader/floor.log` (`manifest.json` carries `floor_ok`).
4. **Run** (per rep) — the agent solves the task in an isolated git worktree (it sees only the task).
5. **Grade** — run the battery against the solution; LLM-adjudicate the `llm` criteria (`--judge-votes`
   for majority); write `judge.json` (verdict ∈ accepted/partial/rejected + score + per-criterion evidence).
6. **Self-analysis** — the same agent reflects on its own result → `agent-analysis/<run-id>.md`.
7. **Telemetry + report** — `run_record.json` (tokens/timing/navigation), per-run `report.md`, job `REPORT.md`.

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
