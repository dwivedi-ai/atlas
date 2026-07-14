# AI Agent Operating Contract — `exp-runner`

Welcome, AI Agent. This document acts as your system guide, operating contract, and repository map for working within the [exp-runner](.) codebase. 

---

## 1. System Overview

`exp-runner` is a generalized framework for testing coding-agent performance against a **context ladder** (from a bare repository to full documentation overlays). It points an agent (`codex`, `claude`, `gemini`, or `agy`) at a target repository and a task, runs that task across 7 escalating-context environments (E0..E6), grades the output using a synthesized test battery, and charts the token costs and file access heatmaps.

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
- [lib/run_job.sh](lib/run_job.sh): Coordinates the runner matrix (Tasks × Envs × Reps). Maps parallel execution for `codex` and serial execution for `claude`/`gemini`/`agy`.
- [lib/setup_run.sh](lib/setup_run.sh): Prepares the run worktree, clears pre-existing documentation (for E0), overlays context docs, and baseline-commits them.
- [lib/run_agent.sh](lib/run_agent.sh): Headless agent CLI wrapper that executes the prompt with approvals bypassed.
- [lib/teardown_run.sh](lib/teardown_run.sh): Captures git patches, stores the raw agent transcripts/databases, and invokes the grading pipeline.
- [lib/self_analysis.sh](lib/self_analysis.sh): Prompts the agent post-run to reflect on its solution quality. Can run post-hoc if the workspace was torn down.

### Evaluation, Telemetry & Reporting
- [lib/judge.py](lib/judge.py): LLM Judge system. Parses plain-text acceptance criteria into executable pytest/shell batteries and adjudicates subjective checks.
- [lib/battery.py](lib/battery.py): Helper classes executing synthesized test assertions.
- [lib/agent.py](lib/agent.py): LLM client wrapping library for the judge and context generation commands.
- [lib/telemetry.py](lib/telemetry.py): Normalizes token usage, command histories, and file access patterns.
- [lib/report.py](lib/report.py): Generates job scorecard summaries (`REPORT.md`).
- [lib/figures.py](lib/figures.py): Generates matplotlib PNG charts for cost and file access heatmaps.

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
* **Codex**: Can run reps/environments in parallel (utilizes `--jobs N` sliding window).
* **Claude / Gemini / Antigravity (`agy`)**: **Must run serially** (`--jobs 1`).
  - *Why?* These agents interact with global system settings/config locks (e.g. modifying `~/.claude/settings.json`, `~/.gemini/settings.json`, or holding locks on OAuth files / token caches) which will race and deadlock under concurrent runs.

### IV. Backend Token Extraction Differences
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

### Debugging Failures
All logs, databases, and assets generated during execution reside in the `jobs/` folder (which is globally gitignored).
1. Check [brew.log](jobs/) under the active job folder if dependencies failed to install.
2. Check `jobs/<job_id>/runs/<run_id>/run_meta.json` and `transcript.jsonl` to inspect the agent interaction trajectory.
3. The temporary run workspaces are deleted post-grading, but `git.patch` is preserved.
