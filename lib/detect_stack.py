#!/usr/bin/env python3
"""
detect_stack.py — sniff a checked-out repo and decide how to build its env.

Python-first (per design): a repo is "python" if it carries any standard Python
dependency manifest. Node is recognized as a fallback. Anything else is "other"
and relies on an explicit build.command (or no build at all).

Usage:
  python3 detect_stack.py <workspace_dir>   -> prints JSON to stdout:
    {
      "stack": "python" | "node" | "other",
      "signals": ["requirements.txt", ...],
      "py_install": "requirements" | "project" | null
    }
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PY_MANIFESTS = ["requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile"]
NODE_MANIFESTS = ["package.json"]
OTHER_MANIFESTS = {
    "go.mod": "go",
    "Cargo.toml": "rust",
    "pom.xml": "java",
    "build.gradle": "java",
    "Gemfile": "ruby",
}


def detect(ws: Path) -> dict:
    present = [m for m in PY_MANIFESTS if (ws / m).is_file()]
    node = [m for m in NODE_MANIFESTS if (ws / m).is_file()]
    other = [m for m in OTHER_MANIFESTS if (ws / m).is_file()]

    if present:
        # Prefer installing from requirements.txt (deps only, fast + reliable);
        # otherwise install the project itself (pulls deps via pyproject/setup).
        py_install = "requirements" if (ws / "requirements.txt").is_file() else "project"
        return {"stack": "python", "signals": present, "py_install": py_install}
    if node:
        return {"stack": "node", "signals": node, "py_install": None}
    return {
        "stack": "other",
        "signals": other,
        "py_install": None,
    }


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: detect_stack.py <workspace_dir>", file=sys.stderr)
        sys.exit(2)
    ws = Path(sys.argv[1])
    if not ws.is_dir():
        print(f"ERROR: not a directory: {ws}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(detect(ws)))


if __name__ == "__main__":
    main()
