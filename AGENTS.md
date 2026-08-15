# AI Agent Operating Contract — `exp-runner`

Welcome, AI Agent. This document acts as your system guide, operating contract, and repository map for working within the [exp-runner](.) codebase. 

> **Before you change a measurement, read [docs/GRADER.md](docs/GRADER.md).** The dangerous defects in
> this repository do not crash — they produce a complete-looking `judge.json` or `fact_trace.jsonl`
> carrying a number that is quietly false, and that number reads as a *finding*. Two columns here
> were structural constants for their entire existence before anyone noticed. When you add or change
> a measurement, ask what it would look like if the mechanism were dead; if the answer is "a
> plausible zero", write the test that fails when it is.
>
> Full documentation index: [docs/README.md](docs/README.md).

---

## 1. System Overview

`exp-runner` is a generalized framework for measuring coding-agent behaviour against a target repository. It points an agent (`codex`, `claude`, `gemini`, or `agy`) at a repo and a task, runs the matrix in isolated worktrees, grades each run with a mechanical battery, and writes telemetry. It carries **two experiments over one machinery** — know which one you are touching:

* **Ladder mode** (default) — the same task across 7 escalating-context environments (E0..E6, bare repo → full scaffold), measuring how success and token cost move with context. Code: [lib/ladder.py](lib/ladder.py), [lib/context_gen.py](lib/context_gen.py).
* **WUR mode** (`--experiment wur`) — *Workspace Uptake & Retention.* Plants one **counter-prior fact** in the workspace and tracks it across four boundaries (**available → read → used → retained**), with a probe interrupting every 1–3 tool calls. Arms vary only *where* the fact sits and *in what format*. Code: [lib/wur/](lib/wur/) (25 modules).

A change to shared code (`judge.py`, `battery.py`, `run_job.sh`, `setup_run.sh`, `telemetry.py`) lands in **both**. Check both before you call it done.

---

## 2. Directory Structure & Key Files

Below is the layout of the codebase. Use these file links to inspect the code before editing:

### Core Orchestration & CLI
- [run.sh](run.sh): The main entry point. Orchestrates the interactive wizard and flags parsed into jobs.
- [lib/wizard.py](lib/wizard.py): Terminal-based wizard guiding job creation.
- [lib/jobspec.py](lib/jobspec.py): Job specifications schema, parsing, and metadata helpers.
- [install.sh](install.sh): Installer script that checks dependencies, installs python requirements, and hooks the runner executable to the system path.

### Environment Management (The Context Ladder)
- [lib/ladder.py](lib/ladder.py): Defines the 7 context environments (E0..E6) and what docs are added/stripped at each level.
- [lib/context_gen.py](lib/context_gen.py): Invokes the agent once per job to write the project-level context documentation (README, AGENTS, PROJECT, etc.).
- [lib/brew.sh](lib/brew.sh): Prepares the pristine bare-cloned repository and detects/pins the stack.
- [lib/detect_stack.py](lib/detect_stack.py): Inspects files to determine the package manager (npm, pip, poetry, etc.) and compiles a build/test plan.

### Run Lifecycle & Execution Loop
- [lib/run_job.sh](lib/run_job.sh): Coordinates the runner matrix (Tasks × Envs × Reps). Holds a job-level runner lock, claims each cell atomically, and applies the per-backend concurrency policy (`claude` 4, `codex` 8, `gemini`/`agy` 1).
- [lib/setup_run.sh](lib/setup_run.sh): Prepares the run worktree, clears pre-existing documentation (for E0), overlays context docs, and baseline-commits them.
- [lib/run_agent.sh](lib/run_agent.sh): Headless agent CLI wrapper that executes the prompt with approvals bypassed.
- [lib/teardown_run.sh](lib/teardown_run.sh): Captures git patches, stores the raw agent transcripts/databases, and invokes the grading pipeline.
- [lib/self_analysis.sh](lib/self_analysis.sh): Prompts the agent post-run to reflect on its solution quality. Can run post-hoc if the workspace was torn down.

