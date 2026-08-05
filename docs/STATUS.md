# STATUS — What we observe, what we get, and what it's for

**Last updated:** 2026-08-05
**Phase:** design complete, build not started. Next action is Phase 0 (§9).
**Companion doc:** [IMPLEMENTATION.md](IMPLEMENTATION.md) — how it's built. This doc is about **the data**.

---

## 1. The question, and why the data looks like this

> Not whether the workspace **contains** useful information, but whether the agent **carries it across the boundary into action.**

That sentence dictates the entire data model. We are not measuring "did the task succeed." We are measuring a **funnel**, and every artifact exists to place one run at one point in it:

```
available ──────► read ──────► used ──────► retained
    │               │             │             │
 was it in       did it enter  did it change  did it stay in the
 the tree?       the context?  the output?    agent's self-report?
```

A workspace that is *read but not used* is decorative. A workspace that is *used but not retained* needs re-reading on every turn. A workspace that is *never read* is invisible regardless of quality. These are three different failures with three different fixes, and today nobody can tell them apart. That is the gap this instrument fills.

---

## 2. Trust levels — read this before using any number

Every field carries one of four provenances. **Mixing them is the main way to draw a wrong conclusion.**

| level | meaning | example |
|---|---|---|
| **MEASURED** | observed in bytes the model provably saw | `read` via nonce in a tool result |
| **ASSERTED** | true by construction, verified out-of-band | `read = 1` for `d0-push` (auto-loaded content appears in *no* log; verified by a separate canary run) |
| **DERIVED** | computed from MEASURED inputs by a rule we wrote | `use_rate_cond`, `RMST` |
| **MODELLED** | an estimate, not an observation | every cost and wall-clock figure until the pilot re-measures them |

Anything MODELLED is labelled **UNVERIFIED** at point of use. The cost table in IMPLEMENTATION.md §7.2 is entirely MODELLED right now.

---

## 3. Layer 1 — Raw data (captured, never computed)

Written during the run. Immutable. Every derived table can be rebuilt from these months later after a scanner bugfix.

| file | what it is | why it exists | trust |
|---|---|---|---|
| `stream.jsonl` | **verbatim** child stdout, the full bidirectional stream-json | The ground truth. Everything the model emitted and every tool result it received. This is the only artifact from which exposure can be honestly computed. | MEASURED |
| `transcript.jsonl` | Claude Code's on-disk session file, copied **by `--session-id`** (not by newest-mtime) | **Load-bearing, not a convenience copy.** It is the *only* source of truncation information: `stream.jsonl` carries none at all, so `read_censored` is computable only from `toolUseResult.file.numLines < totalLines` here. Also carries `isSidechain`. Note: on-disk uses camelCase `toolUseResult`, the stream uses snake_case — **not interchangeable.** | MEASURED |
| `gate/tool_calls.jsonl` | one `flock`'d line per tool call at the `PreToolUse` barrier: `{ts, barrier, tool_use_id, tool_name, tool_input}` | The authoritative tool-call ordering and index. The stream alone cannot give a reliable barrier count. | MEASURED |
| `probe_plan.json` | the seeded probe schedule, written **before** the child starts | Lets us diff intended vs realized cadence, and survives a dead run. | MEASURED |
| `run_meta.json` | condition id, factors, seeds, and six hashes of everything the model saw | `cli_argv_sha256`, `probe_text_sha256`, `pacing_prompt_sha256`, `workspace_sha256`, `init_sha256`, `canary_sha256`. A silent edit invalidates cross-run comparison **loudly** instead of quietly. | MEASURED |
| `hygiene.json` | preflight `H1..H12`, fail-closed, no API call | Proves no uncontrolled context reached the model: no ancestor `CLAUDE.md`, registry unreachable from the workspace, exactly one `NOTES.md`, plant landed. | MEASURED |
| `git.patch` | `git diff $BASELINE_SHA` + untracked | The solution. Baseline is the *post-overlay* commit, so injected context never appears in the diff. | MEASURED |
| `use_detect.json` | detector output, run **before anything else touches the tree** | `used`. Ordering matters: grading mutates the workspace. | MEASURED |
| `judge.json` | mechanical battery result | `success`, deliberately independent of `used`. | MEASURED |
| `.registry/conditions/<arm>/context_manifest.json` | verbatim `system/init` per arm | What the model was actually given, before its first tool call. | MEASURED |
| `.registry/conditions/<arm>/canary.md` | model-authored "what was I given" dump | The out-of-band verification for `d0-push`, whose auto-loaded content is invisible in every log. | ASSERTED |
| `watch/persisted/` | copies of `persistedOutputPath` files | A 926 KB Bash stdout reaches the model as a 2,299-char stub. These files are what the model **did not** see — kept for deferred-exposure analysis, never counted as exposure. | MEASURED |

