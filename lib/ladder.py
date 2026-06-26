#!/usr/bin/env python3
"""
ladder.py — the 7 context environments E0..E6, mirroring the l1-doxed experiment.

  E0 bare → E1 +README → E2 +AGENTS → E3 +AGENTS+PROJECT →
  E4 full scaffold (+OBJECTIVES/PLAN/PROGRESS/CLAUDE + memory skeleton) →
  E5 +seeded memory → E6 DOX (root rail AGENTS + child AGENTS + .xo/ + episodic memory)

The runner sets each environment up for the *user's* repo: the agent generates the
content artifacts (README, AGENTS, PROJECT, …) once per job (see context_gen.py),
structural files (CLAUDE.md, .xo/*, memory skeleton) are templated, and each env's
overlay is composed from those. E0 is the repo as-is.
"""
from __future__ import annotations

# env id -> short label used on the figures' x-axis
LABELS = {
    "E0": "bare", "E1": "+README", "E2": "+AGENTS", "E3": "+PROJECT",
    "E4": "full-XO", "E5": "+memory", "E6": "DOX",
}
VALID_ENVS = ["E0", "E1", "E2", "E3", "E4", "E5", "E6"]
DEFAULT_ENVS = list(VALID_ENVS)   # "run it in all 7 environments"

# Content artifacts the agent generates ONCE per job (filename -> prompt). The agent
# reads the repo at the pinned SHA and writes exactly that file.
ARTIFACT_PROMPTS = {
    "README.md": (
        "Read this repository and write a concise, accurate README.md: what the project "
        "is, how to set it up and run it, and a short map of the key files. Base it strictly "
        "on the code. Write ONLY README.md in the current directory; change nothing else."
    ),
    "AGENTS.md": (
        "Write an AGENTS.md operating contract for an AI agent working in THIS repository: "
        "how it is structured, the conventions to follow, where the important code lives, and "
        "how to run and test it. Practical and specific. Write ONLY AGENTS.md; change nothing else."
    ),
    "PROJECT.md": (
        "Write a PROJECT.md describing this project's purpose, scope, and architecture, "
        "inferred from the code. Write ONLY PROJECT.md; change nothing else."
    ),
    "OBJECTIVES.md": (
        "Write an OBJECTIVES.md listing this project's north-star objectives / OKRs, inferred "
        "from the code and its apparent goals. Write ONLY OBJECTIVES.md; change nothing else."
    ),
    "PLAN.md": (
        "Write a PLAN.md: a short current plan / roadmap for this project based on its code "
        "and any TODOs. Write ONLY PLAN.md; change nothing else."
    ),
    "PROGRESS.md": (
        "Write a PROGRESS.md: a brief running narrative of what already works in this project, "
        "inferred from the code. Write ONLY PROGRESS.md; change nothing else."
    ),
    # E5 seeded memory — distilled durable facts about the repo
    "memory/semantic/project-facts.md": (
        "Write memory/semantic/project-facts.md: distilled, durable facts about this repository "
        "(stack, key modules, conventions, gotchas), one per line, no narrative. Create the "
        "directory if needed and write ONLY that file; change nothing else."
    ),
    # E6 DOX — a child AGENTS.md placed in the repo's main source subdirectory
    "AGENTS.child.md": (
        "Pick this repo's most important source subdirectory and write an AGENTS.md FOR THAT "
        "SUBDIRECTORY (local conventions, what lives there, how to change it safely). Output the "
        "subdirectory path on the FIRST line as 'DIR: <path>', then the file content. Write it to "
        "AGENTS.child.md in the current directory; change nothing else."
    ),
}

# Per-environment overlay spec. `files` = artifacts copied in; flags drive templated extras.
ENV_SPEC = {
    "E0": {"files": []},
    "E1": {"files": ["README.md"]},
    "E2": {"files": ["AGENTS.md"]},
    "E3": {"files": ["AGENTS.md", "PROJECT.md"]},
    "E4": {"files": ["AGENTS.md", "PROJECT.md", "OBJECTIVES.md", "PLAN.md", "PROGRESS.md"],
           "claude_md": True, "memory_skeleton": True},
    "E5": {"files": ["AGENTS.md", "PROJECT.md", "OBJECTIVES.md", "PLAN.md", "PROGRESS.md"],
           "claude_md": True, "memory_skeleton": True, "memory_seeded": True},
    "E6": {"files": ["AGENTS.md"], "claude_md": True, "dox": True},
}


def label(env: str) -> str:
    return LABELS.get(env, env)


def files_needed(envs) -> list[str]:
    """Union of generated content artifacts required by the selected envs."""
    need = []
    for e in envs:
        spec = ENV_SPEC.get(e, {})
        for f in spec.get("files", []):
            if f not in need:
                need.append(f)
        if spec.get("memory_seeded") and "memory/semantic/project-facts.md" not in need:
            need.append("memory/semantic/project-facts.md")
        if spec.get("dox") and "AGENTS.child.md" not in need:
            need.append("AGENTS.child.md")
    return need
