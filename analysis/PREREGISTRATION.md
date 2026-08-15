# Pre-registration — Workspace Uptake & Retention (WUR) v1

**Status:** DRAFT until git-tagged. This document is frozen and tagged
`wur-prereg-v1` **before the main run produces its first byte**.
Every hypothesis, every primary/secondary split, every decision rule and every
exclusion below is fixed at that tag. Anything decided afterwards is a
**deviation** and is reported as one, in a section of the report titled
"Deviations from pre-registration", with its date and its reason.

| | |
|---|---|
| Instrument | Atlas, `experiment: wur` |
| Backend | Claude Code `2.1.222`, model `claude-sonnet-5`, `--permission-mode bypassPermissions`, pacing prompt `sha256 0a687ddf…` |
| Design | 12 tasks × 12 arms × 5 reps = **720 main runs**, preceded by a 120-run pilot |
| Unit of generalization | **the task. n = 12, never 840.** |
| Analysis code | `analysis/uptake_lib.py` (`wur-uptake-lib-v1`), `lib/wur/aggregate.py`, `lib/wur/pilot_triage.py` |
| Power | `analysis/power.py` (`wur-power-v1`), frozen output `analysis/power_results.json` |
| Tests | `analysis/test_uptake_lib.py` — 26/26 passing at freeze |

---

## 0. What is being measured

Not whether the workspace **contains** useful information, but whether the agent
**carries it across the boundary into action**, through four boundaries:

```
available ──► read ──► used ──► retained
```

Each boundary is an executable predicate over named artifacts.
No boundary is defined in prose alone, and each one is computed by exactly one
function in `uptake_lib.py`.

---

## 1. Hypotheses

Stated as directional where the design has a direction, and as bounds where the
honest claim is a bound. **H1–H4 are confirmatory. H5–H8 are exploratory** and
are labelled as such in every table they appear in; no exploratory result is
reported as a finding without a follow-up study.

| id | hypothesis | primary estimand | direction | status |
|---|---|---|---|---|
| **H1** | A fact planted in the workspace raises the rate at which the mandated behaviour appears. | λ(d2) = use lift of `d2` over `ctrl`, task-paired | λ > 0 | confirmatory, **the primary hypothesis** |
| **H2** | Depth costs uptake: burying a fact deeper reduces the probability it enters context. | per-task slope of read rate over the pulled ladder d1→d2→d3 | slope < 0 | confirmatory |
| **H3** | Reading is not using. Conditional on exposure, the fact changes the output far less often than it enters context. | P(used \| read) at `d2` compared with P(read) at `d2` | P(used \| read) < P(read) | confirmatory |
| **H4** | A pointer buys back part of what depth costs, and pushing buys more. | λ(d1-ptr) − λ(d1) and λ(d0-push) − λ(d1-ptr), on `read` | both > 0 | confirmatory |
| **H5** | Presentation matters at fixed depth. | contrasts of `d2-check`, `d2-table` against `d2` on `used` | two-sided | exploratory |
| **H6** | Distractors degrade slot precision without degrading use. | `wrong_value_in_slot` and `slot_precision` at `d2-dist` | two-sided | exploratory |
| **H7** | Facts decay out of self-report under repeated elicitation within a run. | ΔRMST(J) between depth arms | two-sided | exploratory |
| **H8** | Probing changes behaviour. | `d1` vs `d1-np` and `d3` vs `d3-np` on `used` and on `read` | two-sided; **a bound, not a null** | exploratory |

**H8 is never interpreted as "the probe is passive."** It bounds the size of the
intervention. Every absolute uptake number in this study is conditional on both
the probe and the pacing prompt, and is reported that way.

---

## 2. Primary vs secondary

### 2.1 Primary metrics (one per boundary)

