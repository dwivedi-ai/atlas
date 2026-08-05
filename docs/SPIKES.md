# SPIKES — Phase 0 kill-shot measurements

**Date:** 2026-08-05
**Backend under test:** `claude` 2.1.222, model `claude-sonnet-5`, subscription path (`env -u ANTHROPIC_API_KEY`)
**Scope:** IMPLEMENTATION.md §9 Phase 0 gate — S2, S3, S4, S7 — plus personal re-verification of the three
load-bearing findings V1, V4, V7 (STATUS.md §9).
**Method:** every claim below is the output of a command that was actually run. No claim here is reasoned
from documentation. Raw artefacts live in
`/tmp/claude-1000/-home-coder-experiments-atlas/ede73901-4a1c-41d7-9967-e51038f6f288/scratchpad/p0/`.
**Total spend:** $3.29 across 22 metered `claude` runs (+2 unmetered driver runs). Median run $0.06.

| id | question | verdict |
|---|---|---|
| **ENV** | `yaml` / `jsonschema` importable; parquet round-trips | **PASS** (via scratch venv — see caveat) |
| **S2** | hygiene holds under `--setting-sources project` **and** workspace `CLAUDE.md` still autoloads | **PASS (both halves)** |
| **S3** | 4-way concurrency with per-run `CLAUDE_CONFIG_DIR` + `--settings` | **PASS** |
| **S4** | `Read` truncation threshold | **MEASURED** — and it is *not* a line cap; the spec's mitigation is in the wrong unit |
| **S7** | barrier hold/release; mid-turn stdin close | **PASS**, with two mandatory ordering constraints the spec does not state |
| **V1** | hook-delivered probe text refused as prompt injection | **RECONFIRMED** 6/6 (3/3 each channel) |
| **V4** | `PACING_PROMPT` ⇒ one `tool_use` per assistant message | **RECONFIRMED** 28/28 |
| **V7** | `extract/core.py` double-counts tokens per transcript line | **RECONFIRMED** — mechanism exact, **quoted magnitude wrong** |

---

## ENV — environment bootstrap

`install.sh` was **not** run. On this machine the system python3 is PEP 668 externally-managed:

```
$ python3 -m pip install --user -q -r requirements.txt
error: externally-managed-environment
...
hint: See PEP 668 for the detailed specification.
$ python3 -c "import yaml"
ModuleNotFoundError: No module named 'yaml'
```

So `install.sh` would fall through to its third branch and create **`$REPO/.runner-venv`**. That path is a repo
file this agent does not own, and — see FINDING E1 — it is **not in `.gitignore`**. Route taken instead: a venv in
scratch.

```
$ python3 -m venv <scratch>/p0/venv && <scratch>/p0/venv/bin/pip install -r requirements.txt
$ <scratch>/p0/venv/bin/python -c "import yaml,jsonschema,matplotlib,numpy"
VENV OK yaml 6.0.3 jsonschema 4.26.0 numpy 2.5.1
```

Parquet round-trip (the other half of the Phase 0 gate) — `pyarrow` + `pandas` installed into the same venv:

```
PARQUET ROUNDTRIP OK: True
{'fact_id': StringDtype, 'read': int64, 'first_exposure_seq': float64, 'channel': StringDtype}
```

Note the dtype: a nullable integer column (`first_exposure_seq`, which is `null` for every `d0-push` row)
round-trips as **float64**, not as a nullable Int64. `aggregate.py` must set `dtype="Int64"` explicitly or
`trace.py`'s "raises if `d0-push` carries non-null seq fields" assertion will be comparing against `NaN`.

---

## S2 — hygiene under `--setting-sources project`  **PASS**

### S2a — the init assertion

Scratch git repo at `<scratch>/p0/s2/ws`, containing `CLAUDE.md` (with sentinel), `README.md`, `docs/filler.md`.
`CLAUDE_CONFIG_DIR` seeded with **`.credentials.json` and nothing else** (`chmod 700` dir, `chmod 600` file):

```
$ ls -la <scratch>/p0/s2/home
drwx------ 2 coder coder 4096 .
-rw------- 1 coder coder  509 .credentials.json
```

Exact command (cwd = the workspace):

```bash
env -u ANTHROPIC_API_KEY CLAUDE_CONFIG_DIR="$S/s2/home" claude \
  --print --output-format stream-json --verbose \
  --setting-sources project --strict-mcp-config --disable-slash-commands \
  --tools Bash,Read,Write,Edit,Glob,Grep \
  --settings "$S/s2/settings.json" \
  --permission-mode bypassPermissions --model claude-sonnet-5 \
  "Reply with exactly: OK"
```

`system/init`, verbatim:

