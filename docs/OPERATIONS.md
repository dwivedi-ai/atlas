# OPERATIONS — running Atlas, and the traps that cost real time

Everything here has been hit on a real machine. It is ordered by how much time each one costs when
you meet it unprepared.

---

## 1. Prerequisites

```bash
./install.sh
```

Checks `git` and `python3 ≥ 3.9`, makes the runtime deps importable, bootstraps the separate
analysis environment, and puts an `exp-runner` command on your `PATH`.

**Log in to your agent CLI first** — Atlas does not handle auth. `claude login` (the default
backend, and the only one WUR runs on), `codex login`, the Gemini CLI's own flow, or `agy`.

> **The interpreter is the first thing that goes wrong.** On a modern distro the system `python3` is
> PEP 668 externally-managed and has neither `yaml` nor `jsonschema`. `install.sh` falls back to a
> private `.runner-venv`; `run.sh`, `run_job.sh` and `visualize.sh` prepend it to `PATH` and fail
> loudly if the imports still do not resolve. Anything you run by hand should use
> `.runner-venv/bin/python3`. A bare `python3` will die with `ModuleNotFoundError` deep inside a
> redirected subshell where nobody reads the output.

---

## 2. Running a job

```bash
./run.sh                                      # interactive wizard (ladder jobs)
./run.sh --repo <url> --task-file t.txt --accept-file a.txt --model claude --envs E0,E6
./run.sh --job <job_id>                       # resume; finished cells are skipped
./run.sh --job <job_id> --brew-only           # clone + build the env, stop there
```

`./run.sh --help` lists every flag and every environment variable.

The whole pipeline is **resume-safe**: brewing, context artifacts, the registry, grader installation
and finished cells are all sentinel-guarded, so re-running after an interruption continues instead
of restarting.

### Never point two runners at one job

`run_job.sh` takes `$JOB_DIR/.runner.lock` for the whole matrix and refuses to start alongside a live
one. The per-cell `.claim` guards workers *within* one runner; it cannot survive an operator purging
cells while another runner is alive, and the observed result of that was two runs containing nothing
but `.run_done` entering a published dataset. Liveness is checked by **PID** —
`pgrep -f <pattern>` matches its own command line and its own shell pipeline, which is its own hour
of confusion. Override with `ATLAS_ALLOW_CONCURRENT_RUNNERS=1` only when you have checked.

### Concurrency

A per-backend policy table, not a hard clamp; `parallelism.per_backend.<backend>` in `job.yaml`
overrides it.

| backend | cap | why |
|---|---|---|
| `codex` | 8 | stateless per invocation |
| `claude` | **4** | per-run `CLAUDE_CONFIG_DIR` + per-run `--settings`; nothing global is mutated. **Claude is not serial** — anything saying otherwise is stale |
| `gemini`, `agy` | 1 | both still share global CLI state no per-run directory isolates |

---

## 3. Environment knobs

| variable | effect |
|---|---|
| `ATLAS_RUNS_ROOT` | where WUR run directories live (default `/tmp/atlas-runs`). `jobs/<id>/runs/<run_id>` is a **symlink** into it, so the fact registry is never an ancestor of a workspace. Point it at durable storage — anything that resolves by realpath lands there |
| `ATLAS_NONCE_SALT` | the **deployment secret** for WUR. See §5 |
| `ATLAS_KEEP_WORKSPACE=1` | keep each run's worktree instead of removing it at teardown |
| `ATLAS_ALLOW_CONCURRENT_RUNNERS=1` | bypass the one-runner-per-job lock |
| `EXTRA_STRIP` / `extra_strip:` | extra paths removed from **every** arm before its overlay lands. Empty by default. Set it when the target is a real repository that ships its own `README`/`CONTRIBUTING`/`AGENTS.md`, which would otherwise leave a "bare" arm carrying orienting context and make a scaffold arm compete with the repo's own contract instead of replacing it |

---

## 4. The traps

