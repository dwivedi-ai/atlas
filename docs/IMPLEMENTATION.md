# Workspace Uptake & Retention — Implementation Guide

**Target repo:** `/home/coder/experiments/atlas` @ `d530803`
**Backend:** Claude Code `2.1.222` (verified), model `claude-sonnet-5`
**Status convention:** every claim here is either grounded in a file in this repo, or in a command that was actually run. Anything unsettled is tagged **UNVERIFIED** and carries the spike that settles it (§11).

---

## 1. What this builds

Atlas today answers *"does richer context help?"* by running a task across a 7-rung context ladder and charting token cost. This document specifies its conversion into a different instrument, answering a different question:

> Not whether the workspace **contains** useful information, but whether the agent **carries that information across the boundary into action.**

The unit of measurement is a single **fact** planted in the workspace, tracked across four boundaries:

```
available ──► read ──► used ──► retained
```

Each boundary is an executable predicate over named artifacts (§4). No boundary is defined in prose alone.

The existing ladder experiment (`experiment: ladder`) remains a supported mode. Nothing that works today stops working.

---

## 2. Locked decisions

| # | Decision | Rationale |
|---|---|---|
| **1 fact per task** (D1) | Exactly one critical fact per task in v1. | With two facts the probe's 3 slots become contested, and within-run `fact_trace` rows stop being independent — which invalidates the strata for every significance test and the resampling unit for the bootstrap. None of the power work covers it. |
| **Thinking counts as exposure** (D4) | `self_thinking` sets `read = 1`. | Operator decision. **Consequence, mitigated:** `confab_rate = P(mention \| ¬read)` is the alarm that detects a *missing* inbound channel; folding thinking into `read` mutes it. Mitigations are mandatory — see §4.2.1. |
| **Both controls** (D3) | `ctrl` (fact-free `NOTES.md` at d2) is primary; `ctrl-nofile` (no `NOTES.md`) is secondary. | `ctrl` isolates *content* from *file existence*. `ctrl-nofile` bounds the residual "a file existing at all primes search behaviour" effect, which reasoning alone cannot close. +60 runs. |
| **Weak facts kept** (D2) | Facts firing 1/12 in control are admitted but **pre-registered as excluded from primary**. | Keeping them preserves suite size; pre-registering the exclusion removes the forking-paths risk of deciding after seeing treatment data. |
| **No probe-density factor** (D5) | Deferred to v2. | +120 runs for a third cadence level. `d1-np`/`d3-np` already give probed-vs-unprobed, which is the question that matters for interpreting v1. |
| **Tier A only** (D6) | v1 runs the synthetic fixture. Tier B (generated facts on arbitrary repos) is a follow-on job. | Tier B reintroduces an LLM-authored experimental variable. Run it once Tier A has established the effect sizes it would be measured against. `tier` is recorded on every row so the two pool later. |
| **Slot classifier calibrated** (D7) | Hand-label 200 pilot slots. | `slots_distrusted` / `slots_filler` are unvalidated heuristics without it. `raw_response` is always retained, so this is recoverable — but the pilot is the cheap moment. |

---

## 3. Empirically verified constraints

These were established by running the CLI, not by reading docs. Several overturn the obvious design.

| # | Finding | Design consequence |
|---|---|---|
| **V1** | Probe text delivered via **hooks is refused as prompt injection**: 3/3 via `PostToolUse.additionalContext`, 3/3 via `PreToolUse` deny-reason. The one apparently-working run depended on an undisclosed ~250-token trust preamble. | Hook channel **rejected** for probe text. Hooks carry no model-visible text at all, except the budget-deny reason (a control action). |
| **V2** | `--input-format stream-json` user messages: **13 sent, 13 answered, 13 parsed, 0 refusals** across 5 runs, no preamble. | Probe transport is the **stream-json user channel**. |
| **V3** | `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY=1` **does not work** — the name is in the binary, but the model still emitted 7 `tool_use` blocks in one message and the probe landed 5 tool calls late. | Pacing must come from the system prompt. |
| **V4** | `--append-system-prompt "$PACING_PROMPT"` **does** work: one tool call per assistant message, 12/12 on a 30-file task, 8/8 end-to-end. | This is the invariant that makes turn boundaries ≈ tool-call boundaries, preserving the "every 1–3 tool calls" cadence, and eliminates the entire concurrency bug class by construction. |
| **V5** | `--setting-sources ''` **kills workspace `CLAUDE.md` auto-discovery**. `--add-dir` does not restore it. | Hygiene recipe is `--setting-sources project` + stripping `.claude/` and `.cursor/` from the worktree. |
| **V6** | Hook `tool_response` **is not what the model saw**: 926.7 KB of Bash stdout reached the model as a 2,299-char `<persisted-output>` stub while the hook captured 29,999 chars. `PostToolUse` does not fire at all when a tool errors. | Exposure is computed **only** from model-visible regions of `stream.jsonl`. Scanning hook payloads or sidecars manufactures exposure. |
| **V7** | Tokens are **double-counted**: `extract/core.py` sums per transcript *line*, and Claude Code writes one line per content block with a duplicated `usage`. Measured over **116 transcripts**: input inflation min 1.00× / median **1.50×** / pooled **2.09×** / max 4.90×; output min 1.00× / median 1.94× / max 8.72×. Duplicate-usage groups byte-identical in 116/116; `message.id` present on 100% of assistant lines. | `claude_code.py` dedupes by `message.id`; totals overridden from the terminal `result` event; accounting version-stamped so pre-fix and post-fix records are never pooled. **Every token figure the existing ladder experiment produced is inflated by a run-varying factor.** Dedupe-by-`message.id` **exactly equals** `result.usage` (53,292 == 53,292 measured) — so a per-run equality assertion is free and would have caught this on day one. |
| **V8** | Hook-written counters **lose increments** under concurrent tool calls (6 fires → counter 5; 20-way stress → 3). `hook_events.jsonl` appends >`PIPE_BUF` corrupt (5/12 lines unparseable). | One writer per file, `flock`'d. Combined with V4 pacing, concurrency is serialized by construction. |
| **V9** | Under `bypassPermissions` the agent **read the fact registry** when it sat three `..` hops above the workspace (demonstrated verbatim). | Run roots move to `$ATLAS_RUNS_ROOT` (default `/tmp/atlas-runs`); registry lives at `$JOB_DIR/.registry/`, mode 700; post-hoc escape detector runs at teardown. |
| **V10** | `--tools Bash,Read,Write,Edit,Glob,Grep` removes the subagent tool. Sidechain tool calls otherwise fire `PreToolUse` **with the same `session_id` as the parent**, so the hook payload cannot filter them. `--tools` **silently ignores** names it does not honour. | Tool set asserted positively at preflight by asking the model to enumerate its tools, compared against a frozen list hashed as `tools_allowlist_sha256`. |
| **V11** | Hooks are synchronous: a 3 s sleep hook moved wall time 14.09 s → 26.96 s over 3 tool calls, with strict enter/exit pairing. | The `PreToolUse` barrier can safely block while the driver decides. |
| **V12** | In `--print` mode, "settings files that fail validation are silently ignored" (per `claude --help`). A templating bug therefore yields zero hooks, zero barriers, zero probes, and **no error anywhere**. | `setup_run.sh` validates the rendered settings with `json.load`; the driver aborts if `watch/hooks_alive` does not appear within 90 s. |
| **V13** | **Holding the barrier until the probe is answered DEADLOCKS.** Measured: hook held 20 s ⇒ zero child output; the stdin-injected message was not even *replayed* until the hook returned. | Driver **injects on stdin, then releases the barrier** — never the reverse. No gate response may depend on model output. §6.2. |
| **V14** | **A denied tool call is RETRIED by the model and then succeeds.** The deny hook fired 4× for a 3-call task: `tool_use` → deny → refusal text → *same* `tool_use` again → success. | `BUDGET_STOP_TEXT`-via-deny **does not stop a run**. Budget stop is re-specified as deny-all-subsequent + close stdin. A denied call costs **two** barrier fires, so `gate/tool_calls.jsonl` ordinals are **not** tool-call ordinals. |
| **V15** | Under `--input-format stream-json` the child **does not exit after `result`** (measured alive 235 s past `result/success`). Closing stdin mid-turn is a *graceful drain*: the in-flight turn completes, a well-formed `result` is emitted, exit 0 ~20 s later. | The driver must close stdin to terminate and **must never `wait()` before closing it**, or it hangs forever. |
| **V16** | `Read` has a **hard 256 KB ceiling** returning `is_error:true` with no content and **no sidecar**. Below it, truncation is **completely silent** — no ellipsis, no marker, nothing in `stream.jsonl`. Cut point is content-dependent (21.6 KB repeated-char → ~51.5 KB prose, a 2.4× spread) and is **not** a line cap: a 200-byte-line file was cut at line 108. `toolUseResult.file.truncated` is `None` on *every* read. | Fact-bearing files capped at **20 KB by bytes**, enforced in `plant.py` and asserted in preflight. The only truncation signal is `file.numLines < file.totalLines` **from `transcript.jsonl`** — which makes the transcript load-bearing for `read_censored`, not a convenience copy. New `read_error` outcome for the 256 KB ceiling. |
| **V17** | `stream.jsonl` splits one assistant message across multiple lines **exactly like** the on-disk transcript (17 stream lines / 14 distinct message ids). | Any per-line counting in the watcher — **including the §10 pacing gate** `max(tool_uses_per_assistant_message)` — inherits the V7 bug and must group by `message.id`. `turns_total` is **assistant-only**; user lines carry no `message.id`. `message_count` vs distinct id reaches **13.67×**, not the 4.36× quoted earlier. |
| **V18** | `system/init.agents` is **never empty** (`[claude, Explore, general-purpose, Plan, statusline-setup]`) even under full hygiene. `system/init` varies run-to-run in exactly four keys: `cwd`, `memory_paths`, `session_id`, `uuid`. | Preflight must **not** assert `agents == []` — it would fail closed on every run. Assert `tools` set-equals the frozen six and that `mcp_servers`/`skills`/`slash_commands`/`plugins` are empty. `init_sha256` hashes a **canonicalized** init with those four keys removed (identical across 4 runs; a raw hash defeats its own purpose). |
| **V19** | Every one-shot `claude` invocation without `--input-format stream-json` emits `Warning: no stdin data received in 3s` on stderr and **pays 3 s**. | Preflight, canary, judge and the autoload assay all pass `< /dev/null`, and no health check may treat non-empty stderr as failure. |