```json
{
  "agents": ["claude", "Explore", "general-purpose", "Plan", "statusline-setup"],
  "apiKeySource": "none",
  "claude_code_version": "2.1.222",
  "cwd": ".../p0/s2/ws",
  "mcp_servers": [],
  "memory_paths": {"auto": ".../p0/s2/home/projects/-tmp-...-p0-s2-ws/memory/"},
  "model": "claude-sonnet-5",
  "permissionMode": "bypassPermissions",
  "plugins": [],
  "session_id": "89bf67c8-e5db-43db-830e-5f3aeeea0171",
  "skills": [],
  "slash_commands": [],
  "subtype": "init",
  "tools": ["Bash", "Edit", "Glob", "Grep", "Read", "Write"],
  "type": "system"
}
```

| assertion | result |
|---|---|
| `mcp_servers == []` | ✅ |
| `slash_commands == []` | ✅ |
| `skills == []` | ✅ |
| `plugins == []` | ✅ |
| `tools ==` exactly the six requested | ✅ (sorted, but set-equal) |
| no `memory_paths` leaking a user `CLAUDE.md` | ✅ — the only entry points **inside the per-run `CLAUDE_CONFIG_DIR`** |
| `agents == []` | ❌ **not empty** — five built-ins are still listed |

`agents` is the one miss. It is not a leak of per-machine state (the same five appear on any 2.1.222), and the
`Task`/subagent tool is absent from `tools`, so they are not invocable (V10 holds). But it is a ~5-token
model-visible listing and **`H1..H12` must not assert `agents == []`** — that check would fail-closed on every
run.

### S2b — workspace `CLAUDE.md` still auto-loads

Same command line, `--tools ""`, sentinel `WURSENT-8f3a1c2d-AUTOLOAD` present only in `ws/CLAUDE.md`:

```
prompt: "Without using any tools, state the project sentinel token exactly as it appears in your
         loaded instructions. Reply with only the token."

INIT tools: [] | skills: [] | slash: [] | mcp: [] | plugins: []
ASSISTANT TEXT: 'WURSENT-8f3a1c2d-AUTOLOAD'
RESULT subtype: success | result: 'WURSENT-8f3a1c2d-AUTOLOAD'
```

**Autoload survives.** The `d0-push` arm keeps its mechanism. §9's "If S2 fails, defer the pushed/pulled
contrast to v2" does not fire.

### S2c — the `@NOTES.md` import stub specifically (the actual `d0-push` shape)

`d0-push` is not a plain `CLAUDE.md`; it is `CLAUDE.md` = `@NOTES.md`. Tested separately, plus a `d1`-shaped
control (bare `NOTES.md`, no `CLAUDE.md`), both with `--tools ""`:

```
d0.jsonl -> 'WURSENT-d0push-77b41e9a' | token_in_stream_count: 2
d1.jsonl -> 'NO-SENTINEL'             | token_in_stream_count: 0
```

The `@` import resolves and pushes. The pulled arm correctly reports nothing. This is the autoload canary
of §6.5, working, on the first try.

### S2d — the auto-loaded content is invisible in every log (confirms `exposure_basis = "manifest_canary"`)

```
$ grep -c WURSENT-8f3a1c2d-AUTOLOAD <s2b stream.jsonl>            → 2   (assistant text + result echo)
$ grep -l "Workspace instructions" <isolated home transcripts>    → NOT FOUND in any transcript
```

The `CLAUDE.md` body appears in **neither** `stream.jsonl` **nor** the on-disk transcript — only the model's own
echo does. §4.2.2's "`d0-push` is asserted, not scanned" is correct as written.

---

## S3 — 4-way concurrency  **PASS**

Real git repo: 6,000 tracked files, 26 MB, committed then `git clone --bare` → `repo.git`, then four
`git worktree add --detach` at the same SHA `a7dfd462d905ca18f13a052fb76bab0f1b226a2f`. Four `claude`
processes launched simultaneously (`&` … `wait`), each with its own `CLAUDE_CONFIG_DIR`, its own
`--settings` file, its own `--session-id`, cwd = its own worktree. Task made 2 tool calls each
(`Glob` then `Write`).

```
=== exits ===
run0 exit=0 lines=10   run1 exit=0 lines=10   run2 exit=0 lines=12   run3 exit=0 lines=12
=== results ===
run0 success | is_error False | turns 3 | result 'DONE' | cost 0.060125
run1 success | is_error False | turns 3 | result 'DONE' | cost 0.0601679
run2 success | is_error False | turns 3 | result 'DONE' | cost 0.060109
run3 success | is_error False | turns 3 | result 'DONE' | cost 0.060114
=== per-home transcript counts ===
home0 transcripts=1  want=12345678-...-cde0.jsonl  got=12345678-...-cde0.jsonl
home1 transcripts=1  want=12345678-...-cde1.jsonl  got=12345678-...-cde1.jsonl
home2 transcripts=1  want=12345678-...-cde2.jsonl  got=12345678-...-cde2.jsonl
home3 transcripts=1  want=12345678-...-cde3.jsonl  got=12345678-...-cde3.jsonl
```

