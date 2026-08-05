"""
lib/wur — Workspace Uptake & Retention: the second Atlas experiment mode.

RESPONSIBILITY
  Package marker and the one re-export (`PROTOCOL_VERSION`) that callers may
  read without deciding which submodule they want. Everything else is imported
  explicitly, e.g. `from wur import protocol, cadence`.

INPUTS / OUTPUTS
  None. This file deliberately performs no work at import time: gate.py runs
  inside a synchronous PreToolUse hook on every tool call (§6.2) and a hook must
  always exit 0 and stay cheap, so importing the package must never pull in the
  derivation chain.

LAYOUT (IMPLEMENTATION.md §5.2)
  protocol.py   frozen probe/pacing text, probe_id(), parse ladder, slot classes
  cadence.py    seeded probe schedule — the only RNG in the subsystem
  gate.py       PreToolUse barrier + SessionStart marker; fail-open, exit 0
  driver.py     parent of the claude child: stdin injection, raw capture, budget
  nonce/facts/render/plant.py       the fact layer
  detectors/detect_use.py           the closed 6-predicate `used` registry
  regions/exposure/events/probes/trace/reconcile.py   the derivation chain
  preflight/canary/schedule/settings/validate/aggregate.py   pipeline + hygiene

  Every module here observes; only driver.py, gate.py and plant.py write
  anything the agent can reach, and none of them writes inside workspace/ (§5.1).

MODULE IMPORT NOTE
  protocol.py and cadence.py have no intra-package imports, so both
  `from wur import protocol` (with lib/ on sys.path) and a flat `import protocol`
  (with lib/wur/ on sys.path, as when gate.py is exec'd by path) work.
"""
from __future__ import annotations

__all__ = ["PROTOCOL_VERSION"]


def __getattr__(name: str):  # PEP 562 — lazy, so importing the package stays free
    if name == "PROTOCOL_VERSION":
        from . import protocol

        return protocol.PROTOCOL_VERSION
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
