#!/usr/bin/env python3
"""
context_gen.py — set up the 7 context environments (E0..E6) for a job.

The agent reads the repo once and writes the content artifacts (README, AGENTS,
PROJECT, OBJECTIVES, PLAN, PROGRESS, a memory fact file, a DOX child AGENTS).
Structural files (CLAUDE.md, .xo/*, memory skeleton, episodic notes) are templated.
Each environment's overlay is then composed under jobs/<id>/environments/<env>/,
which setup_run.sh drops into the run workspace.

Resume-safe: existing artifacts and composed overlays are kept.

Usage: python3 context_gen.py --job-dir jobs/<id>
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))

import agent      # noqa: E402
import jobspec    # noqa: E402
import ladder     # noqa: E402
from judge import _flocked_worktree, _remove_worktree, _exclude_venv  # noqa: E402


# ── templated structural content ─────────────────────────────────────────────
def _claude_md() -> str:
    return "# CLAUDE.md\n\nImports the root operating contract for Claude Code:\n\n@AGENTS.md\n"


def _native_context(model: str) -> tuple[str, str]:
    """The agent-native root context file for the E4/E5/E6 scaffold, by model.

    gemini reads GEMINI.md; claude (and, inertly, codex) read CLAUDE.md. Both point at
    the AGENTS.md rail so the scaffold's content lives in one place.
    """
    m = (model or "").lower()
    if m.startswith("gemini") or m == "google":
        return "GEMINI.md", (
            "# GEMINI.md\n\nProject operating context for Gemini CLI. "
            "See @AGENTS.md for the full operating contract.\n"
        )
    if m.startswith("agy") or m == "antigravity":
        # agy auto-loads <workspace>/.agents/AGENTS.md (verified); root AGENTS.md is NOT
        # auto-injected, so point the auto-loaded file at the rest of the scaffold.
        return ".agents/AGENTS.md", (
            "# AGENTS.md — Antigravity workspace rules\n\n"
            "Project operating context. Read the repository's AGENTS.md and PROJECT.md for the "
            "full operating contract, conventions, and architecture before making changes.\n"
        )
    return "CLAUDE.md", _claude_md()


def _xo_files() -> dict:
    return {
        ".xo/project.json": json.dumps({"name": "project", "schema": "1"}, indent=2),
        ".xo/peers.json": json.dumps({"peers": []}, indent=2),
        ".xo/todos.json": json.dumps({"todos": []}, indent=2),
        ".xo/runtime/stats.json": json.dumps({"runs": 0}, indent=2),
        ".xo/runtime/activity.json": json.dumps({"activity": []}, indent=2),
        ".xo/runtime/sync.json": json.dumps({"last_sync": None}, indent=2),
        ".xo/runtime/sessions/sessionslist.json": json.dumps({"sessions": []}, indent=2),
        ".xo/runtime/timeline.jsonl": "",
        ".gitignore": ".xo/\n",
    }


def _memory_skeleton() -> dict:
    return {
        "memory/semantic/.gitkeep": "",
        "memory/episodic/.gitkeep": "",
        "memory/procedural/.gitkeep": "",
        "memory/working/.gitkeep": "",
        "memory/procedural/README.md": "# Procedural memory\n\nReusable procedures live here.\n",
    }


def _memory_seeded_extra() -> dict:
    return {
        "memory/semantic/constraints.md": "# Constraints\n\n- Keep changes minimal and reversible.\n- Follow the repo's existing style.\n",
        "memory/semantic/preferences.md": "# Preferences\n\n- Small, focused diffs.\n- Tests alongside behavior changes.\n",
        "memory/episodic/seed-note.md": "# Episode\n\nThe project state is file-based; prefer reading the code before editing.\n",
    }


# ── artifact generation ──────────────────────────────────────────────────────
def _generate_artifacts(job_dir: Path, spec: dict, needed: list[str]) -> Path:
    art_dir = job_dir / "environments" / "_artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    sha = spec["repo"]["pinned_sha"]
    model = spec["model"]
    max_seconds = int(spec.get("max_seconds") or 0)
    venv = job_dir / ".venv"
    for art in needed:
        dest = art_dir / art
        if dest.exists() and dest.stat().st_size > 0:
            print(f"[ctx] {art}: already generated"); continue
        scratch = job_dir / f".ctx-{art.replace('/', '_').replace('.', '-')}"
        _remove_worktree(job_dir, scratch)
        _flocked_worktree(job_dir, ["add", "--detach", str(scratch), sha])
        if venv.exists() and not (scratch / "venv").exists():
            (scratch / "venv").symlink_to(venv)
        _exclude_venv(scratch)
        try:
            print(f"[ctx] generating {art} …")
            text, _ = agent.run_text(model, ladder.ARTIFACT_PROMPTS[art], scratch, max_seconds)
            src = scratch / art
            # agy writes new files flat in its scratch (harvested to the worktree root),
            # so a nested artifact (memory/semantic/…) may land under its basename — try that.
            if not (src.exists() and src.stat().st_size > 0):
                alt = scratch / Path(art).name
                if alt.exists() and alt.stat().st_size > 0:
                    src = alt
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.exists() and src.stat().st_size > 0:
                shutil.copyfile(src, dest)
            elif text.strip():
                dest.write_text(text)
            else:
                dest.write_text(f"# {Path(art).name}\n")
        finally:
            _remove_worktree(job_dir, scratch)
    return art_dir


# ── env composition ──────────────────────────────────────────────────────────
def _write(edir: Path, rel: str, content: str) -> None:
    p = edir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _copy(art_dir: Path, edir: Path, rel: str) -> None:
    src = art_dir / rel
    if src.exists():
        dst = edir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def _compose_env(env: str, art_dir: Path, edir: Path, model: str = "claude") -> None:
    spec = ladder.ENV_SPEC[env]
    if edir.exists():
        shutil.rmtree(edir)
    edir.mkdir(parents=True, exist_ok=True)
    for f in spec.get("files", []):
        _copy(art_dir, edir, f)
    if spec.get("claude_md"):
        fname, content = _native_context(model)
        _write(edir, fname, content)
    if spec.get("memory_skeleton"):
        for rel, c in _memory_skeleton().items():
            _write(edir, rel, c)
    if spec.get("memory_seeded"):
        _copy(art_dir, edir, "memory/semantic/project-facts.md")
        for rel, c in _memory_seeded_extra().items():
            _write(edir, rel, c)
    if spec.get("dox"):
        for rel, c in _xo_files().items():
            _write(edir, rel, c)
        # child AGENTS.md: first line "DIR: <path>" then content
        child = art_dir / "AGENTS.child.md"
        if child.exists():
            lines = child.read_text().splitlines()
            d = "src"
            body = child.read_text()
            if lines and lines[0].strip().upper().startswith("DIR:"):
                d = lines[0].split(":", 1)[1].strip().strip("/") or "src"
                body = "\n".join(lines[1:]).lstrip("\n")
            _write(edir, f"{d}/AGENTS.md", body)
        _write(edir, "memory/episodic/seed-note.md",
               "# Episode\n\nRead the root AGENTS.md rail and the nearest child AGENTS.md before editing.\n")


def generate(job_dir: Path) -> dict:
    job_dir = Path(job_dir).resolve()
    spec = jobspec.load(job_dir)
    envs = spec.get("environments", ["E0"])
    env_root = job_dir / "environments"
    env_root.mkdir(parents=True, exist_ok=True)

    needed = ladder.files_needed(envs)
    if needed:
        art_dir = _generate_artifacts(job_dir, spec, needed)
    else:
        art_dir = env_root / "_artifacts"
        art_dir.mkdir(parents=True, exist_ok=True)
        print("[ctx] only E0 (bare) — no artifacts to generate")

    for env in envs:
        _compose_env(env, art_dir, env_root / env, spec.get("model", "claude"))
    print(f"[ctx] environments ready: {', '.join(envs)}")
    return {"environments": envs, "artifacts": needed}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-dir", required=True)
    a = ap.parse_args()
    generate(Path(a.job_dir))


if __name__ == "__main__":
    main()