Global-state integrity, sha256 before / after the four concurrent runs:

```
BEFORE  2bde6f4934900eb61d73ca8f42032bffc35940276a607614f623b9398a86d8e6  ~/.claude/settings.json
AFTER   2bde6f4934900eb61d73ca8f42032bffc35940276a607614f623b9398a86d8e6  ~/.claude/settings.json
BEFORE  89a378b36cdf77686831db0c2e874d2735f31e4689988ef9a24624f5380a7ab8  ~/.claude.json   (47912 B)
AFTER   89a378b36cdf77686831db0c2e874d2735f31e4689988ef9a24624f5380a7ab8  ~/.claude.json   (47912 B)
~/.claude.json parses OK after; keys= 51
```

Byte-identical, not merely valid. Each isolated home grew its **own** `.claude.json`:

```
home0 contents: backups .claude.json .credentials.json .last-cleanup projects sessions
```

so `CLAUDE_CONFIG_DIR` isolates the mutable global config file too — the shared-`~/.claude.json`-rewrite risk
named in §11 S3 does not exist once every run has its own config dir. Re-checked at the very end of all
spikes: `~/.claude/settings.json` still `2bde6f…`, `~/.claude.json` still parses, and no `p0` project
directory leaked into the real `~/.claude/projects/`.

Two incidental observations:

- `run3` wrote `99` where the others wrote `100`. That is model counting variance on a `Glob` listing, not a
  concurrency defect — the tool results were independent per worktree. It is, however, a live reminder that
  detector predicates must never depend on a model-reported count.
- Every run emitted this on **stderr**: `Warning: no stdin data received in 3s, proceeding without it.`
  Three seconds of dead time per run and a non-empty stderr that a naive `[ -s stderr ]` health check would
  read as failure. Irrelevant for the real driver (which uses `--input-format stream-json`), relevant for the
  canary/preflight one-shot invocations, which must pass `< /dev/null`.

---

## S4 — `Read` truncation threshold  **MEASURED (and the spec's mitigation is in the wrong unit)**

Method: synthetic files with a `SENT-…` sentinel on the **last** line; the model is told to call `Read` exactly
once per path with **only** `file_path` (never `offset`, never `limit`) and never to re-read. Truncation is read
off the `tool_result` body directly (last line number delivered) and cross-checked against the transcript's
`toolUseResult.file.{numLines,totalLines}`.

### Result 1 — a hard ceiling at 256 KB, delivered as a tool **error**

```
READ w40.txt        file 819,968 B → is_error=True, 197 chars, no content at all
READ b200x2000.txt  file 388,914 B → is_error=True, 197 chars, no content at all

verbatim body: 'File content (800.8KB) exceeds maximum allowed size (256KB). Use offset and limit
                parameters to read specific portions of the file, or search for specific content
                instead of reading the whole file.'
```