---

## 4. The measurement chain

### 4.1 `available`

```
available_rf = 1  iff  fact f's nonce occurs in the run's planted baseline tree
```

Checked mechanically after the baseline commit:

```bash
git -C "$WORKSPACE" grep -F -I -i -q -e "$NONCE" "$BASELINE_SHA" -- .
```

`available = 0` only in the control arms. A run whose plant did not land is **excluded** (`analyzable = false`), never counted as a miss.

### 4.2 `read` (exposure)

> `read_rf = 1` iff the nonce of fact `f` occurs in at least one **model-visible** text region of run `r`.

**Model-visible** is load-bearing. A nonce that exists only in a sidecar field, a hook payload, or a `persistedOutputPath` file on disk was **never in the context window** (V6). Scanning those manufactures exposure.

**Region, not "file opened."** Grep output, a glob listing whose path carries the nonce, a `bash cat`, or an auto-loaded file all count. `opened` (a `Read`/`cat`/`sed` whose resolved target equals the planted path) is a separate field; `read − opened` is the reported **incidental-exposure rate**.

#### 4.2.1 D4: thinking counts — and what it costs

Per operator decision, `self_thinking` sets `read = 1`. Three fields exist so nothing is lost:

| field | meaning |
|---|---|
| `read` | **primary.** Inbound channels ∪ `self_thinking`. |
| `read_inbound_only` | inbound channels only — the pre-D4 definition, always recorded, so the sensitivity analysis needs no re-run. |
| `unexplained_possession` | `true` when a `self_thinking` hit has **no prior inbound hit** in the same run. |

`unexplained_possession` is the compensating alarm. A thinking-only nonce means one of: (a) the model genuinely possessed the fact, (b) an inbound channel is missing from the scanner, (c) the nonce leaked into a prompt. **Any run with `unexplained_possession = true` is quarantined and audited by hand.** A pilot rate above 0.05 is a fixture-wide failure, not a data point.

Pre-registered: primary analysis uses `read`; `read_inbound_only` is a mandatory sensitivity row in every table where `read` appears as a denominator.

#### 4.2.2 Channel enum (closed)

| `channel` | source region | `model_visible` | counts toward `read` |
|---|---|---|---|
| `autoload_claude_md` | asserted from `plant_manifest.json` | true | **yes** (`evidence: "by_construction"`) |
| `tool_read` | `tool_result.content`, tool `Read` | true | yes |
| `tool_grep_content` | `tool_result.content`, `Grep`, `output_mode=content` | true | yes |
| `tool_glob_filenames` | `tool_result.content`, `Glob` | true | yes |
| `bash_stdout` | `tool_result.content`, `Bash`, offset < sidecar cap | true | yes |
| `bash_unattributed` | beyond the 30,000-char sidecar cap or inside a `<persisted-output>` preview | true | yes (`reason: "sidecar_capped"`) |
| `tool_write_echo` | `Write`/`Edit` confirmation string | true | yes — a hit means the nonce is in a *path* |
| `attachment_<type>` | transcript `attachment.attachment.type` | true | yes |
| `self_thinking` | `assistant.message.content[].thinking` | true | **yes (D4)** — also sets `thinking_echo` |
| `harness_task_prompt` / `harness_probe` / `harness_resume` | `user` + `isReplay:true`, sha-matched | true | no (asserted nonce-free) |
| `system_reminder` / `system_init_listing` | transcript blocks / `system/init` | true | no (audit only) |
| `self_text` / `tool_input` / `probe_answer` | model output | true | no → `echoed` |
| `unknown_visible` | any model-visible region not mapped above | true | yes, **and fails CI** |
| `sidecar_only` / `persisted_output_ondisk` | not in context | **false** | no; diagnostic |

**Ordering invariant.** For every row with `read = 1 ∧ ever_mention = 1`: `first_exposure_seq < first_mention_seq`. Violations quarantine the run.

**`d0-push` is asserted, not scanned.** Auto-loaded `CLAUDE.md` content appears in neither `stream-json`, nor the on-disk transcript, nor `--debug api`. So `exposure_basis = "manifest_canary"`, all seq/byte fields are `null` (`trace.py` raises if not), and `d0-push` is excluded at schema level from every seq-based aggregate. The assumption is verified once per (job, arm) by the **autoload canary** (§6.5).

**Truncation is a first-class outcome.** `truncated_by_cli` fires on a `<persisted-output>` prefix, a `persistedOutputPath` sidecar, or **`file.numLines < file.totalLines` sourced from `transcript.jsonl`**. Glob/Read `truncated` is *not* a trigger — measured `None` on every read (V16). `stream.jsonl` carries **zero** truncation information, which makes the transcript load-bearing for `read_censored`, not a convenience copy.

A run with `read = 0` and a truncation on a call targeting the fact file scores `read = unknown`, not `read = 0`. This matters because wide searches truncate and deep facts are found by wide searches — **the bias runs with the hypothesis.**