### Evaluation, Telemetry & Reporting
- [lib/judge.py](lib/judge.py): LLM Judge system. Parses plain-text acceptance criteria into executable pytest/shell batteries, proves the battery discriminates (`--synthesize` / `--floor-check`), adjudicates subjective checks (`--grade`), and re-derives a finished run's verdict from archived artifacts (`--regrade`).
- [lib/battery.py](lib/battery.py): Executes one criterion — a shell command plus a `pass_condition` expression evaluated in a sealed namespace — and reports a TRI-STATE verdict (`True`/`False`/`None`), never inventing one.
- [lib/agent.py](lib/agent.py): LLM client wrapping library for the judge and context generation commands.
- [lib/telemetry.py](lib/telemetry.py): Normalizes token usage, command histories, and file access patterns.
- [lib/report.py](lib/report.py): Generates job scorecard summaries (`REPORT.md`), including the **grader provenance** section — which battery hash scored each run, and whether it was re-graded.
- [lib/figures.py](lib/figures.py): Generates matplotlib PNG charts for cost and file access heatmaps.

### WUR Mode (`lib/wur/`, 25 modules)
The uptake instrument. Read the module docstring before editing any of these — each states its single responsibility.
- [lib/wur/facts.py](lib/wur/facts.py): The fact registry — load, validate, mint, collision-check, leak-check, gate.
- [lib/wur/nonce.py](lib/wur/nonce.py): Mints the tracer token the whole instrument rests on (`blake2s(salt | repo_sha | fact_id)`) and scans for it.
- [lib/wur/plant.py](lib/wur/plant.py): Renders the arms into overlays, stamps them, and proves they landed.
- [lib/wur/driver.py](lib/wur/driver.py): Parent process of the agent child for one run; owns its lifecycle and the probe schedule.
- [lib/wur/gate.py](lib/wur/gate.py): The `PreToolUse` barrier and the `SessionStart` liveness marker.
- [lib/wur/preflight.py](lib/wur/preflight.py): **H1..H12** — proves the run is uncontaminated *before* the child starts.
- [lib/wur/exposure.py](lib/wur/exposure.py): Nonce scan over model-visible regions → `exposure.jsonl` (the `read` boundary).
- [lib/wur/detectors.py](lib/wur/detectors.py) + [lib/wur/detect_use.py](lib/wur/detect_use.py): The **closed** registry of six `used` predicates, run over one tree.
- [lib/wur/probes.py](lib/wur/probes.py): Probe answers → `probes.jsonl` (the `retained` boundary).
- [lib/wur/trace.py](lib/wur/trace.py) / [lib/wur/reconcile.py](lib/wur/reconcile.py) / [lib/wur/aggregate.py](lib/wur/aggregate.py): The derivation chain — one row per (run × fact) → `fact_trace.jsonl`, rebuilt idempotently, then rolled up.
- [lib/wur/verify_pack.py](lib/wur/verify_pack.py): The truth-table gate for a fact pack; a pack that does not SEPARATE must not collect.

### Fixtures, Task Packs & Contracts
- [fixtures/ledgerline/](fixtures/ledgerline/): A synthetic 67-file Python project (156 passing tests), rebuilt **deterministically** by `build.sh` to the SHA pinned in `repo_sha.txt`. `--check` verifies the pin. Every measured number in [docs/](docs/README.md) came from it.
- [tasks/](tasks/): Four task packs, each with a **frozen** `criteria.json` (no synthesis step, so runs are comparable), `reference_patches`, and an `accept.md` that must grade what `task.md` asks. `tasks.yaml` is **generated** — edit `make_tasks_file.py` and regenerate, never the YAML.
- [schemas/](schemas/): The authoritative field-level documentation for every artifact a run writes. A new field is not shipped until it is declared here.
- [analysis/](analysis/): The WUR analysis notebook and its library; [viz/](viz/): the read-only dashboard (ladder mode only — see README).

---

## 3. Important Development Conventions

When modifying this repository, you **must** adhere to the following conventions:

### I. Command Substitution & Output Separation
Bash script outputs are captured by wrappers in multiple places. 
> [!IMPORTANT]
> All interactive prompts must route to `stderr` (`>&2`), whereas output meant for command substitution variables (such as target directories, status flags, and json dumps) must print directly to `stdout`. Swallowing prompts in stdout will cause the scripts to hang.