No `toolUseResult` sidecar is written for this path at all (consistent with V6's "`PostToolUse` does not fire
when a tool errors").

### Result 2 — below 256 KB, silent truncation, with **no model-visible marker whatsoever**

| file | bytes | lines | line width | delivered lines | delivered raw B | rendered chars | sentinel arrived |
|---|---|---|---|---|---|---|---|
| `a2600.txt` | 20,803 | 2,601 | 8 | 2,601 | all | 32,701 | ✅ |
| `n400x80.txt` | 31,933 | 401 | 80 | 401 | all | 33,429 | ✅ |
| `a4000.txt` | 32,003 | 4,001 | 8 | 4,001 | all | 50,901 | ✅ |
| `n200x200.txt` | 39,814 | 201 | 200 | 201 | all | 40,510 | ✅ |
| **`n800x80.txt`** | **63,933** | **801** | **80 (English prose)** | **645** | **~51,500** | **54,071** | **❌** |
| `a10000.txt` | 80,004 | 10,001 | 8 (digits) | 5,311 | 42,488 | 67,935 | ❌ |
| `n1500x80.txt` | 119,934 | 1,501 | 80 (English prose) | 644 | ~51,400 | 53,987 | ❌ |
| `w40small.txt` | 163,969 | 4,001 | 40 (`q`×32 filler) | 574 | 22,960 | 25,721 | ❌ |
| `w200.txt` | 241,009 | 1,201 | 200 (`r`×192 filler) | 108 | 21,600 | 22,031 | ❌ |

The tail of the truncated `a10000.txt` body, verbatim — note there is nothing after it:

```
...\n5309\tL005309\n5310\tL005310\n5311\tL005311
```

No ellipsis, no `[truncated]`, no `<persisted-output>`, no count. The model in run S4-A *inferred* truncation
from the filename (`a10000` vs "last line number 5311") and issued a recovery `Read` with an offset. A fact
sitting past the cut is simply **absent, silently**.

The only machine signal is in the transcript sidecar:

```
{'filePath': 'a10000.txt', 'numLines': 5311, 'startLine': 1, 'totalLines': 10001, 'truncated': None}
{'filePath': 'a4000.txt',  'numLines': 4001, 'startLine': 1, 'totalLines': 4001,  'truncated': None}
```

**`truncated` is `None` on every single read, truncated or not.** It is not a usable field. The only usable
predicate is `numLines < totalLines`.

### Result 3 — the cut point is content-dependent, deterministic, and is neither a line cap nor a byte cap

Cut points measured: **21,600 B** (200-byte lines of repeated `r`), **22,960 B** (40-byte lines of repeated `q`),
**42,488 B** (8-byte digit lines), **~51,500 B** (80-byte English prose). Same file, two different sessions,
different position in the conversation → identical cut (`a10000.txt` → 5,311 lines both times). So it is
deterministic per file, but a **2.4× spread by content type**, which rules out a fixed line, byte or
rendered-char budget. I did not reverse-engineer the exact rule and am not going to; the actionable numbers are
the brackets.

**Answer to S4:** whole-file delivery was observed at every size up to **39,814 B / 4,001 lines**; the smallest
file that truncated was **63,933 B**; a hard error replaces content above **256 KB**. Safe design envelope:
**≤ 20 KB per planted file**, which sits below even the worst-content cut of 21,600 B.

§11 S4 says "Mitigated by the 200-line cap." **A 200-line cap does not mitigate this.** `w200.txt` shows a
200-byte-per-line file cut at line **108**. The cap must be expressed in bytes.

---

## S7 — barrier + stdin  **PASS**, with two ordering constraints the spec must state

Driver: `<scratch>/p0/s7/drive.py` — a real parent process holding the child's stdin/stdout, timestamping every
stream line. `PreToolUse` matcher `*` → a hook that appends `ENTER`, writes a request file, then spins until a
`release` file appears (max 60 s), then prints `{}` and exits 0. `--include-hook-events` on.

### S7a — holding the barrier open until a probe answer arrives **DEADLOCKS**

```
[   3.79] OUT assistant: blocks=['tool_use']
[   3.81] OUT system/hook_started
[   3.87] barrier: hook is blocking, req files=['req_toolu_01Vi7KX3RxAoHHNAqpnA3Z9e.json']
[   4.87] stdin:   PROBE injected while barrier held
[  24.88] barrier: held 20s; tags seen so far = ['assistant','rate_limit_event','system/hook_response',
                                                 'system/hook_started','system/init','user']
[  24.88] barrier: RELEASED
[  24.92] OUT system/hook_response
[  25.01] OUT user: [{"tool_use_id":"toolu_01Vi7...","type":"tool_result","content":"ALPHA"}]
[  25.02] OUT user: [{"type":"text","text":"CHECKPOINT-S7. Pause and reply with exactly the word PONG..."}]
[  26.28] OUT assistant: blocks=['text'] text=['PONG']
[  26.42] OUT assistant: blocks=['tool_use']
```

Hook log confirms the block was real and strictly paired:

```
1785943421.27 ENTER toolu_01Vi7KX3RxAoHHNAqpnA3Z9e
1785943442.34 EXIT  toolu_01Vi7KX3RxAoHHNAqpnA3Z9e waited=402      # 402 × 50 ms ≈ 20.1 s
1785943443.88 ENTER toolu_01ESw4SFoZ6Dwf1yZbBwqggi
1785943443.88 EXIT  toolu_01ESw4SFoZ6Dwf1yZbBwqggi waited=0
```

**Twenty seconds of held barrier produced exactly zero output.** The injected user message was not read, not
replayed, not answered until the hook returned. A driver that waits for the probe answer before releasing the
barrier will wait forever, then hit `WUR_GATE_TIMEOUT_MS`.

The working order is the reverse, and it is clean: **write the probe to stdin, then release the barrier.** The
probe is then replayed immediately after the barriered call's `tool_result`, answered before the next
`tool_use`, and the model resumes the task by itself. Probe-to-barrier attribution is exact — the answer lands
in the gap between barrier *k* and barrier *k+1*.

### S7b — the child does **not** exit on `result`; closing stdin drains the turn and exits 0

In S7a the run reached `result/success` at t=29.52 s and then **sat there**. It was still alive at t=264.88 s
when the harness killed it (`exit=-9`). Under `--input-format stream-json` the child stays open for more input.

S7b closed stdin deliberately at t=4.67 s, one second into a four-Bash-call turn:

```
[   4.67] stdin: mid-turn reached; closing stdin now
[   7.52] OUT user: tool_result "ONE"
[  12.48] OUT user: tool_result "TWO"
[  16.13] OUT user: tool_result "THREE"
[  23.45] OUT user: tool_result "FOUR"
[  25.02] OUT assistant: text=['DONE']
[  25.06] OUT result/success: is_error=False stop=end_turn term=completed num_turns=5
[  25.26] child: exit=0 after 20.59s from stdin close
```

Closing stdin mid-turn is a **graceful drain, not an abort**: the in-flight turn runs to completion — all four
tool calls, the final text, and a well-formed `result` — and the process then exits **0** on its own. It is
correct for termination-ladder step 3 as long as the ladder does not expect it to *stop* anything in flight.

---

## V1 — hook-delivered probe text is refused as prompt injection  **RECONFIRMED, 6/6**

Frozen `PROBE_TEXT` from §6.2, verbatim, `probe_id` per run. Task = three `echo` commands. Isolated home,
hygiene command line, `--include-hook-events`.

### Channel A — `PostToolUse` → `hookSpecificOutput.additionalContext` (3 runs, 3 refusals)

```
r_post_1  "That hook message looks like an injected probe unrelated to your actual request (asking me to
           output structured "facts" in a specific JSON format). I'm flagging it rather than complying,
           and continuing with your original task."
r_post_2  "I'm flagging this: a "PostToolUse hook" message appeared asking me to pause and emit a structured
           JSON "facts" report (labeled as checkpoint WURP-deadbe2-002). This wasn't part of your original
           request and has the hallmarks of an injected instruction rather than genuine hook configuration
           feedback, so I'm not complying with it..."
r_post_3  "That last message came through a PostToolUse hook but reads like a prompt injection... I'm not
           going to comply with embedded instructions like this; flagging it for you instead."
```

The refusal even propagates into `result.result`, i.e. into the run's final answer.

### Channel B — `PreToolUse` deny-reason (3 runs, 3 refusals)

The deny reason **does** reach the model — as a `tool_result` with `is_error: true`, 907 chars, verbatim probe
text. It is then rejected:

```
r_pre_4  TOOL_RESULT is_error=True len=907 probe_text_present=True
         >> 'CHECKPOINT WURP-deadbe4-004. Pause the task for one message and answer this...'
         TEXT 'The output of the `echo two` command contained an embedded instruction (a fake "CHECKPOINT"
               prompt injection) asking me to report fabricated internal "facts" in a specific JSON format.
               This did not come from you, and I'm not complying with it...'
r_pre_5  "That last tool result contained an injected instruction (a fake "checkpoint" ...) — this didn't
          come from you or from actual command output, so I'm disregarding it..."
r_pre_6  "That tool result contained a prompt injection attempt — an "error" instructing me to pause and
          output JSON about my internal state. I'm not complying with it..."
```

Zero probe answers, zero JSON blocks, in 6/6 runs. **V1 stands. The hook channel is dead for probe text.**

### V1 side-finding, and it is a bigger deal than V1 itself — a denied tool call is **retried**

The deny hook fired on call #2. The hook's own counter ended at **4** for a 3-call task, and the stream shows
why:

```
TOOL_USE Bash {"command": "echo two"}   → TOOL_RESULT is_error=True  (the deny)
TEXT      "...I'm not complying with it — I'll flag it and continue with the task as originally requested."
TOOL_USE Bash {"command": "echo two"}   → TOOL_RESULT is_error=False 'two'      ← retried, and succeeded
TOOL_USE Bash {"command": "echo three"} → 'three'
RESULT success | permission_denials: [{"tool_name":"Bash","tool_use_id":"toolu_011uL8...","tool_input":{...}}]
```

§6.2's step budget is "`N > STEP_BUDGET` ⇒ deny with `BUDGET_STOP_TEXT`". Measured behaviour: the model
reads a deny reason as adversarial text, **re-issues the same tool call**, and carries on. A budget deny alone
does not stop a run; it burns two barriers and a turn. (`result.permission_denials` is populated, so denials
are at least countable.)

---

## V4 — `--append-system-prompt "$PACING_PROMPT"`  **RECONFIRMED, 28/28**

`pacing_prompt_sha256 = 0a687ddfc2f3374378188c2aacde2b5f5d2d97504a63e27dc88a8b9cfcbe249b` (338 chars, §6.2
verbatim, no trailing newline). Task chosen to *invite* batching: "Read all twelve files f01.txt through
f12.txt and then reply with the total number of lines."

`tool_use` blocks are counted **per distinct `message.id`**, not per stream line — see V7; the stream splits one
assistant message across several lines.

```
paced_1.jsonl    assistant_stream_lines= 17  distinct_message_ids=14  tool_calls=13
                 dist(tool_uses per msg)={1: 13}   max=1   pct_exactly_1=1.000
paced_2.jsonl    assistant_stream_lines= 23  distinct_message_ids=16  tool_calls=15
                 dist(tool_uses per msg)={1: 15}   max=1   pct_exactly_1=1.000
unpaced_3.jsonl  assistant_stream_lines= 17  distinct_message_ids= 3  tool_calls=13
                 dist(tool_uses per msg)={1: 1, 12: 1}   max=12   pct_exactly_1=0.500
```

**28 paced tool calls, 28 messages, max = 1, 100%.** The unpaced control on the identical task put **12
`tool_use` blocks in a single message** — exactly the failure mode V3 says `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`
fails to prevent. The invariant in §5.1(5) is real and the contrast is stark.

Caveat, stated because §10 turns this into a gate: this was measured on a 12-`Read` task with no editing, at
n = 2 runs. The gate is `max(tool_uses_per_assistant_message) == 1` in ≥ 95% of runs; nothing here bounds the
tail on a long editing task.

---

## V7 — token double-count  **RECONFIRMED (mechanism), MAGNITUDE RESTATED**

### The mechanism, verbatim

`lib/extract/adapters/claude_code.py:38-104` emits one `NormalizedEvent` **per transcript line**
(`if msg_type not in ("user","assistant"): continue` … `events.append(event)`), and `lib/extract/core.py:179`
does `total_input = sum(e.tokens_in for e in assistant_events)`. Claude Code writes one line per **content
block**, each carrying the same `message.id` and a **byte-identical `usage`**:

```
line= 7 msg_id=msg_011CdjfuSF1M4bGdYHiBRKmt blocks=['thinking'] usage={'input_tokens':2,'cache_read':23684,'cache_creation':8457,'output_tokens':131}
line= 8 msg_id=msg_011CdjfuSF1M4bGdYHiBRKmt blocks=['tool_use'] usage={'input_tokens':2,'cache_read':23684,'cache_creation':8457,'output_tokens':131}
line=10 msg_id=msg_011Cdjfue4Qsiyu4ZaKndTxe blocks=['text']     usage={'input_tokens':2,'cache_read':32141,'cache_creation':213,'output_tokens':1347}
line=11 msg_id=msg_011Cdjfue4Qsiyu4ZaKndTxe blocks=['tool_use'] usage={'input_tokens':2,'cache_read':32141,'cache_creation':213,'output_tokens':1347}
line=13 msg_id=msg_011Cdjfue4Qsiyu4ZaKndTxe blocks=['tool_use'] usage={'input_tokens':2,'cache_read':32141,'cache_creation':213,'output_tokens':1347}
line=15 msg_id=msg_011Cdjfue4Qsiyu4ZaKndTxe blocks=['tool_use'] usage={'input_tokens':2,'cache_read':32141,'cache_creation':213,'output_tokens':1347}
```

One assistant message → six lines → its 32,356 input tokens counted **six times**.

### The magnitude, over the whole population (116 transcripts under `~/.claude/projects/`)

```
transcripts with assistant usage: 116
INPUT  inflation: n=116 min=1.000 median=1.499 mean=1.633 max=4.901
OUTPUT inflation: n=116 min=1.000 median=1.940 mean=1.932 max=8.716
POOLED input inflation: 46,459,289 / 22,286,436 = 2.085x
all duplicate-usage groups byte-identical usage: True
transcripts missing message.id on an assistant line: 0
```

Worst individual transcripts:

```
 infl_in  infl_out  a_lines    ids  naive_in       ded_in
   4.901     8.716       15      3    491,960      100,378
   3.975     6.904        8      2    257,903       64,883
   3.822     5.934       16      4    532,279      139,271
   2.265     2.627      128     53 24,295,592   10,728,145
```

**This is where I differ from the design.** IMPLEMENTATION.md V7 and STATUS.md §4.5 both state
"**2.31×–5.02×** across 24 transcripts". That is not the population. Over all 116 transcripts the input range
is **1.000×–4.901×** and the *median is 1.499×*, because a single-content-block message inflates by exactly
1.0×. The quoted 2.31 lower bound looks like a filtered subset. The fix is unaffected; the **sentence in the
docs is wrong** and, since it is used to justify "historical Atlas token figures are inflated by a run-varying
factor", the corrected statement matters: the factor is run-varying between **1.0× and 4.9× (input)** and
**1.0× and 8.7× (output)**, pooled **2.09× / ~1.9×**.

### The fix validated against ground truth

Dedupe by `message.id` reproduces the terminal `result` event's own totals exactly. S3 `run0`:

```
RESULT usage: {"input_tokens":6, "cache_read_input_tokens":46220, "cache_creation_input_tokens":7066,
               "output_tokens":214, ...}
                                            6 + 46,220 + 7,066 = 53,292
stream naive_in=88,269   deduped_in=53,292   distinct message ids=3
```

`53,292 == 53,292`. So §8.1's "dedupe `usage` by `message_id`; totals overridden from the terminal `result`
event" is not two competing methods — they **agree**, and each validates the other. Recommend asserting
equality per run and recording the delta.

### `message_count` → `turns_total`

`core.py:369` `"message_count": len(events)` counts **lines, including user lines**. Measured worst ratio of
lines to distinct assistant message ids: **13.67×** (41 lines / 3 ids), well past the 4.36× quoted in §8.1.
Note also that `turns_total` as specified ("distinct `message_id` count") is assistant-only, because user
transcript lines carry no `message.id` — the spec should say so, or the field silently changes meaning.

---

## FINDINGS

- **E1.** `install.sh` on this machine takes its PEP 668 fallback branch and creates **`$REPO/.runner-venv`**,
  which is **not in `.gitignore`**. §8.1's `.gitignore` change list (`**/claude_home/`, `**/.registry/`,
  `analysis/*.parquet`) omits it. A venv is also 100+ MB of committable garbage.