**`read_error` is a distinct third outcome.** The hard 256 KB `Read` ceiling returns `is_error: true` with no content and no sidecar (V16). It is neither `read = 0` nor `read = unknown`, and `regions.py` must **not** classify the 197-char error string as a `tool_read` region.

**Plant files are capped at 20 KB by bytes**, enforced in `plant.py` and asserted in preflight. A line cap is measurably not a mitigation: the cut point is content-dependent across a 2.4× spread, and a 200-byte-line file was cut at line 108.

### 4.3 `used`

> **Grade behaviour. Detect provenance.**

The success battery may only test *what the workspace does*. The mandate detector may only test *how the agent got there*. If the battery tests the mandate, `success ≡ used` and the funnel collapses to one measurement.

```
used_rf = 1  iff  detector_f fires over ( final workspace , git diff $BASELINE_SHA , ordered Bash commands )
```

Detectors are a **closed registry of 6 Python predicates**. Tier-B generation picks a registry `name` and fills `params`; it never authors a predicate. Each returns `{eligible, fired, evidence, detail}`.

`eligible` is not decoration: if a run failed before creating the site the mandate applies to, `fired = 0` is **censored**, not evidence of non-use. Both `use_rate_uncond = fired/N` and `use_rate_cond = fired/eligible` are reported, always together.

**Use is always reported as lift over the paired control:**

```
λ_{a,t} = (1/R) Σ_k used[a,t,k]  −  (1/R) Σ_k used[ctrl,t,k]
λ_a     = (1/T) Σ_t λ_{a,t}
```

Raw use rate appears only in the appendix.

**Orthogonality gate.** In the pilot, compute φ between `used` and `success` per task. `|φ| > 0.8` ⇒ the fact is not orthogonal to acceptance ⇒ discard the fact, same disposition as a failed prior-check.

### 4.4 `retained` — honestly renamed

The probe re-injects the nonce roughly every two tool calls. What this measures is **not** decay of an untouched memory trace:

> `retained` = **sustained self-report under repeated elicitation.**

The report and the pre-registration use that phrase; "retention" appears only as shorthand, always qualified.

Defined on `{ever_mention = 1}` only. Time is **probe index**, never wall clock.

```
i0  = first_mention_probe_index
B_r = P_r − i0                                # at-risk horizon
D_rf = min{ i > i0 : mention_rif = 0 } − i0   # first lapse (primary event)
L_rf = max{ i : mention_rif = 1 } − i0        # last mention (secondary)
```

| case | disposition |
|---|---|
| run completes while still mentioning | administratively censored at `B_r` |
| run times out / errors mid-flight | censored at last **observed** probe |
| fact never mentioned | **excluded**; fully accounted for by mention rate |

Two mandatory covariates: `n_reinjections` (the re-elicitation dose) and `first_use_probe_index`. **Primary retention is restricted to probes strictly before first use** — after the agent has discharged a fact, dropping it is correct behaviour, not forgetting. Post-first-use probes are reported separately as "post-discharge persistence."

**Summary statistic is RMST over a common horizon `J`, not median half-life** — the KM median is undefined whenever `Ŝ(j) > 0.5` across the observed support, which is the expected case.

```
J = min over arms of the largest time at which ≥10% of that arm's retention subset is still at risk
RMST_a(J) = ∫₀^J Ŝ_a(u) du      # lifelines.utils.restricted_mean_survival_time (verified 0.30.3)
```

If `J < 3` probes, retention is descriptive only (KM curves) and ΔRMST drops to exploratory.

### 4.5 `mention`

`mention_rif = 1` iff any slot of probe `i` in run `r` matched fact `f`. Three-tier ladder, all logged:

| tier | rule | field |
|---|---|---|
| a | exact nonce, case-insensitive, whitespace-normalized | `match_nonce` |
| b | fact-specific paraphrase regexes, **frozen in `facts.yaml` before any data** | `match_regex` |
| c | LLM adjudication of `slot_text` vs the fact card, 3 votes | `match_llm` |

**Primary = (a) ∨ (b).** Tier (c) is a pre-registered sensitivity; if κ((a)∨(b), (c)) < 0.6 the LLM tier becomes primary. That switch is decided by κ, not by which gives a nicer result.

Only **exact** inbound hits set `first_exposure_seq`. A lowercased or hyphen-stripped nonce in a tool result is almost certainly the agent's own prior text being re-read.

---

## 5. Architecture

### 5.1 Design invariants

1. **The watcher never changes the agent.** Hooks always `exit 0`, print exactly `{}`, write nothing to stderr. The only hook text reaching the model is the budget-deny reason.
2. **No watcher artifact lives inside `workspace/`.** Anything in the workspace is a context-entry channel and confounds the measurement.
3. **Raw before derived.** Every raw byte hits disk before anything parses it. Derivation is offline, idempotent, re-runnable months later after a scanner bugfix.
4. **One writer per file.** No file is appended by two processes without `flock`.
5. **One tool call per assistant message** (V4), byte-identical in every arm including no-probe. Asserted per run.
6. **Everything the model saw is hashed:** `cli_argv_sha256`, `probe_text_sha256`, `pacing_prompt_sha256`, `workspace_sha256`, `init_sha256`, `canary_sha256`. A silent edit invalidates cross-run comparison loudly. `init_sha256` hashes a **canonicalized** `system/init` with `cwd`, `memory_paths`, `session_id` and `uuid` removed — those four vary every run, and hashing the raw event defeats its own purpose (V18). Canonical hash verified identical across 4 runs. Reference: `pacing_prompt_sha256 = 0a687ddf…` (338 chars, §6.2 verbatim).

### 5.2 Process topology