---

## 4. Layer 2 — Derived data (computed offline, idempotent, re-runnable)

Four tables, produced by `wur/reconcile.py`. This is what analysis actually reads.

### 4.1 `events.jsonl` — one row per event

Join of `stream.jsonl` + `gate/tool_calls.jsonl` on `tool_use_id`. Join coverage ≥ 0.99 is a pilot gate.

| field | meaning |
|---|---|
| `seq`, `ts`, `turn_idx` | position; `ts` only where the backend supplies it |
| `type` | `assistant` / `tool_use` / `tool_result` / `probe_in` / `probe_out` / `hook` |
| `is_probe_turn` | **the field that keeps the instrument out of the measurement** — probe turns are excluded from tool counts, phase splits and nav entropy |
| `tool`, `tool_input`, `barrier` | what ran, and its ordinal |
| `result_digest`, `result_bytes` | tool result, digested **after** nonce scanning, never before |
| `nonce_hits[]` | `{fact_id, channel, offset, match_form}` |
| `truncated_by_cli` | the CLI elided content before the model saw it |

### 4.2 `exposure.jsonl` — one row per (fact × exposure event)

| field | meaning |
|---|---|
| `fact_id`, `seq`, `channel` | which fact entered context, when, through which of the 18 enumerated channels |
| `model_visible`, `inbound` | fixed per channel; both are what make the definition honest |
| `match_form` | `exact` / `lower` / `nohyphen` / `regex` — only `exact` inbound hits set first-exposure |
| `bytes_before` | context position at the moment of exposure |

### 4.3 `probes.jsonl` — one row per probe

| field | meaning |
|---|---|
| `probe_idx`, `probe_id`, `sent_at_barrier`, `sampled_interval` | the realized cadence — `probe_id` is echoed back, so answer↔probe attribution is exact, not positional |
| `raw_response` | **always retained verbatim.** Every classifier above this line is re-derivable. |
| `parse_ok`, `parse_tier` | strict JSON / retry / failed |
| `slots[3]` | `{fact, source, affects_next_action, slot_class, match_nonce, match_regex, match_llm, source_verified}` |
| `next_action` | the model's stated next move — the input to **probe fidelity** |
| `outcome` | `answered` / `superseded` / `unanswered` / `refused` |

`slot_class ∈ {critical_fact, task_restatement, generic_workspace, filler, empty, distrusted}`. With one fact per task (D1), ≥2 slots are filler by construction — **the filler distribution is itself a finding**, not noise.

### 4.4 `fact_trace.jsonl` — one row per (run × fact). **The headline table.**

Everything else exists to produce this.