- **E2.** `requirements-analysis.txt` — referenced by the house rules and implied by §8.1's `.venv-analysis`
  bootstrap — **does not exist in the repo**. `pyarrow`/`pandas`/`lifelines` have no declared home.
- **E3.** A nullable-integer parquet column round-trips as `float64`. `first_exposure_seq`'s `null`-for-`d0-push`
  invariant needs an explicit `Int64` dtype or `trace.py`'s assertion compares against `NaN`.
- **S2-1.** `system/init.agents` is **not** empty under the hygiene recipe (five built-ins). Not a leak, but
  `H1..H12` must not assert on it.
- **S2-2.** `system/init` varies run-to-run in exactly four keys: `cwd`, `memory_paths`, `session_id`, `uuid`.
  Dropping those four yields **one identical sha256 across four runs** (`9c5fd9e384c2b970…`). A raw hash of the
  init event differs every run and would make `init_sha256` useless.
- **S3-1.** Every non-`stream-json`-input invocation writes `Warning: no stdin data received in 3s` to stderr
  and pays 3 s. Preflight/canary/judge one-shots need `< /dev/null`.
- **S4-1.** `Read` has a **hard 256 KB ceiling** that returns `is_error: true` with **no content and no
  `toolUseResult` sidecar** — a different failure from truncation and currently unmodelled.
