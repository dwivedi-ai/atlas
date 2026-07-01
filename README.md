# environment-runner

**Does giving a coding agent more project context actually help — and what does it cost?**

`environment-runner` points a headless coding agent at **your** repo and a task, runs that task across a
ladder of **context environments** (from a bare repo up to a fully-scaffolded one), grades each run against
an auto-synthesized test battery, and charts the **token cost per environment**. It's a generalized,
repo-agnostic version of the "context ladder" experiment: give it a repo + task + acceptance description +
model, and it produces a reproducible answer to *how much context is worth it here.*

Works headless with **four agent backends** — OpenAI **Codex**, Anthropic **Claude**, Google **Gemini**, and
Google **Antigravity (`agy`)** — behind one interface.

---

## The idea: a context ladder

Each task is run in every rung of an escalating-context ladder, all built **for your repo** (the agent
writes the docs once per job), so you can measure how cost and success change as context grows:

| env | what's added |
|-----|--------------|
| **E0** | bare repo (orienting docs stripped) |
| **E1** | `+README` |
| **E2** | `+AGENTS` |
| **E3** | `+PROJECT` |
| **E4** | full scaffold (AGENTS/PROJECT/OBJECTIVES/PLAN/PROGRESS + agent-native context file + memory skeleton) |
| **E5** | `+` seeded memory (facts, constraints, preferences) |
| **E6** | DOX (per-directory context rails + `.xo/` + episodic memory) |

The runner strips the repo's own orienting docs so **E0 is genuinely bare**, then commits each environment's
context as a baseline — so the agent's solution diff never mixes in the injected docs. `--envs` selects any
subset (default: all seven).

---

## Supported agents

| model | CLI | notes |
|-------|-----|-------|
| `codex` | OpenAI Codex | runs cells in parallel |
| `claude` | Anthropic Claude Code | serial (per-run settings hook) |
| `gemini` | Google Gemini CLI | serial; default `gemini-2.5-pro` |
| `agy` | Google Antigravity | serial + per-run state isolation; one login covers Gemini + Claude + GPT-OSS models |

The agent's native context file is chosen per model (`AGENTS.md` for codex, `CLAUDE.md` for claude,
`GEMINI.md` for gemini, `.agents/AGENTS.md` for agy).

## Prerequisites

- `python3 ≥ 3.9`, `git`, and Python deps `PyYAML jsonschema matplotlib numpy`.
- **Log in to your agent CLI first** — the tool does not handle auth:
  - Codex: `codex login`
  - Claude: `claude login` (uses your subscription; a stray `ANTHROPIC_API_KEY` is ignored)
  - Gemini: authenticate the Gemini CLI; runs off your `~/.gemini` credentials
  - Antigravity: sign in once (`agy`, then complete the browser OAuth on the individual tier)

`./install.sh` checks prerequisites, installs the deps, and puts an `environment-runner` command on your PATH.
Or `pip install -r requirements.txt` and use `./run.sh` directly.

## Quickstart

```bash
# Interactive — asks the questions, then runs the whole thing:
./run.sh

# Non-interactive: run a task across all 7 environments (7 codex cells at once):
./run.sh --repo https://github.com/acme/widgets.git \
         --task-file task.txt --accept-file accept.txt --model codex --jobs 7

# Knobs:
#   --model <codex|claude|gemini|agy>   agent backend (default: codex)
#   --envs E0,E4,E6                     subset of environments (default: all 7)
#   --jobs N                            concurrent cells (codex only; others forced serial)
#   --reps N                            runs per (task × environment)
#   --tasks-file f.yaml                 multiple tasks in one job
#   --no-analyze                        skip the per-run self-analysis text
#   --brew-only                         stop after cloning + building the env
#   --job <id>                          re-run an already-authored job (resume-safe)
```

## How a run works

1. **Author** — wizard or flags → a validated `jobs/<id>/job.yaml`.
2. **Brew** — bare-clone the repo, pin the ref to a SHA, detect the stack, build a hermetic `.venv`
   (Python-first; other stacks run as-is or via an explicit `build.command`).
3. **Build the environments** — the agent reads the repo once and writes the context artifacts; each
   environment's overlay is composed under `jobs/<id>/environments/`.
4. **Synthesize the grader** — the model turns the plain-English acceptance text into a mechanical test
   battery (`grader/<task>/criteria.json`), then **floor-checks** it against the pristine base.
5. **Run the matrix** — every (task × environment × rep) cell runs in an isolated git worktree; the agent
   sees only the task plus that environment's context. Live per-cell status.
6. **Grade** — run the battery against the solution and LLM-adjudicate the subjective criteria → a verdict
   (`accepted` / `partial` / `rejected`) + score + per-criterion evidence.
7. **Self-analysis** (optional) — the same agent writes an honest reflection on its own result.
8. **Report + figures** — a job scorecard plus two PNGs: **token cost by environment** and a
   **file-access heatmap**.

The **task** is handed verbatim to the agent; the **acceptance** text goes only to the grader (never the
agent), so the battery is unbiased and identical across reps. Agents run with approvals bypassed against an
**isolated worktree + per-job venv — never your working copy**; the solution is saved as `git.patch`.

## Output

```
jobs/<job_id>/
├── job.yaml                     frozen spec (repo, pinned SHA, tasks, model, environments, reps)
├── repo.git/  .venv/  brew.log  the brew
├── environments/E0..E6/         per-env context overlays (agent-written for your repo)
├── grader/<task>/               synthesized test battery + floor-check
├── agent-analysis/
│   ├── fig_token_cost_by_env.png    token cost per environment
│   ├── fig_file_access_by_env.png   file × environment access heatmap
│   └── <run-id>.md                  per-run self-analysis (unless --no-analyze)
├── REPORT.md                    scorecard + cost-by-environment table
└── runs/<run-id>/               one per (task, env, rep): git.patch, transcript, judge.json,
                                 run_record.json, report.md
```

## Notes for researchers

- **The grader is model-authored.** Synthesis is an LLM step; the floor-check catches the common failure
  modes, but skim `grader/<task>/floor.log` and feel free to hand-edit `criteria.json` before trusting a
  verdict. Bump `--judge-votes` for subjective criteria on high-stakes jobs.
- **Token counts vary by backend.** Codex/Claude/Gemini report the provider's billed usage (cache-aware);
  Antigravity reports client-side *estimates* with no cache metric — comparable within a backend, but read
  cross-backend token numbers with that caveat.
- **Cost scales with the matrix.** A 7-environment × multi-rep job with context generation is a real token
  spend — scale `--envs` / `--reps` deliberately.

## License

See `LICENSE` if present; otherwise all rights reserved by the repository owner.