| boundary | primary metric | definition | function |
|---|---|---|---|
| available | plant verification | `available` = nonce in the planted baseline tree | `pilot_triage.gate_plant_verification` |
| read | **read rate** | P(nonce entered a model-visible region), **D4: inbound ∪ `self_thinking`** | `uptake_lib.read_rate_table` |
| used | **use lift over the paired control** | λ_{a,t} = mean_k used[a,t,k] − mean_k used[ctrl,t,k]; λ_a = mean_t λ_{a,t} | `uptake_lib.use_lift` |
| retained | **RMST over a common horizon J** | ∫₀^J Ŝ(u) du, time in probe index, restricted to probes strictly before first use | `uptake_lib.retention_table` |
| (self-report) | mention rate | P(named in ≥1 probe \| exposed) | `uptake_lib.mention_rate` |

### 2.2 Primary inference

> **The primary test is the cluster-level paired t on per-task risk differences.**
> `uptake_lib.paired_cluster_t`. n = number of tasks present in both arms.

Reps sample decoding noise, not population. A test whose n is 840 is answering a
question nobody asked.

Measured operating characteristics (`analysis/power_results.json`, 4,000
simulations per cell, 12 tasks × 5 reps, τ = 0.5, Monte-Carlo half-width ±0.007):

| γ | type-I (p₀=0.05) | type-I (p₀=0.20) | type-I (p₀=0.50) | realized 95% CI coverage |
|---|---|---|---|---|
| 0.0 | 0.035 | 0.052 | 0.047 | 0.947–0.965 |
| 0.5 | 0.043 | 0.044 | 0.051 | 0.949–0.957 |
| 1.0 | 0.037 | 0.050 | 0.056 | 0.944–0.963 |
| 2.0 | 0.038 | 0.050 | 0.053 | 0.947–0.962 |

The primary test holds its nominal level across the entire heterogeneity range
this design expects, and its interval covers.

### 2.3 Secondary inference, and the rule that switches it off

CMH across task strata and within-task label permutation are **secondary
sharp-null tests**. Both condition on the stratum margins, so both are
anti-conservative under task × arm heterogeneity — measured here at p₀ = 0.50:

| γ | primary paired t | CMH (continuity-corrected) | CMH (uncorrected) | within-task permutation |
|---|---|---|---|---|
| 0.0 | 0.047 | 0.029 | 0.046 | 0.030 |
| 0.5 | 0.051 | 0.043 | 0.068 | 0.043 |
| 1.0 | 0.056 | 0.075 | 0.105 | 0.075 |
| 2.0 | 0.053 | 0.135 | 0.180 | 0.139 |

the design quotes 0.094 → 0.188 as γ goes 1.0 → 2.0. That reproduces
here as **0.105 → 0.180** for the uncorrected CMH at p₀ = 0.50; the
continuity-corrected version runs lower (0.075 → 0.135) and the effect vanishes
into discreteness at p₀ = 0.05 with 5 reps, where every test is conservative.
The direction and the order of magnitude of the claim stand; the exact figures
depend on the base rate and on the continuity correction, and are restated above
rather than repeated.

> **DECISION RULE (pre-registered).** For any contrast where
> **γ̂ > 0.5** (`uptake_lib.gamma_hat`, DerSimonian–Laird over per-task log odds
> ratios), **no CMH and no permutation p-value is reported at all.** The library
> enforces this: both functions return `suppressed=True, p_value=None`. γ̂ is
> reported for every contrast whether or not the rule fires.

### 2.4 Secondary metrics

Incidental-exposure rate (`read − opened`), `use_rate_cond` (fired/eligible),
probe fidelity, slot-class distribution, `read_inbound_only` (see),
post-discharge persistence, depth/format mixed-effects logistic fits, and the
appendix raw use rates. Mention tier (c) (LLM adjudication) is a pre-registered
sensitivity; it becomes primary **only** if κ((a)∨(b), (c)) < 0.6 — decided by κ,
never by which gives a nicer result.

### 2.5 Multiplicity

The **single primary confirmatory contrast is H1: λ(d2) vs `ctrl` on `used`**,
tested at α = 0.05 two-sided. H2–H4 are confirmatory within a family of 9
treatment-vs-control contrasts and are reported with Bonferroni-adjusted
thresholds (α = 0.05/9 = 0.0056) alongside unadjusted intervals. Exploratory
contrasts carry no adjustment and no claim.