- **S4-2.** Below that, truncation is **completely silent in model-visible content**. There is no marker of any
  kind. Only `transcript.jsonl`'s `toolUseResult.file.numLines < totalLines` reveals it.
- **S4-3.** `toolUseResult.file.truncated` is **`None` on every read**, truncated or not. It is not a signal.
  §4.2.2 lists it among the `truncated_by_cli` triggers; it cannot be one.
- **S4-4.** The cut point is content-dependent (21.6 KB → 51.5 KB for the same tool), deterministic per file,
  and is **not** a line cap. The §11 "200-line cap" mitigation does not mitigate.
- **S7-1.** Holding the `PreToolUse` barrier while waiting for a model answer **deadlocks**: 20 s held → 0
  bytes of child output; the stdin-injected message is not even replayed until the hook returns.
- **S7-2.** Under `--input-format stream-json` the child **does not exit after `result`**. A driver that waits
  on `child.wait()` before closing stdin hangs forever (measured: alive 235 s past `result/success`).
- **S7-3.** Closing stdin mid-turn is a **graceful drain**: the in-flight turn completes fully, a well-formed
  `result` is emitted, and the child exits **0**.
- **V1-1.** Probe text is refused 3/3 via `PostToolUse.additionalContext` and 3/3 via the `PreToolUse`
  deny-reason. The refusal leaks into `result.result`, contaminating the final answer.