| trap | symptom | what to do |
|---|---|---|
| **setgid inheritance** | preflight **H3** fails on *every* cell: `claude_home mode is 0o2700, want 0o700` | a directory created under a `drwxr-sr-x` parent inherits the bit, and a **numeric** `chmod 700` does not clear it. Atlas now clears it explicitly (`chmod g-s`). If you add another mode-checked directory, do the same |
| `git -C <dir> apply <relpath>` | "can't open patch" | `-C` chdirs **before** resolving the patch path. Always pass an absolute path |
| **CLI version drift mid-matrix** | preflight **H12**: `CLI moved since the canary` | this is the gate working. Do **not** re-canary and continue — you would mix backend versions across the dataset. Exclude the cell and record why |
| **a transiently failed canary** | H12 fails closed on every cell of one arm | a failed canary is a measurement to retake, not a fact to keep. `run.sh` now skips only a recorded `pass` and retakes otherwise, 3 attempts with backoff |
| **disk** | `OSError: [Errno 28] No space left on device` while `df /` reports plenty | `/home` may be a separate, much smaller volume than the overlay root. Check `df` on the path the jobs actually live on. A job's `.venv` can be gigabytes if the repo's `requirements.txt` is unfiltered |
| **doubled `-q`** | a criterion parses an empty string | if the repo's `pytest.ini` already sets `-q` in `addopts`, adding another raises the quiet level and suppresses the summary line and the node-id listing |

---

## 5. The nonce salt is a deployment secret (WUR)

Every nonce is `blake2s(salt | repo_sha | fact_id)`. `repo_sha` and `fact_id` are public in any
checkout, so **at the default salt every nonce in this repository is recomputable by anyone holding
it.** That does not corrupt a run by itself, but a model that has seen the repository could emit a
nonce it never read — which is exactly what `read` is trying to measure.

```bash
ATLAS_NONCE_SALT="$(openssl rand -hex 16)" ./run.sh --experiment wur --job <id>
```

`run.sh` warns whenever the default is in use. Leave it at the default for smoke runs and for
reproducing a published result; set it before collecting anything you intend to publish or believe.

The registry itself (`$JOB_DIR/.registry/`, mode 0700, gitignored) holds the minted nonces, the
rendered overlays and the detector bindings. Preflight **H2** fails closed if it is reachable from a
workspace by `..`.

---

## 6. Commands

```bash
# fixture
bash fixtures/ledgerline/build.sh --out /tmp/atlas-fixtures/ledgerline --force
bash fixtures/ledgerline/build.sh --check          # verify it still hashes to repo_sha.txt

# the matrix only, skipping finished cells
JOB_DIR=$PWD/jobs/<id> JOBS=1 bash lib/run_job.sh

# the grader
python3 lib/judge.py --floor-check --job-dir jobs/<id> --task-id <t>   # re-prove, no LLM call
python3 lib/judge.py --regrade    --job-dir jobs/<id> --run-id <r>     # re-derive from the archive
python3 lib/judge.py --synthesize --job-dir jobs/<id> --task-id <t>    # author one (LLM call)

# WUR derivation, offline and idempotent
python3 lib/wur/reconcile.py --run-dir <run>       # re-derive all four tables
python3 lib/wur/validate.py  --run-dir <run>       # schema-validate every emitted table
python3 lib/wur/schedule.py  --job-dir <job> --force
.venv-analysis/bin/python lib/wur/aggregate.py --job-dir jobs/<id>     # the parquet rollup

# task packs
python3 tasks/make_tasks_file.py --out tasks/tasks.yaml   # regenerate after editing a pack
python3 tasks/make_tasks_file.py --check                  # exit 1 on drift

# dashboard (read-only over jobs/)
./visualize.sh [--port 8080]
```

---

## 7. Reading a finished job

```
jobs/<job_id>/
├── job.yaml                     the frozen spec
├── repo.git/  .venv/  brew.log  the brew
├── environments/E0..E6/         ladder: per-env context overlays
├── grader/<task>/               criteria.json + manifest.json (proof_ok, lint) + floor.log
├── REPORT.md                    scorecard; ⚠ Grader provenance if the graders disagree
├── analysis/                    WUR: the parquet rollup
└── runs/<run-id>/               git.patch, transcript.jsonl, judge.json, run_record.json,
                                 event_log.jsonl, report.md; WUR adds stream.jsonl.gz,
                                 hygiene.json, fact_trace.jsonl, probes.jsonl, exposure.jsonl
```

Run workspaces are deleted after grading; `git.patch` is what survives — `git apply git.patch`
replays a solution, and `judge.py --regrade` rebuilds the whole tree from it plus
`refs/atlas/baseline-run/<run-id>`.