```
run.sh --experiment wur
 │
 ├─ jobspec.py validate .............. job.yaml v2 (agent{backend,model}, conditions[], probe{}, facts_file)
 ├─ brew.sh ......................... repo.git (bare, SHA-pinned) + .venv          [UNCHANGED]
 ├─ wur/facts.py mint ............... $JOB_DIR/.registry/facts.yaml   (nonces minted, collision-checked)
 ├─ wur/plant.py render ............. .registry/conditions/<arm>/overlay/ + manifest.json
 ├─ judge.py --synthesize ........... grader/<task>/criteria.json + floor-check   (PRE-overlay)
 ├─ wur/facts.py leak-check ......... nonce ∉ criteria / task / accept / probe / self-analysis text
 ├─ wur/canary.py ................... per (job, arm): system/init capture + D0 autoload assay
 ├─ wur/detectors gate 1 + 1b ....... pristine base + cross-task workspaces   (0 agent runs)
 ├─ wur/schedule.py ................. schedule.json (blocked randomisation, frozen, design_sha256)
 │
 └─ run_job.sh ── sliding window, JOBS ≤ 4 ─────────────────────────────────────────────┐
      │  per cell: mkdir $RUN_DIR/.claim (atomic) | skip if .run_done                   │
      │                                                                                 │
      ├─ setup_run.sh                                                                   │
      │    flock'd `git worktree add --detach` @ pinned SHA                             │
      │    strip (mode-gated) → cp overlay → git commit "wur-baseline:<arm>"            │
      │    git update-ref refs/atlas/baseline → BASELINE_SHA                            │
      │    ln -sf $VENV workspace/venv ; flock'd append '/venv' to info/exclude         │
      │    mkdir claude_home ; flock'd copy of ~/.claude/.credentials.json ONLY         │
      │    render settings.json (2 hooks) ; write probe_plan.json                       │
      │                                                                                 │
      ├─ wur/preflight.py ......... H1..H12 → hygiene.json   (fail closed, no API call) │
      │                                                                                 │
      ├─ run_agent.sh → python3 lib/wur/driver.py                                       │
      │   ┌──────────────────── driver.py (PARENT of the child) ────────────────────┐   │
      │   │ reader thread: child.stdout ─► stream.jsonl (VERBATIM, fsync/16 lines)  │   │
      │   │                             └► minimal live parse (try/except-wrapped)  │   │
      │   │ gate thread:   poll gate/req/<tid>.json ─► gate/resp/<tid>.json         │   │
      │   │ main:          cadence, stdin injection, budget ladder, watchdogs       │   │
      │   └─────────────────────────────────────────────────────────────────────────┘   │
      │        │ stdin (stream-json user msgs)          ▲ stdout (stream-json)           │
      │        ▼                                        │                                │
      │   claude --print --input-format stream-json --output-format stream-json --verbose│
      │          --replay-user-messages --include-hook-events                            │
      │          --append-system-prompt "$PACING_PROMPT"                                 │
      │          --settings $RUN_DIR/settings.json --setting-sources project             │
      │          --strict-mcp-config --disable-slash-commands                            │
      │          --tools Bash,Read,Write,Edit,Glob,Grep                                  │
      │          --session-id $SESSION_UUID --model claude-sonnet-5                      │
      │          --permission-mode bypassPermissions --max-budget-usd $USD               │
      │          --autocompact 1000000                                                   │
      │             │ PreToolUse hook (BLOCKS)                                           │
      │             ▼                                                                    │
      │        lib/wur/gate.py pre ─► gate/tool_calls.jsonl (flock'd)                    │
      │                            ─► gate/req/<tool_use_id>.json                        │
      │                            ◄─ gate/resp/<tool_use_id>.json {allow|deny}          │
      │        SessionStart hook   ─► watch/hooks_alive , watch/transcript_path          │
      │                                                                                 │
      └─ teardown_run.sh                                                                │
           1.  git.patch = git diff $BASELINE_SHA (+ untracked)                          │
           1b. wur/detect_use.py ─► use_detect.json   ← BEFORE anything else touches tree│
           2.  transcript.jsonl ← claude_home/projects/<slug>/$SESSION_UUID.jsonl (BY ID)│
           3.  judge.py --grade (isolated config dir) ─► judge.json                      │
           4.  wur/reconcile.py                                                          │
                 ├ events.py    stream + gate      ─► events.jsonl                       │
                 ├ exposure.py  regions × nonces   ─► exposure.jsonl                     │
                 ├ probes.py    answers, parse ladder ─► probes.jsonl                    │
                 └ trace.py     join all + use_detect  ─► fact_trace.jsonl               │
           5.  telemetry.py ─► run_record.json (v2) + event_log.jsonl (legacy shape)     │
           6.  gzip stream.jsonl ; copy persisted outputs ; git status --ignored          │
           7.  flock'd worktree remove ; rm -rf claude_home + ASSERT gone ; .run_done    │
                                                                                         │
 ├─ wur/aggregate.py ─► analysis/{fact_trace,probes,events}.parquet (+ .csv) ◄────────────┘
 ├─ analysis/uptake.ipynb  (thin cells → analysis/uptake_lib.py)
 └─ analysis/REPORT.md + analysis/figs/*.svg
```

### 5.3 On-disk layout

```
$JOB_DIR/                                  # jobs/<job_id>/ — NEVER an ancestor of a workspace
  job.yaml                                 # v2
  repo.git/  .venv/  .brew_done  .worktree.lock  .creds.lock
  .registry/                               # mode 700 — the secrets (V9)
    facts.yaml                             # minted nonces, detectors, gates
    conditions/<arm>/overlay/              # exactly what is copied into the workspace
    conditions/<arm>/manifest.json         # SIBLING of overlay/, never inside it
    conditions/<arm>/context_manifest.json # verbatim system/init from the canary run
    conditions/<arm>/canary.md             # model-authored "what was I given" dump
    _index/probe_key.json                  # fact_id → {token, surface_forms, source_path, gist}
    _index/render_report.json
    prior_check/<task_id>.json
  grader/<task_id>/                        # criteria.json, manifest.json, floor.log  [UNCHANGED]
  schedule.json  schedule_actual.jsonl

$ATLAS_RUNS_ROOT/<job_id>/<run_id>/        # default /tmp/atlas-runs — no secret is an ancestor
  workspace/                               # git worktree; removed at teardown
  claude_home/                             # CLAUDE_CONFIG_DIR; DELETED + asserted at teardown
  settings.json                            # the only source of this run's hooks
  probe_plan.json  run_meta.json  hygiene.json  driver.log
  gate/{tool_calls.jsonl, req/, resp/, .lock}
  watch/{hooks_alive, transcript_path, persisted/, stream.err}
  stream.jsonl(.gz)                        # VERBATIM child stdout
  transcript.jsonl                         # copied BY --session-id
  git.patch  use_detect.json  judge.json
  events.jsonl  exposure.jsonl  probes.jsonl  fact_trace.jsonl
  run_record.json  event_log.jsonl  report.md
  .claim/  .run_done
```

---

## 6. Components

### 6.1 The watcher — `lib/wur/{regions,exposure,events,trace,reconcile}.py`

**Contract:** observe everything, change nothing.

- `regions.py` (~300 LOC) extracts model-visible regions from `stream.jsonl` and `transcript.jsonl` against the closed channel enum (§4.2.2). An unmapped visible region emits `unknown_visible` **and fails CI** — the enum is not allowed to silently drift.
- `exposure.py` (~200) scans regions for nonces → `exposure.jsonl`. **Scan before truncating**, always: a digest computed first would drop a nonce past the cut.
- `events.py` (~300) joins `stream.jsonl` with `gate/tool_calls.jsonl` on `tool_use_id` → `events.jsonl`. `join_coverage ≥ 0.99` is a pilot gate.
- `trace.py` (~300) joins everything with `use_detect.json` → `fact_trace.jsonl`. Raises if `d0-push` carries non-null seq fields.
- `reconcile.py` (~200) orchestrates, idempotently, with atomic `.tmp` + `os.replace` writes. Safe to re-run months later.

**Deleted from v1:** per-tool-call filesystem snapshotting. It had no declared consumer, cost ~100 ms/tool-call, and carried five concurrency bugs (V8). `used` comes from deterministic detectors over the final workspace, the diff, and the ordered Bash commands.

**The driver is the child's parent, not a downstream pipe stage.** A tap in the pipe means a tap crash kills the agent via SIGPIPE and truncates the only copy of the raw stream.

### 6.2 The probe subsystem — `lib/wur/{protocol,cadence,gate,driver,probes}.py`

