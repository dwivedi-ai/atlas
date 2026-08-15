# CHANGELOG

Changes that alter a **recorded number** are marked ⚠ — they mean data collected before that date is
not poolable with data collected after it.

---

## 2026-08-15 — grading, hygiene and provenance

Atlas was used as the recording environment for two experiments against real repositories
(`sqlglot`, `pandera`) rather than its own fixture — the first time it had run outside `ledgerline`.
That surfaced a set of defects, and looking for the rest of the class surfaced more. Full write-up
with measurements: [GRADER.md](GRADER.md) Appendix A.

### ⚠ Measurement fixes — these change recorded values

| | |
|---|---|
| **`success` could never be true** | `trace.py::_success_of` matched `("pass","passed","success","ok")`; `judge.py` emits `{accepted, partial, rejected, error, timeout}`. Disjoint. `fact_trace.success` was the constant `False` on every row of every run, and the orthogonality gate `\|φ(used, success)\|` was undefined by construction. Now a tri-state; `partial` with unevaluable criteria is `None`, not `False`. **Every WUR `success` collected earlier is void** |
| **The eval namespace was split across scopes** | `battery.py` passed its safe namespace as eval *locals*, so free names inside a lambda or comprehension raised `NameError` and scored the criterion `None` — "undecided", not "failed". Measured: 3 of 7 criteria on a verified-correct solution. Now one namespace passed as globals; sandbox unchanged and re-asserted by test |
| **`_index/probe_key.json` dropped the paraphrase regexes** | teardown hands reconcile that index *before* `facts.yaml`. Tier (a) is the literal nonce, which a paraphrasing agent never emits, and tier (c) is not wired — so tier (b) was the only live matcher and it was absent. `ever_mention`, `first_mention_seq`, `mention_run_length`, `n_reinjections` and `slot_precision` were empty and `retention_censored` was true for **every run of every arm**. `probe_key_entry()` now spreads `FactSpec.card()` verbatim |
| **`phi_used_success` was never filled** | φ is a per-task property; `trace.py` sees one run. `aggregate.backfill_orthogonality` now fills it at rollup, `None` (never `0.0`) on a degenerate margin |
| **The score's denominator did not reach the analysis table** | `criteria_total` / `criteria_graded` / `criteria_errored` now travel with `score_automated` on the `fact_trace` row |

### Grading

- **Two-sided proof.** A task may declare `reference_patches`; the floor check then proves *fails
  before* **and** *passes after*. `manifest.proof_ok`, per-criterion `two_sided_ok`. Without
  references the manifest records `proof.two_sided: false` instead of implying a proof it never ran.
- **Frozen batteries.** A task may declare `criteria_file`; `judge.py` installs it verbatim and never
  calls the model, but still floor-checks and still proves it. All four shipped packs now ship one,
  each proven against four reference solutions, all lint-clean.
- **`judge.py --floor-check`** — re-prove the criteria on disk, no LLM call.
- **`judge.py --regrade`** — rebuild a finished run's tree from `refs/atlas/baseline-run/<RUN_ID>` +
  `git.patch` and re-score it. Teardown deletes the workspace, so grading used to be a one-shot.
- **Criteria linter** — `sandbox`, `guard_inside_lambda`, `tree_grep`, `double_quiet`,
  `short_circuit` (the last suppressed once the proof is two-sided, which is its own remedy).
- **Grader provenance** — `judge.json` carries `grader.criteria_sha256`; `REPORT.md` grows a
  ⚠ section when a job's runs were scored by more than one battery, or a re-grade disagrees.
- **Acceptance texts now grade what the work orders ask.** Three packs told the agent to add tests
  while the acceptance required only the pre-change count. Guarded by `TaskAndAcceptanceAgree`.