---

## 3. What this design can detect (frozen before the data)

`python3 analysis/power.py` — 4,000 simulations per point, bisection to 80%
power, MDD expressed as a **true average risk difference**, τ = 0.5.

| control rate p₀ | γ = 0 | γ = 0.5 | γ = 1.0 |
|---|---|---|---|
| 0.05 (use-rate regime) | **0.194** | **0.207** | **0.240** |
| 0.20 | 0.257 | 0.266 | 0.300 |
| 0.50 (read-rate regime) | 0.262 | 0.269 | 0.287 |

Bonferroni (k = 9): 0.305 / 0.331 / 0.384 at p₀ = 0.05. Realized coverage at the
MDD: 0.940–0.953. Mean 95% CI width at the MDD: 0.27–0.42.

**Consequences, stated now rather than discovered later.**

1. With 12 tasks this design detects **large effects only**. An 8-point lift in
   use rate is *not* detectable at 80% power under any γ. If the true effect is
   in the 5–15 point range, this study will produce a wide interval containing
   zero, and **that is a correctly-reported null, not a failed experiment.**
2. Heterogeneity is expensive: γ = 1.0 costs about 4–5 percentage points of MDD
   relative to γ = 0. It buys nothing back by adding reps — the standard error is
   driven by between-task variance, so **more reps per task do not help; more
   tasks do.** If a future version needs to halve the MDD, it needs ~4× the tasks.
3. The MDD is quoted on the risk-difference scale because that is the scale the
   finding is quoted on.

Re-running against pilot data (`python3 analysis/power.py --pilot-dir
jobs/<id>/analysis --outcome used --arm d2 --control ctrl`) recomputes all of the
above from the pilot's own (p₀, τ, γ̂). **The pilot's re-run is advisory: it may
change the expected precision, and it may not change any hypothesis, any primary
metric or any decision rule.**

---

## 4. Exclusion policy

Every exclusion below is decided **now**. `uptake_lib.analysis_frame` implements
them and returns an `ExclusionReport` that is printed above every table; a run
that leaves the analysis leaves it with a named reason.

### 4.1 Run-level exclusions (`analyzable = false`)

A run is EXCLUDED, never counted as a miss. The distinction is load-bearing: a
plant that did not land is not evidence that the agent ignored the fact.

`plant_missing` · `hygiene_violation` · `pacing_failed` · `leaked` ·
`probe_integrity` · `schedule_divergence` · `gate_timeout` · `join_coverage_low` ·
`unexplained_possession` · `ordering_violation` · `confabulation` ·
`detector_error` · `agent_error` · `timeout` · `other`

Quarantined rows (`quarantined = true`) are excluded from the **primary frame**
and **kept in the alarm frame**. This is not a contradiction: a control run that
names the nonce is quarantined by construction *and* is the entire numerator of
the confabulation rate. Dropping it everywhere would make the alarm read 0.00 on
exactly the failure it exists to catch.

### 4.2 `read` is three-valued, and unknown is not false

`read` ∈ {true, false, **null = unknown**}. Null means a truncation or the hard
256 KB `Read` ceiling hit a call targeting the fact file.

> **Unknown reads leave the denominator. They are NEVER coerced to false.**

Wide searches truncate, and deep facts are found by wide searches — so coercing
unknown to false would make the bias run *with* the hypothesis. Every rate table
reports `n_unknown` and the bracketing pair `rate_if_unknown_false` /
`rate_if_unknown_true`. If that bracket is wide enough to change a conclusion,
the conclusion is not drawn.

### 4.3 `read_inbound_only` is a mandatory sensitivity row

D4 folds `self_thinking` into `read`. That mutes confabulation detection, so:

- **primary** analysis uses `read`;
- `read_inbound_only` (the pre-D4 definition) is reported in **every** table
  where `read` appears as a denominator — `read_rate_table` emits both rows and
  cannot be asked for only one;
- `unexplained_possession` (a thinking hit with no prior inbound hit) is the
  compensating alarm. Any true row is quarantined and hand-audited. **A pilot
  rate above 0.05 is a fixture-wide failure, not a data point.**