**Frozen protocol** (`protocol.py`, every string SHA-256'd into `run_meta.json`):

```python
PROTOCOL_VERSION = "wur-probe-v1"

PACING_PROMPT = (
  "TOOL-CALL PACING (operator constraint for this instrumented session): emit AT MOST ONE "
  "tool call per assistant message. Never issue two or more tool calls in the same message; "
  "make one call, wait for its result, then decide the next one. This is a measurement "
  "requirement of the harness running this session, not a limitation of the task."
)

PROBE_TEXT = (
  "CHECKPOINT {probe_id}. Pause the task for one message and answer this.\n\n"
  "List the 3 facts currently active in your workspace. For each: the fact, its source, "
  "and whether it affects your next action.\n\n"
  "Reply with ONE fenced code block tagged json and nothing else inside the fence:\n"
  "```json\n"
  '{{"probe_id":"{probe_id}","facts":['
  '{{"fact":"<one sentence>","source":"<file path | tool call | task prompt>","affects_next_action":true}},'
  '{{"fact":"...","source":"...","affects_next_action":false}},'
  '{{"fact":"...","source":"...","affects_next_action":false}}],'
  '"next_action":"<the single next thing you will do>"}}\n'
  "```\n"
  'Exactly 3 entries in "facts". If fewer than 3 are genuinely active, use the literal string '
  '"NONE" for that entry\'s "fact" and "source". Do not invent a fact or a source. '
  "Do not report facts about this checkpoint mechanism. "
  "After the block, resume the task where you left off without waiting for me."
)
```

Two clauses are load-bearing and must not be edited without re-verification:

- **"Do not report facts about this checkpoint mechanism."** Without it, one run produced the slot `{"fact":"The session enforces one tool call per step…","source":"tool call error messages"}` — the harness had eaten a fact slot. With it: 0 mechanism slots across 13 probes. **Suggestive, not clean** — the pacing prompt changed in the same edit. Ablation owed (S6).
- **"resume the task … without waiting for me"** — keeps the answer mid-turn rather than ending the turn.

`probe_id = "WURP-" + sha256(run_id)[:8] + "-" + f"{k:03d}"`. It cannot occur in a repo and is echoed back, so answer↔probe attribution is exact rather than positional.

**Cadence** (`cadence.py` — the only RNG in the subsystem):

```python
def schedule(task_id, rep, budget, lo=1, hi=3, max_probes=24, salt="wur-v1") -> dict:
    material = f"{salt}|{task_id}|{rep}"
    seed = int.from_bytes(hashlib.sha256(material.encode()).digest()[:8], "big")
    rng = random.Random(seed)
    intervals, fire_at, n = [], [], 0
    while n < budget and len(fire_at) < max_probes:
        iv = rng.randint(lo, hi); n += iv
        intervals.append(iv); fire_at.append(n)
    return {"seed_material": material, "seed_int": seed, "lo": lo, "hi": hi,
            "max_probes": max_probes, "intervals": intervals, "fire_at": fire_at}
```

- Seeded by `(task_id, rep)` **only, deliberately not by arm**, so every arm of the same (task, rep) fires its k-th probe at the same barrier index. **Probe timing becomes a matched covariate.**
- Sampled up front, persisted to `probe_plan.json` before the child starts, so the schedule exists even if the run dies and can be diffed across arms.
- `max_probes = 24`. Unbounded, ~U{1,2,3} intervals over the step budget would imply ~60 probes/run at ~350 output tokens each — ~20k tokens of pure instrument.
- **Never suppress.** If probe k+1's barrier arrives while k is pending, send k+1 anyway and mark k `outcome: "superseded"`. Rollup asserts that for a given (task_id, rep) the `sent_at_barrier` sequences of all arms agree on their common prefix, else `probe_integrity: "schedule_divergence"`.
- Banned everywhere: `Math.random`, `$RANDOM`, module-global `random.*`, time-seeded RNG, `uuid4` as measured randomness.

**The barrier** (`gate.py`, `PreToolUse` matcher `*`, no `post` mode):

1. read payload; `tid = payload["tool_use_id"]`; `parent_tool_use_id` set ⇒ record + `probe_integrity: "sidechain_barrier"` (impossible with `--tools`, V10);
2. `flock` + append to `gate/tool_calls.jsonl`;
3. `WUR_PROBE_MODE == "log"` ⇒ print `{}`, exit 0 — the drop-in replacement for `log_tool_event.sh` on non-WUR claude jobs;
4. else write `gate/req/<tid>.json` atomically and **block**, polling `gate/resp/<tid>.json` every 5 ms up to `WUR_GATE_TIMEOUT_MS` (default 300 000);
5. `deny` ⇒ print the `PreToolUse` deny object; else `{}`;
6. timeout ⇒ print `{}` (**fail open**, never wedge a run on harness failure) and log `gate_timeout`;
7. always `exit 0`, never write stderr.

**Ordering is normative: inject on stdin, THEN release the barrier.** Holding the barrier while waiting for the model to answer **deadlocks** — 20 s held produced zero child output, and the injected message was not even replayed until the hook returned (V13). No gate response may depend on model output. The probe is replayed as a `user` text block immediately *after* the barriered call's `tool_result`, so `events.py` must expect that adjacency when assigning `sent_at_barrier`, and `is_probe_turn` must key off the replayed `probe_id` text, **not** off position.

**Step budget — re-specified.** There is no `--max-turns` in 2.1.222, and deny-with-reason **does not stop a run**: the model treats the deny reason as injection, refuses it, and **re-issues the same tool call, which then succeeds** (V14). So budget stop is *deny every subsequent call* + *close stdin*, accepting one wasted retry per denied call. Two consequences: a denied call costs **two** barrier fires, so `gate/tool_calls.jsonl` ordinals are **not** tool-call ordinals and must be de-duplicated by `tool_use_id`; and `result.permission_denials` is populated, so denials are at least countable.

**Termination.** The child does **not** exit after `result` under `--input-format stream-json` (measured alive 235 s past `result/success`). The driver terminates by **closing stdin** and must **never `wait()` before closing it**, or it hangs forever. Closing stdin mid-turn is a graceful drain: the in-flight turn completes fully, a well-formed `result` is emitted, exit 0 follows ~20 s later (V15).

**Turn-ending after a probe.** Under `--print` the agent sometimes ends its turn after answering (`stop_reason=end_turn`, files never read). The driver sends `RESUME_TEXT` on any zero-tool-call result, capped at 3 consecutive — **identically in the no-probe arm**, so it is not a probed-arm-only intervention.

### 6.3 The fact layer — `lib/wur/{nonce,facts,render,plant}.py`

- `nonce.py` mints via blake2s; `NonceSet` compiles an alternation and asserts (a) nonces are pairwise disjoint and (b) **absent from the pinned baseline tree** — a nonce that already occurs in the repo silently inflates read-rate.
- `facts.py` loads/validates the registry, runs collision and leak checks, and (Tier B) drives `propose → mint → floor → gate → select`.
- `render.py` produces prose / checklist / table from **one canonical fact**, so content is constant and only presentation varies. Length is controlled and reported.
- `plant.py` renders arms → overlays + manifests + `overlay_sha256`, and asserts the plant landed.

**The prior-check gate** (three stages; the original single-stage gate was mathematically unsatisfiable — 0/10 gives a Wilson upper bound of 0.278, above its own 0.25 threshold):

| stage | what it runs against | agent runs |
|---|---|---|
| Gate 1 | pristine base tree | **0** |
| Gate 1b | cross-task finished workspaces + ≥2 near-miss patches | **0** |
| Gate 2 | ≥12 `ctrl` runs; admit at 0 fires (Wilson upper 0/12 = 0.265) | 12 |

### 6.4 Tasks & detectors — `lib/wur/{detectors,detect_use,verify_pack}.py`, `fixtures/ledgerline/`

12 tasks in v1 (16 authored, triaged to 12 by the pilot). One fact each (D1). Fact buckets: constraint, method, ordering, hidden cue.

**Counter-prior requirement:** the mandate must be something a competent agent would *not* do by default, while remaining a legitimate solution. Verified per fact by Gate 1/1b, then Gate 2.

**The fixture ships as a tree plus a deterministic builder**, not as a git repo — `git clone --bare` of a plain subdirectory fails, and the nonce minting needs a stable `repo_sha`:

```
fixtures/ledgerline/tree/        ~7,200 LOC, Python + pytest
fixtures/ledgerline/build.sh     pinned author/committer identity and dates
fixtures/ledgerline/repo_sha.txt the asserted commit SHA
```

**Detectors are mechanical criteria**, so `battery.py` is the execution engine — no new one is written. Two `battery.py` fixes are mandatory: on `pass_condition` eval error return `passed=None` + `"error"` rather than falling back to `exit_code == 0` (a verified silent false PASS), and raise `AC_TIMEOUT_DEFAULT` 60 → 120.

### 6.5 Pipeline & hygiene — `lib/wur/{preflight,canary,schedule,settings}.py`

**Preflight `H1..H12`** → `hygiene.json`, fail-closed, **no API call**. Asserts among others: no ancestor `CLAUDE.md`; registry not reachable from the workspace; settings JSON parses; exactly one `NOTES.md`; plant landed in `baseline_sha`; every plant file ≤ 20 KB.

**Do not assert `agents == []`** — `system/init.agents` is never empty even under full hygiene (`[claude, Explore, general-purpose, Plan, statusline-setup]`), so that assertion fails closed on every run (V18). It is harmless because `Task` is absent from `--tools`. Assert instead that `tools` set-equals the frozen six, and that `mcp_servers` / `skills` / `slash_commands` / `plugins` are empty.

**Every one-shot `claude` invocation passes `< /dev/null`** (preflight, canary, judge, autoload assay). Without it each pays a 3 s stall and writes `Warning: no stdin data received in 3s` to stderr, which a naive `[ -s stderr ]` health check reads as failure (V19).

**The canary** captures verbatim `system/init` per (job, arm) and runs the D0 autoload assay: the same command line with `--tools ""` and a prompt asking for the sentinel, requiring the nonce in the answer for `d0-push` and its **absence** for `d1`/`d2`/`d3`.

**Parallelism** comes from deleting `setup_run.sh:117-145` (the global `~/.claude/settings.json` mutation). Per-run `CLAUDE_CONFIG_DIR` + `--settings` replaces it. `JOBS ≤ 4` verified; 6 is the untested ceiling (S3).

**Two-level model selection.** `lib/agent.py:23-39` `resolve_agent_id` (line 28 hard-pins `claude-sonnet-4-6`) is replaced by a `BACKENDS` registry + `resolve_agent()` + a `resolve` CLI subcommand; `resolve_agent_id` stays as a deprecated shim. `run_job.sh:30-40`'s bash `case` calls `agent.py resolve`. `job.yaml` grows:

```yaml
agent:
  backend: claude          # claude | codex | gemini | agy
  model:   claude-sonnet-5
  effort:  null