```
── funnel ─────────────────────────────────────────────────────────────
available                 in the planted baseline tree
read                      PRIMARY (D4: inbound ∪ self_thinking)
read_inbound_only         the pre-D4 definition — mandatory sensitivity row
unexplained_possession    thinking-hit with no prior inbound hit → QUARANTINE
opened                    a Read/cat whose resolved target == the planted path
                          (read − opened = incidental-exposure rate)
read_censored             truncation hit a call targeting the fact file
echoed / thinking_echo    the model produced the nonce (not exposure)
used / used_in_diff       detector fired over final state / over the diff only
eligible                  the site the mandate applies to was created at all
── provenance ─────────────────────────────────────────────────────────
exposure_basis            event_stream | manifest_canary  (d0-push is asserted)
first_exposure_seq        null for d0-push; trace.py RAISES if not
exposure_channel          which of the 18 channels got there first
first_used_seq            when provenance became visible in the output
── self-report ────────────────────────────────────────────────────────
ever_mention              named in at least one probe
first_mention_probe       i0 — the retention clock starts here
last_mention_probe        L
mention_run_length        consecutive probes still naming it
n_reinjections            THE DOSE — how many times the probe re-fed the fact
first_use_probe_index     retention is measured strictly BEFORE this
── discrimination (d2-dist only) ──────────────────────────────────────
wrong_value_in_slot       a distractor's token landed in the critical slot
slot_precision            how cleanly the right fact won
── gates and outcome ──────────────────────────────────────────────────
control_fire_rate         the detector's spurious-fire rate, carried per row
prior_check_status        pass | weak | n_a
success / score_automated deliberately independent of `used`
analyzable                false ⇒ excluded, never counted as a miss
exclusion_reason          plant_missing | hygiene_violation | pacing_failed | leaked | …
── cost ───────────────────────────────────────────────────────────────
tool_calls_total          includes probe turns
tool_calls_task           EXCLUDES them — the difficulty-band metric
turns_total               distinct message ids (NOT line count — 4.36× apart)
tokens_*, cost_usd        the instrument's own cost, measured
```

### 4.5 `run_record.json` v2

Backward-compatible extension of the existing schema so the dashboard keeps working. Adds `condition.factors{depth, format, channel, distractors, fact_present, probe, pointer_regime}`, the six hashes, `matrix_seed` / `cell_index` / `run_order_index` / `concurrency_at_launch`, and `tokens.accounting_version`.

> **`accounting_version` exists because of a real defect.** `extract/core.py` sums token usage per transcript *line*, and Claude Code writes one line per content block with a duplicated `usage`. Measured over **116 transcripts**: input inflation median **1.50×**, pooled **2.09×**, max 4.90×; output median 1.94×, max 8.72×. Records written before and after the fix must **never be pooled** — including the existing context-ladder results, which are affected. Dedupe-by-`message.id` was measured to equal the terminal `result.usage` totals **exactly**, so a per-run equality assertion is free and now ships as a gate.

---

## 5. Layer 3 — Metrics, and what each one is actually for

| metric | definition | the question it answers | the decision it drives |
|---|---|---|---|
| **Read rate** | P(nonce entered context) | Is the file reachable at all? | If low at `d1`, nothing deeper matters — fix discoverability before fixing content. |
| **Incidental-exposure rate** | `read − opened` | Did the agent *find* it, or *stumble into* it via grep? | Distinguishes "good docs" from "lucky search". Changes whether you invest in structure or in an index. |
| **Use rate (lift)** | `λ = used(arm) − used(ctrl)`, paired by task | Once it arrives, does it change the output? | **The decorative-workspace test.** High read + zero lift = the file is theatre. |
| **`use_rate_cond`** | `fired / eligible` | Same, excluding runs that never reached the site | Separates "ignored the fact" from "never got that far". Reported *always* alongside the unconditional rate. |
| **Mention rate** | P(named in ≥1 probe \| exposed) | Does the agent hold it in reportable state? | If facts are used but never mentioned, self-report is not a usable proxy — and probe-based tooling is dead. |
| **Retention (RMST)** | area under the survival curve of continued mention, before first use | How long does a fact stay live under repeated elicitation? | Sizes the re-anchoring interval a real workspace needs. |
| **Probe fidelity** | agreement between `affects_next_action` and the actually-issued next tool call | Does the agent know what's driving it? | If high, cheap self-report becomes a legitimate monitoring instrument. If low, introspective agent telemetry is not trustworthy. |
| **Confabulation rate** | P(mention \| ¬read) | Is the instrument lying to us? | An alarm, not a finding. > 0.05 invalidates every exposure-conditioned metric. |
| **`unexplained_possession`** | thinking-hit, no inbound hit | The D4 alarm | Folding thinking into `read` mutes confabulation detection; this restores it. Any hit is hand-audited. |
| **Depth sensitivity** | read/use vs depth, mixed-effects logistic, random intercept for task | Does burying a fact cost you? | Directly prices "how deep can documentation go". |
| **Format sensitivity** | prose vs checklist vs table at fixed depth | Does presentation beat placement? | If format wins, workspaces should be structured for **routing**, not for human readability. |
| **Slot precision** (`d2-dist`) | did the right fact win against confusable distractors | Noise resistance | Prices the cost of a cluttered workspace. |
| **Probe reactivity** | `d1`/`d3` vs `d1-np`/`d3-np` | How much did asking change the answer? | Bounds every other number. Without it nothing here is quotable unprobed. |

