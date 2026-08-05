#!/usr/bin/env python3
"""
test_wur_lib.py — the invariants that must never silently break.

RESPONSIBILITY
  Guard the properties of the WUR instrument whose failure mode is a WRONG
  NUMBER rather than a crash. Every test here corresponds to something that,
  if it regressed, would produce a complete-looking fact_trace.jsonl carrying
  a measurement that is quietly false — the only class of bug this experiment
  cannot recover from after the fact, because the runs are not repeatable and
  the raw stream would already have been interpreted.

  Deliberately NOT here: anything that fails loudly on its own (a traceback, a
  schema rejection, a non-zero exit). Those are caught by running the thing.

INPUTS   lib/wur/* imported directly; synthetic fixtures built in tmp dirs.
         No network, no `claude` invocation, no API spend, no job dir required.
OUTPUTS  unittest results on stderr; exit 0 iff every invariant holds.

WHAT EACH TEST DEFENDS, AND WHY IT IS LOAD-BEARING
  scan-before-truncate   events.py replaces region text with a digest. If it
      ever runs before exposure.py, a nonce past the digest boundary scores
      `read = 0` for a fact the model was demonstrably shown. §6.1.
  hook never blocks      gate.py runs inside a synchronous PreToolUse hook.
      Non-zero exit, stdout that is not one JSON object, or ANY stderr changes
      the agent — and the watcher's whole contract is "observe everything,
      change nothing". §5.1(1).
  global settings untouched  the pre-WUR harness merged hooks into the global
      ~/.claude/settings.json. That is what forced JOBS=1 and what makes a
      crashed run poison the next one. §6.5.
  reconcile idempotent   derivation must be re-runnable months later after a
      scanner bugfix, over the same raw bytes, to the same output. §6.1.
  channel enum closed    an unmapped model-visible region must surface as
      `unknown_visible` and fail CI, never be silently dropped — a dropped
      channel is an invisible exposure path and reads as "not read". §4.2.2.
  join coverage          stream <-> gate join integrity; a pilot gate at 0.99.
  D4 fields              read / read_inbound_only / unexplained_possession are
      three different questions. Folding thinking into `read` (D4) mutes the
      confabulation alarm; unexplained_possession is the compensating check,
      and if it stops firing the alarm is gone with no other trace. §4.2.1.
  d0-push seq nulls      auto-loaded content appears in NO log, so its
      exposure is asserted, not measured. A non-null seq means something
      scanned a channel it cannot legitimately see. trace.py must RAISE. §4.2.2.

CLI
  python3 tests/test_wur_lib.py [-v]        (stdlib unittest; no pytest needed)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(LIB / "wur"))

import events as events_mod          # noqa: E402
import exposure as exposure_mod      # noqa: E402
import gate as gate_mod              # noqa: E402
import protocol as protocol_mod      # noqa: E402
import reconcile as reconcile_mod    # noqa: E402
import regions as regions_mod        # noqa: E402
import settings as settings_mod      # noqa: E402
import trace as trace_mod            # noqa: E402

GATE_PY = LIB / "wur" / "gate.py"
NONCE = "ZQ-TESTNONCE1"


# ── fixtures ─────────────────────────────────────────────────────────────────
def fact_card(nonce: str = NONCE, fact_id: str = "f1", regexes=()) -> protocol_mod.FactCard:
    return protocol_mod.FactCard(fact_id=fact_id, nonce=nonce, regexes=tuple(regexes),
                                 gist="the test fact")


def region(seq: int, channel: str, text: str, *, model_visible=True, inbound=True,
           counts=True, idx: int = 0, **kw) -> regions_mod.Region:
    return regions_mod.Region(seq=seq, region_idx=idx, channel=channel, text=text,
                              source="stream", model_visible=model_visible,
                              inbound=inbound, counts_toward_read=counts, **kw)


def stream_line(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))


def minimal_run_dir(tmp: Path, *, nonce: str = NONCE, arm: str = "d2",
                    with_nonce_in_read: bool = True) -> Path:
    """A run dir with just enough raw material for the whole derivation chain.

    One assistant message carrying one Read tool_use, one tool_result whose
    content does or does not carry the nonce, one terminal result event, and a
    matching gate/tool_calls.jsonl row so the join has something to join.
    """
    rd = tmp / "run"
    (rd / "gate").mkdir(parents=True, exist_ok=True)
    (rd / "watch").mkdir(parents=True, exist_ok=True)
    tid = "toolu_test0001"
    mid = "msg_test0001"
    body = f"vendored reports live here, register {nonce}\n" if with_nonce_in_read \
        else "nothing to see here\n"
    lines = [
        {"type": "system", "subtype": "init", "session_id": "s1",
         "tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
         "mcp_servers": [], "slash_commands": [], "skills": [], "plugins": []},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "Do the task."}]}},
        {"type": "assistant", "message": {"id": mid, "role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Read",
             "input": {"file_path": "docs/NOTES.md"}}],
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": body}]}},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
         "total_cost_usd": 0.01, "result": "DONE",
         "usage": {"input_tokens": 10, "output_tokens": 5,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
    ]
    (rd / "stream.jsonl").write_text("\n".join(stream_line(o) for o in lines) + "\n")
    (rd / "gate" / "tool_calls.jsonl").write_text(stream_line({
        "ts": 1.0, "barrier": 1, "tool_use_id": tid, "tool_name": "Read",
        "tool_input": {"file_path": "docs/NOTES.md"}}) + "\n")
    (rd / "run_meta.json").write_text(json.dumps({
        "run_id": "test-run", "job_id": "testjob", "task_id": "t1",
        "condition_id": arm, "env_id": arm, "replication": 1,
        "experiment": "wur", "factors": {"fact_present": arm != "ctrl", "probe": True},
    }))
    (rd / "facts.json").write_text(json.dumps({"facts": [
        {"fact_id": "f1", "task_id": "t1", "token": nonce, "nonce": nonce,
         "gist": "the test fact", "surface_forms": [nonce]}]}))
    (rd / "probe_plan.json").write_text(json.dumps(
        {"intervals": [1], "fire_at": [1], "lo": 1, "hi": 3, "max_probes": 24}))
    return rd


# ── 1. scan before truncate ──────────────────────────────────────────────────
class ScanBeforeTruncate(unittest.TestCase):
    """§6.1: exposure.py scans full region text; events.py digests afterwards."""

    def test_full_text_is_scanned_and_nonce_found(self):
        rs = regions_mod.RegionSet(regions=[region(1, "tool_read", "x" * 5000 + NONCE)])
        rows = exposure_mod.scan(rs, [fact_card()], "r1")
        self.assertEqual(len(rows), 1, "a nonce past 5 KB must still be found")
        self.assertEqual(rows[0]["match_form"], "exact")

    def test_digested_region_raises_rather_than_scoring_not_read(self):
        r = region(1, "tool_read", "")
        r.meta["digested"] = True
        rs = regions_mod.RegionSet(regions=[r])
        with self.assertRaises(RuntimeError) as cm:
            exposure_mod.scan(rs, [fact_card()], "r1")
        self.assertIn("scan-before-truncate", str(cm.exception))

    def test_reconcile_orders_exposure_before_events(self):
        """The ordering is a property of reconcile, not of a comment."""
        src = (LIB / "wur" / "reconcile.py").read_text()
        self.assertLess(src.index("exposure_mod.scan"), src.index("events_mod.build"),
                        "reconcile must scan for nonces before events.py digests regions")

    def test_events_digest_does_not_carry_raw_text(self):
        """A digest must not be the text itself — otherwise nothing was truncated."""
        with tempfile.TemporaryDirectory() as td:
            rd = minimal_run_dir(Path(td))
            reconcile_mod.reconcile(rd, facts=rd / "facts.json")
            for row in (json.loads(l) for l in (rd / "events.jsonl").read_text().splitlines()):
                for key in ("result_digest", "tool_input"):
                    blob = json.dumps(row.get(key) or "")
                    self.assertNotIn("vendored reports live here", blob,
                                     "events.jsonl must carry a digest, not the region text")


# ── 2. the hook never blocks and always exits 0 ──────────────────────────────
class HookNeverBlocks(unittest.TestCase):
    """§5.1(1): hooks exit 0, print exactly {}, and write nothing to stderr."""

    def _run_gate(self, payload: str, run_dir: Path, extra=(), timeout=30):
        p = subprocess.run(
            [sys.executable, str(GATE_PY), "pre", "--run-dir", str(run_dir),
             "--timeout-ms", "300", "--poll-ms", "5", *extra],
            input=payload, capture_output=True, text=True, timeout=timeout)
        return p

    def test_timeout_fails_open_with_empty_object(self):
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "run"
            rd.mkdir()
            p = self._run_gate(json.dumps({"tool_use_id": "t1", "tool_name": "Read"}), rd)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(json.loads(p.stdout), {}, "fail open, never wedge the run")
            self.assertEqual(p.stderr, "", "a hook may never write to stderr")

    def test_malformed_payloads_never_break_the_contract(self):
        bad = ["", "not json", "[]", "null", '{"no_tool_use_id": 1}', "{" * 200]
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "run"
            rd.mkdir()
            for payload in bad:
                p = self._run_gate(payload, rd)
                with self.subTest(payload=payload[:20]):
                    self.assertEqual(p.returncode, 0)
                    self.assertEqual(json.loads(p.stdout), {})
                    self.assertEqual(p.stderr, "")

    def test_unwritable_gate_dir_still_exits_zero_quietly(self):
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "run"
            rd.mkdir()
            (rd / "gate").write_text("i am a file, not a directory")
            p = self._run_gate(json.dumps({"tool_use_id": "t1", "tool_name": "Read"}), rd)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(json.loads(p.stdout), {})
            self.assertEqual(p.stderr, "")

    def test_log_mode_short_circuits_without_a_driver(self):
        """--mode log is the drop-in for the deleted log_tool_event.sh."""
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "run"
            rd.mkdir()
            p = self._run_gate(json.dumps({"tool_use_id": "t2", "tool_name": "Bash"}),
                               rd, extra=["--mode", "log"], timeout=10)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(json.loads(p.stdout), {})
            self.assertEqual(p.stderr, "")
            rows = (rd / "gate" / "tool_calls.jsonl").read_text().splitlines()
            self.assertEqual(len(rows), 1, "log mode still records the barrier")

    def test_deny_is_the_only_model_visible_hook_text(self):
        obj = gate_mod.deny_object("stop")
        flat = json.dumps(obj)
        self.assertIn("stop", flat)
        self.assertEqual(gate_mod.ALLOW, {}, "the allow object carries no text at all")


# ── 3. the global ~/.claude/settings.json is untouched ───────────────────────
class GlobalSettingsUntouched(unittest.TestCase):
    """§6.5: per-run CLAUDE_CONFIG_DIR + --settings replaced the global mutation."""

    def test_no_script_writes_the_global_settings_file(self):
        offenders = []
        for path in list(ROOT.glob("*.sh")) + list((ROOT / "lib").rglob("*.sh")):
            for i, line in enumerate(path.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                if not code.strip():
                    continue
                if ".claude/settings.json" in code:
                    offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()}")
        self.assertEqual(offenders, [], "no shell script may touch the global settings file")

    def test_render_writes_only_inside_the_run_dir(self):
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "run"
            rd.mkdir()
            out = settings_mod.render_settings(rd)
            self.assertEqual(out.parent.resolve(), rd.resolve())
            self.assertEqual(settings_mod.check_settings_file(out, run_dir=rd,
                                                              require_barrier=True), [])

    def test_rendered_hook_timeout_is_positive_and_outlives_the_barrier(self):
        """A hook entry with timeout 0 registers NO hooks at all and no error (V12)."""
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "run"
            rd.mkdir()
            obj = json.loads(settings_mod.render_settings(rd, timeout_ms=300_000).read_text())
            for event, groups in obj["hooks"].items():
                for g in groups:
                    for h in g["hooks"]:
                        with self.subTest(event=event):
                            self.assertIsInstance(h.get("timeout"), int)
                            self.assertGreater(h["timeout"], 0)
            pre = obj["hooks"]["PreToolUse"][0]["hooks"][0]
            self.assertGreaterEqual(pre["timeout"] * 1000, 300_000,
                                    "the CLI must not kill the barrier before it fails open")

    def test_template_ships_no_zero_timeout(self):
        tmpl = json.loads((LIB / "wur" / "settings_template.json").read_text())
        for groups in tmpl["hooks"].values():
            for g in groups:
                for h in g["hooks"]:
                    self.assertNotEqual(h.get("timeout"), 0,
                                        "a copied-as-is template must not disable hooks")


# ── 4. reconcile is idempotent ───────────────────────────────────────────────
class ReconcileIdempotent(unittest.TestCase):
    """§6.1: safe to re-run months later, over the same raw bytes."""

    def _hashes(self, rd: Path) -> dict:
        import hashlib
        out = {}
        for name in ("events.jsonl", "exposure.jsonl", "probes.jsonl", "fact_trace.jsonl"):
            p = rd / name
            out[name] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
        return out

    def test_three_runs_produce_byte_identical_tables(self):
        with tempfile.TemporaryDirectory() as td:
            rd = minimal_run_dir(Path(td))
            reconcile_mod.reconcile(rd, facts=rd / "facts.json")
            first = self._hashes(rd)
            for _ in range(2):
                reconcile_mod.reconcile(rd, facts=rd / "facts.json")
                self.assertEqual(self._hashes(rd), first)
            self.assertIsNotNone(first["fact_trace.jsonl"])

    def test_rerun_does_not_append(self):
        with tempfile.TemporaryDirectory() as td:
            rd = minimal_run_dir(Path(td))
            reconcile_mod.reconcile(rd, facts=rd / "facts.json")
            n = len((rd / "events.jsonl").read_text().splitlines())
            reconcile_mod.reconcile(rd, facts=rd / "facts.json")
            self.assertEqual(len((rd / "events.jsonl").read_text().splitlines()), n)

    def test_no_tmp_files_survive(self):
        with tempfile.TemporaryDirectory() as td:
            rd = minimal_run_dir(Path(td))
            reconcile_mod.reconcile(rd, facts=rd / "facts.json")
            self.assertEqual(list(rd.glob("*.tmp")), [], "writes must be atomic replace")


# ── 5. the channel enum is closed ────────────────────────────────────────────
class ChannelEnumClosed(unittest.TestCase):
    """§4.2.2: an unmapped model-visible region is `unknown_visible` and fails CI."""

    def test_every_declared_channel_is_known(self):
        for name in regions_mod.CHANNELS:
            self.assertTrue(regions_mod.is_valid_channel(name), name)

    def test_attachment_family_is_accepted_by_pattern(self):
        self.assertTrue(regions_mod.is_valid_channel("attachment_queued_command"))
        self.assertTrue(regions_mod.is_valid_channel("attachment_anything"))

    def test_an_invented_channel_is_not_silently_accepted(self):
        self.assertFalse(regions_mod.is_valid_channel("totally_made_up_channel"))

    def test_the_schema_and_the_code_agree_on_the_enum(self):
        """events.schema.json duplicates the enum; drift would pass code, fail schema."""
        schema = json.loads((ROOT / "schemas" / "events.schema.json").read_text())
        blob = json.dumps(schema)
        missing = [c for c in regions_mod.CHANNELS if f'"{c}"' not in blob]
        self.assertEqual(missing, [], "channels known to regions.py but absent from the schema")

    def test_unknown_visible_is_itself_a_declared_channel(self):
        """regions.py maps anything it cannot classify to `unknown_visible` (FAILS CI)."""
        self.assertIn("unknown_visible", regions_mod.CHANNELS)
        spec = regions_mod.channel_spec("unknown_visible")
        self.assertTrue(spec.model_visible)
        self.assertTrue(spec.counts_toward_read,
                        "an unclassified visible region must count as exposure, "
                        "never be silently treated as not-read")

    def test_an_off_enum_channel_raises_rather_than_being_dropped(self):
        """Fail closed: a channel nobody declared must stop the chain, not vanish."""
        rs = regions_mod.RegionSet(regions=[region(1, "totally_made_up_channel", NONCE)])
        with self.assertRaises(KeyError):
            exposure_mod.scan(rs, [fact_card()], "r1")

    def test_reconcile_counts_unknown_visible_as_an_alarm(self):
        with tempfile.TemporaryDirectory() as td:
            rd = minimal_run_dir(Path(td))
            reconcile_mod.reconcile(rd, facts=rd / "facts.json")
            rec = json.loads((rd / "reconcile.json").read_text())
            self.assertIn("unknown_visible", rec["alarms"])
            self.assertEqual(rec["alarms"]["unknown_visible"], 0)

    def test_asserted_channel_is_declared(self):
        self.assertIn("autoload_claude_md", regions_mod.ASSERTED_CHANNELS)


# ── 6. join coverage ─────────────────────────────────────────────────────────
class JoinCoverage(unittest.TestCase):
    """§6.1 / §10: stream <-> gate join on tool_use_id, gate at 0.99."""

    def test_perfect_join_on_a_clean_run(self):
        with tempfile.TemporaryDirectory() as td:
            rd = minimal_run_dir(Path(td))
            reconcile_mod.reconcile(rd, facts=rd / "facts.json")
            summ = json.loads((rd / "reconcile.json").read_text())["events_summary"]
            self.assertEqual(summ["join_coverage"], 1.0)
            self.assertTrue(summ["join_coverage_pass"])
            self.assertEqual(summ["n_gate_only"], 0)
            self.assertEqual(summ["n_stream_only"], 0)

    def test_a_gate_row_with_no_stream_partner_lowers_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            rd = minimal_run_dir(Path(td))
            with (rd / "gate" / "tool_calls.jsonl").open("a") as fh:
                fh.write(json.dumps({"ts": 2.0, "barrier": 2,
                                     "tool_use_id": "toolu_orphan", "tool_name": "Bash",
                                     "tool_input": {"command": "ls"}}) + "\n")
            reconcile_mod.reconcile(rd, facts=rd / "facts.json")
            summ = json.loads((rd / "reconcile.json").read_text())["events_summary"]
            self.assertLess(summ["join_coverage"], 1.0)
            self.assertGreaterEqual(summ["n_gate_only"], 1)

    def test_the_gate_threshold_is_the_preregistered_one(self):
        self.assertEqual(trace_mod.JOIN_COVERAGE_GATE, 0.99)

    def test_pacing_is_counted_per_message_id_not_per_line(self):
        """V17: stream.jsonl splits one message across lines; a per-line count inflates."""
        with tempfile.TemporaryDirectory() as td:
            rd = minimal_run_dir(Path(td))
            reconcile_mod.reconcile(rd, facts=rd / "facts.json")
            summ = json.loads((rd / "reconcile.json").read_text())["events_summary"]
            self.assertEqual(summ["max_tool_uses_per_message"], 1)
            self.assertTrue(summ["pacing_ok"])
            self.assertTrue(all(isinstance(k, str) and k.startswith("msg_")
                                for k in summ["tool_uses_per_message"]))


# ── 7. the D4 fields ─────────────────────────────────────────────────────────
class D4Fields(unittest.TestCase):
    """§4.2.1: read (primary) vs read_inbound_only vs unexplained_possession."""

    def _row(self, tmp: Path, exposure_rows: list[dict], *, arm="d2") -> dict:
        rd = minimal_run_dir(tmp, arm=arm, with_nonce_in_read=False)
        rows, _ = trace_mod.run(rd, facts=rd / "facts.json",
                                exposure_rows=exposure_rows, event_rows=[], probe_rows=[])
        return rows[0]

    def _ex(self, seq: int, channel: str, *, inbound: bool, form="exact") -> dict:
        return {"schema_version": "1", "run_id": "test-run", "fact_id": "f1", "seq": seq,
                "region_idx": 0, "channel": channel, "source": "stream",
                "model_visible": True, "inbound": inbound, "counts_toward_read": True,
                "match_form": form, "bytes_before": 0, "sets_first_exposure":
                    bool(inbound and form == "exact"), "n_hits": 1}

    def test_inbound_hit_sets_both_read_and_read_inbound_only(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._row(Path(td), [self._ex(3, "tool_read", inbound=True)])
            self.assertTrue(r["read"])
            self.assertTrue(r["read_inbound_only"])
            self.assertFalse(r["unexplained_possession"])

    def test_thinking_only_sets_read_but_not_read_inbound_only(self):
        """D4: thinking counts toward `read`; the pre-D4 column must disagree."""
        with tempfile.TemporaryDirectory() as td:
            r = self._row(Path(td), [self._ex(3, "self_thinking", inbound=False)])
            self.assertTrue(r["read"], "D4: self_thinking sets read = 1")
            self.assertFalse(r["read_inbound_only"], "the sensitivity column must not move")
            self.assertTrue(r["unexplained_possession"], "the compensating alarm must fire")
            self.assertTrue(r["thinking_echo"])

    def test_thinking_after_an_inbound_hit_is_explained(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._row(Path(td), [self._ex(2, "tool_read", inbound=True),
                                     self._ex(5, "self_thinking", inbound=False)])
            self.assertTrue(r["read"])
            self.assertTrue(r["read_inbound_only"])
            self.assertFalse(r["unexplained_possession"],
                             "a prior inbound hit explains the thinking hit")

    def test_unexplained_possession_quarantines_the_row(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._row(Path(td), [self._ex(3, "self_thinking", inbound=False)])
            self.assertTrue(r["quarantined"])
            self.assertIn("unexplained_possession", json.dumps(r))

    def test_echo_channels_are_not_exposure(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._row(Path(td), [self._ex(3, "self_text", inbound=False)])
            self.assertFalse(r["read"], "the model's own output is not exposure")
            self.assertFalse(r["read_inbound_only"])
            self.assertTrue(r["echoed"])
        self.assertIn("self_text", trace_mod.ECHO_CHANNELS)
        self.assertIn("tool_input", trace_mod.ECHO_CHANNELS)
        self.assertIn("probe_answer", trace_mod.ECHO_CHANNELS)

    def test_only_exact_inbound_hits_set_the_strict_first_exposure(self):
        """§4.5: a lowercased nonce in a tool result is the agent's own prior text.

        The STRICT value lives in extra.first_inbound_exact_seq. The emitted
        first_exposure_seq deliberately falls back to the first read-counting hit
        (fact_trace.schema.json forbids a MEASURED read with no position, which
        would make every unexplained_possession row unrecordable) and is stamped
        with extra.first_exposure_rule so the two can never be pooled by accident.
        """
        with tempfile.TemporaryDirectory() as td:
            r = self._row(Path(td), [self._ex(3, "tool_read", inbound=True, form="lower")])
            extra = r.get("extra") or {}
            self.assertIsNone(extra.get("first_inbound_exact_seq"),
                              "a non-exact hit must not set the strict §4.5 value")
            self.assertEqual(r["first_exposure_seq"], 3)
            self.assertNotEqual(extra.get("first_exposure_rule"), "inbound_exact")

    def test_an_exact_inbound_hit_sets_the_strict_value(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._row(Path(td), [self._ex(3, "tool_read", inbound=True, form="exact")])
            extra = r.get("extra") or {}
            self.assertEqual(extra.get("first_inbound_exact_seq"), 3)
            self.assertEqual(r["first_exposure_seq"], 3)


# ── 8. d0-push seq fields are null, and trace.py raises if not ───────────────
class D0PushAsserted(unittest.TestCase):
    """§4.2.2: auto-loaded content is in NO log; exposure_basis = manifest_canary."""

    def test_d0_push_row_is_asserted_with_null_seq_fields(self):
        with tempfile.TemporaryDirectory() as td:
            rd = minimal_run_dir(Path(td), arm="d0-push", with_nonce_in_read=False)
            rows, _ = trace_mod.run(rd, facts=rd / "facts.json",
                                    exposure_rows=[], event_rows=[], probe_rows=[])
            r = rows[0]
            self.assertEqual(r["exposure_basis"], "manifest_canary")
            self.assertTrue(r["read"], "d0-push is read = 1 by construction")
            for field in ("first_exposure_seq", "first_exposure_bytes_before",
                          "first_mention_seq"):
                self.assertIsNone(r[field], f"{field} must be null on an asserted row")

    def test_trace_raises_on_a_manifest_canary_row_carrying_a_seq(self):
        row = {"run_id": "r", "exposure_basis": "manifest_canary", "first_exposure_seq": 7}
        with self.assertRaises(trace_mod.D0PushSeqViolation):
            trace_mod.assert_d0_push_nulls(row)

    def test_trace_accepts_a_manifest_canary_row_with_all_nulls(self):
        row = {"run_id": "r", "exposure_basis": "manifest_canary",
               "first_exposure_seq": None, "first_exposure_bytes_before": None}
        trace_mod.assert_d0_push_nulls(row)  # must not raise

    def test_event_stream_rows_are_not_subject_to_the_rule(self):
        row = {"run_id": "r", "exposure_basis": "event_stream", "first_exposure_seq": 7}
        trace_mod.assert_d0_push_nulls(row)  # must not raise


# ── 9. D1: one fact_trace row per run ────────────────────────────────────────
class OneRowPerRun(unittest.TestCase):
    """D1 / STATUS §7: one fact per task means one fact_trace row per run.

    A registry spans every task in the job. Emitting a row per REGISTRY fact
    marks the other tasks' facts `available` in a workspace where they were
    never planted, and puts them in the read-rate denominator.
    """

    def test_only_this_runs_task_is_traced(self):
        with tempfile.TemporaryDirectory() as td:
            rd = minimal_run_dir(Path(td))
            (rd / "facts.json").write_text(json.dumps({"facts": [
                {"fact_id": "f1", "task_id": "t1", "token": NONCE, "gist": "mine"},
                {"fact_id": "f2", "task_id": "OTHER", "token": "ZQ-OTHER00001", "gist": "not mine"},
                {"fact_id": "f3", "task_id": "OTHER2", "token": "ZQ-OTHER00002", "gist": "no"},
            ]}))
            rows, _ = trace_mod.run(rd, facts=rd / "facts.json",
                                    exposure_rows=[], event_rows=[], probe_rows=[])
            self.assertEqual([r["fact_id"] for r in rows], ["f1"])

    def test_a_registry_without_task_ids_still_traces_everything(self):
        with tempfile.TemporaryDirectory() as td:
            rd = minimal_run_dir(Path(td))
            (rd / "facts.json").write_text(json.dumps({"facts": [
                {"fact_id": "f1", "token": NONCE, "gist": "a"},
                {"fact_id": "f2", "token": "ZQ-OTHER00001", "gist": "b"},
            ]}))
            rows, _ = trace_mod.run(rd, facts=rd / "facts.json",
                                    exposure_rows=[], event_rows=[], probe_rows=[])
            self.assertEqual(len(rows), 2, "no task mapping => no filtering")


# ── 10. the frozen protocol ──────────────────────────────────────────────────
class FrozenProtocol(unittest.TestCase):
    """§5.1(6): a silent edit must invalidate cross-run comparison loudly."""

    def test_pacing_prompt_matches_the_measured_hash(self):
        self.assertEqual(protocol_mod.sha256(protocol_mod.PACING_PROMPT),
                         "0a687ddfc2f3374378188c2aacde2b5f5d2d97504a63e27dc88a8b9cfcbe249b")
        self.assertEqual(len(protocol_mod.PACING_PROMPT), 338)

    def test_verify_frozen_is_clean(self):
        self.assertEqual(protocol_mod.verify_frozen(), [])

    def test_the_two_load_bearing_probe_clauses_survive_rendering(self):
        text = protocol_mod.render_probe(protocol_mod.probe_id("run", 1))
        self.assertIn("Do not report facts about this checkpoint mechanism.", text)
        self.assertIn("without waiting for me", text)

    def test_probe_id_is_echoable_and_unlikely_in_a_repo(self):
        pid = protocol_mod.probe_id("some-run-id", 3)
        self.assertRegex(pid, r"^WURP-[0-9a-f]{8}-003$")
        self.assertEqual(protocol_mod.find_probe_ids(f"CHECKPOINT {pid}."), [pid])

    def test_use_detect_shape_is_read_by_trace(self):
        """detect_use.py writes {"facts": [...]}; trace must not look only for "results"."""
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td)
            (rd / "use_detect.json").write_text(json.dumps({"facts": [
                {"fact_id": "f1", "eligible": True, "fired": True, "evidence": ["x"]}]}))
            got = trace_mod.load_use_detect(rd, "f1")
            self.assertTrue(got["eligible"])
            self.assertTrue(got["fired"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