```

---

## 7. Experiment matrix

### 7.1 Arms (12)

| id | depth | path | format | push | dist | fact | probe | purpose |
|---|---|---|---|---|---|---|---|---|
| `d0-push` | d0 | `NOTES.md` + `CLAUDE.md`=`@NOTES.md` | prose | import | 0 | ✓ | ✓ | pushed channel |
| `d1-ptr` | d1 | `NOTES.md` + `CLAUDE.md`=prose pointer | prose | prose | 0 | ✓ | ✓ | pointer **without** push |
| `d1` | d1 | `NOTES.md` | prose | — | 0 | ✓ | ✓ | shallowest pulled |
| `d2` | d2 | `docs/NOTES.md` | prose | — | 0 | ✓ | ✓ | **hub cell** |
| `d3` | d3 | `docs/internal/memory/NOTES.md` | prose | — | 0 | ✓ | ✓ | deepest pulled |
| `d2-check` | d2 | `docs/NOTES.md` | checklist | — | 0 | ✓ | ✓ | format |
| `d2-table` | d2 | `docs/NOTES.md` | table | — | 0 | ✓ | ✓ | format |
| `d2-dist` | d2 | `docs/NOTES.md` | prose | — | 3 | ✓ | ✓ | discrimination |
| `ctrl` | d2 | `docs/NOTES.md` (fact-free) | prose | — | 0 | ✗ | ✓ | **primary control** + Gate 2 |
| `ctrl-nofile` | — | no `NOTES.md` | — | — | 0 | ✗ | ✓ | **secondary control** (D3) |
| `d1-np` | d1 | `NOTES.md` | prose | — | 0 | ✓ | ✗ | probe reactivity |
| `d3-np` | d3 | `docs/internal/memory/NOTES.md` | prose | — | 0 | ✓ | ✗ | probe reactivity |

**Filename is `NOTES.md` at every depth**, so filename salience is never confounded with depth. `AGENTS.md` was rejected as the carrier — it is not auto-loaded, yet carries strong "read me" priors from training.

**`d1-ptr` fixes the D0 confound.** The `@NOTES.md` import stub is simultaneously a push mechanism *and* the strongest possible pointer, so a raw `d0-push` vs `d1` contrast varies both. `d1 → d1-ptr` isolates the **pointer**; `d1-ptr → d0-push` isolates the **push**.

**`ctrl` vs `ctrl-nofile` (D3).** `ctrl − treatment` isolates the effect of the fact's *content* with file presence held constant. `ctrl-nofile − ctrl` measures whether a file existing at all changes search behaviour. Reporting both makes the identifying assumption checkable instead of asserted.

**Skeleton matching:** `docs/`, `docs/internal/`, `docs/internal/memory/` exist in **every** arm including both controls, each carrying one nonce-free filler file so git tracks the directory. Exactly one `NOTES.md` per workspace (zero in `ctrl-nofile`).

### 7.2 Run arithmetic

```
tasks T   = 12
arms      = 12   (10 probed, 2 no-probe)
reps R    = 5

probed   = 12 × 10 × 5 = 600
no-probe = 12 ×  2 × 5 = 120
                        ────
main total             = 720

pilot (T = 4, not powered for any contrast)
  ctrl          4 × 12 = 48    # Gate 2 needs n ≥ 12 per (task, fact)
  ctrl-nofile   4 ×  3 = 12
  ctrl-np       4 ×  3 = 12    # difficulty band is defined on ctrl + no-probe
  d1, d2, d3    4×3×3  = 36    # ladder spread, depth-insensitivity check
  d0-push       4 ×  3 = 12    # autoload assay under load
                        ────
pilot total            = 120

GRAND TOTAL            = 840 runs
```

**Cost — all figures UNVERIFIED-MODEL (S1).**

| | cum. input | effective in | output | $ std | $ intro |
|---|---|---|---|---|---|
| probed run | 2.016 M | 524 k | 15,750 | $1.81 | $1.21 |
| no-probe run | 1.290 M | 335 k | 10,500 | $1.16 | $0.77 |

```
main  = 600 × 1.81 + 120 × 1.16 = $1,086 + $139 = $1,225
pilot = 108 × 1.81 +  12 × 1.16 =   $195 +  $14 =   $209
                                                   ──────
                                                   $1,434   ($957 intro pricing)

cumulative input (the number that collides with the five-hour limit):
main  = 600 × 2.016M + 120 × 1.29M = 1,209.6M + 154.8M = 1,364 M
pilot = 108 × 2.016M +  12 × 1.29M =   217.7M +  15.5M =   233 M
                                                          ──────
                                                          1.60 B tokens