### II. Diff Purity and Baseline Commits
To prevent injected context files (README, AGENTS, etc.) from polluting the final `git.patch` of the solution:
1. `setup_run.sh` initializes the isolated git worktree.
2. It strips any pre-existing orienting documentation (for E0).
3. It writes the specific context overlay for that rung.
4. It performs a **git commit** to establish the env context as baseline.
5. The agent is then run. The resulting `git.patch` is generated relative to this commit.

### III. Serial vs Parallel Execution Rules
Concurrency is a **per-backend policy table** in [lib/run_job.sh](lib/run_job.sh), not a hard clamp; `parallelism.per_backend.<backend>` in `job.yaml` overrides it.

* **Codex**: 8. Stateless per invocation.
* **Claude**: **4** — *this is no longer serial.* The clamp existed because `setup_run.sh` merged hooks into the global `~/.claude/settings.json`, so two concurrent runs raced on one file. That mutation is deleted; its replacement is a per-run `CLAUDE_CONFIG_DIR` plus a per-run `--settings`, measured clean four-way against a real 6,000-file repo. **Applies to ladder mode as well as `wur`.** Anything in the docs still saying "claude must run serially" is stale.
* **Gemini / Antigravity (`agy`)**: 1. Both still share global CLI state (`~/.gemini/settings.json`, OAuth token caches) that no per-run directory isolates.

**Never point two runners at one job.** `run_job.sh` takes `$JOB_DIR/.runner.lock` for the whole matrix and refuses to start if a live runner holds it (`ATLAS_ALLOW_CONCURRENT_RUNNERS=1` overrides). The per-cell `.claim` guards workers *within* one runner; it cannot survive an operator purging cells while another runner is alive, and the observed result of that was runs containing nothing but `.run_done` entering a dataset. Check liveness by PID — `pgrep -f <pattern>` matches its own command line.

### IV. A Battery Is Not Ground Truth Until It Is Proven From BOTH Sides
`judge.floor_check()` runs the criteria against the pristine base. That establishes **"fails before"** and silently *assumes* **"passes after"**. A criterion shaped `exit_code == 0 and <broken expression>` short-circuits on the base tree — the command exits non-zero, so the broken half is never evaluated — looks cleanly discriminating, and breaks only once a correct solution makes the command succeed. Measured consequence: three of seven criteria scored `None` ("undecided") on a verified-correct solution, which grades a completed run as not-completed.