---

## 6. How to use it — the questions you can actually answer

**"Is my AGENTS.md doing anything?"**
Read rate tells you if it's opened. Use lift tells you if it changes behaviour. The interesting cell is **high read, zero lift** — the file is read and ignored, which no existing tool can distinguish from "the file helped".

**"How deep can I bury documentation?"**
The `d1 → d2 → d3` read-rate curve is a price list. Combined with `d1-ptr`, you learn whether a pointer buys back what depth costs — a cheaper fix than restructuring a repo.

**"Should I push context or let the agent pull it?"**
`d1 → d1-ptr` isolates the pointer effect; `d1-ptr → d0-push` isolates the push effect. Pushed context has read-rate 1.0 by construction but costs tokens on every single turn. This gives you the exchange rate.

**"Prose, checklist, or table?"**
`d2` vs `d2-check` vs `d2-table` — same fact, same depth, same length, three renderings. If format dominates depth, that reorders the whole workspace-design backlog.

**"How often must a workspace re-assert a fact?"**
The retention curve, read in probe-index units, with the honest caveat that the probe *is* the re-assertion. RMST is the number; the re-injection dose is the covariate that keeps it honest.

**"Can I trust an agent that says it's following a constraint?"**
Probe fidelity. This is the only metric here that generalizes past workspace design and into monitoring.

**Workflow:**

```bash
./run.sh --experiment wur --job jobs/<id>          # runs the matrix
python3 lib/wur/aggregate.py --job-dir jobs/<id>   # → analysis/*.parquet
jupyter lab analysis/uptake.ipynb                  # thin cells over uptake_lib.py
```

The notebook holds no logic. Every statistic lives in `analysis/uptake_lib.py`, importable and unit-testable outside Jupyter, so results are reproducible without a kernel.

---

## 7. The scale of what we're collecting

| | |
|---|---|
| Runs | **840** (120 pilot + 720 main) |
| Design | 12 tasks × 12 arms × 5 reps, **1 fact per task** |
| `fact_trace` rows | 840 (one per run — D1 makes this clean) |
| `probes` rows | ~9,000 (≈15 probes × 600 probed runs) |
| `events` rows | ~40,000 |
| Cumulative input | **~1.60 B tokens** — the number that collides with the five-hour rate limit, not the dollar figure |
| Cost | ~$1,434 API-equivalent (subscription path ⇒ opportunity cost, not a bill) |
| Wall clock | ~36 h at `--jobs 4` |

**All MODELLED.** Every anchor came from an Opus 5 research session, not a paced Sonnet 5 harness run. The pilot re-measures them and the main-run budget is re-derived before launch.

---

## 8. What this data cannot tell you

Stated up front so no one has to discover it in review.

- **Nothing about "agents" in general.** Claude Code 2.1.222 / Sonnet 5, under a pacing constraint, on a synthetic fixture, on one date. Task is the unit of generalization: **n = 12**, not 840.
- **"Retention" is not memory decay.** The probe re-injects the fact every ~2 tool calls. The honest name is *sustained self-report under repeated elicitation*, and that phrase is used in the report.
- **The probe is an intervention.** `d1-np`/`d3-np` bound its size; they do not remove it. Absolute uptake numbers are conditional on both the probe and the pacing prompt.
- **Cross-backend token comparisons are void.** Only Claude is full-fidelity. Codex has no timestamps; Gemini and agy infer file reads by regex over shell strings. And per §4.5, historical Atlas token figures need rescaling before any reuse.
- **Tier B is not in v1.** Generated facts on arbitrary repos is a follow-on job; `tier` is recorded on every row so the two pool later.

---

## 9. Where we are

**Done:** design complete and adversarially reviewed (11 blockers found by running the CLI, all resolved). Seven operator decisions locked (D1 one fact, D4 thinking counts, D3 both controls, plus four taken by default). **Phase 0 complete** — see [SPIKES.md](SPIKES.md).