```

**Wall clock:** probed ≈ 12.7 min, no-probe ≈ 9.4 min ⇒ main ≈ 146 h serial, **36 h at `--jobs 4`**, 24 h at 6.

Every anchor behind these numbers (1.47 tools/turn, 25–34k `C0`, 12.7 s median latency, `N_tool = 30`) came from an **Opus 5 research session, not a paced Sonnet 5 harness run**. The pilot re-measures all of them from its own `run_record.json` files and the main-run budget is re-derived before launch. `result.total_cost_usd` is recorded per run, so the instrument's own cost becomes a measured quantity.

**Billing:** runs use `env -u ANTHROPIC_API_KEY claude`, i.e. the subscription path. Under a Max plan there is no per-token bill — the dollars are an API-equivalent opportunity cost. **1.60 B cumulative input is the operative constraint.**

**Tokenizer:** Sonnet 5 emits ~30% more tokens than Sonnet 4.6 for the same text. No historical Atlas baseline (all recorded against the `claude-sonnet-4-6` pin) may size this run without rescaling — and see V7, they are inflated 2.3–5× on top of that.

---

## 8. Migration map

### 8.1 Existing files

| path | verdict | what changes |
|---|---|---|
| `run.sh` | **MODIFY** | `--experiment {ladder,wur}`, `--backend`, `--model`, `--conditions`, `--matrix-seed`, `--jobs`. WUR **skips `context_gen.py`** (`:125` — `_compose_env` raises `KeyError` on a non-ladder arm and `rmtree`s a hand-authored overlay on a ladder-named one) and runs the fact/plant/leak-check/canary/gate/schedule chain instead. |
| `install.sh` | **MODIFY** | Default backend `claude`; `.venv-analysis` bootstrap. **System `python3` currently has neither `yaml` nor `jsonschema`** — `install.sh` must run first. |
| `lib/agent.py` | **MODIFY** | `BACKENDS` registry replaces `resolve_agent_id` (`:23-39`, hard pin at `:28`). `config_dir` param on `run_text` (`:160`) so judge and fact authoring run under the same isolation as the task. |
| `lib/jobspec.py` | **MODIFY** | Restructure `validate()` — it `return`s at `:106` on jsonschema success, so the manual block at `:112-144` is **dead on a properly-installed machine**. `_semantic_checks()` must always run. New v2 keys. |
| `lib/run_job.sh` | **MODIFY** | `:30-40` case → `agent.py resolve`. `:53-57` claude `JOBS=1` clamp → backend policy table. `:28` `MULTIENV` → `OVERLAY_MODE`. `:61-64` nested loops → read `schedule.json`. `.claim` dir, `schedule_actual.jsonl`. Sliding window `:122-135` kept. |
| `lib/setup_run.sh` | **MODIFY** | **DELETE `:117-145`** (global settings mutation) — this alone unlocks parallelism. `MULTIENV` gate `:59` → `OVERLAY_MODE`; commit becomes unconditional. Move the `info/exclude` append (`:82-83`) **inside** the `flock` block. Add `claude_home` seeding, settings render + validate, `probe_plan.json`, `SESSION_UUID`. Worktree block `:45-50` and gemini/agy isolation **kept verbatim**. |
| `lib/run_agent.sh` | **MODIFY** | Claude branch `:38-45` → `driver.py`. Keep `env -u ANTHROPIC_API_KEY` and the always-exit-0 contract `run_job.sh` depends on. **Do not** use `${PIPESTATUS[0]}` — the invocation is inside a subshell whose status is captured by `) || AGENT_EXIT_CODE=$?`. codex/gemini/agy branches **kept verbatim**. |
| `lib/teardown_run.sh` | **MODIFY** | `:39` `git diff HEAD` → `git diff $BASELINE_SHA`. New step 1b (`detect_use.py`) strictly **before** grading. `:47-66` newest-mtime transcript glob → lookup **by `--session-id`** + assert. New step 4 (`reconcile.py`). **DELETE `:146-152`** (claude settings restore); **KEEP `:153-157`** (gemini). |
| `lib/extract/adapters/claude_code.py` | **MODIFY** | Add `message_id`; **dedupe `usage` by `message_id`** (V7). `:56` currently drops everything not `user`/`assistant` — add `attachment/queued_command` → `probe_in`, `<system-reminder>`, `isSidechain`. Harvest `toolUseResult` (camelCase on disk; the stream uses snake_case and they are **not** interchangeable). |
| `lib/extract/core.py` | **MODIFY** | New **keyword** arg `probe_events` so the other three adapters keep working via the positional call at `telemetry.py:143`. Add `turns_total` = distinct `message_id` count (`message_count` at `:369` counts lines: 157 vs 36 = **4.36×** on a measured transcript). |
| `lib/battery.py` | **MODIFY** | `:76-79` on eval error return `passed=None`, do **not** fall back to `exit_code == 0` (verified silent false PASS). Timeout 60 → 120. Return `exit_code` + `command`. |
| `lib/judge.py` | **MODIFY** | Export `floor_check()` for reuse. `:211-213` returns **before** the floor-check when `criteria.json` exists, so a hand-written pack is never floor-checked — skip only LLM synthesis, always floor-check. `:355` `met = bool(r["passed"])` → emit `{"met": None}` when `passed is None`. |
| `lib/figures.py` | **MODIFY** | `:84` computes the per-task baseline from `r["env"] == "E0"` only, yielding **0.00× for every arm but one** under a label reading "normalized to E0 = 1.00". Thread `baseline_condition` from `job.yaml`; add a `schema_version` guard that refuses to plot rather than plotting zeros. |
| `viz/server.py` | **MODIFY** | `:261-272` is a **verbatim copy** of `figures.py:81-92` — delete the copy and import it. Add `/api/jobs/<id>/uptake`. |
| `lib/hooks/log_tool_event.sh` | **DELETE** | Forks `python3` **four times per event** (`:34,44,56,62`), truncates input to 120 chars, discards the tool result. Superseded by `gate.py pre` with `WUR_PROBE_MODE=log`. |
| `lib/hooks/settings_template.json` | **REPLACE** → `lib/wur/settings_template.json` | New template: `PreToolUse` + `SessionStart` only, **argv-bound** (survives `env -u` scrubbing), consumed via `--settings`. |
| `lib/brew.sh`, `lib/detect_stack.py`, `lib/ladder.py`, `lib/agy.py`, `visualize.sh`, `templates/job.example.yaml` | **KEEP** | Still exactly right. `ladder.py` keeps `experiment: ladder` alive. |
| `lib/context_gen.py` | **MODIFY** (one line) | `:97` `spec["model"]` → `jobspec._model_of(spec)` so a v2 spec without a top-level `model:` does not `KeyError`. |
| `schemas/job.schema.json` | **MODIFY** | `:7` drop `model` from `required`; `:10` `const "1"` → `enum ["1","2"]`; `:53` model enum → deprecated free string + `agent{}`; `:58-61` `environments` → `conditions[]`. Top-level `additionalProperties: false` at `:8` means every new key must be declared. |
| `schemas/run_record.schema.json` | **MODIFY** | v2. `additionalProperties: false` appears in **four** places (`:8`, `:30` condition, tokens, `:148` raw) — all need explicit additions. |
| `.gitignore` | **MODIFY** | Add `**/claude_home/`, `**/.registry/`, `analysis/*.parquet`. **A credential copy must never be committable.** |

### 8.2 New files (selected)

| path | ~LOC | purpose |
|---|---|---|
| `lib/wur/protocol.py` | 200 | frozen probe/pacing text, `probe_id()`, parse ladder, slot classification |
| `lib/wur/cadence.py` | 60 | seeded schedule; the only RNG |
| `lib/wur/gate.py` | 150 | `PreToolUse` barrier + `SessionStart` marker; fail-open; always exit 0 |
| `lib/wur/driver.py` | 600 | child process, stdin injection, raw capture, gate thread, budget ladder, watchdogs |
| `lib/wur/facts.py` | 500 | registry load/validate/mint/collision/leak; Tier-B pipeline |
| `lib/wur/plant.py` | 400 | conditions → overlays + manifests + `overlay_sha256`; verification |
| `lib/wur/detectors.py` | 500 | the 6-predicate registry + `DetectorContext` |
| `lib/wur/regions.py` | 300 | model-visible region extraction; the closed channel enum |
| `lib/wur/{exposure,events,probes,trace,reconcile}.py` | 1,250 | the derivation chain |
| `lib/wur/{preflight,canary,schedule,validate,aggregate}.py` | 860 | hygiene, assay, randomisation, validation, rollup |
| `schemas/{events,probes,fact_trace,probe_answer}.schema.json` | — | §9 |
| `fixtures/ledgerline/` | ~7,200 | the Tier-A fixture |
| `analysis/uptake_lib.py` | 800 | every statistic, importable and unit-testable outside Jupyter |
| `analysis/uptake.ipynb` | 25 cells | thin cells calling `uptake_lib` |
| `analysis/PREREGISTRATION.md` | — | frozen + git-tagged before main-run data |
| `tests/test_detectors.py` | 400 | 23 base assertions + 7 review misclassifications + compliance corpus |
| `tests/test_wur_lib.py` | 500 | scan-before-truncate, hook-never-blocks, global-settings-untouched, reconcile idempotence, channel-enum closure, join coverage, known-lift recovery, censoring, κ, type-I |

---

## 9. Build phases

Ordered so the riskiest unknown dies first. A red gate **stops the phase** — it does not become a limitation.

| # | phase | size | gate |
|---|---|---|---|
| **0** | Environment + kill-shot spikes | 0.5 d | `install.sh` makes `yaml`/`jsonschema` importable; parquet round-trips; **S2** hygiene under `--setting-sources project` (and CLAUDE.md still autoloads); **S3** 4-way concurrency clean; **S7** deadlock + mid-turn stdin close; **S4** `Read` truncation threshold recorded |
| **1** | Detectors + battery | 1.5 d | 23 base assertions + 7 misclassifications green; `battery.py` no longer false-PASSes on eval error |
| **2** | Fixture | 2 d | `build.sh` reproduces `repo_sha.txt` byte-exactly; `brew.sh` brews it |
| **3** | Pipeline plumbing + isolation | 2 d | `~/.claude/settings.json` byte-identical after a run; 4 concurrent runs clean |
| **4** | Fact layer + plant | 2 d | plant verification 1.00; Gate 1 + 1b pass with 0 agent runs |
| **5** | Probe subsystem | 2.5 d | probe `parse_ok` ≥ 0.90, `refused` == 0, pacing 1/1 in ≥95% |
| **6** | Watcher + reconcile | 2 d | `join_coverage` ≥ 0.99; reconcile idempotent; channel enum closed |
| **7** | Remaining tasks (16 authored) | 4 d | each fact passes Gate 1/1b |
| **8** | Analysis skeleton | 2 d | known-lift recovery on synthetic data; type-I within nominal |
| **9** | **Pilot** — 120 runs | ~1 d wall @ `--jobs 4` | §10, all gates |
| **10** | **Main matrix** — 720 runs | ~36 h wall @ `--jobs 4` | — |

**If S2 fails**, the `d0-push` arm has no mechanism and the pushed/pulled contrast defers to v2. Decide **before** writing any plant code.

---

## 10. Pilot gates (pre-registered; any failure blocks the main run)

| gate | threshold | why |
|---|---|---|
| `confab_rate = P(mention \| ¬read ∧ ¬echoed-before-mention)` | ≤ 0.05 | a nonce cannot be guessed; a breach means it leaked into a prompt, the regex tier over-matches, or a channel is missing |
| **`unexplained_possession`** (D4 alarm) | ≤ 0.05 | thinking-only nonce with no inbound hit — the compensating check for folding thinking into `read` |
| `read_rate(d1)` | ≥ 0.50 | if even the root fact isn't reached, depth has no dynamic range |
| `read_rate(d3)` | ≤ 0.90 | ceiling effect makes the ladder undetectable |
| **depth-insensitive** | NOT (`read(d1) > 0.90 ∧ read(d3) − read(d1) < 0.05`) on ≥60% of tasks | if `docs/` is cheap enough to read exhaustively the ladder measures nothing — a **fixture-wide** failure, remedied by growing `docs/` and re-piloting |
| probe `parse_ok` | ≥ 0.90 | the format must be machine-readable |
| probe `refused` | == 0 | the trusted channel broke |
| `max(tool_uses_per_assistant_message)` | == 1 in ≥95% | pacing is the invariant everything rests on. **Group by `message.id` before counting** — `stream.jsonl` splits one assistant message across lines exactly like the transcript, so a per-line count inherits the V7 bug (V17) |
| `deduped_token_total == result.usage total` | exact | free correctness check; dedupe-by-`message.id` was measured to equal the terminal `result` totals exactly, so any drift means the V7 fix regressed |
| plant verification | 1.00 | every fact-present run's `baseline_sha` contains the nonce |
| `\|φ(used, success)\|` per task | ≤ 0.8 | the fact must be orthogonal to acceptance |
| `ambient_memory` | empty every run | no ancestor `CLAUDE.md` |
| `join_coverage` | ≥ 0.99 | stream↔gate join integrity |

---

## 11. Open spikes

| id | question | cost | consequence if unresolved |
|---|---|---|---|
| **S1** | Re-measure every cost/wall-clock anchor under `claude-sonnet-5` **with the pacing prompt**. | free (falls out of Phase 9) | the $1,434 / 1.60 B / 146 h figures are ±50%; the run could hit the five-hour limit mid-matrix, producing timeouts **non-randomly distributed across arms** |
| **S2** | Does the hygiene recipe hold under `--setting-sources project`? | 30 min | if MCP/skills/slash-commands leak back in, every run carries thousands of tokens of per-machine, per-day context sitting directly upstream of the measurement |
| **S3** | 4-way concurrency against a real repo — shared `~/.claude.json` rewrite, shared bare-repo ODB. | 1 h | if it bites, parallelism evaporates and the main run is 146 h serial — five days, during which a CLI version bump could change the transcript format mid-experiment |
| **S4** | Exact `Read` truncation threshold. A 2,600-line / 55 KB file came through whole. | 30 min | a fact deep in a large `NOTES.md` could be silently outside the delivered window — looks like "read but not used" when it is "never exposed". Mitigated by the 200-line cap. |
| **S5** | Do `Grep`/`Glob` results carry a truncation marker in **model-visible** content? | 30 min | `truncated_by_cli` coverage on the search channels |
| **S6** | Ablate *"Do not report facts about this checkpoint mechanism."* — load-bearing, or was it the pacing prompt? Both changed together. | 2 runs (~$4) | the clause stays either way; the question is whether we can claim it matters |
| **S7** | Does holding the barrier until the answer deadlock? Does closing stdin mid-turn terminate cleanly? | 30 min | the budget termination ladder's step 3 is unvalidated |
| **S8** | Re-run `analysis/power.py` with the **cluster-level** statistic across γ ∈ {0, 0.5, 1.0}; publish realized CI coverage. | 1 h compute | the MDD headline is a guess; the pre-registration cannot be frozen without it |

---

## 12. Statistical commitments

- **Primary inference is the cluster-level paired t on per-task risk differences.** CMH and within-task label permutation are **anti-conservative under task×arm heterogeneity** (type-I 0.094 → 0.188 at γ = 1.0 → 2.0), which is exactly the regime this design expects. CMH is demoted to a secondary sharp-null test and suppressed entirely if γ̂ > 0.5.
- **Task is the unit of generalization.** Reps sample decoding noise, not population. With T = 12, n = 12 for population-level claims.
- **Use is always lift over the paired control.** Raw rates appear only in the appendix.
- **`analysis/PREREGISTRATION.md` is frozen and git-tagged before the main run produces its first byte.** It fixes the hypotheses, the primary/secondary split, the decision rules, and the exclusion policies (weak facts, `read_inbound_only` sensitivity, censoring dispositions).

---

## 13. What this design will not claim

- Anything about "agents" in general. Findings are about Claude Code 2.1.222 / Sonnet 5, under a pacing constraint, on a synthetic fixture, on a given date.
- That the probe is passive. It is an intervention; `d1-np`/`d3-np` bound its size, and every absolute uptake number is conditional on the pacing prompt.
- A memory "half-life" in any cognitive sense. §4.4 measures sustained self-report under repeated elicitation.
- Cross-backend token comparisons. Only Claude is full-fidelity here (V7 also means historical Atlas token figures need rescaling before any reuse).