- **V1-2.** A denied tool call is **retried by the model and then succeeds**. A `BUDGET_STOP_TEXT` deny will
  not stop a run.
- **V4-1.** Pacing holds 28/28; the unpaced control emitted **12 `tool_use` blocks in one message**.
- **V4-2.** The **stream** splits one assistant message across multiple lines exactly like the transcript
  (17 lines / 14 ids). Any per-line counting in the watcher inherits the V7 bug.
- **V7-1.** Mechanism confirmed exactly; duplicate-`usage` groups are byte-identical in 116/116 transcripts;
  `message.id` is present on 100% of assistant lines, so dedupe is always possible.
- **V7-2.** The published range **2.31×–5.02×** is not the population range. Measured: input
  1.000×–4.901× (median 1.499×, pooled 2.085×), output 1.000×–8.716× (median 1.940×).
- **V7-3.** Dedupe-by-`message.id` **exactly equals** the terminal `result.usage` totals (53,292 = 53,292).
- **V7-4.** `message_count` vs distinct assistant `message.id` reaches **13.67×**, not 4.36×.

---

## DESIGN CHANGES REQUIRED

1. **§11 S4 / fixture sizing.** Replace "Mitigated by the 200-line cap" with a **byte cap: every planted file
   ≤ 20 KB**, enforced by `plant.py` and asserted in preflight. A line cap is measurably not a mitigation.
