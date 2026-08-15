# RESULTS — what has been measured, what is open, what is not built

This is the honest state of the instrument. It separates three things that are easy to blur: what
has been **measured**, what is **built but unexercised**, and what is **not built**.

Read [GRADER.md](GRADER.md) before quoting any number that derives from `success`.

---

## 1. The instrument measures — demonstrated on live data

Six cells on `claude-sonnet-5`, task `cashflow-report`, fixture `ledgerline`, and reproduced again
on a later two-cell run after every fix in GRADER.md Appendix A:

| arm | fact planted | pointer | available | read | opened | **used** |
|---|---|---|---|---|---|---|
| `ctrl` | ✗ | — | False | False | False | False |
| `d2` (`docs/NOTES.md`) | ✓ | ✗ | True | **False** | False | False |
| `d1-ptr` (`NOTES.md`) | ✓ | ✓ | True | **True** | True | **True** |

The chain fires **causally**. In `d1-ptr` the agent read `NOTES.md` at seq 5
(`channel=tool_read, form=exact`), then edited the mandated override path, leaving the vendored file
untouched. In `d2` and `ctrl` it edited the forbidden file. Same task, same model, same step budget.

Three things this establishes:

- The full `available → read → opened → used` funnel measures on live data.
- The mandate is genuinely **counter-prior** — agents violate it in 4/4 fact-absent-or-unread runs.
  This is the hardest property in the design to get right, and it holds.
- **The discovery-regime floor is a pointer problem, not a budget problem.** Raising the step budget
  from 8 to 30 changed nothing (still `read=False` at 20 tool calls); one sentence in an auto-loaded
  `CLAUDE.md` moved read-rate from 0/2 to 2/2.

On the post-fix run, `retained` also measures (`ever_mention=True`, `retention_censored=False` on
`d1-ptr`) and `success` is a real `True` on both cells — both of those columns were structural
constants beforehand.

---

## 2. The open decision — settle this before anything expensive

In the **discovery regime** (nothing in the workspace names `NOTES.md`), a well-specified coding task
gives an agent no reason to open documentation when it can read code. `read_rate ≈ 0` at `d2`.

If that holds across `d1`/`d2`/`d3`, **the pulled-depth ladder has no dynamic range** — you cannot
measure a decline from zero — and pilot gate G2 (`read_rate(d1) ≥ 0.50`) fails, which the design
classifies as a fixture-wide failure. Note the design's stated remedy for depth-insensitivity
("grow `docs/`") targets the *opposite* failure and would make this worse.

Three candidate responses:

1. **Build the `routing` regime.** Every arm carries a byte-identical `README.md` whose single path
   line names that arm's `NOTES.md`. Read-rate then measures whether the agent *follows a pointer*
   rather than whether it *searches*. **Not implemented** — `discovery_regime` is a declared schema
   knob but `plant.py` only renders `pointer_regime` (`none|prose|import`).
   *Why it is scientifically better, not just a workaround:* in routing the mechanical cost of
   reading is identical at every depth (one `Read` with a full path), so any surviving depth effect
   is **pure salience**, not search cost. Across both regimes you get
   `discovery = search + salience`, `routing = salience`, difference = search cost.
   *Risk:* routing may saturate at read-rate 1.0 and trip gate G3 (`read_rate(d3) ≤ 0.90`).
2. **Under-specify the tasks** so consulting conventions is required, not optional.
3. **Accept the pulled ladder as a measured floor** and report it — *"unprompted, agents do not read
   documentation"* is itself a result.

---

## 3. Built and verified, but not yet exercised at scale

| | state |
|---|---|
| Ladder mode | end-to-end on the fixture (E0 + E6, live agent, 2/2 accepted). E1–E5 compose from the same code path but have not been run live |
| WUR mode | end-to-end on the fixture (`d1-ptr` + `ctrl`, live agent). H1–H12 pass, 12 probes per run, parquet rollup builds |
| Task packs | all 4 ship a frozen battery, each proven two-sided against 4 reference solutions, all lint-clean. **1 of 4 has been run end-to-end with a live agent** |
| Backends | only `claude` is exercised. `codex`, `gemini` and `agy` adapters are untested here — no CLI on the machine |
| Concurrency | claude 4-way measured clean against a real 6,000-file repo (spike S3). Larger matrices unmeasured |

---

## 4. Known limitations