* Give a task **`reference_patches: [...]`** — independent known-correct solutions — and the check becomes two-sided: base must FAIL every new-behaviour criterion, every reference must PASS all of them. `manifest.proof_ok` is the verdict; `two_sided_ok` is per criterion. A reference that will not apply is a broken proof, never a passing one.
* Without them, `manifest.proof.two_sided` is `false` and `floor.log` says so. **Do not read a one-sided `floor_ok: true` as "this battery works."**
* `manifest.lint` flags the three shipped failure shapes: a `pass_condition` reaching for a name the sandbox does not provide (`__import__`, `open`, `eval`, …), the short-circuit above, and a piped command (the shell reports the *last* stage's status, so `exit_code` stops meaning anything).
* A mechanical battery separates *did nothing* from *did something shaped right*. It does **not** prove correctness — a hardcoded stub that ignores its input has scored 7/7 and certified accepted. Treat the verdict as a floor and say so.

### V. Grade From Archived Artifacts, Not the Live Workspace
Teardown removes the worktree, so `--grade` can only ever run once. What survives is enough to rebuild the exact tree: `refs/atlas/baseline-run/<RUN_ID>` (the post-overlay baseline commit) plus `git.patch`. `judge.py --regrade --run-id <id>` does that and writes `judge.regrade.json` (`--in-place` overwrites `judge.json`). Use it after any grader fix, so a harness defect cannot silently define the truth it is measured against. `ATLAS_KEEP_WORKSPACE=1` keeps the worktree when you want to open it by hand.

> [!IMPORTANT]
> If you patch the grader mid-collection, the dataset is no longer graded by one instrument. Re-grade **everything** from the archive and say which grader produced the published numbers.

### VI. Backend Token Extraction Differences
Tokens are extracted differently per provider in [lib/telemetry.py](lib/telemetry.py):
- **Claude & Codex**: Parsed by matching token events from the standard JSONL log.
- **Gemini**: Overlaid using the session stats reported directly in `agent_stdout.json` via stdout `-o json` logs.
- **Antigravity (`agy`)**: Parsed from the generated run's SQLite conversation database (`agy_conversation.db` / `lib/agy.py`) using WAL-safe extraction.

---

## 4. Running and Testing the Harness

### Prerequisites & Login
The harness relies on a pre-authenticated agent CLI. Verify authentication before execution:
```bash
# Codex
codex login

# Claude Code
claude login

# Gemini CLI
# (Run a query to ensure credentials are active)

# Antigravity CLI
agy
```

### Running a Job
```bash
# Start the interactive wizard to generate and execute a job
./run.sh

# Non-interactive CLI run across all environments
./run.sh --repo <repo_path_or_url> --task-file <task.txt> --accept-file <accept.txt> --model <model>
```

### Testing the Harness Itself
```bash
python3 -m pytest tests/ -q        # or run any file directly — stdlib unittest, no pytest needed
```
| file | guards |
|---|---|
| `tests/test_wur_lib.py` | anything whose failure is a wrong number rather than a crash |
| `tests/test_grader_proof.py` | the grading defects: sandbox scope, the two-sided proof, re-grading, provenance |
| `tests/test_task_packs.py` | the task-pack contract: frozen battery, references, lint, baseline, task/acceptance agreement |
| `tests/test_detectors.py` | the closed detector registry |

Each test in the first three corresponds to a defect that shipped and produced a false number. Do not weaken them.

### Verifying a Change End-to-End Without an LLM Call
The full matrix costs agent calls; the grading path does not. This loop exercises brew → frozen-battery install → two-sided proof against the shipped fixture, and is the check to run before claiming a change is safe:
```bash
bash fixtures/ledgerline/build.sh --out /tmp/atlas-fixtures/ledgerline --force   # deterministic; SHA must match
./run.sh --path /tmp/atlas-fixtures/ledgerline --tasks-file tasks/tasks.yaml --brew-only
for t in cashflow-report export-envelope fx-trial-balance opening-balances; do
  python3 lib/judge.py --synthesize  --job-dir jobs/<id> --task-id "$t"
  python3 lib/judge.py --floor-check --job-dir jobs/<id> --task-id "$t"
done
```
All four must report `floor_ok=True proof_ok=True` with a two-sided proof over their references and a clean `manifest.lint`. Anything less means the grader stopped discriminating — treat it as a broken build, not a flaky check.

### Environment Knobs
| variable | effect |
|---|---|
| `ATLAS_RUNS_ROOT` | where `wur` run directories live (default `/tmp/atlas-runs`). `jobs/<id>/runs/<run_id>` is a **symlink** into it, so the fact registry is never an ancestor of a workspace. Point it at durable storage; anything resolving by realpath lands there. |
| `ATLAS_KEEP_WORKSPACE=1` | keep the run worktree at teardown instead of removing it |
| `ATLAS_ALLOW_CONCURRENT_RUNNERS=1` | bypass the job-level runner lock (you have checked, and you accept the consequence) |
| `EXTRA_STRIP` / `extra_strip:` | extra paths removed from **every** arm before its overlay is applied — for real repos that ship their own README/CONTRIBUTING/AGENTS.md, which would otherwise leave a "bare" arm carrying orienting context |

> [!NOTE]
> A directory created under a **setgid** parent (`drwxr-sr-x`) inherits the bit, and a *numeric* `chmod 700` does not clear it — so `claude_home` came out `0o2700` and preflight **H3** failed on every cell. The scripts now clear it explicitly (`chmod g-s`). If you add another mode-checked directory, do the same.

### Debugging Failures
All logs, databases, and assets generated during execution reside in the `jobs/` folder (which is globally gitignored).
1. Check [brew.log](jobs/) under the active job folder if dependencies failed to install.
2. Check `jobs/<job_id>/runs/<run_id>/run_meta.json` and `transcript.jsonl` to inspect the agent interaction trajectory.
3. The temporary run workspaces are deleted post-grading, but `git.patch` is preserved — rebuild the tree with `judge.py --regrade` rather than assuming it is gone for good.
4. **Atlas records no per-command process exit codes.** The `exit_code`/`exitCode` fields in the logs belong to the *hook subprocess* and are uniformly 0; the tool result's `is_error` flag is the substitute. Do not build an analysis on them.
