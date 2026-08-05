# environment-runner

**Does giving a coding agent more project context actually help — and what does it cost?**

`environment-runner` points a headless coding agent at **your** repo and a task, runs that task across a
ladder of **context environments** (from a bare repo up to a fully-scaffolded one), grades each run against
an auto-synthesized test battery, and charts the **token cost per environment**. It's a generalized,
repo-agnostic version of the "context ladder" experiment: give it a repo + task + acceptance description +
model, and it produces a reproducible answer to *how much context is worth it here.*

Works headless with **four agent backends** — OpenAI **Codex**, Anthropic **Claude**, Google **Gemini**, and
Google **Antigravity (`agy`)** — behind one interface.

> The code, the CLI banner and the installed command all call this `exp-runner`; the dashboard's
> top-level view is called the **Atlas**. Same tool, three names.

---

## The idea: a context ladder

Each task is run in every rung of an escalating-context ladder, all built **for your repo** (the agent
writes the docs once per job), so you can measure how cost and success change as context grows:

| env | what's added |
|-----|--------------|
| **E0** | bare repo (orienting docs stripped) |
| **E1** | `+README` |
| **E2** | `+AGENTS` |
| **E3** | `+AGENTS+PROJECT` |
| **E4** | full scaffold (AGENTS/PROJECT/OBJECTIVES/PLAN/PROGRESS + agent-native context file + memory skeleton) |
| **E5** | `+` seeded memory (facts, constraints, preferences) |
| **E6** | DOX (root AGENTS rail + per-directory child AGENTS + `.xo/` + episodic memory) |

The runner strips the repo's own orienting docs so **E0 is genuinely bare**, then commits each environment's
context as a baseline — so the agent's solution diff never mixes in the injected docs. `--envs` selects any
subset (default: all seven).

> **The ladder only engages when a job has 2+ environments.** With a single environment the run is
> deliberately a plain "repo as-is" run: nothing is stripped and no overlay is applied — which means
> `--envs E4` on its own runs the *bare* repo, not the E4 scaffold. Use at least two rungs
> (`--envs E0,E4`) whenever you want context injected.

---

## Supported agents

| model | CLI | notes |
|-------|-----|-------|
| `codex` | OpenAI Codex | runs cells in parallel |
| `claude` | Anthropic Claude Code | serial (per-run settings hook) |
| `gemini` | Google Gemini CLI | serial; default `gemini-2.5-pro` |
| `agy` | Google Antigravity | serial + per-run state isolation; one login covers Gemini + Claude + GPT-OSS models |

The agent's native context file is chosen per model (`AGENTS.md` for codex, `CLAUDE.md` for claude,
`GEMINI.md` for gemini, `.agents/AGENTS.md` for agy). Model variants are selectable directly:
`gemini-2.5-flash`, `agy-pro-high`, `agy-sonnet`, `agy-opus`, … (see `schemas/job.schema.json` for the full list).

## Prerequisites

- `python3 ≥ 3.9`, `git`, and Python deps `PyYAML jsonschema matplotlib numpy`.
- **Log in to your agent CLI first** — the tool does not handle auth:
  - Codex: `codex login`
  - Claude: `claude login` (uses your subscription; a stray `ANTHROPIC_API_KEY` is ignored)
  - Gemini: authenticate the Gemini CLI; runs off your `~/.gemini` credentials
  - Antigravity: sign in once (`agy`, then complete the browser OAuth on the individual tier)

`./install.sh` checks prerequisites, installs the deps, and puts an `exp-runner` command on your PATH
(a thin wrapper around `run.sh`; override the location with `BINDIR=…`). Or `pip install -r requirements.txt`
and use `./run.sh` directly.

## Quickstart

```bash
# Interactive — asks the questions, then runs the whole thing:
./run.sh

# Non-interactive: run a task across all 7 environments (7 codex cells at once):
./run.sh --repo https://github.com/acme/widgets.git \
         --task-file task.txt --accept-file accept.txt --model codex --jobs 7
```

**Knobs**

| flag | meaning |
|------|---------|
| `--repo <url>` / `--path <dir>` | repository source (one required, unless `--job`) |
| `--ref <ref>` | branch / tag / SHA; resolved to a SHA and pinned into `job.yaml` (default `HEAD`) |
| `--task <text>` / `--task-file <f>` | the task handed verbatim to the agent |
| `--accept <text>` / `--accept-file <f>` | the acceptance handed only to the grader |
| `--tasks-file <f.yaml>` | YAML list of `{id?, task, accept}` for a multi-task job |
| `--model <codex\|claude\|gemini\|agy>` | agent backend (default: `codex`) |
| `--envs E0,E4,E6` | subset of environments (default: all 7) |
| `--jobs N` | concurrent cells (codex only; the others are forced serial) |
| `--reps N` | runs per (task × environment) |
| `--max-seconds N` | per-run timeout; `0` = none (default). A timed-out run is graded `timeout` |
| `--no-analyze` | skip the per-run self-analysis pass (one extra agent call per cell) |
| `--build-stack <auto\|python\|node\|none>` / `--build-cmd <cmd>` | override stack detection / add a build step |
| `--brew-only` | stop after cloning + building the env |
| `--rebuild-venv` | force-rebuild the job's `.venv` during brew |
| `--job-id <id>` / `--job <id>` | name a new job folder / re-run an existing one |

Everything else lives in `job.yaml` — notably `judge_votes` (majority-vote the subjective criteria),
which has no CLI flag. Edit the file, or:

```bash
python3 lib/jobspec.py set jobs/<id> judge_votes 3
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
7. **Self-analysis** (on by default) — the same agent writes an honest reflection on its own result.
8. **Report + figures** — a job scorecard plus two PNGs: **token cost by environment** and a
   **file-access heatmap**.

The **task** is handed verbatim to the agent; the **acceptance** text goes only to the grader (never the
agent), so the battery is unbiased and identical across reps. Agents run with approvals bypassed against an
**isolated worktree + per-job venv — never your working copy**; the solution is saved as `git.patch`.

The whole pipeline is **resume-safe**: brewing, context artifacts, grader synthesis and finished cells are
all sentinel-guarded, so `./run.sh --job <id>` after an interruption continues instead of restarting.

## Output

```
jobs/<job_id>/
├── job.yaml                     frozen spec (repo, pinned SHA, tasks, model, environments, reps)
├── repo.git/  .venv/  brew.log  the brew
├── environments/E0..E6/         per-env context overlays (agent-written for your repo)
├── grader/<task>/               synthesized test battery + floor-check log
├── agent-analysis/
│   ├── fig_token_cost_by_env.png    token cost per environment
│   ├── fig_file_access_by_env.png   file × environment access heatmap
│   └── <run-id>.md                  per-run self-analysis (unless --no-analyze)
├── REPORT.md                    scorecard + cost-by-environment table
└── runs/<run-id>/               one per (task, env, rep): git.patch, transcript.jsonl, judge.json,
                                 run_record.json, event_log.jsonl, report.md
```

Run workspaces are deleted after grading; `git.patch` is what survives (`git apply git.patch` to replay a
solution). `run_record.json` is the canonical telemetry record, validated against
`schemas/run_record.schema.json`.

## The second experiment: Workspace Uptake & Retention

`--experiment wur` runs a different instrument on the same machinery. Instead of asking *does richer
context help*, it plants a single **counter-prior fact** in the workspace and measures whether that
fact crosses four boundaries: **available → read → used → retained**. Arms vary only *where* the fact
sits and *in what format*, and a probe interrupts every 1–3 tool calls to ask the agent which facts
are currently active.

Start from `templates/job.wur.example.yaml`. **Ladder mode is unchanged** — everything above this
section works exactly as before.

The design notes, data contract and measured CLI evidence live in `docs/` and are deliberately
**not published** (see `.gitignore`); the code, schemas, fixture and task packs here are
self-describing. `schemas/*.schema.json` is the authoritative field-level documentation for every
artifact a run writes.

## Visualize the results

Once you have one or more jobs under `jobs/`, launch the built-in dashboard:

```bash
./visualize.sh                # serves http://127.0.0.1:8000
./visualize.sh --port 8080    # custom port
```

It's a **zero-dependency, read-only** web app (`viz/server.py`, Python's stdlib
`http.server`) that reflects the telemetry each run already wrote — nothing is
generated or mutated. (`numpy`, if installed, adds bootstrap confidence intervals;
without it the bars just show point estimates.) Two levels:

- **Atlas** — every job as a card: accept-rate, token cost, and a mini
  cost-by-environment sparkline.
- **Job view** — the `task × env × rep` **matrix** (click a run for its detail
  drawer), the interactive **cost-by-environment** ladder, a **cost-vs-quality**
  scatter, **phase-activity-by-environment**, and cost/efficiency tables. The run
  drawer shows vitals, **phase activity** (tool-call share) and the token split,
  the token curve, graded criteria, files changed, operations, and the derived
  metrics (`efficiency_ratio`, `nav_efficiency`, …).

> **The dashboard has NOT been updated for `wur` mode.** It still reads the ladder's
> `run_record` shape and labels environments `E0..E6`. A WUR job's arms will render
> alphabetically with the wrong baseline and no uptake funnel. `viz/server.py` and
> `viz/static/index.html` are listed as MODIFY in the migration map and that work is
> not done — use `analysis/uptake.ipynb` for WUR results until it is.

**Backend fidelity.** Some signals are only real for backends that report
per-message tokens/timestamps. The dashboard is honest about this: the
token-level phase split, cumulative token curve, and timing render **only** for
backends that produce them (Claude; partially Gemini), while the **phase-activity**
view (tool-call share) and the cost/efficiency metrics work for **every** backend.

## Notes for researchers

- **The grader is model-authored.** Synthesis is an LLM step; the floor-check catches the common failure
  modes but is **advisory only** — a `floor_ok: false` manifest does not stop the job. Skim
  `grader/<task>/floor.log` and feel free to hand-edit `criteria.json` before trusting a verdict. Raise
  `judge_votes` for subjective criteria on high-stakes jobs.
- **Token counts vary by backend.** Codex/Claude/Gemini report the provider's billed usage (cache-aware);
  Antigravity reports client-side *estimates* with no cache metric — comparable within a backend, but read
  cross-backend token numbers with that caveat.
- **Cost scales with the matrix.** A 7-environment × multi-rep job also pays for context generation
  (~7 agent calls), grader synthesis, and — unless `--no-analyze` — one reflection call per cell. Scale
  `--envs` / `--reps` deliberately.
- **E6 replaces the repo's `.gitignore`** with a one-line `.xo/` ignore, by construction of the DOX overlay.
  If your repo relies on `.gitignore` to keep build output untracked, expect that noise in the E6 patch.
- **Claude runs touch `~/.claude/settings.json`** to install logging hooks, backing it up per run and
  restoring it in teardown. If you had no `settings.json` at all, there is nothing to restore and the hook
  block stays behind — delete it manually if you don't want it.

## License

See `LICENSE` if present; otherwise all rights reserved by the repository owner.