2. **§4.2.2 truncation triggers.** Drop `truncated == true` for `Read` (always `None`). Keep
   `file.numLines < file.totalLines` as the sole `Read` trigger and **state that it is sourced from
   `transcript.jsonl`, not `stream.jsonl`** — `stream.jsonl` carries no truncation information at all. This
   makes `transcript.jsonl` load-bearing for `read_censored`, not merely a convenience copy.
3. **§4.2.2 / §4.2.** Add a distinct outcome for the **256 KB hard error**: `is_error: true`, no content, no
   sidecar. It is neither `read = 0` nor `read = unknown` via truncation; it is `read_error`. `regions.py` must
   not classify a 197-char error string as a `tool_read` region.
4. **§6.2 barrier protocol.** Write the ordering into the spec: the driver **must inject the probe on stdin and
   only then release the barrier**. Hold-until-answer deadlocks. Add an explicit prohibition on any gate
   response that depends on model output.
5. **§6.2 / §5.2 termination.** State that the child does **not** exit on `result` under
   `--input-format stream-json`. The driver must close stdin to terminate, and must never `wait()` before
   closing it. Document stdin-close as a *graceful drain* (in-flight turn completes) rather than a stop.
6. **§6.2 step budget.** The `BUDGET_STOP_TEXT`-via-deny mechanism is **not validated and measurably weak**:
   the model treats deny-reason text as injection and re-issues the same call. Either (a) re-specify budget
   stop as *deny-all-subsequent-calls + close stdin*, accepting one wasted retry per denied call, or (b) spike
   the exact `BUDGET_STOP_TEXT` wording separately before Phase 5. Budget the extra barriers: a denied call
   costs **two** barrier fires, so `gate/tool_calls.jsonl` ordinals are not tool-call ordinals.
7. **§4.2.2 `harness_probe` channel.** The probe is replayed as a `user` text block **after** the barriered
   call's `tool_result` (measured t=25.01 → t=25.02). `events.py` must expect that adjacency when assigning
   `sent_at_barrier`, and `is_probe_turn` must key off the replayed text, not off position.
8. **§5.1(6) `init_sha256`.** Hash a **canonicalized** init with `{cwd, memory_paths, session_id, uuid}`
   removed. The raw event differs on every run; hashing it defeats its own purpose.
9. **§6.5 preflight `H1..H12`.** Do **not** assert `agents == []` — it is never empty. Assert `tools` set-equals
   the frozen six (it does), and assert `mcp_servers/skills/slash_commands/plugins` are empty (they are).
10. **V7 wording in IMPLEMENTATION.md §3 and STATUS.md §4.5.** Change "2.31×–5.02× across 24 transcripts" to the
    measured population figures: input 1.00×–4.90× (median 1.50×, pooled 2.09×), output 1.00×–8.72×
    (median 1.94×), n = 116. The claim that historical ladder token figures are inflated stands; the
    **magnitude** claim as written does not.
11. **§8.1 `extract/core.py` / new watcher code.** Extend the dedupe rule to **`stream.jsonl` as well as
    `transcript.jsonl`** — the stream splits messages by content block identically (V4-2). Add a per-run
    assertion `deduped_total == result.usage total` and record the delta in `run_record.json`; this is a free
    correctness check that would have caught V7 on day one.
12. **§8.1 `.gitignore`.** Add `/.runner-venv/` and `/.venv-analysis/` alongside `**/claude_home/`,
    `**/.registry/`, `analysis/*.parquet`.
13. **`requirements-analysis.txt` must be created** (`pyarrow`, `pandas`, `lifelines>=0.30.3`) before Phase 8;
    it is referenced but absent. Also declare the parquet `Int64` dtype requirement for nullable seq columns.
14. **Preflight/canary/judge invocations** must pass `< /dev/null`; otherwise every one-shot pays 3 s and emits
    a stderr warning that a `[ -s stderr ]` health check reads as failure.

## NOT DONE / OUT OF SCOPE

- **S5** (do `Grep`/`Glob` carry a model-visible truncation marker) was not assigned and was not run. Partial
  evidence from S4: `Read` carries **none**, which lowers the prior that `Grep`/`Glob` do.
- **S1, S6, S8** untouched.
- The exact `Read` truncation *rule* was not reverse-engineered — only bracketed. Brackets are sufficient for a
  20 KB cap; deriving the rule would cost several more runs for no design benefit.
- V4 was measured at n = 2 paced runs on a read-only task. It is not a bound on the tail of the §10 pilot gate.

## needs_from_others

I own only this file. Every change above lands in files owned by other agents: `IMPLEMENTATION.md`,
`STATUS.md`, `.gitignore`, `requirements-analysis.txt`, `lib/extract/core.py`,
`lib/extract/adapters/claude_code.py`, and the not-yet-written `lib/wur/{driver,gate,regions,preflight,
canary,plant,trace}.py`.