**Phase 0 result: all four kill-shots PASS. No redesign forced.** Total spend $3.29 across 22 metered runs.

| spike | result |
|---|---|
| **S2** hygiene + `CLAUDE.md` autoload under `--setting-sources project` | **PASS** on both halves — `d0-push` keeps its mechanism |
| **S3** 4-way concurrency, per-run `CLAUDE_CONFIG_DIR` + `--settings` | **PASS** — parallelism is real; ~36 h not 146 h |
| **S4** `Read` truncation | **measured** — hard 256 KB ceiling, and *silent* truncation below it |
| **S7** barrier hold / stdin close | **PASS as a mechanism**, with two ordering constraints the spec had wrong |

All three load-bearing findings independently reconfirmed: **V1** hook probe text refused 6/6 (3/3 `PostToolUse`, 3/3 `PreToolUse` deny) · **V4** pacing held 28/28 tool calls, vs an unpaced control emitting 12 `tool_use` blocks in one message · **V7** mechanism exact, magnitude restated over 116 transcripts.

**Nine measured corrections were folded back into IMPLEMENTATION.md** before any code was written. The four that would have caused real bugs:

1. **Hold-until-answer deadlocks.** The driver must inject on stdin *then* release the barrier, never the reverse.
2. **Deny does not stop a run.** The model treats a deny reason as injection and re-issues the same call, which then succeeds — so the step budget is re-specified, and a denied call costs *two* barrier fires (gate ordinals ≠ tool-call ordinals).
3. **The child does not exit on `result`.** A driver that `wait()`s before closing stdin hangs forever.
4. **The 200-line plant cap does not mitigate anything.** Truncation is content-dependent across a 2.4× spread; a 200-byte-line file was cut at line 108. Now a 20 KB *byte* cap.

**Built:** ~27,000 lines across `lib/wur/` (24 modules), 6 schemas, the `ledgerline` fixture (4,200 LOC + 25 doc files), 4 task packs, the analysis layer, and 3 test files. All 24 WUR modules import; every shell script passes `bash -n`; **99 tests + 16 subtests pass**; the fixture rebuilds to its pinned SHA `6f1f1f73` byte-exactly and its own 156 tests pass; a v1 ladder `job.yaml` still validates.

**Proven end-to-end on real Sonnet 5** ($1.80, 4 WUR cells + 1 ladder cell): the chain `brew → facts → plant → gates → canary → schedule → setup → preflight → driver(+probes) → teardown → detect_use → reconcile → fact_trace → parquet` completes, and every artifact validates against its schema. Measured across all 4 runs: hygiene 12/12 pass, probes **10/10 answered, 0 refused**, `max_tool_uses_per_message == 1`, `join_coverage == 1.0`, `dedupe == result.usage` true, reconcile byte-identical over 3 consecutive runs, and **no WUR artifact leaked into the ladder run**.

### RESOLVED: a pointer lifts read-rate off the floor, and the full funnel now measures

Two `d1-ptr` cells were run live (fact at root `NOTES.md`, prose pointer in auto-loaded `CLAUDE.md` saying *"Read that file before changing code"*). Result, against the existing arms:

| arm | fact | pointer | available | read | opened | **used** | n |
|---|---|---|---|---|---|---|---|
| `ctrl` | ✗ | — | False | False | False | False | 2 |
| `d2` | ✓ at `docs/NOTES.md` | ✗ | True | **False** | False | False | 2 |
| `d1-ptr` | ✓ at `NOTES.md` | ✓ | True | **True** | True | **True** | 2 |

**The whole chain fires, and it fires causally.** In `d1-ptr` the agent read `NOTES.md` at seq 5 (`channel=tool_read`, `form=exact`), then edited `ledgerline/local_reports.py` — the mandated override path — leaving the vendored `ledgerline/reports.py` untouched. In `d2` and `ctrl` it edited `ledgerline/reports.py`, the forbidden file. Same task, same model, same budget; the only difference is whether the agent was told where to look.

This settles three things at once: the instrument measures the full `available → read → used` funnel on live data; the mandate is genuinely **counter-prior** (agents violate it by default in 4/4 fact-absent-or-unread runs); and **the discovery-regime floor is a pointer problem, not a budget problem** — 30 steps did not help, one sentence in `CLAUDE.md` did.