- **The dashboard is not WUR-aware.** `viz/server.py` and `viz/static/index.html` read the ladder's
  `run_record` shape and label environments `E0..E6`. A WUR job renders its arms alphabetically,
  normalises against the wrong baseline, and shows no uptake funnel. Use `analysis/uptake.ipynb`.
- **Tier-(c) mention adjudication is plumbed but dead** — `slots[].match_llm` is always null. Mention
  matching is tier (a) the literal nonce and tier (b) the frozen paraphrase regexes.
- **The `d0-push` arm has never run live.** Its exposure is asserted via the manifest canary and
  unit-tested only, because auto-loaded content appears in no log.
- **Only 4 of 12 designed task packs exist.**
- **Tier B** (generated facts on arbitrary repos) is not implemented — deferred by decision D6.
- **No per-command exit codes.** See [GRADER.md](GRADER.md) §3.1.
- **`analysis/REPORT.md` and `analysis/figs/*.svg` are not produced.** `summarize()` and the five
  `plot_*` functions exist; nothing writes the report.

---

## 5. Data that must not be pooled

- **Every WUR `success` value collected before 2026-08-15 is void** — the column could never be true
  (GRADER.md §A.1). Re-derive with `reconcile.py`; the raw artifacts are unaffected.
- **Every token figure from the original context-ladder study is inflated** by a run-varying factor
  (median 1.50×, pooled 2.09×, max 4.90× on input). `lib/extract/core.py` summed usage per
  transcript *line* while Claude Code writes one line per content block with a duplicated `usage`.
  Fixed by deduping on `message.id`, validated because dedupe-by-id equals the terminal
  `result.usage` exactly. `tokens.accounting_version` (`per_line_v1` / `per_message_v2`) exists so
  the two generations can never be pooled silently.
- **The six original smoke runs' `success` values are void** for a second reason: they were graded
  against a battery that was repaired afterwards. `read`/`opened`/`used` are unaffected — they come
  from the diff and the stream, not the battery.
- **A job whose `schedule.json` was reshuffled mid-flight** mixes frozen designs. Fine for a go/no-go
  probe; not poolable for analysis.

---

## 6. Locked decisions

Operator decisions, not defaults. Do not relitigate them without a reason that is written down.

| id | decision |
|----|----------|
| D1 | **One fact per task.** Two makes probe slots contested and breaks within-run row independence. |
| D2 | Weak facts are kept, pre-registered as excluded from primary. |
| D3 | **Both controls**: `ctrl` (fact-free `NOTES.md`, primary) and `ctrl-nofile` (no file, secondary). |
| D4 | **`self_thinking` counts toward `read`** (primary). `read_inbound_only` is recorded alongside as a mandatory sensitivity, and `unexplained_possession` quarantines a thinking-hit with no prior inbound hit. |
| D5 | Probe-density as a third factor is deferred to v2. |
| D6 | **Tier A only** (synthetic fixture). Tier B is a follow-on. |
| D7 | Hand-label 200 pilot slots to calibrate the slot classifier. |

---

## 7. Next, in order

1. **Decide the read-rate response** (§2). Nothing below is worth running until `read` can be
   non-zero across the depth ladder.
2. Run the remaining three task packs end-to-end. Their batteries are proven; nothing has driven an
   agent through them.
3. Add and canary the `d1` and `d3` arms — 4 of 13 pilot gates are unevaluable without them.
4. Run one `d0-push` cell, the only arm whose exposure is asserted rather than measured.
5. Make the dashboard WUR-aware, or delete the claim that it works for WUR.
6. Then the pilot. **Re-measure every cost anchor from it** — every cost and wall-clock figure this
   project has ever quoted is MODELLED (§8), never observed.

---

## 8. Trust levels

Every number this harness produces carries one of four provenances. **Mixing them is the main way to
draw a wrong conclusion**, so state which one you are quoting.

| level | meaning | example |
|---|---|---|
| **MEASURED** | observed in bytes the model provably saw | `read`, via the nonce appearing in a tool result |
| **ASSERTED** | true by construction, verified out of band | `read = 1` for the `d0-push` arm — auto-loaded content appears in *no* log, so a separate canary run establishes it |
| **DERIVED** | computed from MEASURED inputs by a rule we wrote | `use_rate`, `phi_used_success`, the retention RMST |
| **MODELLED** | an estimate, not an observation | every cost and wall-clock figure, until a pilot re-measures them |

Anything MODELLED must be labelled **UNVERIFIED** at the point of use. `success` is a fifth case worth
naming separately: it is MEASURED, but it measures a *floor* — see [GRADER.md](GRADER.md) §3.