- **No answer key is tracked.** `{nonce}` is substituted at both use sites — detector params
  (`binding_from_entry`) and reference patches (`apply_case`) — so a pack whose mandate is "stamp
  this token" no longer hardcodes it. Guarded by `NoAnswerKeyIsTracked`.

### Hygiene and operations

- **setgid.** A directory created under a setgid parent inherits the bit, and a numeric `chmod 700`
  does not clear it — `claude_home` came out `0o2700` and preflight **H3** failed on every cell.
  Cleared explicitly for `claude_home` and `.registry`.
- **A failed canary is retaken**, not cached. `run.sh` skipped whenever `canary.json` existed,
  including when it recorded a failure, so H12 failed closed on every cell of that arm forever.
  Now: skip only a recorded `pass`, otherwise retake, 3 attempts with backoff.
- **One runner per job.** `run_job.sh` holds `$JOB_DIR/.runner.lock` for the whole matrix,
  PID-liveness checked. Two runners on one job previously produced runs containing nothing but
  `.run_done`.
- **Nonce salt warning.** Every nonce is `blake2s(salt | repo_sha | fact_id)` and the last two are
  public, so at the default salt every nonce is recomputable from a checkout. `run.sh` warns;
  `ATLAS_NONCE_SALT` sets a private one.
- **`EXTRA_STRIP` / `extra_strip:`** — extra paths removed from every arm, for real repositories that
  ship their own orienting docs.
- **`ATLAS_KEEP_WORKSPACE=1`** — keep a run's worktree instead of removing it.
- **The E6/DOX overlay merges `.gitignore`** instead of replacing it. Replacing pushed
  `.pytest_cache/`, `*.egg-info/` and `build/` into `git.patch` **in the E6 arm only** — an
  arm-correlated confound in the diff the experiment measures.
- **`run_job.sh` and `visualize.sh` resolve their own interpreter.** Both are entry points; both died
  or silently degraded on a PEP 668 machine. `visualize.sh` was the worse one — PyYAML's absence was
  swallowed, so the dashboard came up looking fine with every job card missing its spec.
- **`condition_id` declared in `run_record.schema.json`** — it had been emitted since v2 but never
  declared, so every run printed a schema warning. An always-on warning is where a real one goes
  unread.
- **`facts_file` accepts a directory**, the form the template documents.
- **`--tasks-file` passes `criteria_file` and `reference_patches` through.** Dropping them silently
  downgraded a proven pack into a job that synthesized its own unproven battery.
- Documentation corrected: **Claude runs 4-way, not serially** — the global-settings mutation that
  forced serialisation was replaced by per-run `CLAUDE_CONFIG_DIR` + `--settings`.
- `run.sh --help` no longer truncates its last flag; the wizard defaults to `claude` (what
  `install.sh` checks for) and answers `--help`; the dashboard reads the model from a v2 spec.

### Tests

99 + 16 subtests → **158 + 113**. New: `tests/test_grader_proof.py` (the grading defects, the
two-sided proof, re-grading, provenance) and `tests/test_task_packs.py` (the pack contract).

---

## 2026-08-05 — the WUR instrument

Added `--experiment wur` alongside the context ladder: a planted counter-prior fact tracked across
**available → read → used → retained**, with a `flock`'d `PreToolUse` barrier log, verbatim
`stream.jsonl`, per-run `CLAUDE_CONFIG_DIR`, preflight H1–H12, a probe protocol, and the offline
derivation chain (`regions → exposure → events → probes → trace`). Ladder mode unchanged.

⚠ **`lib/extract/core.py` token accounting.** It summed usage per transcript *line* while Claude Code
writes one line per content block with a duplicated `usage`. Measured over 116 transcripts: input
inflation median 1.50×, pooled 2.09×, max 4.90×. **Every token figure the original context-ladder
study produced is inflated by a run-varying factor.** Fixed by deduping on `message.id`;
`tokens.accounting_version` (`per_line_v1` / `per_message_v2`) exists so the two generations can
never be pooled silently.