Caveat: n=2 per arm, one task, and `success=False` in both `d1-ptr` runs while grader synthesis reported `floor_ok=False` with a non-discriminating criterion. The battery needs repair before `success` — or the orthogonality gate |φ(used, success)| — is trustworthy.

### The problem this replaced: `read` was 0 at `steps = 30`

Raising the step budget was **necessary but not sufficient**. A live `d2` cell at `steps=30` (20 tool calls, 947k tokens, vs 343k at steps=8) still produced `read = False`. The agent ran `find`, went straight to source files, and **never touched `docs/` at all** — 0 references across the whole run.

This is not a bug. `docs/` is visible at the workspace root, but in the **discovery regime** nothing names `NOTES.md`, and a fully-specified task gives an agent no reason to read documentation when it can read code. Two live `d2` runs (steps 8 and 30) both read nothing.

The consequence is structural: **if `read_rate ≈ 0` across `d1`/`d2`/`d3`, the pulled-depth ladder has no dynamic range** — you cannot measure a decline from zero — and pilot gate G2 (`read_rate(d1) ≥ 0.50`) fails, which the design classifies as a fixture-wide failure. Note the design's stated remedy for depth-insensitivity ("grow `docs/`") addresses the *opposite* failure and would make this worse.

Three candidate responses, in order of cost:

1. **Run the `routing` regime** — a byte-identical `README.md` in every arm whose single path line varies. Read-rate then measures whether the agent *follows a pointer* rather than whether it *searches*. **Not yet implemented:** `discovery_regime` is a declared schema knob, but `plant.py` renders only `pointer_regime` (the per-arm `CLAUDE.md`: `import|prose|none`). A routing README renderer is ~a day of work, not a config change.
2. **Under-specify the tasks** so consulting conventions is required, not optional.
3. **Accept that `d0-push`/`d1-ptr` carry the signal** and report the pulled ladder as a measured floor — itself a publishable result: *unprompted, agents do not read documentation*.

This is exactly what the pilot gates exist to catch, and it cost ~$2 to find instead of surfacing after 840 runs.

### Defects fixed in this pass

| # | Defect | Why it mattered |
|---|---|---|
| 1 | **`trace.py` read `fired`/`fired_in_diff`; `detect_use.py` writes `used`/`used_in_diff`** | Field-name mismatch ⇒ **`used` was `null` on every row of every run**. The funnel's third boundary was silently unmeasured while the detector was firing correctly — a bug that reads as a finding ("the fact is never used"). Now accepts both names. |
| 2 | **`use_evidence` / `use_detector` dropped entirely** | The detector's evidence (`violation: modified ledgerline/reports.py`) was discarded, leaving `used` a bare unauditable boolean. Both now declared in the schema and emitted. |
| 3 | **`probes.py` never emitted `turn_message_ids` / `tool_use_ids`** | `lib/extract/core.py` needs them for `tool_calls_task` (the difficulty-band metric) and silently fell back to `tool_calls_task == tool_calls_total` on every probed run. |
| 4 | **`job.schema.json` required top-level `task` unconditionally** | Multi-task specs only validated *because* `_normalize_tasks` mirrored task 1 back to the top level. Fixing the duplicate-key bug exposed it. Now an `if/then` as §8.1 always specified. |
| 5 | **`--tasks-file` wrote a duplicate top-level `task:`/`accept:`** | `cmd_create`'s `pop` was undone by `_deep_fill`'s mirror, republishing task 1 as if it were the job's task. Mirroring now applies only to genuinely single-task jobs. |
| 6 | **`./run.sh` died on a clean machine** | Every script hardcodes `python3`; `install.sh`'s PEP 668 fallback venv was only on the `exp-runner` wrapper's PATH. One PATH prepend at the entry point fixes all ~40 call sites, plus a loud preflight instead of a failure 200 lines later inside a redirected subshell. |
| 7 | **`exposure.jsonl` had no schema file** | The only emitter validating against an in-module dict. Extracted to `schemas/exposure.schema.json`, loaded from there so a hand-edit cannot drift from what the module emits. |
| 8 | **`discovery_regime` was undeclared** | §7.1 requires it be "a declared knob, not an accident"; the code had reinterpreted `pointer_regime` as the per-arm CLAUDE.md render mechanism (`none\|prose\|import`), leaving discovery-vs-routing unrepresentable. Now a distinct schema key. |
| 9 | **No `templates/job.wur.example.yaml`** | Specified but never created — no way to author a WUR job without copying `jobs/smoke-wur/job.yaml`. Written, with the full 12-arm matrix, and it validates. |
| 10 | **`budget.steps` had no default** | Every new job inherited whatever the author guessed. Now `default: 30` with the measured floor documented. |
| 11 | **`opened` was structurally `False` on every run** | `planted_path_of()` searched the fact card and top-level `run_meta`, but the per-arm path lives at `run_meta.plant.notes_path` and `FactCard` has no path field at all — so it returned `None` and short-circuited. The documented incidental-exposure rate (`read − opened`) therefore always equalled `read`, claiming 100% of exposures were accidental even when the agent opened the file directly after being told to. `plant` is now searched **first**, because a card's `source_path` is per-*fact* (`docs/NOTES.md`) and would mis-resolve every arm but one. |