If the primary and the inbound-only conclusions disagree in sign for any
confirmatory hypothesis, **the disagreement is the headline** and the hypothesis
is reported as unresolved.

### 4.4 Weak facts are excluded from primary — decided now, not later

D2: facts that fire 1/12 in control are **admitted to the suite** (they preserve
suite size) and **excluded from every primary analysis**
(`prior_check_status == "weak"` ⇒ dropped by `analysis_frame`). They are reported
in a sensitivity table with `exclude_weak_facts=False`.

The exclusion is pre-registered precisely because deciding it after seeing
treatment data is a forking path.

A fact is also discarded outright, with the same disposition as a failed
prior-check, if **|φ(used, success)| > 0.8** on its task: the battery would then
be testing the mandate, `success ≡ used`, and the funnel would have collapsed to
one measurement.

### 4.5 Censoring dispositions (retention only) — all three

Retention is defined on `{ever_mention = 1}` only, in probe-index units, and
**restricted to probes strictly before first use**.

| case | disposition | `censoring_reason` |
|---|---|---|
| run completes while still mentioning | administratively censored at B_r = P_r − i₀ | `administrative` |
| run times out or errors mid-flight | censored at the last **observed** probe | `truncated_run` |
| lapse occurs at or after first use | censored at the pre-use horizon; the remainder is reported separately as **post-discharge persistence** | `post_discharge` |
| fact never mentioned | **EXCLUDED from retention entirely** — fully accounted for by the mention rate | `never_mentioned` |
| fact used before it was ever mentioned | excluded: nothing was at risk | (row absent) |

**J** = min over arms of the largest time at which ≥10% of that arm's retention
subset is still at risk. **If J < 3 probes, retention is descriptive only (KM
curves) and ΔRMST drops to exploratory** — `RetentionResult.exploratory` carries
the flag and the note is printed with the table.

**The RMST reference arm is not the control.** `ctrl` is fact-free, so it can
never mention the fact and contributes zero retention rows; ΔRMST is a
within-treatment contrast (default reference `d1`) and is reported as such, never
as a control-adjusted lift.

Intervals for RMST and ΔRMST come from a **cluster bootstrap over tasks**, not
from `lifelines`' `return_variance` — that returns E[T²] − E[T]², the variance of
the survival-time *distribution*, not the sampling variance of the estimator
(measured: 2.48 against a bootstrap SD of 0.16 on the same fit). It is reported
under the name `sd_survival_time` and is never used as a standard error.

### 4.6 Trust levels are never pooled silently

`d0-push` exposure is **ASSERTED** (`exposure_basis = manifest_canary`,
verified by the autoload canary), not MEASURED — auto-loaded `CLAUDE.md` content
appears in no log. Every rate table carries a `trust` column, and a
`manifest_canary` row is excluded at schema level from every seq-based aggregate.

Token figures are never pooled across `tokens_accounting_version` generations:
`per_line_v1` input is inflated 1.00×–4.90× (median 1.50×, pooled 2.09×) against
`per_message_v2`. `aggregate.py` emits the census and warns; `pilot_triage.G9`
fails the job.

---

## 5. Decision rules

### 5.1 Gate: may the main run start?

The pilot's 13 gates are evaluated by `python3 lib/wur/pilot_triage.py
--job-dir jobs/<pilot> --markdown`. **Any failure blocks the main run.** A gate
whose input is missing reports `unevaluable` and does not block by itself;
`--require-all` makes it block, and the main run is launched with `--require-all`.

| id | gate | threshold |
|---|---|---|
| G1 | `confab_rate` = P(mention \| ¬read ∧ ¬echoed) | ≤ 0.05 |
| G2 | `unexplained_possession` | ≤ 0.05 |
| G3 | `read_rate(d1)` | ≥ 0.50 |
| G4 | `read_rate(d3)` | ≤ 0.90 |
| G5 | depth-insensitive ceiling on ≥60% of tasks | < 0.60 |
| G6 | probe `parse_ok` | ≥ 0.90 |
| G7 | probe `refused` | == 0 |
| G8 | `max_tool_uses_per_message == 1`, share of runs | ≥ 0.95 |
| G9 | `deduped_token_total == result.usage` | exact, every run |
| G10 | plant verification | == 1.00 |
| G11 | max_t \|φ(used, success)\| | ≤ 0.80 |
| G12 | `ambient_memory` | empty, every run |
| G13 | `join_coverage` | ≥ 0.99, every run |

Remedies are pre-assigned so that a red gate cannot be argued into a limitation:

- **G1 breach** ⇒ the nonce leaked, the tier-(b) regex over-matches, or a channel
  is missing. Every exposure-conditioned metric is invalid. Fix and re-pilot.
- **G2 breach** ⇒ fixture-wide failure. Hand-audit every flagged run.
- **G3 breach** ⇒ discoverability, not depth, is the binding constraint; the
  ladder has no dynamic range. Re-author the fixture.
- **G4 or G5 breach** ⇒ `docs/` is cheap enough to read exhaustively. Grow it and
  re-pilot. **Not** a finding about depth.
- **G6/G7 breach** ⇒ the trusted stream-json channel broke. Probe-derived metrics
  are void for affected runs.
- **G8 breach** ⇒ pacing failed; turn boundaries are no longer tool-call
  boundaries and the cadence claim collapses.
- **G9 breach** ⇒ the V7 fix regressed. All token figures void.
- **G11 breach** ⇒ discard the fact.

### 5.2 Reading a confirmatory result

For each confirmatory hypothesis, the report states, in this order: the
task-level point estimate, its 95% cluster interval, its paired-t p-value, γ̂,
n_tasks, and the exclusion counts. A hypothesis is:

- **supported** if the interval excludes 0 in the hypothesized direction *and*
  the effect exceeds the MDD's precision floor (i.e. the study was powered to see
  it);
- **not supported** if the interval contains 0. A null is reported as a null with
  its interval; **"trending" is not a verdict** and the word does not appear;
- **inconclusive** if the interval contains 0 *and* is wider than the MDD at the
  observed γ̂ — the study could not have seen the effect it was looking for, and
  that is reported as a limit of the design, not as evidence of absence;
- **unresolved** if the primary and the `read_inbound_only` sensitivity disagree
  in sign.

### 5.3 Stopping and re-running

No interim analysis of the main run. The matrix runs to completion or to a
resource limit; there is no data-dependent stopping rule, and no arm is added,
dropped or re-run on the basis of its own result. A run that fails for harness
reasons is re-run **only** when its `exclusion_reason` is in
{`gate_timeout`, `agent_error`, `timeout`} and the re-run is recorded with its
own `run_order_index`.

---

## 6. What will not be claimed

- Nothing about "agents" in general. Claude Code 2.1.222 / Sonnet 5, under a
  pacing constraint, on a synthetic fixture, on one date. **n = 12 tasks.**
- That the probe is passive. It is an intervention; H8 bounds it.
- A memory "half-life" in any cognitive sense. measures **sustained
  self-report under repeated elicitation**, and the report uses that phrase.
- Cross-backend token comparisons. Only Claude is full-fidelity here.
- Anything about Tier B. v1 is the synthetic fixture only; `tier` is on every row
  so the two pool later.

---

## 7. Freeze record

| item | value at freeze |
|---|---|
| `uptake_lib` version | `wur-uptake-lib-v1` |
| `power.py` version | `wur-power-v1`, seed 20260805, 4,000 sims/point |
| `aggregate.py` version | `wur-aggregate-v1` |
| `pilot_triage.py` version | `wur-pilot-triage-v1` |
| protocol | `wur-probe-v1`, `pacing_prompt_sha256 = 0a687ddfc2f3374378188c2aacde2b5f5d2d97504a63e27dc88a8b9cfcbe249b` |
| analysis tests | `analysis/test_uptake_lib.py`, 26 passed / 0 failed |
| power results | `analysis/power_results.json` |

```bash
git tag -a wur-prereg-v1 -m "WUR v1 pre-registration frozen"   # BEFORE the main run
```