Also removed a phantom `grade:` key from the template: nothing reads it from the spec (grading is controlled by whether `grader/<task_id>/` exists), so declaring it would advertise a control that does not exist.

**Regression state after the fixes:** 99 tests + 16 subtests pass, all 24 WUR modules import, every shell script passes `bash -n`, reconcile is byte-identical across re-runs, all four derived tables validate, and ladder/single-task/multi-task/WUR job specs all validate.

### Known not-done

- **The visualizer is not updated for WUR.** `viz/server.py` hardcodes `ENVS_ALL = E0..E6` and reads
  `cond.get("env_id","E0")`; `viz/static/index.html` hardcodes the same labels. A WUR job renders its
  arms alphabetically, normalises against the wrong baseline, and shows no uptake funnel. Both files
  are MODIFY in the migration map; the work is not started. Use the analysis notebook meanwhile.
- **The six smoke runs were graded against a broken battery.** C1 was repaired *after* they ran, and
  teardown deletes the workspace, so they cannot be re-graded — their `success` values are void.
  `read` / `opened` / `used` are unaffected (they come from the diff and the stream, not the battery).
- **Only 4 of 12 task packs exist**, and only 1 has been exercised end-to-end.
- **`tasks/export-envelope/` pins a live nonce instead of templating it.** `fact_pack.yaml:51` sets
  `nonce: ZQ-7KDM4TQ2` and three `ref_with_mandate_*.patch` files hardcode the same literal, where
  `tasks/cashflow-report/` correctly writes `{nonce}` and lets `facts.py` substitute. Two
  consequences: the answer key for that task is in git history, and the pin breaks the
  `nonce = f(salt, repo_sha, fact_id)` invariant — rebuild the fixture to a new SHA and every other
  fact's nonce rotates while this one does not. *Not* a runtime contamination risk: the nonce is
  minted against the fixture tree, the agent works in a fixture worktree, and
  `assert_absent_from_repo` still holds there. **Deliberately not fixed here:** the reference
  patches must contain the value to demonstrate compliance, and nothing substitutes `{nonce}` into a
  patch — `verify_pack.py` has no nonce handling and passes patches to the detector runner by path.
  The fix is to substitute at patch-load in that path, then retemplate all four files; retemplating
  first would silently break the pack.
- **Tier B** (generated facts on arbitrary repos) is not implemented — deferred by decision D6.

### Next actions, in order

1. **Decide the read-rate response** (routing regime vs under-specified tasks vs accept-the-floor). Nothing below is worth running until `read` can be non-zero.
2. `judge.py --synthesize` for all 4 tasks (~4 API calls). Until it exists `success` is unmeasured and the orthogonality gate cannot be computed.
3. Add and canary the `d1` and `d3` arms — 4 of 13 pilot gates are unevaluable without them.
4. Run one `d0-push` cell — the only arm whose exposure is *asserted* rather than measured; its live path has never executed.
5. Re-run `verify_pack.py --prior-check` and commit results.
6. Then the 120-run pilot.
